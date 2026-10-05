# idl2proto

Convert **OMG IDL** (IDL 4.x / DDS-XTypes, as used for DDS topic types) into **Protocol Buffers (proto3)**.

- Pure Python, **no dependencies**, Python ≥ 3.9
- Produces `.proto` files that compile with `protoc`
- Nothing is silently dropped: IDL semantics protobuf cannot express (bounds, `@key`, extensibility, union discriminators, narrow integer types, …) are preserved as comments next to the generated fields

```bash
pip install idl2proto
idl2proto sensors.idl -r
```

## Example

```idl
module demo { module sensors {
  enum Mode { @value(1) IDLE, ACTIVE, STANDBY };

  union Reading switch (Mode) {
    case IDLE:                   int16 raw_value;
    case ACTIVE: case STANDBY:   sequence<float, 64> samples;
    default:                     string note;
  };

  @mutable
  struct DeviceStatus {
    @key @id(10) uint32 deviceId;
    Reading reading;
    char unit_code;
  };
}; };
```

becomes

```proto
syntax = "proto3";

package demo.sensors;

enum Mode {
  MODE_UNSPECIFIED = 0;  // added: proto3 needs a zero value first
  MODE_IDLE = 1;
  MODE_ACTIVE = 2;
  MODE_STANDBY = 3;
}

// IDL: union Reading switch (Mode)
message Reading {
  Mode discriminator = 1;
  oneof value {
    int32 raw_value = 2;  // IDL: int16; case IDLE
    FloatList samples = 3;  // IDL: sequence<float, 64>; case ACTIVE, STANDBY
    string note = 4;  // default
  }
}

// IDL: @mutable
message DeviceStatus {
  uint32 deviceId = 10;  // @key
  Reading reading = 11;
  uint32 unit_code = 12;  // IDL: char
}

// wrapper: protobuf cannot nest a sequence/array directly
message FloatList {
  repeated float values = 1;
}
```

A larger example covering almost every supported construct is in [`examples/`](examples/) together with its generated output.

## Usage

```bash
idl2proto input.idl                     # writes input.proto next to it
idl2proto input.idl -o out/x.proto      # explicit output path ('-' = stdout)
idl2proto input.idl -I include/dir      # search path for #include (repeatable)
idl2proto input.idl -r                  # also convert included IDL files
python -m idl2proto ...                 # same as the idl2proto command
```

| Option | Effect |
|---|---|
| `-p, --package PKG` | Force the proto package (default: common module path of the file) |
| `--snake-case` | Convert field names to snake_case |
| `--no-enum-prefix` | Keep enum value names unchanged (default: `ENUM_NAME_VALUE`) |
| `--module-separator SEP` | Separator for nested modules folded into type names (default `_`) |
| `--no-comments` | Omit comments about the original IDL |
| `-q, --quiet` | Suppress warnings |

Parse errors are reported as `file:line: error: ...` with exit code 1; warnings go to stderr.

`src/idl2proto/core.py` is self-contained and can also be copied and run as a standalone script.

## Mapping

| IDL | proto3 | Notes |
|---|---|---|
| `module` | `package` | Common module prefix of the file; deeper modules are folded into the type name (`Sub_Type`) |
| `struct` (incl. `: Base`) | `message` | Base members are flattened in first (commented `from Base`) |
| `union U switch(T)` | `message` with `discriminator` field (no. 1) + `oneof value` | Case labels kept as comments; repeated/map members get wrapper messages |
| `enum` | `enum` | Values prefixed, `@value` honored; a `X_UNSPECIFIED = 0` value is added if 0 is missing, the 0 value is moved first; duplicates → `allow_alias` |
| `bitmask` / `bitset` | `uint32` / `uint64` | Flags listed as comments (proto enums are int32) |
| `typedef` | resolved | protobuf has no aliases |
| `const`, `#define` | comment block | Still evaluated for bounds |
| `int8/uint8/int16/uint16/octet/char/wchar` | `int32` / `uint32` | Widened, original type kept as comment |
| `sequence<octet>`, `octet[N]` | `bytes` | |
| `sequence<T>`, `T[N][M]` | `repeated T` | Multi-dim arrays flattened (row-major); nested sequences → wrapper `XList { repeated X values = 1; }` |
| `map<K,V>` | `map<K,V>` | Enum key → `int32`; invalid key types (e.g. structs) → `repeated KToVEntry { key = 1; value = 2; }` |
| `string<N>`, `wstring` | `string` | Bound kept as comment |
| `fixed<d,s>` | `string` | |
| `long double` | `double` | |
| `any` | `google.protobuf.Any` | Import added automatically |
| `@id(n)` | field number n | Otherwise sequential like XTypes (after `@id(10)` comes 11); 19000–19999 are skipped |
| `@optional` | `optional` | Comment only for repeated/map |
| `@key`, `//@key`, `#pragma keylist` | comment `@key` | |
| `@appendable`, `@mutable`, … | comment above the message | |
| `#include "x.idl"` | `import "x.proto";` | Types from other files are referenced fully qualified (`.pkg.Type`) |
| `interface`, `valuetype`, `exception`, … | skipped | with a warning |

Every lossy mapping is marked with `// IDL: <original type>`.

### Design decisions

- **Field numbers follow XTypes member IDs** (`@id`), so they stay stable when the IDL evolves the way DDS expects.
- **Struct inheritance is flattened** instead of embedding a `base` field, keeping the wire format flat and simple.
- **Unions keep an explicit `discriminator` field**, because several case labels can select the same branch.
- **`char` maps to `uint32`**, not `string`, because protobuf strings must be valid UTF-8.

## Limitations

- `#if` / `#ifdef` / `#else` are not evaluated — all branches are converted (with a warning). `#ifndef` include guards are fine.
- Inline type definitions inside structs (`struct A { struct B {...} b; };`) are not supported.
- `bitset` is mapped to a single `uint64`; individual bit fields are not split out.
- `@autoid(HASH)` is ignored (sequential numbers, with a warning).
- `#pragma keylist` with nested keys (`a.b`) is not supported.
- `--package` only applies to the main file; included files keep their computed package.

## Development

```bash
pip install -e ".[test]"
pytest
```

The tests compare the conversion of `examples/` against the golden files in `examples/out/` and compile them with `protoc` (via `grpcio-tools`). After an intentional output change, regenerate the golden files:

```bash
idl2proto examples/sensors.idl -r -o examples/out/sensors.proto
```

## License

[MIT](LICENSE)
