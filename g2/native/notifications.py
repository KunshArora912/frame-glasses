"""Phone-style notification cards: SID 0x04 control (LIVE) and the JSON card over the
EvenFile service (0xC4 cmd / 0xC5 data on characteristic 0x7401, replies on 0x7402 and on
0x5402).  Port of ``lib/protocol/file_service.dart``, ``notification.dart``,
``lib/hub/file_transfer.dart`` plus the community ``feature/notification-simple`` branch.

Two transfer forms are provided, clearly separated:

* **hub form** (DECODED/UNTESTED): START -> per 4096-byte block a DATA prefix + 232-byte
  0xC5 packets (CRC16 of the block only on the last packet) -> RESULT_CHECK, each step
  awaited (file_service.dart:28-67, file_transfer.dart:52-78).
* **community form** (COMMUNITY-live, Jan 2026): the same three control frames but the JSON
  in ONE 0xC5 frame (<=234 B; multi-packet "fails silently"), replies ignored, 0.3 s
  between steps (notification.py:189-231).

The 93-byte START header is byte-identical in both forms (native-protocol.md section 6).

CRC: the EvenFile checksum is CRC-32 poly 0x1EDC6F41, **MSB-first (non-reflected), init 0,
no final XOR** -- check value ``0xC052A8C8`` for ``b"123456789"`` (crc.dart:411-431).
That is NOT the standard CRC-32C/Castagnoli (reflected, init/xorout 0xFFFFFFFF, check
``0xE3069283``); both are implemented here, only :func:`crc32_efs` goes on the wire.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import pb
from .framing import DIR_TX, FLAG_PLAIN, crc16_bytes
from .pb import Writer, bytes_field, message_field, varint_field

SID_NOTIFICATION = 0x04
SID_FILE_CMD = 0xC4
SID_FILE_DATA = 0xC5
CHAR_FILE_WRITE, CHAR_FILE_NOTIFY = 0x7401, 0x7402

CMD_CTRL, CMD_WHITELIST_CTRL = 1, 3
F_CTRL, F_IOS, F_RESP, F_WHITELIST_CTRL, F_WHITELIST_CHK = 3, 4, 5, 6, 7

CID_START, CID_DATA, CID_RESULT_CHECK = 0, 1, 2
RSP_NAMES = {0: "SUCCESS", 2: "DATA_CRC_ERR", 4: "TIMEOUT", 5: "NO_RESOURCES", 6: "RESULT_CHECK_FAIL"}
RSP_RETRY = {2, 4, 5}
FILE_TYPE_WHITELIST, FILE_TYPE_NOTIFICATION = 0, 1
FILE_PATH = b"user/notify_whitelist.json"
BLOCK_SIZE = 4096
PACKET_DATA = 232
COMMUNITY_MAX_JSON = 234

# firmware strncpy caps, bytes (limits.dart:854-859)
JSON_APP_ID_MAX, JSON_TITLE_MAX, JSON_SUBTITLE_MAX, JSON_MESSAGE_MAX, JSON_DISPLAY_NAME_MAX = 63, 63, 63, 511, 31
# SID 0x04 iOS register caps (limits.dart:851-852)
APP_ID_MAX, DISPLAY_NAME_MAX = 32, 32


# ── checksums ──────────────────────────────────────────────────────────────────────────


def _efs_table() -> list[int]:
    t = []
    for i in range(256):
        c = i << 24
        for _ in range(8):
            c = ((c << 1) ^ 0x1EDC6F41) & 0xFFFFFFFF if c & 0x80000000 else (c << 1) & 0xFFFFFFFF
        t.append(c)
    return t


_EFS = _efs_table()


def crc32_efs(data: bytes) -> int:
    """EvenFile CRC-32: poly 0x1EDC6F41, MSB-first, init 0, no XOR (crc.dart:423-431).
    ``crc32_efs(b"123456789") == 0xC052A8C8``.  This is what the START header carries."""
    crc = 0
    for b in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _EFS[((crc >> 24) ^ b) & 0xFF]
    return crc


def _c_table() -> list[int]:
    t = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ 0x82F63B78 if c & 1 else c >> 1
        t.append(c)
    return t


_C = _c_table()


def crc32c(data: bytes) -> int:
    """Standard CRC-32C (Castagnoli, reflected, init/xorout 0xFFFFFFFF): ``crc32c(b"123456789") == 0xE3069283``.
    Provided for reference/tests only; the glasses use :func:`crc32_efs`."""
    crc = 0xFFFFFFFF
    for b in data:
        crc = (crc >> 8) ^ _C[(crc ^ b) & 0xFF]
    return crc ^ 0xFFFFFFFF


# ── SID 0x04 control (LIVE, sent on every connect) ──────────────────────────────────────


def notification_ctrl(magic: int, enable: bool = True, auto_display: bool = True, disp_time: int = 5, dnd: bool = False) -> bytes:
    """``{1: 1, 2: m, 3: {1 enable, 2 autoDisplay, 3 dispTime[, 5 dnd]}}`` -> ``0801 10m 1a06 080110011805``.
    LIVE (file_service.dart:132-137).  The glasses forget notifEnable on reboot: send on every connect."""
    ctrl = Writer().varint(1, 1 if enable else 0).varint(2, 1 if auto_display else 0).varint(3, disp_time)
    if dnd:
        ctrl.varint(5, 1)
    return varint_field(1, CMD_CTRL) + varint_field(2, magic) + message_field(F_CTRL, ctrl.to_bytes())


def whitelist_disable(magic: int, disable: bool = True) -> bytes:
    """``{1: 3, 2: m, 6: {1: 1}}`` -> ``0803 10m 3202 0801``: bypass the glasses-side app whitelist (LIVE)."""
    return varint_field(1, CMD_WHITELIST_CTRL) + varint_field(2, magic) + message_field(F_WHITELIST_CTRL, varint_field(1, 1 if disable else 0))


def ios_register(magic: int, app_id: str, display_name: str, cmd: int = CMD_CTRL) -> bytes:
    """cmd 1 field 4 iOS app register ``{1 appId <=32 B, 2 displayName <=32 B}`` (DECODED; notification.dart:169-179)."""
    from .dashboard import utf8_fit
    inner = bytes_field(1, utf8_fit(app_id, APP_ID_MAX).encode()) + bytes_field(2, utf8_fit(display_name, DISPLAY_NAME_MAX).encode())
    return varint_field(1, cmd) + varint_field(2, magic) + message_field(F_IOS, inner)


@dataclass
class NotificationMessage:
    cmd: int
    magic: int
    enable: int | None = None
    auto_display: int | None = None
    disp_time: int | None = None
    dnd: int | None = None
    app_id: str | None = None
    display_name: str | None = None
    resp_cmd: int | None = None
    whitelist_disable: int | None = None
    whitelist_crc: int | None = None
    check_status: int | None = None  # 0 MATCH 1 MISMATCH
    error: int = 0  # 0 SUCCESS 1 FAIL 8 NOT_SUPPORT


def parse_notification(payload: bytes) -> NotificationMessage:
    """Decode any SID 0x04 message (notification.dart:207-268)."""
    m = NotificationMessage(pb.read_varint(payload, 1, -1), pb.read_varint(payload, 2, -1))
    ctrl = pb.read_bytes(payload, F_CTRL)
    if ctrl is not None:
        m.enable, m.auto_display, m.disp_time, m.dnd = (pb.read_varint(ctrl, i, 0) for i in (1, 2, 3, 5))
        m.error = pb.read_varint(ctrl, 4, 0)
    ios = pb.read_bytes(payload, F_IOS)
    if ios is not None:
        m.app_id, m.display_name = pb.read_string(ios, 1), pb.read_string(ios, 2)
        m.error = pb.read_varint(ios, 3, 0) or m.error
    resp = pb.read_bytes(payload, F_RESP)
    if resp is not None:
        m.resp_cmd = pb.read_varint(resp, 1, 0)
        m.error = pb.read_varint(resp, 2, 0) or m.error
    wlc = pb.read_bytes(payload, F_WHITELIST_CTRL)
    if wlc is not None:
        m.whitelist_disable = pb.read_varint(wlc, 1, 0)
        m.error = pb.read_varint(wlc, 2, 0) or m.error
    wlk = pb.read_bytes(payload, F_WHITELIST_CHK)
    if wlk is not None:
        m.whitelist_crc, m.check_status = pb.read_varint(wlk, 1, 0), pb.read_varint(wlk, 2, 0)
        m.error = pb.read_varint(wlk, 3, 0) or m.error
    return m


# ── the JSON card ──────────────────────────────────────────────────────────────────────


def _fit(s: str, n: int) -> str:
    from .dashboard import utf8_fit
    return utf8_fit(s, n)


def _json_str(s: str) -> str:
    """Python json.dumps(ensure_ascii=False) string escaping (file_service.dart:70-98)."""
    return json.dumps(s, ensure_ascii=False)


def notification_json(title: str, message: str, subtitle: str = "", display_name: str = "Even Realities",
                      app_identifier: str = "com.even.sg", msg_id: int | None = None, now: int | None = None,
                      utc_offset_s: int | None = None) -> bytes:
    """Compact UTF-8 JSON in the app's key order (file_service.dart:105-129):
    ``{"android_notification":{"msg_id","action":0,"app_identifier","title","subtitle","message","time_s","date","display_name"}}``.
    Caps (bytes): app_identifier 63, title 63, subtitle 63, message 511, display_name 31.  ``date`` is
    LOCAL ``YYYYMMDDTHHMMSS``.  Same checksum => cached, not re-displayed: vary msg_id/text.
    The community used ``app_identifier="com.google.android.gm", display_name="Gmail"`` (the only
    combination they confirmed; the hub disables the whitelist instead)."""
    t = int(time.time()) if now is None else int(now)
    title, subtitle, message = _fit(title, JSON_TITLE_MAX), _fit(subtitle, JSON_SUBTITLE_MAX), _fit(message, JSON_MESSAGE_MAX)
    display_name, app_identifier = _fit(display_name, JSON_DISPLAY_NAME_MAX), _fit(app_identifier, JSON_APP_ID_MAX)
    if utc_offset_s is None:
        utc_offset_s = int((datetime.fromtimestamp(t) - datetime.fromtimestamp(t, timezone.utc).replace(tzinfo=None)).total_seconds())
    local = datetime.fromtimestamp(t, timezone.utc) + timedelta(seconds=utc_offset_s)
    date = local.strftime("%Y%m%dT%H%M%S")
    mid = (t & 0x7FFFFFFF) if msg_id is None else int(msg_id)
    body = ('{"android_notification":{"msg_id":%d,"action":0,"app_identifier":%s,"title":%s,"subtitle":%s,'
            '"message":%s,"time_s":%d,"date":"%s","display_name":%s}}'
            % (mid, _json_str(app_identifier), _json_str(title), _json_str(subtitle), _json_str(message), t, date, _json_str(display_name)))
    return body.encode("utf-8")


# ── EvenFile frames, hub form (file_service.dart:28-67) ─────────────────────────────────


def _header(seq: int, length: int, total: int, num: int, sid: int) -> bytes:
    return bytes([0xAA, DIR_TX, seq & 0xFF, length & 0xFF, total & 0xFF, num & 0xFF, sid & 0xFF, FLAG_PLAIN])


def efs_single(seq: int, sid: int, payload: bytes) -> bytes:
    """header + payload + CRC16(payload), flag 0x00, for START / DATA prefix / RESULT_CHECK."""
    body = bytes(payload) + crc16_bytes(payload)
    return _header(seq, len(body), 1, 1, sid) + body


def efs_start_header(data: bytes, file_type: int = FILE_TYPE_NOTIFICATION, path: bytes = FILE_PATH) -> bytes:
    """93 bytes: ``00`` + u32 LE fileType + u32 LE length + u32 LE crc32_efs + 80-byte zero-padded path."""
    meta = file_type.to_bytes(4, "little") + len(data).to_bytes(4, "little") + crc32_efs(data).to_bytes(4, "little")
    return bytes([CID_START]) + meta + path[:80].ljust(80, b"\x00")


def efs_start(seq: int, data: bytes, file_type: int = FILE_TYPE_NOTIFICATION) -> bytes:
    """START frame ``AA 21 seq 5F 01 01 C4 00 <93 B> crc16``; wait for reply ``[0, rsp]``."""
    return efs_single(seq, SID_FILE_CMD, efs_start_header(data, file_type))


def efs_data_prefix(seq: int) -> bytes:
    """DATA prefix ``AA 21 seq 03 01 01 C4 00 01 crc16`` (one per 4096-byte block)."""
    return efs_single(seq, SID_FILE_CMD, bytes([CID_DATA]))


def efs_data_packets(seq: int, block: bytes) -> list[bytes]:
    """The block as 0xC5 frames of 232 bytes with total/num; CRC16 of the whole block only on the
    last packet; one extra CRC-only packet when ``len % 232`` is 0 or 231 (file_service.dart:50-65)."""
    crc = crc16_bytes(block)
    chunks = [block[i:i + PACKET_DATA] for i in range(0, len(block), PACKET_DATA)] or [b""]
    r = len(block) % PACKET_DATA
    if block and (r == 0 or r == PACKET_DATA - 1):
        chunks.append(b"")
    frames = []
    for n, chunk in enumerate(chunks, 1):
        body = chunk + (crc if n == len(chunks) else b"")
        frames.append(_header(seq, len(body), len(chunks), n, SID_FILE_DATA) + body)
    return frames


def efs_result_check(seq: int) -> bytes:
    """RESULT_CHECK ``AA 21 seq 03 01 01 C4 00 02 crc16``; wait for ``[2, 0]``."""
    return efs_single(seq, SID_FILE_CMD, bytes([CID_RESULT_CHECK]))


@dataclass(frozen=True)
class EfsStep:
    name: str  # 'start' | 'data' | 'result_check'
    frames: tuple[bytes, ...]
    expect_cid: int  # reply [cid, rsp] to wait for
    offset: int = 0  # block offset for 'data'


def hub_notification_steps(seq_fn, data: bytes, file_type: int = FILE_TYPE_NOTIFICATION) -> list[EfsStep]:
    """The hub transfer as awaitable steps (file_transfer.dart:545-571): START -> DATA per block
    (prefix + packets, same seq) -> RESULT_CHECK.  Retry a 'data' step up to 3 times on rsp
    2/4/5 (5 s pause after 5), regenerating with a fresh seq.  Write all frames to 0x7401, 10 ms
    apart.  DECODED/UNTESTED."""
    steps = [EfsStep("start", (efs_start(seq_fn(), data, file_type),), CID_START)]
    for off in range(0, max(len(data), 1), BLOCK_SIZE):
        block = data[off:off + BLOCK_SIZE]
        s = seq_fn()
        steps.append(EfsStep("data", (efs_data_prefix(s), *efs_data_packets(s, block)), CID_DATA, off))
    steps.append(EfsStep("result_check", (efs_result_check(seq_fn()),), CID_RESULT_CHECK))
    return steps


def parse_file_reply(payload: bytes) -> tuple[int, int] | None:
    """Replies are raw ``[cid, rsp]`` (file_service.dart:147); None when too short."""
    return None if len(payload) < 2 else (payload[0], payload[1])


# ── EvenFile frames, community form (notification.py:189-231) ───────────────────────────


def community_packet(seq: int, sid: int, payload: bytes, total: int = 1, num: int = 1) -> bytes:
    """``build_packet``: header with ``len = len(payload)+2`` and a CRC16 of this packet's payload."""
    return _header(seq, len(payload) + 2, total, num, sid) + bytes(payload) + crc16_bytes(payload)


