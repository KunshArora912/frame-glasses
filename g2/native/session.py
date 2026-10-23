"""Session prelude, keep-alive and the minimal offline dashboard plan (native-protocol.md
sections 1 and 8).  Port of ``lib/protocol/device.dart``, ``app_sync.dart``,
``evenhub.dart:480-497`` and ``lib/hub/glasses_session.dart:157-284``.

Everything here is pure: builders return protobuf payloads (or ready frames where the doc
gives a fixed frame), parsers take payloads.  :class:`MinimalDashboardPlan` lays the 18 steps
out as data so any transport can execute them (``transport.Glasses.run_plan``).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field as dc_field
from typing import Callable, Sequence

from . import dashboard, notifications, pb, settings
from .framing import FLAG_PLAIN, FLAG_REQUEST, auth_frame, frame_pb
from .pb import encode_key, encode_varint, message_field, varint_field

SID_DASHBOARD = 0x01
SID_NOTIFICATION = 0x04
SID_EVEN_AI = 0x07
SID_SETTINGS = 0x09
SID_QUICKLIST = 0x0C
SID_APP_SYNC = 0x0D
SID_ONBOARDING = 0x10
SID_WATCHFACE = 0x1F
SID_DEVICE = 0x80
SID_CASE = 0x81
SID_EVENHUB = 0xE0

# SID 0x80 commands (device.dart:17-31)
CMD_AUTH, CMD_ROLE_CHANGE, CMD_UNPAIR, CMD_EXCEPTION, CMD_SET_INFO, CMD_GET_INFO = 4, 5, 9, 10, 11, 12
CMD_FACTORY_RESET, CMD_KEEPALIVE, CMD_QUICK_RESTART, CMD_TIME_SYNC, CMD_AUD_CONTROL, CMD_ERROR = 13, 14, 15, 128, 129, 255
DEV_CMD_NAMES = {4: "AUTHENTICATION", 5: "PIPE_ROLE_CHANGE", 6: "RING_CONNECT_INFO", 7: "BLE_CONNECT_PARAM", 8: "DISCONNECT_INFO",
                 9: "UNPAIR_INFO", 10: "COMMAND_EXCEPTION", 11: "SET_DEVICE_INFO", 12: "GET_DEVICE_INFO",
                 13: "RESTORE_TO_FACTORY_SETTINGS", 14: "BASE_CONNECT_HEART_BEAT", 15: "QUICK_RESTART", 128: "TIME_SYNC",
                 129: "AUD_CONTROL", 255: "COMMAND_ERROR"}
DEV_FIELD = {4: 3, 5: 4, 6: 5, 7: 6, 8: 7, 9: 8, 10: 9, 11: 10, 12: 11, 13: 12, 14: 13, 15: 14, 128: 128, 129: 129}
_DEV_RESULT_FIELD = {4: 3, 5: 2, 6: 5, 7: 5, 8: 3, 9: 3, 10: 2, 11: 2, 12: 2, 13: 1, 14: 1, 15: 1, 128: 3, 129: 3}
DEV_ERROR_NAMES = {0: "SUCCESS", 1: "CRC", 2: "PKT_LOST", 3: "TIMEOUT", 4: "NO_RESOURCES", 5: "PB_ERROR", 6: "NULL", 7: "FAIL",
                   8: "NOT_SUPPORT", 9: "SUPPORT", 10: "HEAD_ID", 11: "INVALID_LENGTH", 12: "INVALID_SDID", 13: "DUPLICATE_PACKET",
                   90: "RING_CONNECT_TIMEOUT"}
ROLE_BOTH, ROLE_RIGHT, ROLE_LEFT = 0, 1, 2
PHONE_IOS, PHONE_ANDROID = 3, 4
TZ_UNIT_MINUTES, TZ_MIN, TZ_MAX = 15, -128, 127

# onboarding (device.dart:86-90)
OB_CMD_CONFIG, OB_CMD_HEARTBEAT, OB_CMD_EVENT = 1, 2, 3
OB_START_UP, OB_TOUCH, OB_HEAD_UP, OB_FINISH = 1, 2, 3, 4
OB_EVENT_WEAR_STATUS = 1
CASE_CMD_INFO = 1

# EvenHub (evenhub.dart:350-353, 492-494)
PRELUDE_MAGIC = 156
PRELUDE_SEQ = 0x92
EVENHUB_HEARTBEAT_CMD = 12

# timing (glasses_session.dart:88-94, device.dart:81-82)
HEARTBEAT_INTERVAL_S = 4.0
PACKET_GAP_S = 0.05
CONNECT_SEQUENCE_GAP_S = 0.15
PRELUDE_DRAIN_S = 1.5
SETTLE_S = 0.3
KEEPALIVE_INTERVAL_S = 60.0
KEEPALIVE_LEFT_LEAD_S = 2.0
KEEPALIVE_ECHO_WAIT_S = 5.0
TIME_SYNC_INTERVAL_S = 3600.0
DASHBOARD_SETTLE_S = 5.0  # the native dashboard retakes the screen ~5 s after the heartbeat stops

LEFT, RIGHT, BOTH = "left", "right", "both"


# ── flag rule (device.dart:93-100) ──────────────────────────────────────────────────────


def flag_for(sid: int, payload: bytes) -> int:
    """0x00 only for 0x80 cmd 4 (auth) and cmd 14 (keep-alive); 0x20 for everything else."""
    if sid == SID_DEVICE and pb.read_varint(payload, 1, -1) in (CMD_AUTH, CMD_KEEPALIVE):
        return FLAG_PLAIN
    return FLAG_REQUEST


# ── SID 0x80 builders ──────────────────────────────────────────────────────────────────


def device_package(cmd: int, magic: int, inner: bytes) -> bytes:
    return varint_field(1, cmd) + varint_field(2, magic) + message_field(DEV_FIELD[cmd], inner)


def auth_payload(magic: int, phone_type: int = PHONE_ANDROID) -> bytes:
    """``{1: 4, 2: magic, 3: {1: 1, 2: 4}}`` -- the frame form is :func:`framing.auth_frame` (flag 0x00)."""
    return device_package(CMD_AUTH, magic, varint_field(1, 1) + varint_field(2, phone_type))


def role_change(magic: int, role: int = ROLE_RIGHT) -> bytes:
    """``{1: 5, 2: m, 4: {1: role}}`` 1 RIGHT / 2 LEFT -> ``0805106922020801`` (LIVE captured)."""
    if role not in (ROLE_BOTH, ROLE_RIGHT, ROLE_LEFT):
        raise ValueError(f"role must be one of [0, 1, 2], got {role!r}")
    return device_package(CMD_ROLE_CHANGE, magic, varint_field(1, role))


def tz_units_from_offset(offset_seconds: int) -> int:
    """UTC offset in seconds -> 15-minute units, truncated toward zero, clamped -128..127 (device.dart:114-117)."""
    q = int(offset_seconds / (TZ_UNIT_MINUTES * 60))
    return max(TZ_MIN, min(TZ_MAX, q))


def local_tz_units(now: float | None = None) -> int:
    t = time.time() if now is None else now
    lt = time.localtime(t)
    return tz_units_from_offset(lt.tm_gmtoff)


def time_sync(magic: int, unix_s: int | None = None, tz_units: int | None = None) -> bytes:
    """``{1: 128, 2: m, 128: {1: unix_s (uint32), 2: tz int32 15-min units, negative as 10-byte varint}}``
    (LIVE captured; device.dart:125-132).  Example UTC-4 (tz -16): ``088001 1068 8208 11 08 c0afddd606 10 f0ffffffffffffffff01``.
    Re-send hourly."""
    secs = int(time.time()) if unix_s is None else int(unix_s)
    if not 0 <= secs <= 0xFFFFFFFF:
        raise ValueError(f"unix seconds out of uint32 range: {secs}")
    tz = local_tz_units(secs) if tz_units is None else tz_units
    if not TZ_MIN <= tz <= TZ_MAX:
        raise ValueError(f"tz_units must be in {TZ_MIN}..{TZ_MAX}, got {tz}")
    inner = varint_field(1, secs) + encode_key(2, 0) + encode_varint(tz)
    return device_package(CMD_TIME_SYNC, magic, inner)


def keepalive(magic: int) -> bytes:
    """``{1: 14, 2: m, 13: {}}`` = ``08 0e 10 m 6a 00``; flag 0x00; every 60 s LEFT first, RIGHT 2 s later,
    each with its own magic; the right lens echoes it (LIVE captured)."""
    return device_package(CMD_KEEPALIVE, magic, b"")


def keepalive_frame(magic: int, seq: int) -> bytes:
    return frame_pb(keepalive(magic), SID_DEVICE, FLAG_PLAIN, seq)[0]


# ── SID 0x01 / 0xE0 prelude ────────────────────────────────────────────────────────────


def prelude_payload() -> bytes:
    """``{1: 2, 2: 156, 4: {3: {2: {2: {1: 0, 2: 0}}}}}`` -- an empty schedule widget with fixed magic 156
    (evenhub.dart:481-485)."""
    f4 = message_field(3, message_field(2, message_field(2, varint_field(1, 0) + varint_field(2, 0))))
    return varint_field(1, 2) + varint_field(2, PRELUDE_MAGIC) + message_field(4, f4)


PRELUDE_PAYLOAD = prelude_payload()
#: the ready-made prelude frame (SID 0x01, flag 0x20, fixed seq 0x92); then wait 1.5 s (LIVE)
PRELUDE_FRAME = frame_pb(PRELUDE_PAYLOAD, SID_DASHBOARD, FLAG_REQUEST, PRELUDE_SEQ)[0]


def evenhub_heartbeat(magic: int) -> bytes:
    """``{1: 12, 2: m, 14: {1: 0}}`` -> ``080c10<m>72020800`` on SID 0xE0 every 4 s, fire-and-forget (LIVE).
    SID 0x01 writes are only acked while it runs; STOPPING it lets the native dashboard retake the screen."""
    return varint_field(1, EVENHUB_HEARTBEAT_CMD) + varint_field(2, magic) + message_field(14, varint_field(1, 0))


def app_sync_subscribe(magic: int) -> bytes:
    """SID 0x0D ``{1: 0, 2: m}`` -> ``08001072`` (LIVE); the glasses then push ``{3: {1 bg, 2 fg}}``."""
    return varint_field(1, 0) + varint_field(2, magic)


# ── SID 0x10 / 0x81 ────────────────────────────────────────────────────────────────────


def onboarding_config(magic: int, process_id: int = OB_FINISH) -> bytes:
    if process_id not in (OB_START_UP, OB_TOUCH, OB_HEAD_UP, OB_FINISH):
        raise ValueError(f"process_id must be one of [1, 2, 3, 4], got {process_id!r}")
    return varint_field(1, OB_CMD_CONFIG) + varint_field(2, magic) + message_field(3, varint_field(1, process_id))


def onboarding_finish(magic: int) -> bytes:
    """SID 0x10 ``{1: 1, 2: m, 3: {1: 4}}`` -> ``0801 10m 1a02 0804`` (LIVE captured)."""
    return onboarding_config(magic, OB_FINISH)


def wear_query(magic: int) -> bytes:
    """SID 0x10 ``{1: 3, 2: m, 5: {1: 1}}`` -> ``0803 10m 2a02 0801``; reply ``5: {1: 1, 2: worn}`` (LIVE)."""
    return varint_field(1, OB_CMD_EVENT) + varint_field(2, magic) + message_field(5, varint_field(1, OB_EVENT_WEAR_STATUS))


def case_query(magic: int) -> bytes:
    """SID 0x81 ``{1: 1, 2: m, 3: {}}`` -> ``0801 10m 1a00``; reply ``3: {1 battery, 2 charging, 3 lid, 4 in_case, 5 err}`` (LIVE)."""
    return varint_field(1, CASE_CMD_INFO) + varint_field(2, magic) + message_field(3, b"")


# re-exports so a caller can build the whole prelude from this module
dashboard_init = dashboard.dashboard_init
settings_read = settings.read_settings
units = settings.units
gestures_default = settings.gestures
notification_ctrl = notifications.notification_ctrl
whitelist_off = notifications.whitelist_disable


# ── captured connect sequence (device.dart:176-208) ────────────────────────────────────


@dataclass
class Step:
    """One step of a plan.  ``action`` is 'send', 'start_heartbeat' or 'stop_heartbeat'.
    For 'send': ``frames`` are written in order to ``side`` (50 ms apart if several), then the
    executor waits for ``expect`` (None, 'auth', 'ack', 'echo', 'snapshot', 'reply') up to
    ``timeout_ms`` and sleeps ``wait_ms``."""
    label: str
    action: str = "send"
    side: str = RIGHT
    sid: int | None = None
    payload: bytes | None = None
    flag: int = FLAG_REQUEST
    magic: int | None = None
    frames: list[bytes] = dc_field(default_factory=list)
    wait_ms: int = 0
    expect: str | None = None
    timeout_ms: int = 3000
    number: int = 0

    @property
    def frame(self) -> bytes | None:
        return self.frames[0] if self.frames else None

    def as_tuple(self) -> tuple[str, bytes | None, int, str | None]:
        """(side, frame bytes, wait_ms, expect) -- the shape named in the task description."""
        return self.side, b"".join(self.frames) if self.frames else None, self.wait_ms, self.expect


def connect_sequence(magic_fn: Callable[[], int], now: int | None = None, tz_units: int | None = None,
                     full: bool = False, notifications_enable: bool = True, disp_time: int = 5) -> list[tuple[int, bytes, str]]:
    """The official app's post-auth housekeeping, ``[(sid, payload, side)]``, 150 ms apart, all RIGHT
    (device.dart:178-202 + glasses_session.dart:229-242 which appends the notification CTRL and
    whitelist-off).  ``full`` inserts the other captured fixed-payload steps (Even AI config, empty
    quicklist, 0x0D, watchface info, the 0x04 ctrl at its captured position)."""
    from . import even_ai
    seq: list[tuple[int, bytes, str]] = []

    def add(sid: int, payload: bytes) -> None:
        seq.append((sid, payload, RIGHT))

    add(SID_DEVICE, role_change(magic_fn(), ROLE_RIGHT))
    add(SID_DEVICE, time_sync(magic_fn(), now, tz_units))
    if full:
        add(SID_EVEN_AI, even_ai.config_captured(magic_fn()))
        add(SID_QUICKLIST, dashboard.quicklist_empty(magic_fn()))
        add(SID_APP_SYNC, app_sync_subscribe(magic_fn()))
    add(SID_SETTINGS, settings.read_settings(magic_fn()))
    add(SID_SETTINGS, settings.units(magic_fn()))
    add(SID_ONBOARDING, onboarding_finish(magic_fn()))
    add(SID_ONBOARDING, wear_query(magic_fn()))
    if full:
        add(SID_WATCHFACE, dashboard.watchface_info_request(magic_fn()))
    add(SID_CASE, case_query(magic_fn()))
    if full:
        add(SID_NOTIFICATION, varint_field(1, 1) + varint_field(2, magic_fn())
            + message_field(3, varint_field(1, 1) + varint_field(2, 0) + varint_field(3, 5) + varint_field(5, 0)))
    add(SID_SETTINGS, settings.gestures(magic_fn()))
    if notifications_enable:
        add(SID_NOTIFICATION, notifications.notification_ctrl(magic_fn(), disp_time=disp_time))
        add(SID_NOTIFICATION, notifications.whitelist_disable(magic_fn()))
    return seq


def keepalive_steps(magic_fn: Callable[[], int]) -> list[tuple[int, bytes, str]]:
    """One keep-alive round: LEFT first, then RIGHT (2 s later), each with its own magic."""
    return [(SID_DEVICE, keepalive(magic_fn()), LEFT), (SID_DEVICE, keepalive(magic_fn()), RIGHT)]


# ── parsers ────────────────────────────────────────────────────────────────────────────


def parse_auth_reply(payload: bytes) -> bool:
    """The hub's check: >= 6 bytes and ending ``1a 00`` (glasses_session.dart:181-183)."""
    return len(payload) >= 6 and payload[-2:] == b"\x1a\x00"


