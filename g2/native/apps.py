"""Full-screen native apps: Teleprompter (SID 0x06), Translate (SID 0x05), Navigation (SID 0x08).
Port of ``lib/protocol/teleprompter.dart``, ``translate.dart``, ``navigation.dart`` plus the
community teleprompter flow (``even-g2-protocol/examples/teleprompter/teleprompter.py``).

Common shape: open -> periodic heartbeat -> content -> glasses send their own exit -> close.
Only one native app at a time.  Confidence (native-protocol.md section 7):
Teleprompter hub flow DECODED / community flow COMMUNITY; Translate DECODED (live COMM_RESP
replies seen, incl. FAIL); Navigation DECODED.
"""
from __future__ import annotations

import textwrap
from dataclasses import dataclass
from typing import Sequence

from . import pb
from .dashboard import utf8_fit
from .framing import add_crc
from .pb import Writer, bytes_field, encode_varint, message_field, string_field, varint_field

# ═══════════════════════════════════════════════════════════════════════════════════════
# Teleprompter (SID 0x06)
# ═══════════════════════════════════════════════════════════════════════════════════════

SID_TELEPROMPTER = 0x06
TP_CMD_CONTROL, TP_CMD_FILE_LIST, TP_CMD_PAGE_DATA, TP_CMD_PAGE_AI_SYNC, TP_CMD_STATUS_NOTIFY = 1, 2, 3, 4, 161
TP_CMD_FILE_LIST_REQUEST, TP_CMD_FILE_SELECT, TP_CMD_PAGE_DATA_REQUEST, TP_CMD_PAGE_SCROLL_SYNC = 162, 163, 164, 165
TP_CMD_COMM_RESP, TP_CMD_PAGE_LOAD_DONE, TP_CMD_MODE_SWITCH_REQ, TP_CMD_MODE_SWITCH_RSP = 166, 167, 168, 169
TP_CMD_END_SESSION_REQUEST, TP_CMD_HEARTBEAT = 170, 255
TP_CMD_NAMES = {1: "CONTROL", 2: "FILE_LIST", 3: "PAGE_DATA", 4: "PAGE_AI_SYNC", 161: "STATUS_NOTIFY", 162: "FILE_LIST_REQUEST",
                163: "FILE_SELECT", 164: "PAGE_DATA_REQUEST", 165: "PAGE_SCROLL_SYNC", 166: "COMM_RESP", 167: "PAGE_LOAD_DONE",
                168: "MODE_SWITCH_REQ", 169: "MODE_SWITCH_RSP", 170: "END_SESSION_REQUEST", 255: "HEARTBEAT"}
TP_FIELD = {1: 3, 2: 4, 3: 5, 4: 6, 161: 7, 162: 8, 163: 9, 164: 10, 165: 11, 166: 12, 255: 13, 167: 14, 168: 15, 169: 16, 170: 17}
TP_CTRL_START, TP_CTRL_PAUSE, TP_CTRL_RESUME, TP_CTRL_CLOSE = 1, 2, 3, 4
TP_MODE_AI, TP_MODE_MANUAL, TP_MODE_AUTO = 0, 1, 2
TP_ERROR_NAMES = {0: "SUCCESS", 1: "FAIL", 2: "CLOSED", 3: "REPEATED_MESSAGE", 4: "PD_DECODE_FAIL", 5: "FALLBACK_FAIL"}
TP_DISPLAY_WIDTH, TP_DISPLAY_LINES, TP_LINES_PER_PAGE, TP_CHARS_PER_LINE = 567, 9, 10, 28
TP_PAGE_MAX_BYTES = 1024
TP_HEARTBEAT_INTERVAL_S = 6.0
TP_FILENAME_MAX_CHARS = 30


def tp_package(cmd: int, magic: int, inner: bytes) -> bytes:
    return varint_field(1, cmd) + varint_field(2, magic) + message_field(TP_FIELD[cmd], inner)


def _nz(field: int, value: int) -> bytes:
    return varint_field(field, value) if value else b""


def tp_settings(total_pages: int, total_lines: int, mode: int = TP_MODE_MANUAL, start_page: int = 0, start_line: int = 0,
                display_width: int = TP_DISPLAY_WIDTH, scroll_interval_ms: int = 0, countdown_s: int = 0,
                display_lines: int = TP_DISPLAY_LINES) -> bytes:
    """Settings ``{1 mode, 2 startPage, 3 startLine, 4 totalPages, 5 totalLines, 6 displayWidth 567, 7 scrollMs,
    8 countdown, 9 displayLines 9}``; zero fields omitted (teleprompter.dart:99-110)."""
    return (_nz(1, mode) + _nz(2, start_page) + _nz(3, start_line) + _nz(4, total_pages) + _nz(5, total_lines)
            + _nz(6, display_width) + _nz(7, scroll_interval_ms) + _nz(8, countdown_s) + _nz(9, display_lines))