def community_file_check(data: bytes, seq: int = 0x10) -> bytes:
    """FILE_CHECK: ``u32 0x100, u32 len*256, u32 (crc<<8), u8 crc>>24, 80-byte name`` -- bit-identical
    to :func:`efs_start` for fileType 1 (the shift is the hub's leading ``00`` byte)."""
    crc = crc32_efs(data)
    body = ((0x100).to_bytes(4, "little") + (len(data) * 256 & 0xFFFFFFFF).to_bytes(4, "little")
            + ((crc << 8) & 0xFFFFFFFF).to_bytes(4, "little") + bytes([(crc >> 24) & 0xFF]) + FILE_PATH.ljust(80, b"\x00"))
    return community_packet(seq, SID_FILE_CMD, body)


def community_start(seq: int = 0x49) -> bytes:
    return community_packet(seq, SID_FILE_CMD, bytes([0x01]))


def community_data(data: bytes, seq: int = 0x49) -> list[bytes]:
    """The JSON in 234-byte chunks, each with ITS OWN CRC16 (differs from the hub form for >1 chunk;
    the community reports multi-packet transfers fail silently)."""
    chunks = [data[i:i + COMMUNITY_MAX_JSON] for i in range(0, len(data), COMMUNITY_MAX_JSON)] or [b""]
    return [community_packet(seq, SID_FILE_DATA, c, len(chunks), i + 1) for i, c in enumerate(chunks)]


def community_end(seq: int = 0xDA) -> bytes:
    return community_packet(seq, SID_FILE_CMD, bytes([0x02]))


def community_notification_frames(data: bytes, strict: bool = True) -> list[bytes]:
    """FILE_CHECK, START, DATA..., END in order (write to 0x7401, 0.3 s / 0.1 s / 0.05 s / 0.3 s apart),
    then the section-1 keep-alive to the LEFT lens on 0x5401 (their own keep-alive frame
    ``aa210e0601018020080e106b6a00e174`` is internally inconsistent: do not reuse it).
    ``strict`` raises when the JSON exceeds the 234-byte single-frame limit."""
    if strict and len(data) > COMMUNITY_MAX_JSON:
        raise ValueError(f"community form needs the JSON in one packet (<= {COMMUNITY_MAX_JSON} B), got {len(data)}")
    return [community_file_check(data), community_start(), *community_data(data), community_end()]
