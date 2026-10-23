"""Glasses settings from the laptop, over the gateway's raw channel (no screen of ours).

    python g2/settings_cli.py show
    python g2/settings_cli.py set brightness 12
    python g2/settings_cli.py set height 10 --dry-run        # print the frames, send nothing
    python g2/settings_cli.py keys

Builders come from g2/native/settings.py. Confidence (g2/native/README.md):
LIVE    brightness, auto, headup, angle, wear, silent, time, temp, timeout, language
DECODED height, distance (never sent by either hub: verify the ack and the readback first)
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))

from g2.native import session  # noqa: E402
from g2.native import settings as ns  # noqa: E402
from g2.native.framing import MagicAllocator, frame_pb  # noqa: E402

ON = ("on", "1", "true", "yes")
OFF = ("off", "0", "false", "no")

#: key -> (help, confidence, snapshot attribute that reports it back or None)
KEYS: dict[str, tuple[str, str, str | None]] = {
    "brightness": ("1..50", "LIVE", "brightness_level"),
    "auto": ("on|off  auto-brightness", "LIVE", "brightness_auto"),
    "height": ("0..20  display height", "DECODED", "display_height"),
    "distance": ("0..20  display distance", "DECODED", "display_distance"),
    "headup": ("on|off  head-up display", "LIVE", "head_up"),
    "angle": ("0..60  head-up angle (degrees)", "LIVE", "head_up_angle"),
    "wear": ("on|off  wear detection", "LIVE", "wear_detection"),
    "silent": ("on|off  silent mode", "LIVE", "silent_mode"),
    "time": ("12|24  clock format", "LIVE", None),
    "temp": ("C|F  temperature unit", "LIVE", None),
    "timeout": ("3|5|10|15|30|never  display off after (s)", "LIVE", None),
    "language": ("EN|DE|FR|ES|IT|JA|ZH-TW|ZH-CN|KO  UI language", "LIVE", None),
}


def _flag(v: str, what: str) -> bool:
    s = v.strip().lower()
    if s in ON:
        return True
    if s in OFF:
        return False
    raise ValueError(f"{what}: expected on|off, got {v!r}")


def _int(v: str, what: str) -> int:
    try:
        return int(v)
    except ValueError:
        raise ValueError(f"{what}: expected a number, got {v!r}") from None


def build(key: str, value: str, magic: int, snapshot: ns.Snapshot | None = None) -> tuple[int, bytes]:
    """(sid, payload) for ``set key value``. ``snapshot`` supplies the partner field of head_up."""
    k = key.lower()
    if k == "brightness":
        return ns.SID_SETTINGS, ns.brightness(magic, _int(value, k))
    if k == "auto":
        return ns.SID_SETTINGS, ns.auto_brightness(magic, _flag(value, k))
    if k == "height":
        return ns.SID_SETTINGS, ns.display_height(magic, _int(value, k))
    if k == "distance":
        return ns.SID_SETTINGS, ns.display_distance(magic, _int(value, k))
    if k == "headup":
        angle = getattr(snapshot, "head_up_angle", None)
        return ns.SID_SETTINGS, ns.head_up(magic, _flag(value, k), angle)
    if k == "angle":
        enabled = getattr(snapshot, "head_up", None)
        return ns.SID_SETTINGS, ns.head_up(magic, True if enabled is None else bool(enabled), _int(value, k))
    if k == "wear":
        return ns.SID_SETTINGS, ns.wear_detection(magic, _flag(value, k))
    if k == "silent":
        return ns.SID_SETTINGS, ns.silent_mode(magic, _flag(value, k))
    if k == "time":
        if value not in ("12", "24"):
            raise ValueError("time: expected 12 or 24")
        return ns.SID_SETTINGS, ns.units(magic, time_format=1 if value == "12" else 0)
    if k == "temp":
        v = value.strip().upper()
        if v not in ("C", "F"):
            raise ValueError("temp: expected C or F")
        return ns.SID_SETTINGS, ns.units(magic, temperature_unit=1 if v == "C" else 2)
    if k == "timeout":
        v = value.strip().lower()
        return ns.SID_MODULE, ns.display_timeout(magic, None if v in ("never", "0", "off") else _int(v, k))
    if k == "language":
        v = value.strip().upper()
        names = {"EN": 0, "DE": 1, "FR": 2, "ES": 3, "IT": 4, "JA": 5, "ZH-TW": 6, "ZH-CN": 7, "KO": 8}
        return ns.SID_MODULE, ns.language(magic, names.get(v, v if v.isdigit() else value))
    raise KeyError(f"unknown key {key!r}; see: python g2/settings_cli.py keys")


def describe_snapshot(snap: ns.Snapshot | None, timeout: int | None = None) -> list[str]:
    if snap is None:
        return ["no reply from the glasses"]
    flag = lambda v: "?" if v is None else ("on" if v else "off")  # noqa: E731
    rows = [
        ("brightness", snap.brightness_level), ("auto", flag(snap.brightness_auto)),
        ("height", f"{snap.display_height}  (DECODED set)"), ("distance", f"{snap.display_distance}  (DECODED set)"),
        ("headup", flag(snap.head_up)), ("angle", snap.head_up_angle), ("wear", flag(snap.wear_detection)),
        ("silent", flag(snap.silent_mode)), ("battery", f"{snap.battery}%" + (" charging" if snap.charging else "")),
        ("firmware", f"L {snap.left_version}  R {snap.right_version}"), ("unread", snap.unread_count),
    ]
    if timeout is not None:
        rows.append(("timeout", "never" if timeout > 20 else f"{timeout}s"))
    return [f"{k:<11}{v}" for k, v in rows]


def frames_for(sid: int, payload: bytes, alloc: MagicAllocator) -> list[bytes]:
    return list(frame_pb(payload, sid, session.flag_for(sid, payload), alloc.next_seq()))


async def run(a, transport_factory: Callable[[str], Any] | None = None, out: Callable[[str], None] = print) -> int:
    from g2.native.transport import GatewayRawTransport, Glasses
    alloc = MagicAllocator()
    if a.cmd == "keys":
        for k, (h, conf, _) in KEYS.items():
            out(f"{k:<11}{h:<46}{conf}")
        return 0
    if a.cmd == "set" and a.dry_run:
        sid, payload = build(a.key, a.value, alloc.next_magic())
        out(f"sid 0x{sid:02x} payload {payload.hex()}  ({KEYS.get(a.key.lower(), ('', '?', None))[1]})")
        for f in frames_for(sid, payload, alloc):
            out(f"frame {f.hex()}")
        if sid == ns.SID_SETTINGS:
            out(f"decoded {ns.parse(payload).set}")
        return 0
    transport = (transport_factory or GatewayRawTransport)(a.gateway)
    g = Glasses(transport, alloc)
    await g.start()
    try:
        if hasattr(transport, "connected"):
            for _ in range(40):                      # let the /ws reader connect so the reply is not missed
                if transport.connected:
                    break
                await asyncio.sleep(0.05)
        snap = await g.read_settings(timeout=a.timeout)
        if a.cmd == "show":
            r = await g.send_pb(ns.SID_MODULE, ns.read_display_timeout(alloc.next_magic()), expect="reply", timeout=a.timeout)
            t = ns.parse_module(r.payload).timeout if r is not None else None
            for line in describe_snapshot(snap, t):
                out(line)
            return 0 if snap is not None else 1
        sid, payload = build(a.key, a.value, alloc.next_magic(), snap)
        conf = KEYS.get(a.key.lower(), ("", "?", None))[1]
        reply = await g.apply_setting(payload, sid=sid, timeout=a.timeout)
        if reply is None:
            out(f"set {a.key} {a.value}: NO ACK ({conf})")
            return 1
        ack = ns.parse_ack(reply.payload) if sid == ns.SID_SETTINGS else None
        ok = ack is None or ack.get("flag", 0) == 0
        out(f"set {a.key} {a.value}: {'acked' if ok else 'REJECTED'} ({conf})")
        attr = KEYS.get(a.key.lower(), ("", "", None))[2]
        if attr:
            after = await g.read_settings(timeout=a.timeout)
            out(f"readback {a.key}: {getattr(after, attr, None) if after is not None else 'no reply'}")
        elif a.key.lower() == "timeout":
            r = await g.send_pb(ns.SID_MODULE, ns.read_display_timeout(alloc.next_magic()), expect="reply", timeout=a.timeout)
            t = ns.parse_module(r.payload).timeout if r is not None else None
            out(f"readback timeout: {'no reply' if t is None else ('never' if t > 20 else f'{t}s')}")
        return 0 if ok else 1
    finally:
        await g.stop()


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--gateway", default="http://127.0.0.1:8765")
    ap.add_argument("--timeout", type=float, default=3.0)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show", help="read the settings snapshot and the display timeout")
    sub.add_parser("keys", help="list the keys `set` accepts")
    p = sub.add_parser("set", help="change one setting and read it back")
    p.add_argument("key")
    p.add_argument("value")
    p.add_argument("--dry-run", action="store_true", help="print payload and frames, send nothing")
    return ap.parse_args(argv)


def main(argv=None, **kw) -> int:
    try:
        return asyncio.run(run(parse_args(argv), **kw))
    except (KeyError, ValueError) as e:
        print(f"error: {e}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
