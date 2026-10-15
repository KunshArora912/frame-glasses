"""Measure how long Frame actually survives on its own battery.

A voltage-based gauge reports a healthy percentage for a degraded cell: it
takes surface charge, reads ~92%, then collapses under load. This logs the
reported level over time so capacity can be judged from the shape of the
curve rather than a single reading.

    python battery_test.py                 # poll until it dies
    python battery_test.py --interval 15   # gentler polling

Reference: a healthy 210 mAh cell at ~80 mA active draw should run for
roughly 2.5 hours, and far longer idling. Minutes means the cell is shot.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from datetime import datetime
from pathlib import Path

from brilliant_ble import BrilliantBle

# Reading the firmware made the stakes of this script explicit: `shutdown()`
# is the ONLY code that arms a System OFF wake source, and a battery that
# simply collapses never runs it. A Frame that dies flat therefore has no
# wake source armed at all -- tapping can never bring it back, and only a
# dock/undock will. Running this to zero is what CREATES an unwakeable
# device, so it stops at a floor unless you ask for otherwise.
from park import SAFE_FLOOR_PCT



def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


async def connect_with_retry(frame: BrilliantBle, attempts: int = 12) -> str | None:
    for i in range(1, attempts + 1):
        try:
            return await frame.connect(timeout=15)
        except Exception as exc:
            log(f"  connect {i}/{attempts}: {exc}")
            if i < attempts:
                await asyncio.sleep(3)
    return None


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--interval", type=float, default=10.0,
                    help="seconds between readings (default 10)")
    ap.add_argument("--csv", type=Path, default=Path("out/battery_log.csv"))
    ap.add_argument("--idle", action="store_true",
                    help="poll only; do not keep the display lit")
    args = ap.parse_args()

    args.csv.parent.mkdir(parents=True, exist_ok=True)
    rows: list[tuple[float, float]] = []

    frame = BrilliantBle()
    name = await connect_with_retry(frame)
    if name is None:
        log("could not connect -- dock, wait for the LED, then undock and retry")
        return 2
    log(f"connected to {name!r}")

    t0 = time.monotonic()
    first = None
    try:
        await frame.send_break_signal()
        await asyncio.sleep(0.5)
        await frame.drain_print_channel()

        fw = await frame.send_lua("print(frame.FIRMWARE_VERSION)",
                                  await_print=True, timeout=10)
        log(f"firmware {fw}")
        log(f"polling every {args.interval:.0f}s -- leave the glasses UNDOCKED")
        log("(Ctrl-C to stop early; the CSV is written either way)\n")

        while True:
            elapsed = time.monotonic() - t0
            try:
                raw = await frame.send_lua("print(frame.battery_level())",
                                           await_print=True, timeout=10)
                pct = float(str(raw).strip())
            except Exception as exc:
                log(f"t={elapsed/60:5.1f} min  LINK LOST: {type(exc).__name__}: {exc}")
                break

            if first is None:
                first = pct
            rows.append((elapsed, pct))
            drop = first - pct
            rate = (drop / (elapsed / 3600)) if elapsed > 60 else 0.0
            extra = f"  dropped {drop:.0f}%  ({rate:.0f}%/hr)" if elapsed > 60 else ""
            log(f"t={elapsed/60:5.1f} min  battery {pct:5.1f}%{extra}")

            if not args.idle:
                try:
                    await frame.send_lua(
                        f"frame.display.text('BATT {pct:.0f}%',240,180);"
                        "frame.display.show()", await_print=False)
                except Exception:
                    pass

            await asyncio.sleep(args.interval)
    except KeyboardInterrupt:
        log("stopped by user")
    finally:
        try:
            await frame.disconnect()
        except Exception:
            pass

        if rows:
            args.csv.write_text(
                "elapsed_s,battery_pct\n"
                + "\n".join(f"{e:.1f},{p:.1f}" for e, p in rows),
                encoding="utf-8")
            dur = rows[-1][0] / 60
            drop = rows[0][1] - rows[-1][1]
            log(f"\n--- {len(rows)} readings over {dur:.1f} min ---")
            log(f"start {rows[0][1]:.0f}%  ->  end {rows[-1][1]:.0f}%  "
                f"(dropped {drop:.0f}%)")
            if dur > 1:
                log(f"discharge rate: {drop / (dur / 60):.0f} %/hour")
                if drop > 0:
                    log(f"projected runtime from 100%: "
                        f"{100 / (drop / (dur / 60)):.1f} hours")
            log(f"wrote {args.csv}")
            log("\nVERDICT GUIDE: a healthy 210 mAh cell idles for hours.")
            log("Dying inside ~15 min, or a sudden cliff, means a degraded cell.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
