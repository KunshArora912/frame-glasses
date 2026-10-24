"""Move the G2 picture up/down and closer/further, live, from the laptop keyboard.

    python g2/position.py

    Up / Down      display height    (Even app: "Display Height" slider)
    Left / Right   display distance  closer / further (Even app: Near / Mid / Far)
    r              re-read what the glasses report
    q              quit (the glasses keep the last setting)

Wear the glasses while you press keys; every press is sent at once and then
read back. Both values are stored on the glasses and apply to everything
(Even's dashboard, notifications, our screens), exactly like Even's app.
The SET commands were decoded from Even's app and are confirmed on hardware
only once you see the picture move.

Needs the stack running (python g2/run.py) with raw events on (the default).
"""
from __future__ import annotations

import asyncio
import msvcrt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from g2.native import settings, transport  # noqa: E402

LEVEL_MAX = settings.DISPLAY_LEVEL_MAX      # 20


def read_key() -> str | None:
    if not msvcrt.kbhit():
        return None
    k = msvcrt.getwch()
    if k in ("\x00", "\xe0"):                # arrow keys arrive as a two-part code
        return {"H": "up", "P": "down", "K": "left", "M": "right"}.get(msvcrt.getwch())
    return k.lower()


async def main() -> int:
    t = transport.GatewayRawTransport("http://127.0.0.1:8765")
    async with transport.Glasses(t) as g:
        snap = await g.read_settings()
        if snap is None:
            print("the glasses did not answer a settings read; is python g2/run.py running and are they connected?")
            return 1
        # proto3 leaves zero out of the reply, so "absent" means 0
        height = snap.display_height or 0
        distance = snap.display_distance or 0
        print(f"glasses report: height {height}, distance {distance}   (range 0..{LEVEL_MAX})")
        print("Up/Down = height, Left/Right = closer/further, r = re-read, q = quit")
        while True:
            k = read_key()
            if k is None:
                await asyncio.sleep(0.03)
                continue
            if k == "q":
                return 0
            if k == "r":
                s = await g.read_settings()
                print(f"glasses report: height {s.display_height or 0}, distance {s.display_distance or 0}" if s else "no reply")
                continue
            if k in ("up", "down"):
                height = max(0, min(LEVEL_MAX, height + (1 if k == "up" else -1)))
                reply = await g.apply_setting(settings.display_height(g.alloc.next_magic(), height))
                what = f"height -> {height}"
            elif k in ("left", "right"):
                distance = max(0, min(LEVEL_MAX, distance + (-1 if k == "left" else 1)))
                reply = await g.apply_setting(settings.display_distance(g.alloc.next_magic(), distance))
                what = f"distance -> {distance}"
            else:
                continue
            s = await g.read_settings()
            back = f"reads back height {s.display_height or 0}, distance {s.display_distance or 0}" if s else "no read-back"
            print(f"{what:<16} {'acked' if reply is not None else 'NO ACK'}   {back}", flush=True)


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