def tp_start(magic: int, total_pages: int, total_lines: int, mode: int = TP_MODE_MANUAL, start_page: int = 0,
             start_line: int = 0, scroll_interval_ms: int = 0, countdown_s: int = 0) -> bytes:
    """CONTROL START ``{1:1, 2:m, 3:{1: 1, 2: Settings}}`` (DECODED; retried once on COMM_RESP FAIL)."""
    inner = varint_field(1, TP_CTRL_START) + message_field(2, tp_settings(total_pages, total_lines, mode, start_page, start_line,
                                                                           scroll_interval_ms=scroll_interval_ms, countdown_s=countdown_s))
    return tp_package(TP_CMD_CONTROL, magic, inner)


def tp_ctrl(magic: int, ctrl: int) -> bytes:
    return tp_package(TP_CMD_CONTROL, magic, varint_field(1, ctrl))


def tp_pause(magic: int) -> bytes:
    return tp_ctrl(magic, TP_CTRL_PAUSE)


def tp_resume(magic: int) -> bytes:
    return tp_ctrl(magic, TP_CTRL_RESUME)


def tp_close(magic: int) -> bytes:
    return tp_ctrl(magic, TP_CTRL_CLOSE)


def tp_page_text(lines: Sequence[str]) -> bytes:
    """Lines joined by ``\\n`` plus `` \\n`` (teleprompter.dart:134)."""
    return ("\n".join(lines) + " \n").encode("utf-8")


def tp_page(magic: int, page_id: int, lines: Sequence[str]) -> bytes:
    """PAGE_DATA ``{1 pageId, 2 lineCount, 3 bytes text}`` answering PAGE_DATA_REQUEST; <=1024 B (raises above)."""
    text = tp_page_text(lines)
    if len(text) > TP_PAGE_MAX_BYTES:
        raise ValueError(f"teleprompter page text is {len(text)} bytes; firmware limit is {TP_PAGE_MAX_BYTES}")
    return tp_package(TP_CMD_PAGE_DATA, magic, _nz(1, page_id) + _nz(2, len(lines)) + bytes_field(3, text))


def tp_heartbeat(magic: int, page_id: int = 0, line_id: int = 0) -> bytes:
    """``{255, m, 13: {1 pageId, 2 lineId}}`` every 6 s; the glasses' heartbeat reports position in fields 3/4."""
    return tp_package(TP_CMD_HEARTBEAT, magic, _nz(1, page_id) + _nz(2, line_id))


def tp_scroll_sync(magic: int, page_id: int, line_id: int) -> bytes:
    return tp_package(TP_CMD_PAGE_SCROLL_SYNC, magic, _nz(1, page_id) + _nz(2, line_id))


def tp_file_list(magic: int, files: Sequence[tuple[bytes | str, str]], menu_mode: int | None = None) -> bytes:
    """FILE_LIST ``{1: {1 fileId bytes, 2 name <=30 chars}..., 2: {1 menuMode}}`` for a menu launch."""
    w = Writer()
    for fid, name in files:
        fb = fid if isinstance(fid, (bytes, bytearray)) else str(fid).encode("utf-8")
        w.message(1, bytes_field(1, bytes(fb)) + string_field(2, name[:TP_FILENAME_MAX_CHARS]))
    if menu_mode is not None:
        w.message(2, _nz(1, menu_mode))
    return tp_package(TP_CMD_FILE_LIST, magic, w.to_bytes())


def tp_mode_switch_response(magic: int, error: int = 0) -> bytes:
    return tp_package(TP_CMD_MODE_SWITCH_RSP, magic, _nz(1, error))


