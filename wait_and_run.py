"""Wait for Frame to wake, then launch a program against it.

Frame sleeps into SYSTEM OFF and is then completely absent from a BLE scan,
so a program that merely retries connect() burns its attempts and exits.
This watches cheaply (scan only, no connect) and launches once the glasses
are genuinely there.

    python wait_and_run.py                      # then assistant.py
    python wait_and_run.py --camera             # args pass through
    python wait_and_run.py --run showcase.py

Wake the glasses by docking for ~2s then undocking -- that is the
unconditional wake. Tapping only works if they slept via frame.sleep().
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from bleak import BleakScanner

FRAME_SERVICE = "7a230001-5475-a6a4-654c-8431f6ad49c4"


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def is_frame(device, adv) -> bool:
    """Frame running its app -- not the DFU bootloader ('Frame Update')."""
    name = (adv.local_name or device.name or "").lower()
    if "update" in name:
        return False
    uuids = [u.lower() for u in (adv.service_uuids or [])]
    return FRAME_SERVICE in uuids or "frame" in name


async def wait_for_frame(deadline_min: float) -> bool:
    log(f"waiting up to {deadline_min:.0f} min for Frame to wake")
    log("  (dock for ~2s, then undock -- that always wakes it)")
    t0 = time.monotonic()
    quiet = 0
    while time.monotonic() - t0 < deadline_min * 60:
        dev = await BleakScanner.find_device_by_filter(is_frame, timeout=6.0)
        if dev is not None:
            log(f"awake: {dev.name!r} @ {dev.address}")
            return True
        quiet += 1
        if quiet % 10 == 0:
            log(f"  still asleep ({(time.monotonic() - t0) / 60:.0f} min)")
        await asyncio.sleep(2.0)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", default="assistant.py",
                    help="script to launch once Frame is awake")
    ap.add_argument("--wait-min", type=float, default=30.0)
    args, passthrough = ap.parse_known_args()

    if not asyncio.run(wait_for_frame(args.wait_min)):
        log("Frame never woke up")
        return 2

    # Give the device a moment to finish advertising before we connect.
    time.sleep(1.5)

    script = Path(__file__).parent / args.run
    log(f"launching {args.run} {' '.join(passthrough)}")
    return subprocess.call([sys.executable, "-u", str(script), *passthrough])


if __name__ == "__main__":
    raise SystemExit(main())