@dataclass
class DeviceMessage:
    cmd: int
    magic: int
    result: int = 0
    authenticated: bool | None = None
    timestamp: int | None = None
    timezone: int | None = None
    role: int | None = None
    failed_cmd: int | None = None
    info: list[dict] | None = None

    @property
    def name(self) -> str:
        return DEV_CMD_NAMES.get(self.cmd, f"cmd={self.cmd}")

    @property
    def is_keepalive_echo(self) -> bool:
        return self.cmd == CMD_KEEPALIVE


def parse_device(payload: bytes) -> DeviceMessage:
    """Decode a SID 0x80 message (device.dart:250-279)."""
    m = DeviceMessage(pb.read_varint(payload, 1, -1), pb.read_varint(payload, 2, -1))
    fld = DEV_FIELD.get(m.cmd)
    sub = pb.read_bytes(payload, fld) if fld is not None else None
    if sub is not None:
        rf = _DEV_RESULT_FIELD.get(m.cmd)
        if rf is not None:
            m.result = pb.read_varint(sub, rf, 0)
        if m.cmd == CMD_AUTH:
            m.authenticated = pb.read_varint(sub, 1, 0) == 1
        elif m.cmd == CMD_TIME_SYNC and sub:
            m.timestamp = pb.read_varint(sub, 1, 0)
            m.timezone = pb.read_signed(sub, 2, 0)
        elif m.cmd == CMD_ROLE_CHANGE and sub:
            m.role = pb.read_varint(sub, 1, 0)
        elif m.cmd in (CMD_GET_INFO, CMD_SET_INFO):
            m.info = settings.parse_device_info(payload)
        elif m.cmd == CMD_EXCEPTION:
            m.failed_cmd = pb.read_varint(sub, 1, 0)
    return m


