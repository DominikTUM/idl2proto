"""Convert OMG IDL (IDL 4.x / DDS-XTypes) into Protocol Buffers (proto3)."""
from .core import IdlError, __version__, main

__all__ = ["IdlError", "__version__", "main"]
