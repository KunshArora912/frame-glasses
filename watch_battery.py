"""Wait for Frame to come back, then record the one number we need.

This replaces `wait_and_run.py --run todo.py` as the thing to leave running
overnight. Two reasons it is the better tool here:

  * todo.py loads a Whisper model on start -- hundreds of megabytes. Firing
    that automatically on a laptop already at 88% memory is how the last
    watcher got killed by the OS. This holds a BLE scanner and nothing else.
  * what we actually want the moment the glasses reappear is the battery
    level, before anything has a chance to drain it. Not a voice recorder.

On detection it connects, reads the gauge, writes it to a CSV, parks the
display and lets go. Then it keeps watching, so docking and undocking again
simply adds another row -- which is exactly the charge curve nobody has
managed to measure on this unit yet.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import gc
from datetime import datetime
from pathlib import Path

from bleak import BleakScanner

FRAME_SERVICE = "7a230001-5475-a6a4-654c-8431f6ad49c4"
DEFAULT_CSV = Path(__file__).parent / "out" / "battery_watch.csv"

# Long enough that a sleeping Frame is not polled pointlessly, short enough
# to catch the window after an undock before it sleeps again.
SCAN_SECONDS = 6.0
GAP_SECONDS = 9.0
# Once seen, wait this long before bothering it again.
COOLDOWN = 300.0


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def is_frame(device, adv) -> bool:
    name = (adv.local_name or device.name or "").lower()
    if "update" in name:            # the DFU bootloader, not the app
        return False
    uuids = [u.lower() for u in (adv.service_uuids or [])]
    return FRAME_SERVICE in uuids or "frame" in name


def record(path: Path, level: float | None, note: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if new:
            writer.writerow(["when", "battery_pct", "note"])
        writer.writerow([datetime.now().isoformat(timespec="seconds"),
                         "" if level is None else f"{level:.1f}", note])


async def read_battery(address: str) -> float | None:
    """Connect briefly, read the gauge, park the display, let go.

    Imported here rather than at module scope so the watcher holds nothing
    but a scanner while it waits -- the whole point of this script.
    """
    from brilliant_msg import BrilliantMsg

    from park import park

    frame = BrilliantMsg()
    try:
        for attempt in range(1, 6):
            try:
                await frame.connect()
                break
            except Exception as exc:
                log(f"  connect {attempt}/5: {exc}")
                await asyncio.sleep(2)
        else:
            return None

        try:
            reply = await frame.ble.send_lua("print(frame.battery_level())",
                                             await_print=True, timeout=8)
            return float(str(reply).strip())
        finally:
            try:
                await park(frame, report=False)
            except Exception:
                pass
            await frame.disconnect()
    except Exception as exc:
        log(f"  read failed: {type(exc).__name__}: {exc}")
        return None
    finally:
        del frame
        gc.collect()


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hours", type=float, default=10.0)
    ap.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    args = ap.parse_args()

    log(f"watching up to {args.hours:.0f}h for Frame. Dock it on a WALL "
        f"charger, wait ~45 min, then UNDOCK -- that is the wake.")
    log(f"  readings -> {args.csv}")

    deadline = asyncio.get_running_loop().time() + args.hours * 3600
    last_seen = 0.0
    seen_count = 0

    while asyncio.get_running_loop().time() < deadline:
        try:
            device = await BleakScanner.find_device_by_filter(
                is_frame, timeout=SCAN_SECONDS)
        except Exception as exc:
            log(f"  scan failed: {type(exc).__name__}: {exc}")
            device = None

        now = asyncio.get_running_loop().time()
        if device is not None and now - last_seen > COOLDOWN:
            last_seen = now
            seen_count += 1
            log(f"AWAKE: {device.name!r} @ {device.address}")
            await asyncio.sleep(1.5)        # let it finish advertising
            level = await read_battery(device.address)
            if level is None:
                log("  battery unreadable")
                record(args.csv, None, "seen, unreadable")
            else:
                log(f"  BATTERY {level:.0f}%")
                record(args.csv, level, f"reading {seen_count}")

        await asyncio.sleep(GAP_SECONDS)

    log("window closed; Frame never appeared" if seen_count == 0
        else f"window closed; {seen_count} reading(s) taken")
    return 0 if seen_count else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