@dataclass
class OnboardingMessage:
    cmd: int
    magic: int
    process_id: int | None = None
    event: int | None = None
    event_param: int | None = None
    error: int = 0

    @property
    def worn(self) -> bool | None:
        return None if self.event != OB_EVENT_WEAR_STATUS else self.event_param == 1


def parse_onboarding(payload: bytes) -> OnboardingMessage:
    """Decode a SID 0x10 message (device.dart:282-318)."""
    m = OnboardingMessage(pb.read_varint(payload, 1, -1), pb.read_varint(payload, 2, -1))
    cfg = pb.read_bytes(payload, 3)
    if cfg is not None:
        m.process_id, m.error = pb.read_varint(cfg, 1, 0), pb.read_varint(cfg, 2, 0)
    hb = pb.read_bytes(payload, 4)
    if hb is not None:
        m.error = pb.read_varint(hb, 1, 0)
    ev = pb.read_bytes(payload, 5)
    if ev is not None:
        m.event, m.event_param, m.error = pb.read_varint(ev, 1, 0), pb.read_varint(ev, 2, 0), pb.read_varint(ev, 3, 0)
    return m


def parse_wear_reply(payload: bytes) -> bool | None:
    return parse_onboarding(payload).worn


@dataclass
class CaseInfo:
    magic: int
    battery: int | None
    charging: int | None
    lid: int | None
    in_case: int | None
    error: int


