#!/usr/bin/env python3
"""
idl2proto - converts OMG IDL (IDL 4.x / DDS-XTypes) into Protocol Buffers (proto3).

    idl2proto input.idl                 # writes input.proto next to it
    idl2proto input.idl -o out.proto -I include/dir
    idl2proto input.idl -r              # also converts #included IDL files

This module is self-contained (no third-party dependencies) and can also be
run directly as a script: python core.py input.idl

Mapping overview
----------------
module            -> package (common module prefix of the file); deeper modules
                     are folded into the type name (Sub_Type, see --module-separator)
struct            -> message (base struct members are flattened in)
union             -> message with a 'discriminator' field and a 'oneof'
enum              -> enum (prefixed values, zero value added/moved first if needed)
bitmask / bitset  -> uint32 / uint64 fields, flags listed as comments
typedef           -> resolved (protobuf has no aliases)
const / #define   -> listed as comments (protobuf has no constants)
int8/16, octet,
char, wchar       -> widened to int32 / uint32
sequence<octet>,
octet[N]          -> bytes
sequence / array  -> repeated (multi-dim arrays flattened, nested ones get wrapper messages)
map<K, V>         -> map<K, V> (or repeated KToVEntry if K is not a valid proto key)
fixed<d, s>       -> string
long double       -> double
any               -> google.protobuf.Any
@id(n)            -> field number n (otherwise sequential, like XTypes)
@optional         -> proto3 'optional'
@key, bounds, ... -> kept as comments ("IDL: ..." marks every lossy mapping)
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

__version__ = "0.1.0"

MAX_FIELD_NUMBER = 536_870_911
RESERVED_FIELD_RANGE = range(19000, 20000)


# ════════════════════════════ diagnostics ════════════════════════════

class IdlError(Exception):
    def __init__(self, msg: str, file: str = "", line: int = 0):
        super().__init__(msg)
        self.msg, self.file, self.line = msg, file, line

    def __str__(self) -> str:
        loc = f"{self.file}:{self.line}: " if self.file else ""
        return f"{loc}error: {self.msg}"


class Reporter:
    def __init__(self, quiet: bool = False):
        self.quiet = quiet
        self.seen: Set[Tuple[str, str, int]] = set()
        self.count = 0

    def warn(self, msg: str, file: str = "", line: int = 0) -> None:
        key = (msg, file, line)
        if key in self.seen:
            return
        self.seen.add(key)
        self.count += 1
        if not self.quiet:
            loc = f"{file}:{line}: " if file else ""
            print(f"{loc}warning: {msg}", file=sys.stderr)


# ════════════════════════════ lexer ════════════════════════════

@dataclass
class Token:
    kind: str          # ident | int | float | string | char | op | eof
    value: str
    line: int
    esc: bool = False  # escaped identifier (_name) - never a keyword


_TOKEN_RE = re.compile(r"""
    (?P<nl>\n)
  | (?P<ws>[ \t\r\f\v]+)
  | (?P<lcomment>//[^\n]*)
  | (?P<bcomment>/\*.*?\*/)
  | (?P<string>L?"(?:\\.|[^"\\\n])*")
  | (?P<char>L?'(?:\\.|[^'\\\n])+')
  | (?P<float>(?:\d+\.\d*|\.\d+)(?:[eE][+-]?\d+)?[dD]?|\d+[eE][+-]?\d+[dD]?|\d+[dD])
  | (?P<int>0[xX][0-9a-fA-F]+|\d+)
  | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<op>::|<<|[{}()\[\]<>;:,=@+\-*/%|&^~])
