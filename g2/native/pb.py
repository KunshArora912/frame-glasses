"""Minimal protobuf wire codec, byte-identical to open-g2-hub's ``lib/protocol/pb.dart``.

There is no generated code: every G2 message is built field by field in the order the
official app emits them, so the bytes on the wire match (field order, explicit zeros,
wire types).  Negative ``int32`` values are written as their unsigned 64-bit two's
complement (10 bytes), exactly like ``pb.dart:encodeVarint`` and Python's
``_signed_varint``.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Iterator

WIRE_VARINT = 0
WIRE_FIXED64 = 1
WIRE_LEN = 2
WIRE_FIXED32 = 5

_U64 = (1 << 64) - 1


def encode_varint(value: int) -> bytes:
    """Protobuf varint; negatives as 64-bit two's complement (pb.dart:225-235)."""
    v = value & _U64 if value < 0 else value
    out = bytearray()
    while v >= 0x80:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v)
    return bytes(out)


def decode_varint(data: bytes, i: int = 0) -> tuple[int, int]:
    """(value, next_index). Raises IndexError on a truncated varint (pb.dart:281-291)."""
    result = 0
    shift = 0
    while True:
        b = data[i]
        if shift < 64:
            result |= (b & 0x7F) << shift
        i += 1
        if not b & 0x80:
            return result, i
        shift += 7


def decode_signed(value: int) -> int:
    """Undo a signed int32/int64 written as a 64-bit two's complement varint."""
    return value - (1 << 64) if value >= (1 << 63) else value


def encode_key(field: int, wire: int) -> bytes:
    return encode_varint((field << 3) | wire)


def varint_field(field: int, value: int) -> bytes:
    return encode_key(field, WIRE_VARINT) + encode_varint(value)


def bytes_field(field: int, value: bytes) -> bytes:
    value = bytes(value)
    return encode_key(field, WIRE_LEN) + encode_varint(len(value)) + value


def string_field(field: int, value: str) -> bytes:
    return bytes_field(field, value.encode("utf-8"))


def message_field(field: int, inner: bytes) -> bytes:
    return bytes_field(field, inner)


def float_field(field: int, value: float) -> bytes:
    """protobuf float: wire type 5, 4 bytes little-endian (struct '<f')."""
    return encode_key(field, WIRE_FIXED32) + struct.pack("<f", value)


def double_field(field: int, value: float) -> bytes:
    return encode_key(field, WIRE_FIXED64) + struct.pack("<d", value)


def packed_varint_field(field: int, values: list[int]) -> bytes:
    return bytes_field(field, b"".join(encode_varint(v) for v in values))


def packed_float_field(field: int, values: list[float]) -> bytes:
    return bytes_field(field, b"".join(struct.pack("<f", float(v)) for v in values))


class Writer:
    """Growable protobuf buffer (the ``Pb`` class of pb.dart). Methods chain."""

    __slots__ = ("_parts",)

    def __init__(self) -> None:
        self._parts: list[bytes] = []

    def add(self, raw: bytes) -> "Writer":
        self._parts.append(bytes(raw))
        return self

    def varint(self, field: int, value: int) -> "Writer":
        return self.add(varint_field(field, value))

    def int32(self, field: int, value: int) -> "Writer":
        """Alias of varint: negatives are 10 bytes (pb.dart:223-225)."""
        return self.varint(field, value)

    int64 = int32

    def bool(self, field: int, value: bool) -> "Writer":  # noqa: A003 - mirrors proto names
        return self.varint(field, 1 if value else 0)

    def string(self, field: int, value: str) -> "Writer":
        return self.add(string_field(field, value))

    def bytes(self, field: int, value: bytes) -> "Writer":  # noqa: A003
        return self.add(bytes_field(field, value))

    def message(self, field: int, inner: "bytes | Writer") -> "Writer":
        if isinstance(inner, Writer):
            inner = inner.to_bytes()
        return self.add(message_field(field, inner))

    def float32(self, field: int, value: float) -> "Writer":
        return self.add(float_field(field, value))

    def double(self, field: int, value: float) -> "Writer":
        return self.add(double_field(field, value))

    def packed_varints(self, field: int, values: list[int]) -> "Writer":
        return self.add(packed_varint_field(field, values))

    def packed_floats(self, field: int, values: list[float]) -> "Writer":
        return self.add(packed_float_field(field, values))

    def repeated(self, field: int, inners: list[bytes]) -> "Writer":
        for inner in inners:
            self.message(field, inner)
        return self

    def __len__(self) -> int:
        return sum(len(p) for p in self._parts)

    def to_bytes(self) -> bytes:
        return b"".join(self._parts)

    __bytes__ = to_bytes


@dataclass(frozen=True)
class Field:
    number: int
    wire: int
    value: object  # int for wire 0, bytes otherwise

    @property
    def as_int(self) -> int:
        return int(self.value)  # type: ignore[arg-type]

    @property
    def as_bytes(self) -> bytes:
        return bytes(self.value)  # type: ignore[arg-type]

    @property
    def as_signed(self) -> int:
        return decode_signed(self.as_int)

    @property
    def as_str(self) -> str:
        return self.as_bytes.decode("utf-8", "replace")

    @property
    def as_float(self) -> float:
        b = self.as_bytes
        if len(b) < 4:
            raise ValueError("truncated float")
        return struct.unpack("<f", b[:4])[0]

    @property
    def as_double(self) -> float:
        b = self.as_bytes
        if len(b) < 8:
            raise ValueError("truncated double")
        return struct.unpack("<d", b[:8])[0]