def parse_case(payload: bytes) -> CaseInfo | None:
    """SID 0x81 reply/push ``{3: {1 battery, 2 charging, 3 lid, 4 in_case, 5 err}}`` or None (device.dart:321-337)."""
    info = pb.read_bytes(payload, 3)
    if info is None:
        return None

    def opt(f: int) -> int | None:
        v = pb.read_varint(info, f, -1)
        return None if v < 0 else v

    return CaseInfo(pb.read_varint(payload, 2, -1), opt(1), opt(2), opt(3), opt(4), pb.read_varint(info, 5, 0))


@dataclass
class AppSync:
    cmd: int
    magic: int
    background: int | None = None
    foreground: int | None = None


def parse_app_sync(payload: bytes) -> AppSync:
    """SID 0x0D push ``{3: {1 backgroundSID, 2 foregroundSID}}`` (app_sync.dart:724-748)."""
    m = AppSync(pb.read_varint(payload, 1, -1), pb.read_varint(payload, 2, -1))
    st = pb.read_bytes(payload, 3)
    if st is not None:
        m.background, m.foreground = pb.read_varint(st, 1, 0), pb.read_varint(st, 2, 0)
    return m


parse_dashboard_ack = dashboard.ack_flag
parse_settings = settings.parse
parse_settings_ack = settings.parse_ack