""", re.VERBOSE | re.DOTALL)


def tokenize(text: str, filename: str) -> Tuple[List[Token], Set[int]]:
    """Returns the tokens and the set of lines carrying a legacy '//@key' comment."""
    toks: List[Token] = []
    key_lines: Set[int] = set()
    line, pos, n = 1, 0, len(text)
    while pos < n:
        m = _TOKEN_RE.match(text, pos)
        if not m:
            if text.startswith("/*", pos):
                raise IdlError("unterminated block comment", filename, line)
            raise IdlError(f"unexpected character {text[pos]!r}", filename, line)
        kind, val = m.lastgroup, m.group()
        if kind == "nl":
            line += 1
        elif kind == "ws":
            pass
        elif kind == "lcomment":
            if re.match(r"//\s*@key\b", val):
                key_lines.add(line)
        elif kind == "bcomment":
            line += val.count("\n")
        elif kind == "ident" and val.startswith("_") and len(val) > 1:
            toks.append(Token(kind, val[1:], line, esc=True))   # IDL escaped identifier
        else:
            toks.append(Token(kind, val, line))
        pos = m.end()
    toks.append(Token("eof", "", line))
    return toks, key_lines


def preprocess(text: str) -> Tuple[str, List[Tuple[int, str]]]:
    """Removes preprocessor lines (keeping line numbers) and returns them separately."""
    lines = text.split("\n")
    out: List[str] = []
    directives: List[Tuple[int, str]] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.lstrip().startswith("#"):
            start, full = i, ln.rstrip()
            while full.endswith("\\") and i + 1 < len(lines):
                i += 1
                full = full[:-1] + " " + lines[i].rstrip()
            directives.append((start + 1, full.strip()[1:].strip()))
            out.extend([""] * (i - start + 1))
        else:
            out.append(ln)
        i += 1
    return "\n".join(out), directives


# ════════════════════════════ AST ════════════════════════════

@dataclass
class Expr:
    value: Any   # int / float / str / bool, or None if not evaluable (enumerators, unknown names)
    text: str


@dataclass
class Annotation:
    name: str
    args: List[Tuple[Optional[str], Expr]] = field(default_factory=list)

    def arg(self, key: Optional[str] = None) -> Optional[Expr]:
        for k, e in self.args:
            if k == key or (key is None and k in (None, "value")):
                return e
        return None

    def int_arg(self) -> Optional[int]:
        e = self.arg()
        if e is not None and isinstance(e.value, int) and not isinstance(e.value, bool):
            return e.value
        return None

    def is_true(self) -> bool:
        e = self.arg()
        return e is None or bool(e.value) or e.text.upper() == "TRUE"

    def __str__(self) -> str:
        if not self.args:
            return "@" + self.name
        parts = [f"{k}={e.text}" if k else e.text for k, e in self.args]
        return f"@{self.name}({', '.join(parts)})"


def find_ann(anns: List[Annotation], *names: str) -> Optional[Annotation]:
    for a in anns:
        if a.name in names:
            return a
    return None


class IdlType:
    pass


@dataclass
class PrimType(IdlType):
    name: str  # int8..uint64, octet, char, wchar, boolean, float, double, long double, any


@dataclass
class StringType(IdlType):
    wide: bool
    bound: Optional[Expr] = None


@dataclass
class SeqType(IdlType):
    elem: IdlType
    bound: Optional[Expr] = None


@dataclass
class ArrayType(IdlType):
    elem: IdlType
    dims: List[Expr]


@dataclass
class MapType(IdlType):
    key: IdlType
    value: IdlType
    bound: Optional[Expr] = None


@dataclass
class FixedType(IdlType):
    digits: Optional[Expr] = None
    scale: Optional[Expr] = None


@dataclass
class NamedType(IdlType):
    name: str                 # as written, e.g. "common::Timestamp" or "::a::B"
    scope: Tuple[str, ...]    # module scope where it was written (for name lookup)


@dataclass
class Member:
    name: str
    type: IdlType
    anns: List[Annotation]
    line: int


@dataclass
class Decl:
    name: str
    scope: Tuple[str, ...]
    anns: List[Annotation]
    file: str
    line: int

    @property
    def fqn(self) -> Tuple[str, ...]:
        return self.scope + (self.name,)

    @property
    def idl_name(self) -> str:
        return "::".join(self.fqn)


@dataclass
class StructDecl(Decl):
    base: Optional[NamedType] = None
    members: List[Member] = field(default_factory=list)


@dataclass
class UnionCase:
    labels: List[Optional[Expr]]  # None = default
    member: Member


@dataclass
class UnionDecl(Decl):
    switch_type: Optional[IdlType] = None
    switch_anns: List[Annotation] = field(default_factory=list)
    cases: List[UnionCase] = field(default_factory=list)


@dataclass
class Enumerator:
    name: str
    value: int
    anns: List[Annotation]


@dataclass
class EnumDecl(Decl):
    values: List[Enumerator] = field(default_factory=list)


@dataclass
class BitmaskDecl(Decl):
    flags: List[Tuple[str, int]] = field(default_factory=list)
    bit_bound: int = 32


@dataclass
class BitsetDecl(Decl):
    pass


@dataclass
class TypedefDecl(Decl):
    type: Optional[IdlType] = None


@dataclass
class ConstDecl(Decl):
    type: Optional[IdlType] = None  # None for #define
    expr: Optional[Expr] = None


@dataclass
class IdlFile:
    path: str                                                   # display path
    decls: List[Decl] = field(default_factory=list)
    includes: List[Tuple[str, Optional[str]]] = field(default_factory=list)  # (as written, display path)
    package: str = ""
    prefix: Tuple[str, ...] = ()


class Symbols:
    def __init__(self) -> None:
        self.table: Dict[Tuple[str, ...], Decl] = {}

    def add(self, d: Decl, rep: Reporter) -> None:
        prev = self.table.get(d.fqn)
        if prev is not None:
            rep.warn(f"'{d.idl_name}' redefined (previous definition at {prev.file}:{prev.line})",
                     d.file, d.line)
        self.table[d.fqn] = d

    def lookup(self, name: str, scope: Tuple[str, ...]) -> Optional[Decl]:
        parts = tuple(name.split("::"))
        if parts[0] == "":
            return self.table.get(parts[1:])
        for i in range(len(scope), -1, -1):
            d = self.table.get(tuple(scope[:i]) + parts)
            if d is not None:
                return d
        return None


# ════════════════════════════ expression helpers ════════════════════════════

def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _apply(op: str, a: Any, b: Any) -> Any:
    if not (_is_num(a) and _is_num(b)):
        return None
    try:
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if op == "/":
            return a // b if isinstance(a, int) and isinstance(b, int) else a / b
        if op == "%":
            return a % b
        if not (isinstance(a, int) and isinstance(b, int)):
            return None
        return {"<<": lambda: a << b, ">>": lambda: a >> b, "&": lambda: a & b,
                "|": lambda: a | b, "^": lambda: a ^ b}[op]()
    except (ZeroDivisionError, ValueError, OverflowError):
        return None


def _apply_unary(op: str, v: Any) -> Any:
    if not _is_num(v):
        return None
    if op == "-":
        return -v
    if op == "+":
        return v
    return ~v if isinstance(v, int) else None


def _parse_int(s: str) -> int:
    if s.lower().startswith("0x"):
        return int(s, 16)
    if len(s) > 1 and s.startswith("0"):
        try:
            return int(s, 8)
        except ValueError:
            return int(s, 10)
    return int(s, 10)


def _unquote(s: str) -> str:
    if s.startswith("L"):
        s = s[1:]
    return s[1:-1]


# ════════════════════════════ parser ════════════════════════════

PRIM_START = {"short", "long", "unsigned", "float", "double", "char", "wchar", "boolean", "octet",
              "any", "int8", "uint8", "int16", "uint16", "int32", "uint32", "int64", "uint64"}
SKIPPED_KEYWORDS = {"interface", "exception", "valuetype", "eventtype", "component", "home",
                    "native", "abstract", "local", "custom", "porttype", "connector", "import",
                    "typeid", "typeprefix", "template"}


class Parser:
    def __init__(self, tokens: List[Token], key_lines: Set[int], f: IdlFile,
                 symbols: Symbols, rep: Reporter):
        self.toks, self.i = tokens, 0
        self.key_lines = key_lines
        self.file, self.symbols, self.rep = f, symbols, rep
        self.scope: List[str] = []

    # ── token helpers ──
    def peek(self, k: int = 0) -> Token:
        return self.toks[min(self.i + k, len(self.toks) - 1)]

    def next(self) -> Token:
        t = self.toks[self.i]
        if t.kind != "eof":
            self.i += 1
        return t

    @staticmethod
    def _is(tok: Token, value: str) -> bool:
        if tok.kind == "op":
            return tok.value == value
        return tok.kind == "ident" and not tok.esc and tok.value == value

    def at(self, value: str, k: int = 0) -> bool:
        return self._is(self.peek(k), value)

    def accept(self, value: str) -> bool:
        if self.at(value):
            self.i += 1
            return True
        return False

    @staticmethod
    def desc(t: Token) -> str:
        return "end of file" if t.kind == "eof" else f"'{t.value}'"

    def err(self, msg: str, tok: Optional[Token] = None) -> IdlError:
        tok = tok or self.peek()
        return IdlError(msg, self.file.path, tok.line)

    def expect(self, value: str) -> None:
        if not self.accept(value):
            raise self.err(f"expected '{value}' but found {self.desc(self.peek())}")

    def ident(self) -> str:
        t = self.peek()
        if t.kind != "ident":
            raise self.err(f"expected an identifier but found {self.desc(t)}")
        self.i += 1
        return t.value

    def scoped_name(self) -> str:
        parts = []
        if self.accept("::"):
            parts.append("")
        parts.append(self.ident())
        while self.accept("::"):
            parts.append(self.ident())
        return "::".join(parts)

    # ── constant expressions ──
    def const_expr(self, in_angle: bool = False) -> Expr:
        start = self.i
        v = self._or(in_angle)
        text = " ".join(t.value for t in self.toks[start:self.i])
        text = re.sub(r"\s*::\s*", "::", text).replace("> >", ">>")
        text = re.sub(r"\(\s+", "(", re.sub(r"\s+\)", ")", text))
        text = re.sub(r"(^|\()([-+~])\s+", r"\1\2", text)
        return Expr(v, text)

    def _binary(self, sub, ops: Tuple[str, ...], in_angle: bool) -> Any:
        v = sub(in_angle)
        while True:
            op = None
            for o in ops:
                if o == ">>":
                    # inside <...> a '>>' closes two brackets instead of shifting
                    if not in_angle and self.at(">") and self.at(">", 1):
                        op = o
                        self.i += 2
                        break
                elif self.accept(o):
                    op = o
                    break
            if op is None:
                return v
            v = _apply(op, v, sub(in_angle))

    def _or(self, a: bool) -> Any:
        return self._binary(self._xor, ("|",), a)

    def _xor(self, a: bool) -> Any:
        return self._binary(self._and, ("^",), a)

    def _and(self, a: bool) -> Any:
        return self._binary(self._shift, ("&",), a)

    def _shift(self, a: bool) -> Any:
        return self._binary(self._add, ("<<", ">>"), a)

    def _add(self, a: bool) -> Any:
        return self._binary(self._mul, ("+", "-"), a)

    def _mul(self, a: bool) -> Any:
        return self._binary(self._unary, ("*", "/", "%"), a)

    def _unary(self, a: bool) -> Any:
        for o in ("-", "+", "~"):
            if self.accept(o):
                return _apply_unary(o, self._unary(a))
        return self._primary(a)

    def _primary(self, a: bool) -> Any:
        t = self.peek()
        if self.accept("("):
            v = self._or(False)
            self.expect(")")
            return v
        if t.kind == "int":
            self.i += 1
            return _parse_int(t.value)
        if t.kind == "float":
            self.i += 1
            return float(t.value.rstrip("dD"))
        if t.kind in ("string", "char"):
            self.i += 1
            return _unquote(t.value)
        if t.kind == "ident" or self.at("::"):
            if t.kind == "ident" and not t.esc and t.value in ("TRUE", "FALSE"):
                self.i += 1
                return t.value == "TRUE"
            d = self.symbols.lookup(self.scoped_name(), tuple(self.scope))
            if isinstance(d, ConstDecl) and d.expr is not None:
                return d.expr.value
            return None  # enumerator or unknown symbol - textual form is kept
        raise self.err(f"expected an expression but found {self.desc(t)}")

    # ── annotations ──
    def annotations(self) -> List[Annotation]:
        anns = []
        while self.at("@") and not self.at("annotation", 1):
            self.next()
            name = self.scoped_name().split("::")[-1]
            args: List[Tuple[Optional[str], Expr]] = []
            if self.accept("("):
                if not self.accept(")"):
                    while True:
                        key = None
                        if self.peek().kind == "ident" and self.at("=", 1):
                            key = self.ident()
                            self.next()
                        args.append((key, self.const_expr()))
                        if self.accept(")"):
                            break
                        self.expect(",")
            anns.append(Annotation(name, args))
        return anns

    # ── definitions ──
    def specification(self) -> None:
        while self.peek().kind != "eof":
            self.definition()

    def definition(self) -> None:
        if self.at("@") and self.at("annotation", 1):
            self.rep.warn("annotation declaration skipped", self.file.path, self.peek().line)
            self.i += 2
            self.skip_construct()
            return
        if self.accept(";"):
            return
        anns = self.annotations()
        t = self.peek()
        kw = t.value if t.kind == "ident" and not t.esc else None
        handler = {
            "module": self.p_module, "struct": self.p_struct, "union": self.p_union,
            "enum": self.p_enum, "bitmask": self.p_bitmask, "bitset": self.p_bitset,
            "typedef": self.p_typedef, "const": self.p_const,
        }.get(kw or "")
        if handler:
            handler(anns, t)
        elif kw in SKIPPED_KEYWORDS:
            self.rep.warn(f"'{kw}' has no protobuf data equivalent - skipped", self.file.path, t.line)
            self.skip_construct()
        else:
            raise self.err(f"unexpected {self.desc(t)} at the start of a definition")

    def skip_construct(self) -> None:
        depth = 0
        while True:
            t = self.next()
            if t.kind == "eof":
                return
            if self._is(t, "{"):
                depth += 1
            elif self._is(t, "}"):
                depth -= 1
            elif self._is(t, ";") and depth <= 0:
                return

    def _new(self, cls, name: str, anns: List[Annotation], tok: Token, **kw) -> Any:
        return cls(name=name, scope=tuple(self.scope), anns=anns, file=self.file.path,
                   line=tok.line, **kw)

    def _register(self, d: Decl) -> None:
        self.symbols.add(d, self.rep)
        self.file.decls.append(d)

    def p_module(self, anns: List[Annotation], kw: Token) -> None:
        self.next()
        name = self.ident()
        self.expect("{")
        self.scope.append(name)
        while not self.accept("}"):
            if self.peek().kind == "eof":
                raise self.err(f"missing '}}' for module '{name}'", kw)
            self.definition()
        self.scope.pop()
        self.accept(";")

    def p_struct(self, anns: List[Annotation], kw: Token) -> None:
        self.next()
        name = self.ident()
        if self.accept(";"):
            return  # forward declaration
        base = None
        if self.accept(":"):
            base = NamedType(self.scoped_name(), tuple(self.scope))
        self.expect("{")
        d = self._new(StructDecl, name, anns, kw, base=base)
        while not self.accept("}"):
            if self.peek().kind == "eof":
                raise self.err(f"missing '}}' for struct '{name}'", kw)
            d.members.extend(self.member_decl())
        self.expect(";")
        self._register(d)

    def member_decl(self) -> List[Member]:
        anns = self.annotations()
        typ = self.type_spec()
        members = [Member(n, ArrayType(typ, dims) if dims else typ, list(anns), tok.line)
                   for n, dims, tok in self.declarators()]
        end = self.peek()
        self.expect(";")
        if end.line in self.key_lines and not find_ann(anns, "key"):
            for m in members:
                m.anns.append(Annotation("key"))
        return members

    def declarators(self) -> List[Tuple[str, List[Expr], Token]]:
        out = []
        while True:
            tok = self.peek()
            name = self.ident()
            dims = []
            while self.accept("["):
                dims.append(self.const_expr())
                self.expect("]")
            out.append((name, dims, tok))
            if not self.accept(","):
                return out

    def type_spec(self) -> IdlType:
        t = self.peek()
        if t.kind != "ident" and not self.at("::"):
            raise self.err(f"expected a type but found {self.desc(t)}")
        v = None if t.esc else t.value
        if v == "sequence":
            self.next()
            self.expect("<")
            elem = self.type_spec()
            bound = self.const_expr(True) if self.accept(",") else None
            self.expect(">")
            return SeqType(elem, bound)
        if v in ("string", "wstring"):
            self.next()
            bound = None
            if self.accept("<"):
                bound = self.const_expr(True)
                self.expect(">")
            return StringType(v == "wstring", bound)
        if v == "map":
            self.next()
            self.expect("<")
            k = self.type_spec()
            self.expect(",")
            val = self.type_spec()
            bound = self.const_expr(True) if self.accept(",") else None
            self.expect(">")
            return MapType(k, val, bound)
        if v == "fixed":
            self.next()
            if self.accept("<"):
                d = self.const_expr(True)
                self.expect(",")
                s = self.const_expr(True)
                self.expect(">")
                return FixedType(d, s)
            return FixedType()
        if v in PRIM_START:
            return self.prim_type()
        if v in ("struct", "union", "enum", "bitmask", "bitset"):
            raise self.err(f"inline '{v}' definitions are not supported; define the type separately")
        if v in ("Object", "ValueBase"):
            raise self.err(f"'{v}' has no protobuf equivalent")
        return NamedType(self.scoped_name(), tuple(self.scope))

    def prim_type(self) -> PrimType:
        v = self.next().value
        if v == "unsigned":
            w = self.ident()
            if w == "short":
                return PrimType("uint16")
            if w == "long":
                return PrimType("uint64" if self.accept("long") else "uint32")
            raise self.err(f"unexpected 'unsigned {w}'")
        if v == "long":
            if self.accept("long"):
                return PrimType("int64")
            if self.accept("double"):
                return PrimType("long double")
            return PrimType("int32")
        if v == "short":
            return PrimType("int16")
        return PrimType(v)

    def p_union(self, anns: List[Annotation], kw: Token) -> None:
        self.next()
        name = self.ident()
        if self.accept(";"):
            return
        self.expect("switch")
        self.expect("(")
        sanns = self.annotations()
        st = self.type_spec()
        self.expect(")")
        self.expect("{")
        d = self._new(UnionDecl, name, anns, kw, switch_type=st, switch_anns=sanns)
        while not self.accept("}"):
            labels: List[Optional[Expr]] = []
            while True:
                if self.accept("case"):
                    labels.append(self.const_expr())
                    self.expect(":")
                elif self.accept("default"):
                    labels.append(None)
                    self.expect(":")
                else:
                    break
            if not labels:
                raise self.err(f"expected 'case' or 'default' but found {self.desc(self.peek())}")
            manns = self.annotations()
            typ = self.type_spec()
            decls = self.declarators()
            if len(decls) != 1:
                raise self.err("a union case must declare exactly one member")
            mname, dims, tok = decls[0]
            self.expect(";")
            d.cases.append(UnionCase(labels, Member(mname, ArrayType(typ, dims) if dims else typ,
                                                    manns, tok.line)))
        self.expect(";")
        self._register(d)

    def p_enum(self, anns: List[Annotation], kw: Token) -> None:
        self.next()
        name = self.ident()
        self.expect("{")
        d = self._new(EnumDecl, name, anns, kw)
        nxt = 0
        while True:
            eanns = self.annotations()
            if self.at("}"):
                break
            tok = self.peek()
            en = self.ident()
            val = nxt
            va = find_ann(eanns, "value")
            if va is not None:
                if va.int_arg() is None:
                    raise self.err(f"@value of '{en}' must be an integer constant", tok)
                val = va.int_arg()
            d.values.append(Enumerator(en, val, eanns))
            nxt = val + 1
            if not self.accept(","):
                break
        self.expect("}")
        self.expect(";")
        self._register(d)

    def p_bitmask(self, anns: List[Annotation], kw: Token) -> None:
        self.next()
        name = self.ident()
        self.expect("{")
        bb = find_ann(anns, "bit_bound")
        d = self._new(BitmaskDecl, name, anns, kw,
                      bit_bound=(bb.int_arg() if bb and bb.int_arg() else 32))
        pos = 0
        while True:
            fanns = self.annotations()
            if self.at("}"):
                break
            fname = self.ident()
            pa = find_ann(fanns, "position")
            if pa is not None and pa.int_arg() is not None:
                pos = pa.int_arg()
            d.flags.append((fname, pos))
            pos += 1
            if not self.accept(","):
                break
        self.expect("}")
        self.expect(";")
        self._register(d)

    def p_bitset(self, anns: List[Annotation], kw: Token) -> None:
        self.next()
        name = self.ident()
        self.rep.warn(f"bitset '{name}' has no protobuf equivalent; fields of this type become uint64",
                      self.file.path, kw.line)
        d = self._new(BitsetDecl, name, anns, kw)
        self.skip_construct()
        self._register(d)

    def p_typedef(self, anns: List[Annotation], kw: Token) -> None:
        self.next()
        tanns = anns + self.annotations()
        typ = self.type_spec()
        for name, dims, tok in self.declarators():
            self._register(self._new(TypedefDecl, name, tanns, tok,
                                     type=ArrayType(typ, dims) if dims else typ))
        self.expect(";")

    def p_const(self, anns: List[Annotation], kw: Token) -> None:
        self.next()
        typ = self.type_spec()
        name = self.ident()
        self.expect("=")
        e = self.const_expr()
        self.expect(";")
        self._register(self._new(ConstDecl, name, anns, kw, type=typ, expr=e))


# ════════════════════════════ loader ════════════════════════════

def _display(path: str) -> str:
    try:
        rel = os.path.relpath(path)
        return path if rel.startswith("..") else rel
    except ValueError:
        return path


class Loader:
    def __init__(self, include_dirs: List[str], rep: Reporter):
        self.include_dirs = include_dirs
        self.rep = rep
        self.symbols = Symbols()
        self.files: Dict[str, IdlFile] = {}      # abs path -> file
        self.order: List[IdlFile] = []           # dependency order
        self.import_names: Dict[str, str] = {}   # display path -> include name as written

    def find_include(self, name: str, from_dir: str, quoted: bool) -> Optional[str]:
        dirs = ([from_dir] if quoted else []) + self.include_dirs + ([] if quoted else [from_dir])
        for d in dirs:
            p = os.path.join(d, name)
            if os.path.isfile(p):
                return os.path.abspath(p)
        return None

    def load(self, path: str) -> IdlFile:
        apath = os.path.abspath(path)
        if apath in self.files:
            return self.files[apath]
        try:
            with open(apath, encoding="utf-8-sig", errors="replace") as fh:
                text = fh.read()
        except OSError as e:
            raise IdlError(f"cannot read file: {e.strerror}", path)
        f = IdlFile(path=_display(apath))
        self.files[apath] = f
        clean, directives = preprocess(text)
        keylists = []
        warned_cond = False
        for line, d in directives:
            m = re.match(r'include\s*([<"])([^>"]+)[>"]', d)
            if m:
                written = m.group(2)
                found = self.find_include(written, os.path.dirname(apath), m.group(1) == '"')
                if found is None:
                    self.rep.warn(f"include file '{written}' not found (add -I); its types "
                                  "cannot be resolved", f.path, line)
                    f.includes.append((written, None))
                else:
                    inc = self.load(found)
                    self.import_names.setdefault(inc.path, written)
                    f.includes.append((written, inc.path))
                continue
            m = re.match(r"define\s+([A-Za-z_]\w*)\s+(.+)$", d)
            if m:
                self._define(m.group(1), m.group(2).strip(), f, line)
                continue
            m = re.match(r"pragma\s+keylist\s+([\w:]+)\s*(.*)$", d)
            if m:
                keylists.append((line, m.group(1), m.group(2).split()))
                continue
            if re.match(r"(if|ifdef|elif|else)\b", d) and not warned_cond:
                warned_cond = True
                self.rep.warn("conditional compilation (#if/#ifdef/#else) is not evaluated; "
                              "all branches are converted", f.path, line)
        tokens, key_lines = tokenize(clean, f.path)
        Parser(tokens, key_lines, f, self.symbols, self.rep).specification()
        for line, tname, keys in keylists:
            self._apply_keylist(f, line, tname, keys)
        self.order.append(f)
        return f

    def _define(self, name: str, value: str, f: IdlFile, line: int) -> None:
        try:
            toks, _ = tokenize(value, f.path)
            p = Parser(toks, set(), f, self.symbols, self.rep)
            e = p.const_expr()
            if p.peek().kind != "eof":
                return
        except IdlError:
            return
        d = ConstDecl(name=name, scope=(), anns=[], file=f.path, line=line, type=None, expr=e)
        self.symbols.add(d, self.rep)
        f.decls.append(d)

    def _apply_keylist(self, f: IdlFile, line: int, tname: str, keys: List[str]) -> None:
        d = self.symbols.lookup(tname, ())
        if d is None:
            parts = tuple(tname.split("::"))
            cands = [x for k, x in self.symbols.table.items() if k[-len(parts):] == parts]
            d = cands[0] if len(cands) == 1 else None
        if not isinstance(d, StructDecl):
            self.rep.warn(f"#pragma keylist: struct '{tname}' not found", f.path, line)
            return
        by_name = {m.name: m for m in d.members}
        for k in keys:
            m = by_name.get(k)
            if m is None:
                self.rep.warn(f"#pragma keylist: '{k}' is not a direct member of '{tname}'",
                              f.path, line)
            elif not find_ann(m.anns, "key"):
                m.anns.append(Annotation("key"))


def _common_prefix(seqs: List[Tuple[str, ...]]) -> Tuple[str, ...]:
    if not seqs:
        return ()
    p = list(seqs[0])
    for s in seqs[1:]:
        n = 0
        while n < len(p) and n < len(s) and p[n] == s[n]:
            n += 1
        p = p[:n]
    return tuple(p)


def assign_packages(loader: Loader, main: IdlFile, forced: Optional[str]) -> None:
    for f in loader.order:
        f.prefix = _common_prefix([d.scope for d in f.decls if not isinstance(d, ConstDecl)])
        f.package = ".".join(f.prefix)
    if forced is not None:
        main.package = forced


# ════════════════════════════ proto generation ════════════════════════════

@dataclass
class PType:
    label: str          # '' | 'repeated' | 'map'
    type: str
    kind: str           # scalar | string | bytes | enum | message
    lossy: bool = False


PRIM: Dict[str, Tuple[str, bool]] = {
    "int8": ("int32", True), "uint8": ("uint32", True),
    "int16": ("int32", True), "uint16": ("uint32", True),
    "int32": ("int32", False), "uint32": ("uint32", False),
    "int64": ("int64", False), "uint64": ("uint64", False),
    "octet": ("uint32", True), "char": ("uint32", True), "wchar": ("uint32", True),
    "boolean": ("bool", False), "float": ("float", False), "double": ("double", False),
    "long double": ("double", True), "any": ("google.protobuf.Any", False),
}
MAP_KEY_TYPES = {"int32", "int64", "uint32", "uint64", "sint32", "sint64", "fixed32", "fixed64",
                 "sfixed32", "sfixed64", "bool", "string"}


def _bound(e: Expr) -> str:
    if _is_num(e.value) and str(e.value) != e.text:
        return f"{e.text} ({e.value})"
    return e.text


def idl_str(t: Optional[IdlType]) -> str:
    if isinstance(t, PrimType):
        return t.name
    if isinstance(t, StringType):
        base = "wstring" if t.wide else "string"
        return f"{base}<{_bound(t.bound)}>" if t.bound else base
    if isinstance(t, SeqType):
        e = idl_str(t.elem)
        return f"sequence<{e}, {_bound(t.bound)}>" if t.bound else f"sequence<{e}>"
    if isinstance(t, ArrayType):
        return idl_str(t.elem) + "".join(f"[{_bound(d)}]" for d in t.dims)
    if isinstance(t, MapType):
        b = f", {_bound(t.bound)}" if t.bound else ""
        return f"map<{idl_str(t.key)}, {idl_str(t.value)}{b}>"
    if isinstance(t, FixedType):
        return f"fixed<{t.digits.text}, {t.scale.text}>" if t.digits and t.scale else "fixed"
    if isinstance(t, NamedType):
        return t.name
    return "?"


def _upper_snake(s: str) -> str:
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    return re.sub(r"_+", "_", s).upper()


def _snake(s: str) -> str:
    return _upper_snake(s).lower()


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def proto_import_path(written: str) -> str:
    return os.path.splitext(written)[0].replace("\\", "/") + ".proto"


class Emitter:
    def __init__(self, f: IdlFile, loader: Loader, opts: argparse.Namespace, rep: Reporter):
        self.f, self.loader, self.opts, self.rep = f, loader, opts, rep
        self.sym = loader.symbols
        self.files = {x.path: x for x in loader.order}
        self.wrappers: Dict[str, List[str]] = {}
        self.wrapper_bodies: Dict[str, List[str]] = {}
        self.used_files: Set[str] = set()
        self.uses_any = False
        self.local_names = {self.local_name(d) for d in f.decls
                            if isinstance(d, (StructDecl, UnionDecl, EnumDecl))}

    # ── naming ──
    def local_name(self, d: Decl) -> str:
        fi = self.files[d.file]
        rel = d.scope[len(fi.prefix):] if d.scope[:len(fi.prefix)] == fi.prefix else d.scope
        return self.opts.module_separator.join(rel + (d.name,))

    def ref_name(self, d: Decl) -> str:
        fi = self.files[d.file]
        if fi is self.f:
            return self.local_name(d)
        self.used_files.add(fi.path)
        return "." + (fi.package + "." if fi.package else "") + self.local_name(d)

    def field_name(self, name: str) -> str:
        return _snake(name) if self.opts.snake_case else name

    # ── type resolution ──
    def resolve(self, t: IdlType) -> Any:
        """Follows typedefs. Returns an IdlType, a Decl, or an unresolved NamedType."""
        depth = 0
        while isinstance(t, NamedType):
            d = self.sym.lookup(t.name, t.scope)
            if d is None:
                return t
            if isinstance(d, TypedefDecl):
                depth += 1
                if depth > 64:
                    raise IdlError(f"typedef cycle involving '{d.idl_name}'", d.file, d.line)
                t = d.type
                continue
            return d
        return t

    def is_octet(self, t: IdlType) -> bool:
        r = self.resolve(t)
        return isinstance(r, PrimType) and r.name == "octet"

    def idl_desc(self, t: IdlType) -> str:
        s = idl_str(t)
        if isinstance(t, NamedType):
            d = self.sym.lookup(t.name, t.scope)
            if isinstance(d, TypedefDecl):
                s += f" = {idl_str(d.type)}"
            elif isinstance(d, BitmaskDecl):
                s = f"bitmask {t.name}"
            elif isinstance(d, BitsetDecl):
                s = f"bitset {t.name}"
        return s

    def map_type(self, t: IdlType, loc: Tuple[str, int]) -> PType:
        r = self.resolve(t)
        if isinstance(r, NamedType):
            self.rep.warn(f"unknown type '{r.name}' - emitted unchanged", *loc)
            return PType("", r.name.replace("::", ".").lstrip("."), "message", True)
        if isinstance(r, (StructDecl, UnionDecl)):
            return PType("", self.ref_name(r), "message")
        if isinstance(r, EnumDecl):
            return PType("", self.ref_name(r), "enum")
        if isinstance(r, BitmaskDecl):
            return PType("", "uint64" if r.bit_bound > 32 else "uint32", "scalar", True)
        if isinstance(r, BitsetDecl):
            return PType("", "uint64", "scalar", True)
        if isinstance(r, Decl):
            raise IdlError(f"'{r.idl_name}' is not a type", *loc)
        if isinstance(r, PrimType):
            pt, lossy = PRIM[r.name]
            if r.name == "any":
                self.uses_any = True
                return PType("", pt, "message")
            return PType("", pt, "scalar", lossy)
        if isinstance(r, StringType):
            return PType("", "string", "string", r.wide or r.bound is not None)
        if isinstance(r, FixedType):
            return PType("", "string", "string", True)
        if isinstance(r, (SeqType, ArrayType)):
            lossy = isinstance(r, ArrayType) or r.bound is not None
            if self.is_octet(r.elem):
                return PType("", "bytes", "bytes", lossy)
            inner = self.map_type(r.elem, loc)
            if inner.label:
                return PType("repeated", self.wrapper(r.elem, inner), "message", True)
            return PType("repeated", inner.type, inner.kind, lossy or inner.lossy)
        if isinstance(r, MapType):
            k = self.map_type(r.key, loc)
            v = self.map_type(r.value, loc)
            lossy = r.bound is not None or k.lossy or v.lossy
            if v.label:
                v = PType("", self.wrapper(r.value, v), "message")
                lossy = True
            if not k.label and k.kind == "enum":
                k = PType("", "int32", "scalar")
                lossy = True
            if not k.label and k.type in MAP_KEY_TYPES:
                return PType("map", f"map<{k.type}, {v.type}>", "message", lossy)
            if k.label:
                k = PType("", self.wrapper(r.key, k), "message")
            name = self.type_label(r.key) + "To" + self.type_label(r.value) + "Entry"
            body = [f"  {k.type} key = 1;", f"  {v.type} value = 2;"]
            name = self._add_wrapper(name, body, f"map<{idl_str(r.key)}, {idl_str(r.value)}>: "
                                     "key type not allowed in a protobuf map")
            return PType("repeated", name, "message", True)
        raise IdlError("internal: unhandled type", *loc)

    def type_label(self, t: IdlType) -> str:
        r = self.resolve(t)
        if isinstance(r, NamedType):
            return _cap(r.name.split("::")[-1])
        if isinstance(r, Decl):
            return _cap(self.local_name(r))
        if isinstance(r, PrimType):
            return _cap(PRIM[r.name][0].split(".")[-1])
        if isinstance(r, StringType):
            return "String"
        if isinstance(r, FixedType):
            return "Decimal"
        if isinstance(r, (SeqType, ArrayType)):
            return "Bytes" if self.is_octet(r.elem) else self.type_label(r.elem) + "List"
        if isinstance(r, MapType):
            return self.type_label(r.key) + "To" + self.type_label(r.value) + "Map"
        return "Value"

    def wrapper(self, elem: IdlType, inner: PType) -> str:
        """Message wrapping a repeated/map value that protobuf cannot nest directly."""
        if inner.label == "repeated":
            body = [f"  repeated {inner.type} values = 1;"]
        else:
            body = [f"  {inner.type} entries = 1;"]
        what = "sequence/array" if inner.label == "repeated" else "map"
        return self._add_wrapper(self.type_label(elem), body,
                                 f"wrapper: protobuf cannot nest a {what} directly")

    def _add_wrapper(self, name: str, body: List[str], note: str) -> str:
        if name in self.local_names:
            name += "Wrapper"
        base, n = name, 2
        while name in self.wrapper_bodies and self.wrapper_bodies[name] != body:
            name, n = f"{base}{n}", n + 1
        if name not in self.wrappers:
            self.wrapper_bodies[name] = body
            head = [f"// {note}"] if self.opts.comments else []
            self.wrappers[name] = head + [f"message {name} {{"] + body + ["}"]
        return name

    # ── fields ──
    def field_line(self, indent: str, name: str, p: PType, number: int, anns: List[Annotation],
                   orig: IdlType, extra: Optional[str] = None, in_oneof: bool = False) -> str:
        label = "repeated " if p.label == "repeated" else ""
        notes: List[str] = []
        if p.lossy:
            notes.append("IDL: " + self.idl_desc(orig))
        opt = find_ann(anns, "optional")
        if opt is not None and opt.is_true():
            if not p.label and not in_oneof:
                label = "optional "
            else:
                notes.append("@optional")
        notes += [str(a) for a in anns if a.name not in ("id", "optional", "hashid")]
        if extra:
            notes.append(extra)
        s = f"{indent}{label}{p.type} {self.field_name(name)} = {number};"
        if notes and self.opts.comments:
            s += "  // " + "; ".join(notes)
        return s

    def explicit_id(self, m: Member) -> Optional[int]:
        a = find_ann(m.anns, "id")
        return a.int_arg() if a is not None else None

    def assign_numbers(self, explicit: List[Optional[int]], names: List[str],
                       loc: Tuple[str, int]) -> List[int]:
        used: Set[int] = set()
        nums: List[Optional[int]] = [None] * len(explicit)
        for i, e in enumerate(explicit):
            if e is None:
                continue
            if not 1 <= e <= MAX_FIELD_NUMBER or e in RESERVED_FIELD_RANGE:
                self.rep.warn(f"@id({e}) of '{names[i]}' is not a valid protobuf field number; "
                              "numbering it automatically", *loc)
            elif e in used:
                self.rep.warn(f"duplicate @id({e}) on '{names[i]}'; numbering it automatically", *loc)
            else:
                used.add(e)
                nums[i] = e
        prev = 0
        for i in range(len(nums)):
            if nums[i] is None:
                n = prev + 1
                while n in used or n in RESERVED_FIELD_RANGE:
                    n += 1
                nums[i] = n
                used.add(n)
            prev = nums[i]
        return nums  # type: ignore[return-value]

    def collect_members(self, d: StructDecl, visiting: Set[Tuple[str, ...]]
                        ) -> List[Tuple[Member, StructDecl]]:
        if d.fqn in visiting:
            raise IdlError(f"inheritance cycle involving '{d.idl_name}'", d.file, d.line)
        visiting = visiting | {d.fqn}
        res: List[Tuple[Member, StructDecl]] = []
        if d.base is not None:
            b = self.resolve(d.base)
            if isinstance(b, StructDecl):
                res.extend(self.collect_members(b, visiting))
            else:
                self.rep.warn(f"base type '{d.base.name}' of '{d.name}' not found or not a "
                              "struct; inherited members are missing", d.file, d.line)
        res.extend((m, d) for m in d.members)
        return res

    # ── declarations ──
    def _header(self, d: Decl, what: str) -> List[str]:
        if not self.opts.comments:
            return []
        parts = [str(a) for a in d.anns]
        if not parts and not what:
            return []
        return ["// IDL: " + " ".join(parts + ([what] if what else []))]

    def emit_struct(self, d: StructDecl) -> List[str]:
        members = self.collect_members(d, set())
        what = f"struct {d.name} : {d.base.name} (base members flattened)" if d.base else ""
        out = self._header(d, what)
        autoid = find_ann(d.anns, "autoid")
        if autoid is not None and autoid.arg() is not None and autoid.arg().text.upper() == "HASH":
            self.rep.warn(f"@autoid(HASH) on '{d.name}': protobuf field numbers are assigned "
                          "sequentially instead", d.file, d.line)
        nums = self.assign_numbers([self.explicit_id(m) for m, _ in members],
                                   [m.name for m, _ in members], (d.file, d.line))
        out.append(f"message {self.local_name(d)} {{")
        seen: Set[str] = set()
        for (m, origin), n in zip(members, nums):
            fname = self.field_name(m.name)
            if fname in seen:
                self.rep.warn(f"duplicate field name '{fname}' in '{d.name}'", origin.file, m.line)
            seen.add(fname)
            p = self.map_type(m.type, (origin.file, m.line))
            extra = f"from {origin.name}" if origin is not d else None
            out.append(self.field_line("  ", m.name, p, n, m.anns, m.type, extra))
        out.append("}")
        return out

    def emit_union(self, d: UnionDecl) -> List[str]:
        loc = (d.file, d.line)
        out = self._header(d, f"union {d.name} switch ({idl_str(d.switch_type)})")
        members = [c.member for c in d.cases]
        names = {self.field_name(m.name) for m in members}
        disc = "discriminator"
        while disc in names:
            disc += "_"
        oneof = "value"
        while oneof in names or oneof == disc:
            oneof += "_"
        nums = self.assign_numbers([1] + [self.explicit_id(m) for m in members],
                                   [disc] + [m.name for m in members], loc)
        sw = self.map_type(d.switch_type, loc)
        out.append(f"message {self.local_name(d)} {{")
        out.append(self.field_line("  ", disc, sw, 1, d.switch_anns, d.switch_type))
        out.append(f"  oneof {oneof} {{")
        for c, n in zip(d.cases, nums[1:]):
            m = c.member
            p = self.map_type(m.type, (d.file, m.line))
            if p.label:  # repeated/map are not allowed inside a oneof
                p = PType("", self.wrapper(m.type, p), "message", True)
            cases = [lb.text for lb in c.labels if lb is not None]
            lbl = ("case " + ", ".join(cases)) if cases else ""
            if any(lb is None for lb in c.labels):
                lbl = (lbl + ", " if lbl else "") + "default"
            out.append(self.field_line("    ", m.name, p, n, m.anns, m.type, lbl, in_oneof=True))
        out.append("  }")
        out.append("}")
        return out

    def emit_enum(self, d: EnumDecl) -> List[str]:
        name = self.local_name(d)
        prefix = _upper_snake(name)
        vals: List[Tuple[str, int, List[str]]] = []
        for e in d.values:
            if self.opts.enum_prefix:
                vn = _upper_snake(e.name)
                if not (vn == prefix or vn.startswith(prefix + "_")):
                    vn = f"{prefix}_{vn}"
            else:
                vn = e.name
            if not -2**31 <= e.value < 2**31:
                raise IdlError(f"enumerator '{e.name}' = {e.value} does not fit into a protobuf "
                               "enum (int32)", d.file, d.line)
            vals.append((vn, e.value, [str(a) for a in e.anns if a.name != "value"]))
        out = self._header(d, "")
        out.append(f"enum {name} {{")
        if len({v for _, v, _ in vals}) < len(vals):
            out.append("  option allow_alias = true;")
        zero = [i for i, (_, v, _) in enumerate(vals) if v == 0]
        if not zero:
            zname = f"{prefix}_UNSPECIFIED"
            while any(n == zname for n, _, _ in vals):
                zname += "_"
            vals.insert(0, (zname, 0, ["added: proto3 needs a zero value first"]))
        elif zero[0] != 0:
            vals.insert(0, vals.pop(zero[0]))
        for n, v, notes in vals:
            line = f"  {n} = {v};"
            if notes and self.opts.comments:
                line += "  // " + "; ".join(notes)
            out.append(line)
        out.append("}")
        return out

    def emit_bitmask(self, d: BitmaskDecl) -> List[str]:
        if not self.opts.comments:
            return []
        ftype = "uint64" if d.bit_bound > 32 else "uint32"
        out = [f"// IDL: bitmask {d.name} (bit_bound {d.bit_bound}) - fields use {ftype}. Flags:"]
        out += [f"//   {n} = 1 << {pos} (0x{1 << pos:X})" for n, pos in d.flags]
        return out

    def generate(self) -> str:
        blocks: List[str] = []
        consts = [d for d in self.f.decls if isinstance(d, ConstDecl)]
        if consts and self.opts.comments:
            lines = ["// IDL constants (protobuf has no constants):"]
            for c in consts:
                t = idl_str(c.type) if c.type else "#define"
                lines.append(f"//   const {t} {c.idl_name} = {_bound(c.expr)};")
            blocks.append("\n".join(lines))
        for d in self.f.decls:
            if isinstance(d, StructDecl):
                blk = self.emit_struct(d)
            elif isinstance(d, UnionDecl):
                blk = self.emit_union(d)
            elif isinstance(d, EnumDecl):
                blk = self.emit_enum(d)
            elif isinstance(d, BitmaskDecl):
                blk = self.emit_bitmask(d)
            elif isinstance(d, BitsetDecl) and self.opts.comments:
                blk = [f"// IDL: bitset {d.name} - fields use uint64"]
            else:
                continue
            if blk:
                blocks.append("\n".join(blk))
        blocks += ["\n".join(w) for w in self.wrappers.values()]

        imports = {proto_import_path(w) for w, _ in self.f.includes}
        for p in self.used_files:
            if p in self.loader.import_names:
                imports.add(proto_import_path(self.loader.import_names[p]))
        if self.uses_any:
            imports.add("google/protobuf/any.proto")

        head = [f"// Generated by idl2proto {__version__} from {os.path.basename(self.f.path)}"
                " - do not edit by hand.", "", 'syntax = "proto3";', ""]
        if self.f.package:
            head += [f"package {self.f.package};", ""]
        if imports:
            head += [f'import "{i}";' for i in sorted(imports)] + [""]
        return "\n".join(head) + "\n" + "\n\n".join(blocks) + "\n"


# ════════════════════════════ CLI ════════════════════════════

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="idl2proto",
        description="Convert OMG IDL (IDL 4.x / DDS-XTypes) into a proto3 file.")
    ap.add_argument("input", help="IDL file to convert")
    ap.add_argument("-o", "--output",
                    help="output .proto file ('-' = stdout); default: <input>.proto")
    ap.add_argument("-I", "--include", action="append", default=[], metavar="DIR",
                    help="directory to search for #include files (repeatable)")
    ap.add_argument("-p", "--package", help="proto package (default: common module path)")
    ap.add_argument("-r", "--recursive", action="store_true",
                    help="also convert included IDL files (written next to the output)")
    ap.add_argument("--snake-case", action="store_true",
                    help="convert field names to snake_case")
    ap.add_argument("--no-enum-prefix", dest="enum_prefix", action="store_false",
                    help="keep enum value names unchanged instead of ENUM_NAME_VALUE")
    ap.add_argument("--module-separator", default="_", metavar="SEP",
                    help="joins nested module names into type names (default: '_')")
    ap.add_argument("--no-comments", dest="comments", action="store_false",
                    help="omit comments about the original IDL")
    ap.add_argument("-q", "--quiet", action="store_true", help="suppress warnings")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = ap.parse_args(argv)

    rep = Reporter(args.quiet)
    try:
        loader = Loader(args.include, rep)
        main_file = loader.load(args.input)
        assign_packages(loader, main_file, args.package)
        targets = [main_file]
        if args.recursive:
            targets += [f for f in loader.order if f is not main_file]
        outputs = [(f, Emitter(f, loader, args, rep).generate()) for f in targets]
    except IdlError as e:
        print(e, file=sys.stderr)
        return 1

    main_out = args.output or os.path.splitext(args.input)[0] + ".proto"
    out_dir = "." if main_out == "-" else (os.path.dirname(main_out) or ".")
    for f, text in outputs:
        if f is main_file:
            dest = main_out
        else:
            dest = os.path.join(out_dir, proto_import_path(loader.import_names[f.path]))
        if dest == "-":
            sys.stdout.write(text)
            continue
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        with open(dest, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        if not args.quiet:
            print(f"wrote {dest}", file=sys.stderr)
    if rep.count and not args.quiet:
        print(f"{rep.count} warning(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