def tp_paginate(script: str, chars_per_line: int = TP_CHARS_PER_LINE, lines_per_page: int = TP_LINES_PER_PAGE) -> list[list[str]]:
    """Word-wrap into lines of <= chars_per_line, grouped into pages of <= lines_per_page and <= 1022 B
    (teleprompter.dart:46-95; the hub's textwrap-based layout)."""
    lines: list[str] = []
    for para in script.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        para = para.expandtabs(4).strip()
        w = textwrap.wrap(para, chars_per_line)
        lines.extend(w or [""])
    while lines and not lines[-1]:
        lines.pop()
    while lines and not lines[0]:
        lines.pop(0)
    budget = TP_PAGE_MAX_BYTES - 2
    pages: list[list[str]] = []
    cur: list[str] = []
    used = 0
    for line in lines:
        pieces = []
        rest = line
        while len(rest.encode("utf-8")) > budget:
            head = utf8_fit(rest, budget) or rest[0]
            pieces.append(head)
            rest = rest[len(head):]
        pieces.append(rest)
        for piece in pieces:
            need = len(piece.encode("utf-8")) + (1 if cur else 0)
            if cur and (len(cur) >= lines_per_page or used + need > budget):
                pages.append(cur)
                cur, used, need = [], 0, len(piece.encode("utf-8"))
            cur.append(piece)
            used += need
    if cur:
        pages.append(cur)
    return pages


@dataclass
class TeleprompterMessage:
    cmd: int
    magic: int
    page: int | None = None
    line: int | None = None
    value: int | None = None
    error: int | None = None
    file_id: bytes | None = None
    start_page: int | None = None
    end_page: int | None = None

    @property
    def name(self) -> str:
        return TP_CMD_NAMES.get(self.cmd, f"cmd={self.cmd}")

    @property
    def is_close(self) -> bool:
        return self.cmd == TP_CMD_END_SESSION_REQUEST or (self.cmd == TP_CMD_STATUS_NOTIFY and self.value == TP_CTRL_CLOSE)


def tp_parse(payload: bytes) -> TeleprompterMessage:
    """Decode a teleprompter message from the glasses (teleprompter.dart:168-220)."""
    m = TeleprompterMessage(pb.read_varint(payload, 1, -1), pb.read_varint(payload, 2, -1))
    fld = TP_FIELD.get(m.cmd)
    sub = pb.read_bytes(payload, fld) if fld is not None else None
    if sub is None:
        return m
    v = lambda n: pb.read_varint(sub, n, 0)  # noqa: E731
    if m.cmd == TP_CMD_PAGE_DATA_REQUEST:
        m.page = v(1)
    elif m.cmd == TP_CMD_PAGE_SCROLL_SYNC:
        m.page, m.line = v(1), v(2)
    elif m.cmd == TP_CMD_HEARTBEAT:
        m.page, m.line = v(3), v(4)
    elif m.cmd == TP_CMD_PAGE_LOAD_DONE:
        m.start_page, m.end_page = v(1), v(2)
    elif m.cmd == TP_CMD_STATUS_NOTIFY:
        m.value, m.error = v(1), v(2)
    elif m.cmd == TP_CMD_CONTROL:
        m.value, m.error = v(1), v(3)
    elif m.cmd == TP_CMD_FILE_SELECT:
        m.file_id = pb.read_bytes(sub, 1) or b""
    elif m.cmd == TP_CMD_MODE_SWITCH_REQ:
        m.value = v(1)
    elif m.cmd in (TP_CMD_COMM_RESP, TP_CMD_MODE_SWITCH_RSP):
        m.error = v(1)
    return m


# ── community teleprompter flow (COMMUNITY-live; teleprompter.py:121-178) ───────────────

#: the 106-byte "display config" blob the community sends on SID 0x0E cmd 2 before the init
COMMUNITY_TP_DISPLAY_CONFIG = bytes.fromhex(
    "0801121308021090" "4E1D00E094442500" "000000280030001213"
    "0803100D0F1D0040" "8D44250000000028" "0030001212080410"
    "001D0000884225" "00000000280030" "001212080510001D"
    "00009242250000" "A242280030001212" "080610001D0000C6"
    "42250000C4422800" "30001800")


def community_packet(seq: int, sid: int, flag: int, payload: bytes) -> bytes:
    """Community ``build_packet``: ``AA 21 seq len+2 01 01 sid flag`` + payload + CRC16(payload)."""
    return add_crc(bytes([0xAA, 0x21, seq & 0xFF, len(payload) + 2, 0x01, 0x01, sid & 0xFF, flag & 0xFF]) + payload)


def community_tp_display_config(seq: int, msg_id: int) -> bytes:
    """SID 0x0E flag 0x20: ``{1: 2, 2: id, 4: <106-byte blob>}``.  NOTE 0x0E is the HEALTH channel in the
    hub's SID map (sids.dart:16); the community's name for it is wrong, the bytes are what they sent."""
    payload = bytes([0x08, 0x02, 0x10]) + encode_varint(msg_id) + bytes([0x22, 0x6A]) + COMMUNITY_TP_DISPLAY_CONFIG
    return community_packet(seq, 0x0E, 0x20, payload)


