"""Hardware probe for the native protocol, through the running gateway.

    python g2/native/probe.py                 # read the settings snapshot (safe, read-only)
    python g2/native/probe.py --weather 21 --status Sunny   # also push a weather card
    python g2/native/probe.py --menu          # replace the glasses menu with the defaults
    python g2/native/probe.py --note "Milk" --note "Eggs"   # push quick-list tasks

Needs the gateway started with --raw-events (g2/run.py does). Every frame
written is printed with the reply it got, so a silent firmware is visible.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from g2.native import dashboard, transport  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway", default="http://127.0.0.1:8765")
    ap.add_argument("--weather", type=float, help="temperature in C to push")
    ap.add_argument("--status", default="Sunny")
    ap.add_argument("--type", type=int, default=1, help="weather icon type")
    ap.add_argument("--menu", action="store_true")
    ap.add_argument("--note", action="append", default=[])
    ap.add_argument("--timeout", type=float, default=4.0)
    a = ap.parse_args()

    t = transport.GatewayRawTransport(a.gateway)
    async with transport.Glasses(t) as g:
        seen: list[tuple[str, int]] = []
        g.on(lambda side, p: seen.append((side, p.sid)))
        print("reading settings snapshot (SID 0x09 READ) ...")
        snap = await g.read_settings(timeout=a.timeout)
        if snap is None:
            print("  NO REPLY (frames seen meanwhile: %s)" % (seen[-5:] or "none"))
            return 1
        for k in ("battery", "charging", "display_height", "display_distance", "firmware_left", "firmware_right",
                  "head_up_angle", "unread_count", "brightness", "auto_brightness", "silent_mode", "wear_detection"):
            if hasattr(snap, k):
                print(f"  {k:<18} {getattr(snap, k)!r}")
        if a.weather is not None:
            print(f"pushing weather {a.weather} C {a.status!r} ...")
            r = await g.set_weather(a.weather, weather_type=a.type, status=a.status)
            print("  reply:", r)
        if a.note:
            print("pushing quick list ...")
            ok = await g.set_tasks([dashboard.Task(i + 1, n) for i, n in enumerate(a.note)])
            print("  ok:", ok)
        if a.menu:
            print("replacing menu with defaults ...")
            print("  ok:", await g.set_menu())
        print("inbound frames seen:", len(seen), "sids:", sorted({hex(s) for _, s in seen}))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