def parse_snapshot(payload: bytes) -> settings.Snapshot | None:
    """The 0x09 readback (field 4) of a settings READ reply or unsolicited push, else None."""
    return settings.parse(payload).snapshot


# ── the minimal offline dashboard plan (native-protocol.md section 8) ──────────────────


@dataclass
class MinimalDashboardPlan:
    """Steps 1-18 of native-protocol.md section 8 as data.

    ``alloc`` supplies magics/seqs (``framing.MagicAllocator``); ``now`` unix seconds and
    ``tz_units`` for the time sync (default: this machine's clock/zone); weather values for step
    17.  ``steps()`` returns :class:`Step` objects; ``as_tuples()`` the bare
    ``(side, frame, wait_ms, expect)`` list.  Rebuild the plan for every connection (magics
    must not repeat).
    """
    alloc: object  # MagicAllocator-like: next_magic(), next_seq()
    now: int | None = None
    tz_units: int | None = None
    temperature_c: float = 21.0
    weather_type: int = 1
    weather_status: str = "Sunny"
    temperature_unit: int = dashboard.TEMP_CELSIUS
    time_format_12h: bool = True
    disp_time: int = 5
    auth_magic_start: int = 1

    def _frames(self, sid: int, payload: bytes, flag: int | None = None) -> tuple[list[bytes], int]:
        f = flag_for(sid, payload) if flag is None else flag
        return frame_pb(payload, sid, f, self.alloc.next_seq()), f

    def _send(self, number: int, label: str, sid: int, payload: bytes, magic: int | None, side: str = RIGHT,
              wait_ms: int = 150, expect: str | None = None, timeout_ms: int = 3000, flag: int | None = None) -> Step:
        frames, f = self._frames(sid, payload, flag)
        return Step(label, "send", side, sid, payload, f, magic, frames, wait_ms, expect, timeout_ms, number)

    def steps(self) -> list[Step]:
        a = self.alloc
        now = int(time.time()) if self.now is None else int(self.now)
        tz = local_tz_units(now) if self.tz_units is None else self.tz_units
        s: list[Step] = []
        # 1-2 auth: right (wait <=5 s for a reply ending 1a 00), left fire-and-forget, 300 ms settle
        m = self.auth_magic_start
        s.append(Step("auth R", "send", RIGHT, SID_DEVICE, auth_payload(m), FLAG_PLAIN, m, [auth_frame(m)], 0, "auth", 5000, 1))
        m += 1
        s.append(Step("auth L", "send", LEFT, SID_DEVICE, auth_payload(m), FLAG_PLAIN, m, [auth_frame(m)], 300, None, 0, 2))
        # 3 prelude (fixed frame), 1.5 s drain
        s.append(Step("prelude", "send", RIGHT, SID_DASHBOARD, PRELUDE_PAYLOAD, FLAG_REQUEST, PRELUDE_MAGIC, [PRELUDE_FRAME], 1500, None, 0, 3))
        # 4 start the 0xE0 heartbeat (every 4 s)
        s.append(Step("start EvenHub heartbeat (4 s)", "start_heartbeat", RIGHT, SID_EVENHUB, number=4))
        # 5 prelude again
        s.append(Step("prelude again", "send", RIGHT, SID_DASHBOARD, PRELUDE_PAYLOAD, FLAG_REQUEST, PRELUDE_MAGIC, [PRELUDE_FRAME], 1500, None, 0, 5))
        # 6 dashboard init, wait ack flag 0 (<=4 s)
        mi = a.next_magic()
        s.append(self._send(6, "dashboard init", SID_DASHBOARD, dashboard.dashboard_init(mi), mi, wait_ms=0, expect="ack", timeout_ms=4000))
        # 7 app-sync subscribe
        mm = a.next_magic()
        s.append(self._send(7, "0x0D subscribe", SID_APP_SYNC, app_sync_subscribe(mm), mm))
        # 8 role RIGHT
        mm = a.next_magic()
        s.append(self._send(8, "role RIGHT", SID_DEVICE, role_change(mm, ROLE_RIGHT), mm))
        # 9 time sync
        mm = a.next_magic()
        s.append(self._send(9, "time sync", SID_DEVICE, time_sync(mm, now, tz), mm))
        # 10 settings READ basic -> snapshot
        mm = a.next_magic()
        s.append(self._send(10, "settings READ", SID_SETTINGS, settings.read_settings(mm), mm, expect="snapshot"))
        # 11 units
        mm = a.next_magic()
        s.append(self._send(11, "units", SID_SETTINGS, settings.units(mm, time_format=1 if self.time_format_12h else 0,
                                                                     temperature_unit=self.temperature_unit), mm))
        # 12 onboarding FINISH
        mm = a.next_magic()
        s.append(self._send(12, "onboarding FINISH", SID_ONBOARDING, onboarding_finish(mm), mm))
        # 13 wear query
        mm = a.next_magic()
        s.append(self._send(13, "wear query", SID_ONBOARDING, wear_query(mm), mm, expect="reply"))
        # 14 case query
        mm = a.next_magic()
        s.append(self._send(14, "case query", SID_CASE, case_query(mm), mm, expect="reply"))
        # 15 gestures default
        mm = a.next_magic()
        s.append(self._send(15, "gestures default", SID_SETTINGS, settings.gestures(mm), mm))
        # 16 notification CTRL + whitelist off
        mm = a.next_magic()
        s.append(self._send(16, "notification CTRL", SID_NOTIFICATION, notifications.notification_ctrl(mm, disp_time=self.disp_time), mm))
        mm = a.next_magic()
        s.append(self._send(16, "whitelist off", SID_NOTIFICATION, notifications.whitelist_disable(mm), mm))
        # 17 weather, wait flag 0
        mm = a.next_magic()
        w = dashboard.weather(mm, self.temperature_c, self.temperature_unit, self.weather_type, now * 1000, self.weather_status)
        s.append(self._send(17, "weather", SID_DASHBOARD, w, mm, wait_ms=0, expect="ack", timeout_ms=4000))
        # 18 stop the heartbeat -> native dashboard appears in ~5 s
        s.append(Step("stop EvenHub heartbeat (dashboard shows in ~5 s)", "stop_heartbeat", RIGHT, SID_EVENHUB, number=18))
        return s

    def as_tuples(self) -> list[tuple[str, bytes | None, int, str | None]]:
        return [st.as_tuple() for st in self.steps()]

    def __iter__(self):
        return iter(self.steps())


def refresh_plan(alloc, payloads: Sequence[tuple[int, bytes, int]]) -> list[Step]:
    """A re-push of dashboard content (weather/calendar/news/tasks): start the heartbeat, wait 2 s,
    send each ``(sid, payload, magic)`` awaiting its ack, stop the heartbeat (features.dart:98-117)."""
    steps = [Step("start EvenHub heartbeat", "start_heartbeat", RIGHT, SID_EVENHUB, wait_ms=2000)]
    for i, (sid, payload, magic) in enumerate(payloads, 1):
        f = flag_for(sid, payload)
        steps.append(Step(f"push {i}", "send", RIGHT, sid, payload, f, magic, frame_pb(payload, sid, f, alloc.next_seq()), 0, "ack", 4000))
    steps.append(Step("stop EvenHub heartbeat", "stop_heartbeat", RIGHT, SID_EVENHUB))
    return steps
