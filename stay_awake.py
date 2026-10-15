"""Pin Frame awake in the dock, so it stays usable on cradle power.

Frame powers off whenever it is docked, and `run_lua()` clears the
`stay_awake` flag on every boot (luaport.c:153 runs
`frame.stay_awake(false); require('main')`), so the flag the factory-reset
branch sets is wiped before you can use it. Setting it over Bluetooth after
boot sticks, because `shutdown()` returns early while it is set.

    python stay_awake.py            # wait for the device, then pin it awake
    python stay_awake.py --off      # restore normal sleep behaviour

Run this, then release the cradle pin: the glasses stay on, docked and
powered, until they next reboot. It does NOT survive a power cycle.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from datetime import datetime

from brilliant_ble import BrilliantBle


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


async def grab(attempts: int, pause: float) -> BrilliantBle | None:
    """Keep trying: after a factory reset the host must re-bond first."""
    frame = BrilliantBle()
    for i in range(1, attempts + 1):
        try:
            name = await frame.connect(timeout=12)
            log(f"connected to {name!r}")
            return frame
        except Exception as exc:
            msg = str(exc)
            hint = ""
            if "did not accept" in msg or "Authentication" in msg:
                hint = "  <- pair it: Settings > Bluetooth > Add device > Frame F5"
            log(f"  attempt {i}/{attempts}: {msg}{hint}")
            await asyncio.sleep(pause)
    return None


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--off", action="store_true",
                    help="restore normal sleep instead of pinning awake")
    ap.add_argument("--attempts", type=int, default=60)
    ap.add_argument("--pause", type=float, default=4.0)
    args = ap.parse_args()

    want = "false" if args.off else "true"
    log(f"waiting for Frame -- will set stay_awake({want})")
    log("hold the cradle pin and pair it; I will catch it\n")

    frame = await grab(args.attempts, args.pause)
    if frame is None:
        log("never became connectable")
        return 2

    try:
        await frame.send_break_signal()
        await asyncio.sleep(0.5)
        await frame.drain_print_channel()

        r = await frame.send_lua(f"frame.stay_awake({want});print('set')",
                                 await_print=True, timeout=15)
        log(f"stay_awake({want}) -> {r}")

        if args.off:
            log("normal sleep restored; it will power off when docked")
            return 0

        for q, label in (("frame.FIRMWARE_VERSION", "firmware"),
                         ("frame.battery_level()", "battery"),
                         ("frame.bluetooth.max_length()", "payload")):
            try:
                log(f"  {label}: {await frame.send_lua(f'print({q})', await_print=True, timeout=12)}")
            except Exception as exc:
                log(f"  {label}: {exc}")

        try:
            ok = await frame.send_lua(
                "local ok=pcall(frame.file.mkdir,'probe_d') "
                "pcall(frame.file.remove,'probe_d') print(tostring(ok))",
                await_print=True, timeout=15)
            log(f"  filesystem writable: {ok}")
        except Exception as exc:
            log(f"  filesystem: {exc}")

        # Leave the panel blank: a permanently-lit OLED risks burn-in.
        await frame.send_lua("frame.display.text(' ',1,1);frame.display.show();print('ok')",
                             await_print=True, timeout=12)

        log("")
        log("PINNED AWAKE -- you can release the cradle pin now.")
        log("It stays on while docked until it next reboots.")
        log("Re-run this after any power cycle; `--off` restores normal sleep.")
        return 0
    finally:
        try:
            await frame.disconnect()
        except Exception:
            pass
        log("disconnected (the device stays awake)")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