def iter_fields(pb: bytes) -> Iterator[Field]:
    """Port of ``_iter_fields``: stops at an unknown wire type; slices past the end are
    clipped exactly like Python slicing (pb.dart:305-338)."""
    i, n = 0, len(pb)
    while i < n:
        try:
            key, i = decode_varint(pb, i)
            field, wire = key >> 3, key & 7
            if wire == WIRE_VARINT:
                v, i = decode_varint(pb, i)
        except IndexError:
            return  # truncated varint: stop (the hub wraps every decode in a try; be lenient)
        if wire == WIRE_VARINT:
            yield Field(field, wire, v)
        elif wire == WIRE_FIXED64:
            yield Field(field, wire, bytes(pb[i:i + 8]))
            i += 8
        elif wire == WIRE_LEN:
            length, i = decode_varint(pb, i)
            yield Field(field, wire, bytes(pb[i:i + length]))
            i += length
        elif wire == WIRE_FIXED32:
            yield Field(field, wire, bytes(pb[i:i + 4]))
            i += 4
        else:
            return


def read_varint(pb: bytes, field: int, default: int = -1) -> int:
    for f in iter_fields(pb):
        if f.number == field and f.wire == WIRE_VARINT:
            return f.as_int
    return default


def read_signed(pb: bytes, field: int, default: int = 0) -> int:
    v = read_varint(pb, field, None)  # type: ignore[arg-type]
    return default if v is None else decode_signed(v)


def read_string(pb: bytes, field: int, default: str = "") -> str:
    for f in iter_fields(pb):
        if f.number == field and f.wire == WIRE_LEN:
            return f.as_str
    return default


def read_bytes(pb: bytes, field: int) -> bytes | None:
    for f in iter_fields(pb):
        if f.number == field and f.wire == WIRE_LEN:
            return f.as_bytes
    return None


def read_float(pb: bytes, field: int, default: float = 0.0) -> float:
    for f in iter_fields(pb):
        if f.number == field and f.wire == WIRE_FIXED32:
            return f.as_float
    return default


def read_double(pb: bytes, field: int, default: float = 0.0) -> float:
    for f in iter_fields(pb):
        if f.number == field and f.wire == WIRE_FIXED64:
            return f.as_double
    return default


def read_messages(pb: bytes, field: int) -> list[bytes]:
    """All length-delimited values of a repeated field."""
    return [f.as_bytes for f in iter_fields(pb) if f.number == field and f.wire == WIRE_LEN]


def read_packed_varints(pb: bytes, field: int) -> list[int]:
    raw = read_bytes(pb, field)
    if raw is None:
        return []
    out, i = [], 0
    while i < len(raw):
        v, i = decode_varint(raw, i)
        out.append(v)
    return out


class Reader:
    """Convenience view over one message: ``Reader(pb)[2]`` -> first field 2 (or None)."""

    __slots__ = ("pb", "fields")

    def __init__(self, pb: bytes) -> None:
        self.pb = bytes(pb)
        self.fields = list(iter_fields(self.pb))

    def __getitem__(self, number: int) -> Field | None:
        for f in self.fields:
            if f.number == number:
                return f
        return None

    def all(self, number: int) -> list[Field]:  # noqa: A003
        return [f for f in self.fields if f.number == number]

    def varint(self, number: int, default: int = -1) -> int:
        return read_varint(self.pb, number, default)

    def string(self, number: int, default: str = "") -> str:
        return read_string(self.pb, number, default)

    def bytes(self, number: int) -> bytes | None:  # noqa: A003
        return read_bytes(self.pb, number)

    def sub(self, number: int) -> "Reader | None":
        b = read_bytes(self.pb, number)
        return None if b is None else Reader(b)

    def to_tree(self) -> dict:
        """Best-effort nested dump for debugging (tries to parse every bytes field)."""
        out: dict = {}
        for f in self.fields:
            if f.wire == WIRE_VARINT:
                v: object = f.as_int
            elif f.wire == WIRE_LEN:
                v = _try_nested(f.as_bytes)
            elif f.wire == WIRE_FIXED32:
                v = f.as_float if len(f.as_bytes) == 4 else f.as_bytes.hex()
            else:
                v = f.as_double if len(f.as_bytes) == 8 else f.as_bytes.hex()
            out.setdefault(f.number, []).append(v)
        return {k: (v[0] if len(v) == 1 else v) for k, v in out.items()}


def _try_nested(b: bytes) -> object:
    if not b:
        return {}
    try:
        text = b.decode("utf-8")
        if text.isprintable():
            return text
    except UnicodeDecodeError:
        pass
    try:
        consumed = 0
        fields = []
        for f in iter_fields(b):
            fields.append(f)
        # iter_fields swallows trailing garbage silently; re-check it consumed everything
        consumed = _consumed(b)
        if consumed == len(b) and fields and all(f.wire in (0, 1, 2, 5) for f in fields):
            return Reader(b).to_tree()
    except (IndexError, ValueError):
        pass
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.hex()


def _consumed(b: bytes) -> int:
    i, n = 0, len(b)
    while i < n:
        key, i = decode_varint(b, i)
        wire = key & 7
        if wire == 0:
            _, i = decode_varint(b, i)
        elif wire == 1:
            i += 8
        elif wire == 2:
            ln, i = decode_varint(b, i)
            i += ln
        elif wire == 5:
            i += 4
        else:
            return -1
    return i


def hexs(b: bytes) -> str:
    return bytes(b).hex()


def unhex(s: str) -> bytes:
    return bytes.fromhex(s.replace(" ", "").replace("\n", ""))
