"""Frame envelope ``AA <dir> <seq> <len> <total> <num> <SID> <flag> <payload> [CRC16]``.

Port of open-g2-hub ``lib/protocol/framing.dart`` and ``crc.dart`` (which in turn mirror the
Python ``g2.transport`` package).  Byte semantics, exactly as the Dart:

* ``dir`` is ``0x21`` app -> glasses, ``0x12`` glasses -> app (framing.dart:119-126).
* ``len`` (byte 3) is the number of bytes that follow the 8-byte header **in this packet**,
  i.e. the payload chunk including whatever part of the trailing CRC lands in it
  (framing.dart:38 ``chunk.length``).  A single-packet message therefore has
  ``len == len(pb) + 2``.  ``0xFF`` marks a frame re-assembled by :class:`FrameAssembler`.
* The CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no reflection, no final XOR) is
  computed over the **whole protobuf payload once**, appended little-endian, and the
  result is cut into chunks of at most 232 bytes; all chunks share ``seq`` and carry
  ``total``/``num`` (1-based) (framing.dart:24-43).  The CRC may straddle two packets.
"""
from __future__ import annotations

from dataclasses import dataclass

from .pb import decode_varint

DIR_TX = 0x21  # app -> glasses
DIR_RX = 0x12  # glasses -> app
FLAG_REQUEST = 0x20  # every command except the two below (device.dart:93-100)
FLAG_PLAIN = 0x00  # 0x80 auth (cmd 4) and keep-alive (cmd 14); also the EvenFile service
CHUNK_SIZE = 232  # max payload bytes per packet (framing.dart:19)
ASSEMBLED_LEN = 0xFF  # len byte of a FrameAssembler output (framing.dart:22)
HEADER_LEN = 8
DEFAULT_MAX_WRITE = 240  # framing.dart:27 default maxWrite


# ── CRC-16 ──────────────────────────────────────────────────────────────────────────────


def crc16(data: bytes, init: int = 0xFFFF) -> int:
    """CRC-16/CCITT-FALSE as an int (crc.dart:394-403). Check: crc16(b"123456789") == 0x29B1."""
    crc = init
    for byte in data:
        crc ^= (byte & 0xFF) << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def crc16_bytes(data: bytes) -> bytes:
    """The same CRC as the two wire bytes, little-endian (crc.dart:406-409)."""
    c = crc16(data)
    return bytes([c & 0xFF, (c >> 8) & 0xFF])


# ── building ────────────────────────────────────────────────────────────────────────────


