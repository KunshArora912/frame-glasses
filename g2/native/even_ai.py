"""Even AI (SID 0x07) and the 0x6402 microphone packet layout.  Port of
``lib/protocol/even_ai.dart`` and ``mic_audio.dart``; community form from
``even-g2-protocol`` branch ``feature/even-ai`` (``examples/even-ai/even_ai.py``).

Session (LIVE in the hub; native-protocol.md section 5.4)::

    glasses -> CTRL WAKE_UP           "Hey Even" / menu
    app     -> CTRL ENTER             within a few seconds        enter(m)
    app     -> HEARTBEAT every 2 s    {9, m, 11: {1: count}}      heartbeat(m, n)
    glasses -> LC3 audio on 0x6402    parse_mic_packet()
    app     -> ASK {5: {4: text}}     300 ms                      ask(m, text)
    app     -> ANALYSE {6: {}}        "thinking"                  analyse(m)
    app     -> REPLY chunks <=512 B   150 ms apart, 6:1 on last   reply_chunks()/reply()
    glasses -> EVENT STREAM_COMPLETE
    either  -> CTRL EXIT                                          exit_(m)

Community minimal path (COMMUNITY, Jan 2026 firmware): ENTER -> 0.3 s -> ASK -> 1 s -> REPLY
with explicit zero fields 1/2/3 (cmdCnt/streamEnable/textMode), no heartbeat, no ANALYSE, no end
flag; pass ``community=True`` to :func:`ask` / :func:`reply`.  Expect the screen to drop after a
few seconds without the heartbeat.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from . import pb
from .pb import message_field, varint_field

SID_EVEN_AI = 0x07
CHAR_MIC_NOTIFY = 0x6402  # service 0x6450

CMD_CTRL, CMD_VAD_INFO, CMD_ASK, CMD_ANALYSE, CMD_REPLY, CMD_SKILL = 1, 2, 3, 4, 5, 6
CMD_PROMPT, CMD_EVENT, CMD_HEARTBEAT, CMD_CONFIG, CMD_COMM_RSP = 7, 8, 9, 10, 0xA1
CMD_NAMES = {1: "CTRL", 2: "VAD_INFO", 3: "ASK", 4: "ANALYSE", 5: "REPLY", 6: "SKILL", 7: "PROMPT", 8: "EVENT",
             9: "HEARTBEAT", 10: "CONFIG", 0xA1: "COMM_RSP"}
FIELD_FOR_CMD = {1: 3, 2: 4, 3: 5, 4: 6, 5: 7, 6: 8, 7: 9, 8: 10, 9: 11, 0xA1: 12, 10: 13}

CTRL_WAKE_UP, CTRL_ENTER, CTRL_EXIT = 1, 2, 3
CTRL_NAMES = {1: "WAKE_UP", 2: "ENTER", 3: "EXIT"}
VAD_START, VAD_END, VAD_TIMEOUT = 1, 2, 3
VAD_NAMES = {1: "VAD_START", 2: "VAD_END", 3: "VAD_TIMEOUT"}
EVENT_SCROLL, EVENT_STREAM_COMPLETE = 1, 2
EVENT_NAMES = {1: "SCROLL", 2: "STREAM_COMPLETE"}
PROMPT_NAMES = {1: "NETWORK_ERR", 2: "BLE_DISCONNECT", 3: "SERVER_ERR", 4: "TROUBLE_UNDERSTAND", 5: "COMMAND_UNSUPPORT",
                6: "AUDIO_ERROR", 7: "ASR_SERVER_ERR", 8: "AI_SERVER_ERR", 9: "COMMAND_EXE_FAIL"}

REPLY_MAX_BYTES = 512
REPLY_PACE_MS = 150
HEARTBEAT_INTERVAL_S = 2.0
DEFAULT_STREAM_SPEED = 80


def package(cmd: int, magic: int, inner: bytes) -> bytes:
    """EvenAIDataPackage ``{1 cmd, 2 magic, (field for cmd): inner}`` (even_ai.dart:603-604)."""
    return varint_field(1, cmd) + varint_field(2, magic) + message_field(FIELD_FOR_CMD[cmd], inner)


def ctrl(magic: int, status: int) -> bytes:
    return package(CMD_CTRL, magic, varint_field(1, status))


def enter(magic: int) -> bytes:
    """CTRL ENTER ``{1:1, 2:m, 3:{1: 2}}`` -> ``08011081011a020802`` (magic 129). LIVE + COMMUNITY
    (community frame ``aa21080a01010720080110641a0208025ca9``, magic 100, seq 8)."""
    return ctrl(magic, CTRL_ENTER)


def exit_(magic: int) -> bytes:
    """CTRL EXIT ``{3: {1: 3}}``; stop the heartbeat afterwards."""
    return ctrl(magic, CTRL_EXIT)


def heartbeat(magic: int, count: int) -> bytes:
    """``{1: 9, 2: m, 11: {1: count}}`` every 2 s while the session is open (LIVE)."""
    return package(CMD_HEARTBEAT, magic, varint_field(1, count))


def config(magic: int, voice_switch: int = 0, stream_speed: int = DEFAULT_STREAM_SPEED) -> bytes:
    """``{1: 10, 2: m, 13: {1 voiceSwitch, 2 streamSpeed}}``; the captured connect sequence sends
    ``{13:{1:0,2:80,4:0}}`` -- see :func:`config_captured` (device.dart:185)."""
    return package(CMD_CONFIG, magic, varint_field(1, voice_switch) + varint_field(2, stream_speed))


def config_captured(magic: int) -> bytes:
    """The exact CONFIG of the captured app connect sequence: ``{13: {1: 0, 2: 80, 4: 0}}`` (LIVE captured)."""
    return package(CMD_CONFIG, magic, varint_field(1, 0) + varint_field(2, 80) + varint_field(4, 0))


def _text_info(text: bytes, final: bool = False, community: bool = False) -> bytes:
    pre = (varint_field(1, 0) + varint_field(2, 0) + varint_field(3, 0)) if community else b""
    inner = pre + message_field(4, text)
    return inner + varint_field(6, 1) if final else inner


def ask(magic: int, text: str, community: bool = False) -> bytes:
    """ASK ``{1: 3, 2: m, 5: {4: bytes text}}`` (LIVE). ``community=True`` adds the explicit zero fields
    1/2/3 of the community capture ``aa21091e01010720080310652a16080010001800220e...`` (ASK "What is 2 + 2?")."""
    return package(CMD_ASK, magic, _text_info(text.encode("utf-8"), community=community))


def analyse(magic: int) -> bytes:
    """ANALYSE ``{1: 4, 2: m, 6: {}}`` -- the "thinking" screen (LIVE)."""
    return package(CMD_ANALYSE, magic, b"")


def reply(magic: int, chunk: bytes | str, final: bool, community: bool = False) -> bytes:
    """One REPLY chunk ``{1: 5, 2: m, 7: {4: chunk[, 6: 1]}}``; ``chunk`` <= 512 UTF-8 bytes (raises above).
    ``final`` adds ``6: 1`` on the last chunk (LIVE).  ``community=True`` writes the zero fields and
    never the end flag (community capture ``aa210a2001010720080510663a18080010001800221054686520616e...``)."""
    b = chunk.encode("utf-8") if isinstance(chunk, str) else bytes(chunk)
    if len(b) > REPLY_MAX_BYTES:
        raise ValueError(f"reply chunk is {len(b)} bytes; limit {REPLY_MAX_BYTES}")
    return package(CMD_REPLY, magic, _text_info(b, final=final and not community, community=community))


def reply_chunks(text: str, limit: int = REPLY_MAX_BYTES) -> list[bytes]:
    """Split into <=limit-byte UTF-8 chunks without cutting a character; always >= 1 chunk (even_ai.dart:628-642)."""
    limit = max(1, min(limit, REPLY_MAX_BYTES))
    chunks: list[bytes] = []
    cur = bytearray()
    for ch in text:
        b = ch.encode("utf-8")
        if cur and len(cur) + len(b) > limit:
            chunks.append(bytes(cur))
            cur = bytearray()
        cur += b
    chunks.append(bytes(cur))
    return chunks


def reply_sequence(magic_fn, text: str) -> list[bytes]:
    """All REPLY payloads for ``text`` (send 150 ms apart); the last carries the end flag."""
    chunks = reply_chunks(text)
    return [reply(magic_fn(), c, final=(i == len(chunks) - 1)) for i, c in enumerate(chunks)]


def vad_info(magic: int, status: int) -> bytes:
    """``{1: 2, 2: m, 4: {1: status}}`` (glasses-side normally; for fakes/tests)."""
    return package(CMD_VAD_INFO, magic, varint_field(1, status))


def prompt(magic: int, code: int) -> bytes:
    """PROMPT ``{1: 7, 2: m, 9: {1: code}}`` 1 NETWORK_ERR .. 9 COMMAND_EXE_FAIL (DECODED)."""
    return package(CMD_PROMPT, magic, varint_field(1, code))


def event(magic: int, event_id: int) -> bytes:
    """EVENT ``{1: 8, 2: m, 10: {1: id}}`` (glasses-side; for fakes)."""
    return package(CMD_EVENT, magic, varint_field(1, event_id))


@dataclass
class EvenAIMessage:
    cmd: int
    magic: int
    value: int | None = None  # status / event / vad / prompt code
    error: int | None = None
    text: bytes | None = None  # field 4 of ASK/REPLY when echoed

    @property
    def name(self) -> str:
        return CMD_NAMES.get(self.cmd, f"cmd={self.cmd}")

    @property
    def value_name(self) -> str | None:
        names = {CMD_CTRL: CTRL_NAMES, CMD_VAD_INFO: VAD_NAMES, CMD_EVENT: EVENT_NAMES, CMD_PROMPT: PROMPT_NAMES}.get(self.cmd)
        return None if names is None or self.value is None else names.get(self.value, str(self.value))

    @property
    def is_wake_up(self) -> bool:
        return self.cmd == CMD_CTRL and self.value == CTRL_WAKE_UP

    @property
    def is_exit(self) -> bool:
        return self.cmd == CMD_CTRL and self.value == CTRL_EXIT

    @property
    def is_stream_complete(self) -> bool:
        return self.cmd == CMD_EVENT and self.value == EVENT_STREAM_COMPLETE


def parse(payload: bytes) -> EvenAIMessage:
    """Decode any Even AI message (even_ai.dart:645-671)."""
    m = EvenAIMessage(pb.read_varint(payload, 1, -1), pb.read_varint(payload, 2, -1))
    fld = FIELD_FOR_CMD.get(m.cmd)
    sub = pb.read_bytes(payload, fld) if fld is not None else None
    if sub is not None:
        if m.cmd == CMD_COMM_RSP:
            m.error = pb.read_varint(sub, 1, 0)
        else:
            m.value = pb.read_varint(sub, 1, 0)
            m.error = pb.read_varint(sub, 2, 0)
        if m.cmd in (CMD_ASK, CMD_REPLY):
            m.text = pb.read_bytes(sub, 4)
    return m


# ── microphone packets (mic_audio.dart) ────────────────────────────────────────────────

MIC_SAMPLE_RATE = 16000
MIC_FRAME_BYTES = 40
MIC_FRAMES_PER_PACKET = 5
MIC_AUDIO_BYTES = MIC_FRAME_BYTES * MIC_FRAMES_PER_PACKET  # 200
MIC_PACKET_BYTES = MIC_AUDIO_BYTES + 5  # 205
MIC_SAMPLES_PER_FRAME = 160  # 10 ms at 16 kHz


@dataclass(frozen=True)
class MicPacket:
    frames: tuple[bytes, ...]  # five 40-byte LC3 frames (10 ms, 16 kHz mono, 32 kbps)
    signal_ratio: int | None  # int16 LE at 200..201
    direction: int | None  # int16 LE direction of arrival (degrees) at 202..203
    counter: int | None  # byte 204, wraps at 256

    @property
    def lc3(self) -> bytes:
        return b"".join(self.frames)


def parse_mic_packet(data: bytes) -> MicPacket | None:
    """Raw notify on 0x6402 (no AA frame, no CRC): bytes 0..199 five 40-byte LC3 frames,
    200..201 int16 LE signal ratio, 202..203 int16 LE direction, 204 counter (LIVE, 386 captured
    packets).  None when shorter than 200 bytes; trailer fields None when shorter than 205."""
    data = bytes(data)
    if len(data) < MIC_AUDIO_BYTES:
        return None
    frames = tuple(data[i * MIC_FRAME_BYTES:(i + 1) * MIC_FRAME_BYTES] for i in range(MIC_FRAMES_PER_PACKET))
    if len(data) < MIC_PACKET_BYTES:
        return MicPacket(frames, None, None, None)
    ratio, direction = struct.unpack_from("<hh", data, MIC_AUDIO_BYTES)
    return MicPacket(frames, ratio, direction, data[MIC_AUDIO_BYTES + 4])


def evenhub_audio_control(magic: int, enable: bool) -> bytes:
    """Alternative mic enable without an Even AI session: SID 0xE0 cmd 15 ``{18: {1: on}}`` (LIVE, hub strategy 1;
    evenhub.dart:496-497).  Send on SID 0xE0."""
    return varint_field(1, 15) + varint_field(2, magic) + message_field(18, varint_field(1, 1 if enable else 0))
