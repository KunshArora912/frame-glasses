"""Glasses settings: SID 0x09 ``G2SettingPackage``, SID 0x20 module config, SID 0x80
GET_DEVICE_INFO.  Port of ``lib/protocol/settings.dart`` and ``device.dart:143-146,210-247``.

Confidence (native-protocol.md section 7):
  * SET brightness / auto-brightness / silent / wear / head-up / units / gestures: LIVE.
  * SET display height / distance (fields 2 / 3) and dominant hand: DECODED (never sent by
    either hub) -- verify on hardware before relying on them.
  * READ + readback fields: LIVE.  Module config (timeout, language): LIVE.
  * GET_DEVICE_INFO, quick restart, factory reset, unpair: DECODED.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any

from . import pb
from .pb import Writer, message_field, varint_field

SID_SETTINGS = 0x09
SID_MODULE = 0x20
SID_DEVICE = 0x80

# G2SettingPackage command ids (settings.dart:14-15)
CMD_SET, CMD_READ, CMD_DEVICE_EVENT, CMD_DEVICE_ACK, CMD_APP_ACK = 1, 2, 3, 4, 5
CMD_NAMES = {1: "SET", 2: "READ", 3: "DEVICE_EVENT", 4: "DEVICE_ACK", 5: "APP_ACK"}

READ_BRIGHTNESS, READ_BASIC = 0, 1

# SET leaves (settings.dart:22-34; positioning-and-surfaces.md section 1)
F_BRIGHTNESS, F_HEIGHT, F_DISTANCE, F_HEAD_UP, F_WEAR, F_SILENT = 1, 2, 3, 4, 5, 6
F_APP_PAGE, F_ADVANCED, F_UNITS, F_GESTURES, F_HAND, F_CONTROL = 7, 8, 9, 10, 11, 12
SET_NAMES = {
    1: "brightness", 2: "display_height", 3: "display_distance", 4: "head_up", 5: "wear_detection",
    6: "silent_mode", 7: "app_page", 8: "kill_all_features", 9: "units", 10: "gestures",
    11: "dominant_hand", 12: "turn_on_device",
}

BRIGHTNESS_MIN, BRIGHTNESS_MAX = 1, 50
HEAD_UP_ANGLE_MIN, HEAD_UP_ANGLE_MAX = 0, 60
DISPLAY_LEVEL_MAX = 20
ENUM_MAX = 15

#: (screen_on, operation_type, app_type) the official app sends on every connect (settings.dart:41-46)
GESTURES_APP_DEFAULT: tuple[tuple[int, int, int], ...] = ((0, 0, 0), (0, 1, 0), (0, 2, 0))

READBACK_FIELDS = {
    1: "setting_type", 2: "brightness_level", 3: "display_height", 4: "display_distance",
    5: "left_version", 6: "right_version", 7: "head_up", 8: "head_up_angle", 9: "head_up_calibration",
    10: "wear_detection", 11: "running_status", 12: "battery", 13: "charging", 14: "silent_mode",
    15: "left_calibration", 16: "right_calibration", 17: "head_up_recalibration_ok",
    18: "brightness_auto", 19: "unread_count",
}
_STRING_FIELDS = {5, 6}

# SID 0x20 (settings.dart:71-75)
MOD_CMD_LANGUAGE, MOD_CMD_READ_TIMEOUT, MOD_CMD_SET_TIMEOUT = 0, 1, 2
MOD_CMD_NAMES = {0: "SYSTEM_GENERAL_SETTING", 1: "INQUIRE_AUTO_CLOSE", 2: "SET_AUTO_CLOSE"}
TIMEOUT_NEVER = 65365
TIMEOUT_CHOICES = (3, 5, 10, 15, 30, TIMEOUT_NEVER)
LANGUAGES = {
    0: "English", 1: "German", 2: "French", 3: "Spanish", 4: "Italian", 5: "Japanese",
    6: "Chinese (Traditional)", 7: "Chinese (Simplified)", 8: "Korean",
}

# SID 0x80 device info items (device.dart:63-73)
INFO_ALL, INFO_BRIGHTNESS, INFO_ANTI_SHAKE, INFO_DISPLAY_MODE, INFO_WORK_MODE = 0, 1, 2, 3, 4
INFO_WAKEUP_ANGLE, INFO_GLASSES_SN, INFO_DEVICE_SN, INFO_BLE_MAC = 5, 6, 7, 8
INFO_NAMES = {
    0: "ALL_INFO", 1: "DEVICE_BRIGHTNESS", 2: "DEVICE_ANTI_SHAKE_ENABLE", 3: "DEVICE_DISPLAY_MODE",
    4: "DEVICE_WORK_MODE", 5: "DEVICE_WAKEUP_ANGLE", 6: "DEVICE_GLASSES_SN", 7: "DEVICE_DEVICE_SN",
    8: "DEVICE_BLE_MAC",
}
DEV_CMD_UNPAIR, DEV_CMD_SET_INFO, DEV_CMD_GET_INFO, DEV_CMD_FACTORY_RESET, DEV_CMD_QUICK_RESTART = 9, 11, 12, 13, 15


# ── validation helpers (settings.dart:456-469) ─────────────────────────────────────────


def _flag(value: Any, what: str) -> int:
    if isinstance(value, bool):
        return 1 if value else 0
    if value in (0, 1):
        return int(value)
    raise ValueError(f"{what} must be True/False or 0/1, got {value!r}")


def _int_in(value: Any, lo: int, hi: int, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < lo or value > hi:
        raise ValueError(f"{what} must be an integer {lo}..{hi}, got {value!r}")
    return value


# ── SID 0x09 builders ──────────────────────────────────────────────────────────────────


def build_set(magic: int, leaf: int, inner: bytes) -> bytes:
    """SET = ``{1: 1, 2: magic, 3: {leaf: inner}}`` (settings.dart:104-108)."""
    return varint_field(1, CMD_SET) + varint_field(2, magic) + message_field(3, message_field(leaf, inner))


def read_settings(magic: int, setting_type: int = READ_BASIC) -> bytes:
    """READ ``{1: 2, 2: m, 4: {1: type}}``; type 0 brightness, 1 basic (LIVE; settings.dart:113-116).
    The basic reply carries battery, charging, firmware, calibration -- see :func:`parse`."""
    t = _int_in(setting_type, 0, 1, "setting_type")
    return varint_field(1, CMD_READ) + varint_field(2, magic) + message_field(4, varint_field(1, t))


def brightness(magic: int, level: int) -> bytes:
    """Brightness level 1..50 (LIVE). Example level 20 -> ``0801107d1a040a021014`` (magic 0x7d)."""
    return build_set(magic, F_BRIGHTNESS, varint_field(2, _int_in(level, BRIGHTNESS_MIN, BRIGHTNESS_MAX, "brightness level")))


def auto_brightness(magic: int, on: bool) -> bytes:
    """``{1: {1: 0/1}}`` (LIVE)."""
    return build_set(magic, F_BRIGHTNESS, varint_field(1, _flag(on, "on")))


def brightness_calibration(magic: int, left: int, right: int) -> bytes:
    """``{1: {3: left 0..255, 4: right 0..255}}`` (DECODED)."""
    return build_set(magic, F_BRIGHTNESS, varint_field(3, _int_in(left, 0, 255, "left")) + varint_field(4, _int_in(right, 0, 255, "right")))


def display_height(magic: int, level: int) -> bytes:
    """Field 2 ``fY`` / app "Display Height", level 0..20 (DECODED, never sent by a hub).
    Example level 10 -> ``0801107e1a041202080a``."""
    return build_set(magic, F_HEIGHT, varint_field(1, _int_in(level, 0, DISPLAY_LEVEL_MAX, "display height")))


def display_distance(magic: int, level: int) -> bytes:
    """Field 3 ``fX`` / app "Display Distance", level 0..20 (DECODED).
    Example level 10 -> ``0801107f1a041a02080a``."""
    return build_set(magic, F_DISTANCE, varint_field(1, _int_in(level, 0, DISPLAY_LEVEL_MAX, "display distance")))


def head_up(magic: int, enabled: bool, angle: int | None = None) -> bytes:
    """``{4: {1: enabled[, 2: angle 0..60]}}`` (LIVE)."""
    w = Writer().varint(1, _flag(enabled, "enabled"))
    if angle is not None:
        w.varint(2, _int_in(angle, HEAD_UP_ANGLE_MIN, HEAD_UP_ANGLE_MAX, "head-up angle"))
    return build_set(magic, F_HEAD_UP, w.to_bytes())


def wear_detection(magic: int, on: bool) -> bytes:
    """``{5: {1: on}}`` (LIVE)."""
    return build_set(magic, F_WEAR, varint_field(1, _flag(on, "on")))


def silent_mode(magic: int, on: bool) -> bytes:
    """``{6: {1: on}}`` (LIVE)."""
    return build_set(magic, F_SILENT, varint_field(1, _flag(on, "on")))


def units(magic: int, unit_format: int = 0, distance_unit: int = 0, time_format: int = 1,
          date_format: int = 0, temperature_unit: int = 1) -> bytes:
    """``{9: {1 unit_format, 2 distance(0 metric 1 imperial), 3 time(0 24h 1 12h), 4 date, 5 temp(1 C 2 F)}}``
    each 0..15 (LIVE captured; settings.dart:154-163). Defaults are the captured connect values."""
    vals = [unit_format, distance_unit, time_format, date_format, temperature_unit]
    names = ["unit_format", "distance_unit", "time_format", "date_format", "temperature_unit"]
    w = Writer()
    for i, (v, n) in enumerate(zip(vals, names)):
        w.varint(i + 1, _int_in(v, 0, ENUM_MAX, n))
    return build_set(magic, F_UNITS, w.to_bytes())


def gestures(magic: int, entries=GESTURES_APP_DEFAULT) -> bytes:
    """``{10: {1: {1 screen_on, 2 operation_type 0..15, 3 app_type 0/1}...}}`` (LIVE captured)."""
    entries = list(entries)
    if not entries:
        raise ValueError("entries must not be empty")
    w = Writer()
    for e in entries:
        if len(e) != 3:
            raise ValueError(f"each gesture entry is (screen_on, operation_type, app_type), got {e!r}")
        w.message(1, varint_field(1, _flag(e[0], "screen_on")) + varint_field(2, _int_in(e[1], 0, ENUM_MAX, "operation_type"))
                  + varint_field(3, _int_in(e[2], 0, 1, "app_type")))
    return build_set(magic, F_GESTURES, w.to_bytes())


def dominant_hand(magic: int, hand: int, ring_mac: bytes | None = None) -> bytes:
    """``{11: {1: hand 0/1[, 2: ring_mac 6 B]}}`` (DECODED)."""
    w = Writer().varint(1, _int_in(hand, 0, 1, "hand"))
    if ring_mac is not None:
        if len(ring_mac) != 6:
            raise ValueError("ring_mac must be 6 bytes")
        w.bytes(2, ring_mac)
    return build_set(magic, F_HAND, w.to_bytes())


def app_ack(magic: int, package_id: int = 0, flag: int = 0) -> bytes:
    """cmd 5 APP_ACK ``{7: {1 package_id, 2 flag}}`` (shape from settings.dart:309-316; INFERRED use)."""
    return varint_field(1, CMD_APP_ACK) + varint_field(2, magic) + message_field(7, varint_field(1, package_id) + varint_field(2, flag))


# ── SID 0x20 module config (settings.dart:199-215) ────────────────────────────────────


def _module(cmd: int, magic: int, fld: int, inner: bytes) -> bytes:
    return varint_field(1, cmd) + varint_field(2, magic) + message_field(fld, inner)


def display_timeout(magic: int, seconds: int | str | None) -> bytes:
    """Screen-off time ``{1: 2, 2: m, 4: {1: seconds}}``; 3/5/10/15/30 or 'never'/None -> 65365 (LIVE)."""
    if seconds is None or seconds == "never" or seconds == 0:
        v = TIMEOUT_NEVER
    elif isinstance(seconds, bool) or not isinstance(seconds, int) or seconds not in TIMEOUT_CHOICES:
        raise ValueError(f"display timeout must be one of {TIMEOUT_CHOICES} or 'never', got {seconds!r}")
    else:
        v = seconds
    return _module(MOD_CMD_SET_TIMEOUT, magic, 4, varint_field(1, v))


def read_display_timeout(magic: int) -> bytes:
    """``{1: 1, 2: m, 4: {}}`` e.g. ``08011080012200`` for magic 128 (LIVE)."""
    return _module(MOD_CMD_READ_TIMEOUT, magic, 4, b"")


def language(magic: int, index: int | str) -> bytes:
    """UI language ``{1: 0, 2: m, 3: {1: idx}}`` 0 EN .. 8 KO, by index or name (LIVE)."""
    idx: Any = index
    if isinstance(idx, str):
        match = [k for k, v in LANGUAGES.items() if v.lower() == idx.lower()]
        if not match:
            raise ValueError(f"unknown language {index!r}")
        idx = match[0]
    return _module(MOD_CMD_LANGUAGE, magic, 3, varint_field(1, _int_in(idx, 0, 8, "language index")))


# ── SID 0x80 device info (device.dart:143-146) ─────────────────────────────────────────


def _dev(cmd: int, magic: int, fld: int, inner: bytes) -> bytes:
    return varint_field(1, cmd) + varint_field(2, magic) + message_field(fld, inner)


def get_device_info(magic: int, item: int = INFO_ALL) -> bytes:
    """``{1: 12, 2: m, 11: {1: {1: item}}}`` (DECODED). Read-only; an unknown 0x80 write once
    soft-bricked a pair, so do not improvise SET_DEVICE_INFO."""
    if item not in INFO_NAMES:
        raise ValueError(f"item must be one of 0..8, got {item!r}")
    return _dev(DEV_CMD_GET_INFO, magic, 11, message_field(1, varint_field(1, item)))


def quick_restart(magic: int) -> bytes:
    """``{1: 15, 2: m, 14: {}}`` DISRUPTIVE: reboots the glasses (DECODED; device.dart:138)."""
    return _dev(DEV_CMD_QUICK_RESTART, magic, 14, b"")


def factory_reset(magic: int) -> bytes:
    """``{1: 13, 2: m, 12: {}}`` DESTRUCTIVE: wipes settings and bonds (DECODED; device.dart:141)."""
    return _dev(DEV_CMD_FACTORY_RESET, magic, 12, b"")


def unpair(magic: int) -> bytes:
    """``{1: 9, 2: m, 8: {}}`` (DECODED; device.dart:17-31). Shape inferred from field_for_cmd."""
    return _dev(DEV_CMD_UNPAIR, magic, 8, b"")


# ── parsing ────────────────────────────────────────────────────────────────────────────


@dataclass
class Snapshot:
    """Readback field 4 of a SID 0x09 reply/push (settings.dart:48-68). ``None`` = absent."""
    setting_type: int | None = None
    brightness_level: int | None = None
    display_height: int | None = None
    display_distance: int | None = None
    left_version: str | None = None
    right_version: str | None = None
    head_up: int | None = None
    head_up_angle: int | None = None
    head_up_calibration: int | None = None
    wear_detection: int | None = None
    running_status: int | None = None
    battery: int | None = None
    charging: int | None = None
    silent_mode: int | None = None
    left_calibration: int | None = None
    right_calibration: int | None = None
    head_up_recalibration_ok: int | None = None
    brightness_auto: int | None = None
    unread_count: int | None = None
    extra: dict[int, object] = dc_field(default_factory=dict)

    @property
    def is_push(self) -> bool:
        """A frame with field 4 but no setting_type is an unsolicited push (settings.dart:294-301)."""
        return self.setting_type is None

    def as_dict(self) -> dict[str, object]:
        d = {k: v for k, v in self.__dict__.items() if k != "extra" and v is not None}
        d.update({f"field_{k}": v for k, v in self.extra.items()})
        return d


def parse_snapshot(sub: bytes) -> Snapshot:
    s = Snapshot()
    for f in pb.iter_fields(sub):
        name = READBACK_FIELDS.get(f.number)
        if name is None:
            s.extra[f.number] = f.as_int if f.wire == 0 else f.as_bytes
        elif f.number in _STRING_FIELDS and f.wire == pb.WIRE_LEN:
            setattr(s, name, f.as_str)
        elif f.wire == pb.WIRE_VARINT:
            setattr(s, name, f.as_int)
    return s


@dataclass
class SettingsMessage:
    cmd: int
    magic: int
    snapshot: Snapshot | None = None  # field 4
    set: dict[str, object] | None = None  # field 3 (echo of a SET)
    event: dict[str, int] | None = None  # field 5 device event
    ack: dict[str, int] | None = None  # field 6/7 {package_id, flag}

    @property
    def name(self) -> str:
        return CMD_NAMES.get(self.cmd, f"cmd={self.cmd}")

    @property
    def ok(self) -> bool | None:
        return None if self.ack is None else self.ack.get("flag", 0) == 0


def _decode_set(sub: bytes) -> dict[str, object]:
    out: dict[str, object] = {}
    for f in pb.iter_fields(sub):
        if f.wire != pb.WIRE_LEN:
            continue
        v = f.as_bytes
        name = SET_NAMES.get(f.number, f"field_{f.number}")
        if f.number == F_BRIGHTNESS:
            out[name] = {"auto": pb.read_varint(v, 1, 0), "level": pb.read_varint(v, 2, 0),
                         "left_calibration": pb.read_varint(v, 3, 0), "right_calibration": pb.read_varint(v, 4, 0)}
        elif f.number == F_HEAD_UP:
            out[name] = {"enabled": pb.read_varint(v, 1, 0), "angle": pb.read_varint(v, 2, 0),
                         "calibration_switch": pb.read_varint(v, 3, 0), "calibration": pb.read_varint(v, 4, 0)}
        elif f.number == F_UNITS:
            keys = ["unit_format", "distance_unit", "time_format", "date_format", "temperature_unit"]
            out[name] = {k: pb.read_varint(v, i + 1, 0) for i, k in enumerate(keys)}
        elif f.number == F_GESTURES:
            out[name] = [[pb.read_varint(g, 1, 0), pb.read_varint(g, 2, 0), pb.read_varint(g, 3, 0)] for g in pb.read_messages(v, 1)]
        elif f.number == F_HAND:
            mac = pb.read_bytes(v, 2)
            out[name] = {"hand": pb.read_varint(v, 1, 0), "ring_mac": mac.hex(":") if mac else None}
        else:
            out[name] = pb.read_varint(v, 1, 0)
    return out


def parse(payload: bytes) -> SettingsMessage:
    """Decode any SID 0x09 message (settings.dart:649-686)."""
    m = SettingsMessage(pb.read_varint(payload, 1, -1), pb.read_varint(payload, 2, -1))
    req = pb.read_bytes(payload, 4)
    if req is not None:
        m.snapshot = parse_snapshot(req)
    sub = pb.read_bytes(payload, 3)
    if sub is not None:
        m.set = _decode_set(sub)
    ev = pb.read_bytes(payload, 5)
    if ev is not None:
        m.event = {"recalibration_status": pb.read_varint(ev, 1, 0), "silent_mode": pb.read_varint(ev, 2, 0)}
    for fld in (6, 7):
        ack = pb.read_bytes(payload, fld)
        if ack is not None:
            m.ack = {"package_id": pb.read_varint(ack, 1, 0), "flag": pb.read_varint(ack, 2, 0)}
    return m


def parse_ack(payload: bytes) -> dict[str, int] | None:
    """``{package_id, flag}`` of a settings ack (fields 6/7), else None."""
    return parse(payload).ack


@dataclass
class ModuleMessage:
    cmd: int
    magic: int
    language: int | None = None
    timeout: int | None = None

    @property
    def timeout_never(self) -> bool | None:
        return None if self.timeout is None else self.timeout > 20  # settings.dart:703-706


def parse_module(payload: bytes) -> ModuleMessage:
    """Decode a SID 0x20 reply (settings.dart:689-710)."""
    m = ModuleMessage(pb.read_varint(payload, 1, -1), pb.read_varint(payload, 2, -1))
    sys_ = pb.read_bytes(payload, 3)
    if sys_ is not None:
        m.language = pb.read_varint(sys_, 1, 0)
    dash = pb.read_bytes(payload, 4)
    if dash is not None:
        t = pb.read_varint(dash, 1, -1)
        if t >= 0:
            m.timeout = t
    return m


def parse_device_info(payload: bytes) -> list[dict[str, object]]:
    """Items of a GET/SET_DEVICE_INFO reply, field 11 / 10 (device.dart:210-247)."""
    cmd = pb.read_varint(payload, 1, -1)
    sub = pb.read_bytes(payload, 11 if cmd == DEV_CMD_GET_INFO else 10)
    if sub is None:
        return []
    items = []
    for v in pb.read_messages(sub, 1):
        item_id = pb.read_varint(v, 1, 0)
        item: dict[str, object] = {"item": item_id, "name": INFO_NAMES.get(item_id, item_id)}
        als = pb.read_bytes(v, 2)
        if als is not None:
            item["als_brightness"] = pb.read_varint(als, 1, 0)
            item["als_auto"] = pb.read_varint(als, 2, 0)
        if pb.read_varint(v, 3, -1) >= 0:
            item["anti_shake"] = pb.read_varint(v, 3, 0)
        for fld, key in ((4, "display_mode"), (5, "wake_mode")):
            mm = pb.read_bytes(v, fld)
            if mm is not None:
                item[key] = pb.read_varint(mm, 1, 0)
        ang = pb.read_bytes(v, 6)
        if ang is not None:
            item["wakeup_angle"] = pb.read_varint(ang, 1, 0)
            item["wakeup_offset"] = pb.read_varint(ang, 2, 0)
        for fld, key in ((7, "glasses_sn"), (8, "device_sn")):
            mm = pb.read_bytes(v, fld)
            if mm is not None:
                item[key] = (pb.read_bytes(mm, 1) or b"").decode("utf-8", "replace")
        mac = pb.read_bytes(v, 9)
        if mac is not None:
            item["ble_mac"] = ":".join(f"{b:02X}" for b in (pb.read_bytes(mac, 1) or b""))
        items.append(item)
    return items