def frame_pb(pb: bytes, sid: int, flag: int, seq: int, max_write: int = DEFAULT_MAX_WRITE) -> list[bytes]:
    """Wrap protobuf ``pb`` in one or more frames (framing.dart:27-43 ``framePb``).

    Returns the packets to write, in order, to characteristic 0x5401.  ``size`` is
    ``min(max_write - 8, 232)``; the default MTU 247 gives the full 232-byte chunk.
    """
    size = min(max_write - HEADER_LEN, CHUNK_SIZE)
    if size < 1:
        raise ValueError("max_write too small")
    payload = bytes(pb) + crc16_bytes(pb)
    total = max(1, (len(payload) + size - 1) // size)
    frames = []
    for i in range(total):
        chunk = payload[i * size:(i + 1) * size]
        header = bytes([0xAA, DIR_TX, seq & 0xFF, len(chunk) & 0xFF, total & 0xFF, (i + 1) & 0xFF, sid & 0xFF, flag & 0xFF])
        frames.append(header + chunk)
    return frames


def frame_single(pb: bytes, sid: int, flag: int, seq: int) -> bytes:
    """``frame_pb`` for a payload known to fit one packet (raises otherwise)."""
    frames = frame_pb(pb, sid, flag, seq)
    if len(frames) != 1:
        raise ValueError(f"payload of {len(pb)} bytes needs {len(frames)} packets")
    return frames[0]


def add_crc(packet: bytes) -> bytes:
    """transport.add_crc: packet + CRC16 (LE) of everything after the 8-byte header
    (framing.dart:46).  The community examples build frames this way."""
    return bytes(packet) + crc16_bytes(packet[HEADER_LEN:])


def auth_frame(magic: int, seq: int | None = None) -> bytes:
    """The one-packet authentication frame (LIVE; framing.dart:74-87 ``authFrameFor``).

    ``AA 21 <seq> <len> 01 01 80 00`` + ``{1: 4, 2: magic, 3: {1: 1, 2: 4}}`` + CRC16.
    ``seq`` defaults to ``magic`` (the hub's auth frame uses seq == magic; the community
    scripts use an independent seq, framing.dart:85 vs teleprompter.py:70-80).

    Deviation from the Dart: ``authFrameFor`` writes the magic as a single raw byte, which
    is only a valid varint for 1..127.  Values 128..255 are encoded as a proper 2-byte
    varint here (the Dart would emit a malformed message; its module counter never gets
    there in practice because a session authenticates once).
    """
    if not 1 <= magic <= 255:
        raise ValueError("auth magic must be 1..255")
    m = bytes([magic]) if magic < 0x80 else bytes([(magic & 0x7F) | 0x80, magic >> 7])
    pb = bytes([0x08, 0x04, 0x10]) + m + bytes([0x1A, 0x04, 0x08, 0x01, 0x10, 0x04])
    s = magic if seq is None else seq
    pkt = bytes([0xAA, DIR_TX, s & 0xFF, len(pb) + 2, 0x01, 0x01, 0x80, FLAG_PLAIN]) + pb
    return add_crc(pkt)


# ── parsing ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ParsedFrame:
    direction: int
    seq: int
    length: int
    total: int
    num: int
    sid: int
    flag: int
    payload: bytes  # protobuf bytes (CRC stripped when the frame is complete)
    crc_ok: bool | None  # None when the frame is a fragment (CRC not in this packet)

    @property
    def is_rx(self) -> bool:
        return self.direction == DIR_RX

    @property
    def is_fragment(self) -> bool:
        return self.total > 1 and self.length != ASSEMBLED_LEN


def payload_span(frame: bytes) -> tuple[int, int]:
    """(start, end) of the protobuf inside one AA frame (framing.dart:90-95)."""
    ln = frame[3]
    if ln == ASSEMBLED_LEN:
        return HEADER_LEN, len(frame) - 2
    return HEADER_LEN, HEADER_LEN + max(ln - 2, 0)


def parse_frame(frame: bytes, check_crc: bool = True) -> ParsedFrame | None:
    """Any AA frame (either direction) or None (framing.dart:113-117).  For a complete
    (single or re-assembled) frame the trailing CRC is verified when ``check_crc``."""
    frame = bytes(frame)
    if len(frame) < 10 or frame[0] != 0xAA:
        return None
    a, b = payload_span(frame)
    b = min(b, len(frame))
    payload = frame[a:b]
    total, num = frame[4], frame[5]
    crc_ok: bool | None = None
    if total <= 1 or frame[3] == ASSEMBLED_LEN:
        if check_crc:
            crc_ok = frame[b:b + 2] == crc16_bytes(payload)
    return ParsedFrame(frame[1], frame[2], frame[3], total, num, frame[6], frame[7], payload, crc_ok)


def parse_rx(frame: bytes) -> tuple[int | None, bytes]:
    """transport.parse_rx: (sid, payload) for a glasses -> app frame, else (None, b'')."""
    if len(frame) >= 10 and frame[0] == 0xAA and frame[1] == DIR_RX:
        a, b = payload_span(frame)
        return frame[6], bytes(frame[a:b])
    return None, b""


def read_ack_magic(pb: bytes) -> int | None:
    """session.read_ack_magic: field 2 of ``{1: cmd, 2: magic, ...}`` (or a message that
    starts directly with field 2, since proto3 omits a zero cmd).  None if not that shape
    (framing.dart:130-142)."""
    try:
        if len(pb) >= 2 and pb[0] == 0x10:
            return decode_varint(pb, 1)[0]
        if len(pb) < 2 or pb[0] != 0x08:
            return None
        _, i = decode_varint(pb, 1)
        if i >= len(pb) or pb[i] != 0x10:
            return None
        return decode_varint(pb, i + 1)[0]
    except IndexError:
        return None


class FrameAssembler:
    """Joins multi-packet notifications back into one synthetic frame
    ``AA <dir> <seq> FF 01 01 <sid> <flag> <payload> <crc>`` (framing.dart:146-184).

    ``feed`` returns the bytes to hand on (single packets pass straight through), or None
    while a message is incomplete.  Fragments are keyed by (sid, seq); an out-of-order
    fragment drops the partial message.  ``crc_errors`` counts re-assembled messages whose
    CRC did not match (the frame is still returned, like the Dart).
    """

    def __init__(self) -> None:
        self._parts: dict[int, list[bytes]] = {}
        self.crc_errors = 0

    def feed(self, data: bytes) -> bytes | None:
        data = bytes(data)
        if len(data) < HEADER_LEN or data[0] != 0xAA:
            return data
        total, num = data[4], data[5]
        if total <= 1:
            return data
        key = (data[6] << 8) | data[2]
        chunk = data[HEADER_LEN:HEADER_LEN + data[3]]
        if num == 1:
            self._parts[key] = [chunk]
        elif key in self._parts and len(self._parts[key]) == num - 1:
            self._parts[key].append(chunk)
        else:
            self._parts.pop(key, None)
            return None
        if num < total:
            return None
        body = b"".join(self._parts.pop(key))
        if len(body) < 2 or body[-2:] != crc16_bytes(body[:-2]):
            self.crc_errors += 1
        return bytes([0xAA, data[1], data[2], ASSEMBLED_LEN, 1, 1, data[6], data[7]]) + body


# ── counters ────────────────────────────────────────────────────────────────────────────


class MagicAllocator:
    """Message magic 1..255, wrapping, never 0 (glasses_session.dart:96-100; the firmware
    decoder drops a 1-byte magic above 255).  The hub starts at 100 so the first magic is
    101; frame seq starts at 10 so the first seq is 11 (seq wraps 0..255)."""

    def __init__(self, magic_start: int = 100, seq_start: int = 10) -> None:
        self._magic = magic_start % 255
        self._seq = seq_start & 0xFF

    def next_magic(self) -> int:
        self._magic = self._magic % 255 + 1
        return self._magic

    def next_seq(self) -> int:
        self._seq = (self._seq + 1) & 0xFF
        return self._seq

    @property
    def magic(self) -> int:
        return self._magic

    @property
    def seq(self) -> int:
        return self._seq


Seq = MagicAllocator  # short alias used in the README examples
