"""Smallest possible Frame program: draw text via the Lua REPL.

Uses brilliant-ble only -- no message layer, no device-side app. Good for
one-off control and for checking the link works.

    .venv/Scripts/python.exe hello.py
"""

from __future__ import annotations

import asyncio

from brilliant_ble import BrilliantBle, BrilliantDeviceType
from park import park


async def connect_with_retry(frame: BrilliantBle, attempts: int = 8) -> str | None:
    """Frame sleeps aggressively, so a single 10s scan often misses it.

    It wakes on a tap and advertises in bursts; keep looking rather than
    failing on the first miss.
    """
    for attempt in range(1, attempts + 1):
        try:
            return await frame.connect(timeout=15)
        except Exception as exc:
            print(f"  attempt {attempt}/{attempts}: {exc}")
            if attempt < attempts:
                await asyncio.sleep(3.0)
    return None


async def main() -> None:
    frame = BrilliantBle()
    name = await connect_with_retry(frame)
    if name is None:
        print("\nCould not find Frame. Tap the glasses to wake them, then retry.")
        print("Run 'python diagnose.py' for a fuller picture.")
        return

    try:
        print(f"Connected to {name!r}  (type={frame.type}, max_lua={frame.max_lua_payload()})")

        # A leftover main.lua (e.g. left by Noa) never returns, so the REPL is
        # unreachable and plain Lua strings sit unexecuted. 0x03 aborts it.
        await frame.send_break_signal()
        await asyncio.sleep(0.5)
        await frame.drain_print_channel()

        # An MTU-23 link yields max_lua_payload()==20, which makes every
        # upload/chunking call fail with a confusing ValueError.
        if frame.max_lua_payload() < 100:
            print(f"  WARNING: tiny MTU (payload {frame.max_lua_payload()}B) -- "
                  "reconnect for a usable link")

        for query in (
            "frame.FIRMWARE_VERSION",
            "frame.HARDWARE_VERSION",
            "frame.battery_level()",
        ):
            reply = await frame.send_lua(f"print({query})", await_print=True)
            print(f"  {query} = {reply}")

        # Frame is double-buffered -- text() draws to the back buffer and
        # nothing is visible until show(). Halo draws immediately.
        if frame.type == BrilliantDeviceType.FRAME:
            lua = "frame.display.text('HELLO FRAME',150,180);frame.display.show();print('drawn')"
        else:
            lua = "frame.display.text('HELLO',150,180);print('drawn')"

        print(f"  drawing -> {await frame.send_lua(lua, await_print=True)}")
        await asyncio.sleep(5.0)
    finally:
        await park(frame)
        await frame.disconnect()
        print("disconnected")


if __name__ == "__main__":
    asyncio.run(main())