def community_tp_init(seq: int, msg_id: int, total_lines: int = 10, manual_mode: bool = True) -> bytes:
    """SID 0x06 CONTROL ``{1:1, 2:id, 3:{1: 1, 2:{1:1, 2:0, 3:0, 4:267, 5:contentHeight, 6:230, 7:1294, 8:5, 9:mode}}}``
    with contentHeight = max(1, lines*2665//140) and mode 0 (their "manual") / 1 (teleprompter.py:134-152)."""
    mode = 0x00 if manual_mode else 0x01
    content_height = max(1, (total_lines * 2665) // 140)
    display = (bytes([0x08, 0x01, 0x10, 0x00, 0x18, 0x00, 0x20, 0x8B, 0x02]) + bytes([0x28]) + encode_varint(content_height)
               + bytes([0x30, 0xE6, 0x01]) + bytes([0x38, 0x8E, 0x0A]) + bytes([0x40, 0x05, 0x48, mode]))
    settings = bytes([0x08, 0x01, 0x12, len(display)]) + display
    payload = bytes([0x08, 0x01, 0x10]) + encode_varint(msg_id) + bytes([0x1A, len(settings)]) + settings
    return community_packet(seq, SID_TELEPROMPTER, 0x20, payload)


def community_tp_page(seq: int, msg_id: int, page_num: int, text: str) -> bytes:
    """SID 0x06 PAGE_DATA ``{1:3, 2:id, 5:{1: page, 2: 10, 3: bytes("\\n"+text)}}`` pushed unsolicited."""
    tb = ("\n" + text).encode("utf-8")
    inner = bytes([0x08]) + encode_varint(page_num) + bytes([0x10, 0x0A]) + bytes([0x1A]) + encode_varint(len(tb)) + tb
    payload = bytes([0x08, 0x03, 0x10]) + encode_varint(msg_id) + bytes([0x2A]) + encode_varint(len(inner)) + inner
    return community_packet(seq, SID_TELEPROMPTER, 0x20, payload)


def community_tp_marker(seq: int, msg_id: int) -> bytes:
    """SID 0x06 cmd 255 marker ``{13: {1: 0, 2: 6}}`` sent mid-stream."""
    payload = bytes([0x08, 0xFF, 0x01, 0x10]) + encode_varint(msg_id) + bytes([0x6A, 0x04, 0x08, 0x00, 0x10, 0x06])
    return community_packet(seq, SID_TELEPROMPTER, 0x20, payload)


def community_tp_sync(seq: int, msg_id: int) -> bytes:
    """SID 0x80 flag 0x00 cmd 14 ``{13: {}}`` (= the section-1 keep-alive) sent after page 1."""
    payload = bytes([0x08, 0x0E, 0x10]) + encode_varint(msg_id) + bytes([0x6A, 0x00])
    return community_packet(seq, 0x80, 0x00, payload)


def community_tp_format(text: str, chars_per_line: int = 25, lines_per_page: int = 10, min_pages: int = 14) -> list[str]:
    """Their pagination: wrapped lines padded to 10 per page, text starts with ``\\n`` (added by
    :func:`community_tp_page`), ends `` \\n``, >= 14 pages (teleprompter.py:184-224)."""
    text = text.replace("\\n", "\n")
    wrapped: list[str] = []
    for line in text.split("\n"):
        if not line.strip():
            wrapped.append("")
            continue
        current = ""
        for word in line.split():
            if len(current) + len(word) + 1 > chars_per_line:
                if current:
                    wrapped.append(current.strip())
                current = word + " "
            else:
                current += word + " "
        if current.strip():
            wrapped.append(current.strip())
    if not wrapped:
        wrapped = [text]
    while len(wrapped) < lines_per_page:
        wrapped.append(" ")
    pages = []
    for i in range(0, len(wrapped), lines_per_page):
        pl = wrapped[i:i + lines_per_page]
        while len(pl) < lines_per_page:
            pl.append(" ")
        pages.append("\n".join(pl) + " \n")
    while len(pages) < min_pages:
        pages.append("\n".join([" "] * lines_per_page) + " \n")
    return pages


def community_tp_frames(text: str, seq_start: int = 0x08, msg_id_start: int = 100, manual_mode: bool = True) -> list[bytes]:
    """The whole community sequence (after their auth): display config, init, 10 pages, marker,
    0x80 sync, remaining pages.  Spacing in their script: 0.1 s between frames."""
    pages = community_tp_format(text)
    seq, mid = seq_start, msg_id_start
    out = []

    def nxt(fn, *a):
        nonlocal seq, mid
        f = fn(seq, mid, *a)
        seq, mid = (seq + 1) & 0xFF, mid + 1
        return f

    out.append(nxt(community_tp_display_config))
    out.append(nxt(community_tp_init, len(pages) * 10, manual_mode))
    for i, p in enumerate(pages[:10]):
        out.append(nxt(community_tp_page, i, p))
    out.append(nxt(community_tp_marker))
    out.append(nxt(community_tp_sync))
    for i, p in enumerate(pages[10:], 10):
        out.append(nxt(community_tp_page, i, p))
    return out


# ═══════════════════════════════════════════════════════════════════════════════════════
# Translate (SID 0x05) -- DECODED
# ═══════════════════════════════════════════════════════════════════════════════════════

SID_TRANSLATE = 0x05
TR_CMD_CTRL, TR_CMD_RESULT, TR_CMD_NOTIFY, TR_CMD_COMM_RESP, TR_CMD_MODE_SWITCH = 1, 2, 161, 162, 163
TR_CMD_LANG_SWITCH_REQ, TR_CMD_LANG_SWITCH_RSP, TR_CMD_HEARTBEAT = 164, 165, 255
TR_CMD_NAMES = {1: "CTRL", 2: "RESULT", 161: "NOTIFY", 162: "COMM_RESP", 163: "MODE_SWITCH", 164: "LANG_SWITCH_REQ",
                165: "LANG_SWITCH_RSP", 255: "HEARTBEAT"}
TR_FIELD = {1: 3, 2: 4, 161: 5, 163: 6, 162: 7, 255: 8, 164: 9, 165: 10}
TR_OPEN, TR_CLOSE, TR_PAUSE, TR_RESUME = 1, 2, 3, 4
TR_ERROR_NAMES = {0: "OK", 1: "FAIL", 2: "NETWORK", 3: "NOT_SUPPORT", 4: "FALLBACK_FAIL", 66: "MAGIC_DUPLICATE"}
TR_TEXT_MAX_BYTES, TR_KEY_MAX_BYTES = 1024, 8
TR_HEARTBEAT_INTERVAL_S = 4.0


def tr_package(cmd: int, magic: int, inner: bytes) -> bytes:
    return varint_field(1, cmd) + varint_field(2, magic) + message_field(TR_FIELD[cmd], inner)


def tr_open(magic: int, src_lang: str, dst_lang: str, src_key: str | None = None, dst_key: str | None = None,
            lang_list: Sequence[tuple[str, str]] = ()) -> bytes:
    """CTRL OPEN ``{1: 1, 2: "SRC>DST", 5: {1 key <=8 B, 2 text}..., 6: srcKey, 7: dstKey}`` (translate.dart:260-270).
    Expect COMM_RESP ``{7: {1: err}}``; a FAIL means no session -- do not heart-beat."""
    src, dst = src_lang.strip().upper(), dst_lang.strip().upper()
    w = Writer().varint(1, TR_OPEN).string(2, f"{src}>{dst}")
    for key, text in lang_list:
        w.message(5, string_field(1, utf8_fit(key, TR_KEY_MAX_BYTES)) + string_field(2, text))
    w.string(6, utf8_fit(src_key.strip().upper() if src_key else src, TR_KEY_MAX_BYTES))
    w.string(7, utf8_fit(dst_key.strip().upper() if dst_key else dst, TR_KEY_MAX_BYTES))
    return tr_package(TR_CMD_CTRL, magic, w.to_bytes())


def tr_ctrl(magic: int, tcmd: int) -> bytes:
    return tr_package(TR_CMD_CTRL, magic, varint_field(1, tcmd))


def tr_close(magic: int) -> bytes:
    return tr_ctrl(magic, TR_CLOSE)


def tr_pause(magic: int) -> bytes:
    return tr_ctrl(magic, TR_PAUSE)


def tr_resume(magic: int) -> bytes:
    return tr_ctrl(magic, TR_RESUME)


def tr_text(magic: int, src_text: str, dst_text: str, final: bool = True, speaker: bytes | str | None = None) -> bytes:
    """RESULT ``{4: {1 srcText, 2 dstText (<=1024 B each), 4: 1 endFlag, 5: speaker}}`` (translate.dart:279-289)."""
    dst = dst_text if dst_text == " " else dst_text.strip()
    w = Writer().string(1, utf8_fit(src_text.strip(), TR_TEXT_MAX_BYTES)).string(2, utf8_fit(dst, TR_TEXT_MAX_BYTES))
    if final:
        w.varint(4, 1)
    if speaker:
        w.bytes(5, speaker.encode("utf-8") if isinstance(speaker, str) else bytes(speaker))
    return tr_package(TR_CMD_RESULT, magic, w.to_bytes())


def tr_heartbeat(magic: int) -> bytes:
    """``{255, m, 8: {}}`` every 4 s."""
    return tr_package(TR_CMD_HEARTBEAT, magic, b"")


def tr_lang_switch_response(magic: int, error: int = 0) -> bytes:
    """Answer a LANG_SWITCH_REQ with cmd 165 ``{10: {}}``."""
    return tr_package(TR_CMD_LANG_SWITCH_RSP, magic, varint_field(1, error) if error else b"")


@dataclass
class TranslateMessage:
    cmd: int
    magic: int
    value: int | None = None
    error: int | None = None
    lang_key: str | None = None

    @property
    def name(self) -> str:
        return TR_CMD_NAMES.get(self.cmd, f"cmd={self.cmd}")

    @property
    def is_close(self) -> bool:
        return self.cmd == TR_CMD_NOTIFY and self.value == TR_CLOSE


def tr_parse(payload: bytes) -> TranslateMessage:
    """Decode a translate message (translate.dart:296-327)."""
    m = TranslateMessage(pb.read_varint(payload, 1, -1), pb.read_varint(payload, 2, -1))
    fld = TR_FIELD.get(m.cmd)
    sub = pb.read_bytes(payload, fld) if fld is not None else None
    if sub is None:
        return m
    if m.cmd == TR_CMD_NOTIFY:
        m.value, m.error = pb.read_varint(sub, 1, 0), pb.read_varint(sub, 2, 0)
    elif m.cmd == TR_CMD_MODE_SWITCH:
        m.value = pb.read_varint(sub, 1, 0)
    elif m.cmd == TR_CMD_LANG_SWITCH_REQ:
        m.lang_key = pb.read_string(sub, 1)
    elif m.cmd in (TR_CMD_COMM_RESP, TR_CMD_LANG_SWITCH_RSP):
        m.error = pb.read_varint(sub, 1, 0)
    elif m.cmd == TR_CMD_CTRL:
        m.value, m.error = pb.read_varint(sub, 1, 0), pb.read_varint(sub, 4, 0)
    return m


# ═══════════════════════════════════════════════════════════════════════════════════════
# Navigation (SID 0x08) -- DECODED
# ═══════════════════════════════════════════════════════════════════════════════════════

SID_NAVIGATION = 0x08
NAV_CMD_HEARTBEAT, NAV_CMD_MENU_STARTUP, NAV_CMD_LOCATION_LIST, NAV_CMD_LOCATION_NONE = 0, 1, 2, 3
NAV_CMD_LOCATION_SELECTED, NAV_CMD_START, NAV_CMD_ERROR_INFO, NAV_CMD_BASIC_INFO = 4, 5, 6, 7
NAV_CMD_MINI_MAP, NAV_CMD_MAX_MAP, NAV_CMD_RECALCULATING, NAV_CMD_ARRIVE, NAV_CMD_STOP = 8, 9, 10, 11, 12
NAV_CMD_NOTIFY_EXIT, NAV_CMD_VIEW_CHANGED, NAV_CMD_COMPASS_CHANGED = 13, 14, 15
NAV_CMD_SWITCH_TRANSPORT_REQ, NAV_CMD_SWITCH_TRANSPORT_RSP, NAV_CMD_END_SESSION_REQ = 18, 19, 20
NAV_CMD_NAMES = {0: "HEARTBEAT", 1: "MENU_STARTUP", 2: "LOCATION_LIST", 3: "LOCATION_NONE", 4: "LOCATION_SELECTED", 5: "START",
                 6: "ERROR_INFO", 7: "BASIC_INFO", 8: "MINI_MAP", 9: "MAX_MAP", 10: "RECALCULATING", 11: "ARRIVE", 12: "STOP",
                 13: "NOTIFY_EXIT", 14: "VIEW_CHANGED", 15: "COMPASS_CHANGED", 16: "COMPASS_CALIBRATE_START",
                 17: "COMPASS_CALIBRATE_DONE", 18: "SWITCH_TRANSPORT_REQ", 19: "SWITCH_TRANSPORT_RSP", 20: "END_SESSION_REQ"}
NAV_F_EXT, NAV_F_LIST, NAV_F_INFO, NAV_F_SELECT, NAV_F_VIEW, NAV_F_COMPASS, NAV_F_TREQ, NAV_F_TRSP = 3, 4, 5, 8, 9, 10, 11, 12
_NAV_SUB = {2: (4, 4), 7: (5, 9), 4: (8, 2), 8: (6, 4), 9: (7, 7), 14: (9, None), 15: (10, None), 18: (11, None), 19: (12, None)}
NAV_METHOD_WALKING, NAV_METHOD_CYCLING = 1, 2
NAV_TEXT_MAX_BYTES = 60
NAV_MISSING = "--"
NAV_MAX_LOCATIONS = 20
NAV_HEARTBEAT_INTERVAL_S = 5.0
NAV_ERR_FAIL = 7
NAV_DIRECTION = {
    "DEPART": 1, "STRAIGHT": 2, "TURN_RIGHT": 3, "TURN_LEFT": 4, "TURN_SLIGHT_RIGHT": 5, "TURN_SLIGHT_LEFT": 6,
    "TURN_SHARP_RIGHT": 7, "TURN_SHARP_LEFT": 8, "UTURN_CLOCKWISE": 9, "UTURN_COUNTERCLOCKWISE": 10,
    "ROUNDABOUT_SHARP_RIGHT_CW": 11, "ROUNDABOUT_RIGHT_CCW": 12, "ROUNDABOUT_RIGHT_CW": 13, "ROUNDABOUT_SLIGHT_RIGHT_CCW": 14,
    "ROUNDABOUT_SLIGHT_RIGHT_CW": 15, "ROUNDABOUT_STRAIGHT_CCW": 16, "ROUNDABOUT_STRAIGHT_CW": 17,
    "ROUNDABOUT_SLIGHT_LEFT_CCW": 18, "ROUNDABOUT_SLIGHT_LEFT_CW": 19, "ROUNDABOUT_LEFT_CCW": 20, "ROUNDABOUT_LEFT_CW": 21,
    "ROUNDABOUT_SHARP_LEFT_CCW": 22, "ROUNDABOUT_SHARP_LEFT_CW": 23, "ROUNDABOUT_UTURN_CCW": 24, "ROUNDABOUT_UTURN_CW": 25,
    "ROUNDABOUT_CCW": 26, "ROUNDABOUT_CW": 27, "ROUNDABOUT_EXIT_CCW": 28, "ROUNDABOUT_EXIT_CW": 29, "MERGE_RIGHT": 30,
    "MERGE_LEFT": 31, "FORK_RIGHT": 32, "FORK_LEFT": 33, "MERGE_UNSPECIFIED": 34, "ROUNDABOUT_SHARP_RIGHT_CCW": 35,
}
_NAV_ALIASES = {"RIGHT": "TURN_RIGHT", "LEFT": "TURN_LEFT", "SLIGHT_RIGHT": "TURN_SLIGHT_RIGHT", "SLIGHT_LEFT": "TURN_SLIGHT_LEFT",
                "UTURN": "UTURN_COUNTERCLOCKWISE"}


def nav_package(cmd: int, magic: int, field: int, inner: bytes) -> bytes:
    return varint_field(1, cmd) + varint_field(2, magic) + message_field(field, inner)


def nav_text(text: str | None, limit: int = NAV_TEXT_MAX_BYTES) -> bytes:
    """<=limit UTF-8 bytes without splitting a character; None/blank -> ``--``."""
    if text is None or not str(text).strip():
        return NAV_MISSING.encode()
    return utf8_fit(str(text), limit).encode("utf-8")


def _nav_simple(cmd: int, magic: int) -> bytes:
    return nav_package(cmd, magic, NAV_F_EXT, b"")


def nav_start(magic: int) -> bytes:
    """START (5) ``{3: {}}``; then BASIC_INFO per update, HEARTBEAT every 5 s."""
    return _nav_simple(NAV_CMD_START, magic)


def nav_heartbeat(magic: int) -> bytes:
    return _nav_simple(NAV_CMD_HEARTBEAT, magic)


def nav_recalculating(magic: int) -> bytes:
    return _nav_simple(NAV_CMD_RECALCULATING, magic)


def nav_arrive(magic: int) -> bytes:
    return _nav_simple(NAV_CMD_ARRIVE, magic)


def nav_stop(magic: int) -> bytes:
    return _nav_simple(NAV_CMD_STOP, magic)


def nav_location_none(magic: int) -> bytes:
    return _nav_simple(NAV_CMD_LOCATION_NONE, magic)


def nav_direction_index(direction: int | str) -> int:
    if isinstance(direction, int):
        return direction
    key = direction.strip().upper().replace(" ", "_").replace("-", "_")
    v = NAV_DIRECTION.get(_NAV_ALIASES.get(key, key))
    if v is None:
        raise ValueError(f"unknown direction {direction!r}")
    return v


def nav_basic_info(magic: int, direction: int | str, distance: str | None = None, road: str | None = None,
                   remaining_time: str | None = None, remaining_distance: str | None = None, eta: str | None = None,
                   speed: str | None = None, method: int = NAV_METHOD_WALKING, counter: int = 0) -> bytes:
    """BASIC_INFO (7) ``{5: {1 dir 1..35, 2 distance, 3 road, 4 spendTime, 5 remainDistance, 6 eta ("ETA: " prefixed),
    7 speed, 8 method, 10 counter}}``; each text <=60 B, blank -> ``--`` (navigation.dart:447-466)."""
    if eta and eta.strip() and not eta.startswith("ETA"):
        eta = f"ETA: {eta}"
    w = Writer().varint(1, nav_direction_index(direction))
    for fld, text in ((2, distance), (3, road), (4, remaining_time), (5, remaining_distance), (6, eta), (7, speed)):
        w.bytes(fld, nav_text(text))
    if method:
        w.varint(8, method)
    if counter:
        w.varint(10, counter)
    return nav_package(NAV_CMD_BASIC_INFO, magic, NAV_F_INFO, w.to_bytes())


def nav_error_info(magic: int, message: str, index: int = 0, error: int = NAV_ERR_FAIL) -> bytes:
    inner = varint_field(1, error) + bytes_field(2, nav_text(message)) + (varint_field(3, index) if index else b"")
    return nav_package(NAV_CMD_ERROR_INFO, magic, NAV_F_EXT, inner)


def nav_location_list(magic: int, names: Sequence[str], icons: Sequence[int] = ()) -> bytes:
    """LOCATION_LIST (2) ``{4: {1 n, 2 names..., 3 packed icons}}`` (<=20) answering MENU_STARTUP."""
    if len(names) > NAV_MAX_LOCATIONS or len(icons) > NAV_MAX_LOCATIONS:
        raise ValueError(f"at most {NAV_MAX_LOCATIONS} locations")
    w = Writer().varint(1, len(names))
    for n in names:
        w.bytes(2, nav_text(n))
    if icons:
        w.bytes(3, b"".join(encode_varint(i) for i in icons))
    return nav_package(NAV_CMD_LOCATION_LIST, magic, NAV_F_LIST, w.to_bytes())


def nav_switch_transport_response(magic: int, status: int = 0) -> bytes:
    return nav_package(NAV_CMD_SWITCH_TRANSPORT_RSP, magic, NAV_F_TRSP, varint_field(1, status) if status else b"")


@dataclass
class NavigationMessage:
    cmd: int
    magic: int
    value: int | None = None
    location: str | None = None
    method: int | None = None
    error: int | None = None

    @property
    def name(self) -> str:
        return NAV_CMD_NAMES.get(self.cmd, f"cmd={self.cmd}")

    @property
    def is_exit(self) -> bool:
        return self.cmd in (NAV_CMD_NOTIFY_EXIT, NAV_CMD_END_SESSION_REQ)


def nav_parse(payload: bytes) -> NavigationMessage:
    """Decode a navigation message (navigation.dart:511-542)."""
    m = NavigationMessage(pb.read_varint(payload, 1, 0), pb.read_varint(payload, 2, -1))
    fld, err_fld = _NAV_SUB.get(m.cmd, (NAV_F_EXT, 1))
    sub = pb.read_bytes(payload, fld)
    if sub is None:
        return m
    if m.cmd == NAV_CMD_LOCATION_SELECTED:
        m.location, m.value, m.method = pb.read_string(sub, 1), pb.read_varint(sub, 3, 0), pb.read_varint(sub, 4, 0)
    elif m.cmd in (NAV_CMD_VIEW_CHANGED, NAV_CMD_COMPASS_CHANGED):
        m.value = pb.read_varint(sub, 1, 0)
    elif m.cmd == NAV_CMD_SWITCH_TRANSPORT_REQ:
        m.method = pb.read_varint(sub, 1, 0)
    if err_fld is not None:
        m.error = pb.read_varint(sub, err_fld, 0)
    return m
