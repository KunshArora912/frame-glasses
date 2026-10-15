"""Put device/main.lua on the glasses (or take it off).

    python install_main.py            # upload main.lua, restart Lua, check it runs
    python install_main.py --remove   # delete main.lua; the glasses fall back to
                                      # the stock "Frame is Paired" screen

main.lua is the first thing the firmware runs after a wake, so this is the
screen you see when you lift the glasses off the cradle. A firmware update
(DFU) wipes the filesystem, so run this again after every flash.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

from brilliant_ble import BrilliantBle

LUA = Path(__file__).parent / "device" / "main.lua"


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


async def connect(attempts: int = 6) -> BrilliantBle | None:
    prints: list[str] = []
    frame = BrilliantBle()
    frame._prints = prints  # type: ignore[attr-defined]
    for i in range(1, attempts + 1):
        try:
            name = await frame.connect(timeout=10, print_response_handler=prints.append)
            log(f"connected to {name!r}")
            return frame
        except Exception as exc:
            log(f"  attempt {i}/{attempts}: {type(exc).__name__}: {str(exc)[:70]}")
            await asyncio.sleep(2)
    return None


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--remove", action="store_true")
    args = ap.parse_args()

    frame = await connect()
    if frame is None:
        log("glasses not reachable: tap them three times (or lift them off the cradle) and retry")
        return 2
    prints: list[str] = frame._prints  # type: ignore[attr-defined]

    try:
        # end whatever Lua is looping (an older main.lua, a frame app)
        await frame.send_break_signal()
        await asyncio.sleep(0.3)
        await frame.send_break_signal()
        await frame.drain_print_channel()

        if args.remove:
            r = await frame.send_lua("print(pcall(frame.file.remove, 'main.lua'))",
                                     await_print=True, timeout=5)
            log(f"remove main.lua -> {r}")
            await frame.send_reset_signal()
            log("removed; the glasses now show the stock paired screen")
            return 0

        src = LUA.read_text(encoding="utf-8")
        log(f"uploading {LUA.name} ({len(src)} bytes)")
        await frame.upload_file(str(LUA), "main.lua")

        r = await frame.send_lua("local f=frame.file.open('main.lua','read') local n=0 "
                                 "while true do local c=f:read() if c==nil or #c==0 then break end n=n+#c end "
                                 "f:close() print(n)", await_print=True, timeout=10)
        log(f"on-device size -> {r}")

        prints.clear()
        log("restarting Lua so main.lua runs")
        await frame.send_reset_signal()
        await asyncio.sleep(2.5)

        banner = [p for p in prints if "main.lua" in p]
        errors = [p for p in prints if "error" in p.lower() or "attempt to" in p or ":" in p and "lua" in p.lower() and "main.lua:" in p]
        if banner:
            log(f"running: {banner[0].strip()}")
        else:
            log(f"no startup banner seen; device printed: {[p.strip() for p in prints][:5]}")
        if errors:
            log(f"LUA ERROR: {errors[0].strip()}")
            return 1
        log("done -- lift the glasses off the cradle to see it")
        return 0 if banner else 1
    finally:
        # drop the link so the glasses are free for frame_host.py / other apps
        await frame.disconnect()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
