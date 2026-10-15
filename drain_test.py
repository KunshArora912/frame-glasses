"""Sustained-load battery drain test: stream audio until the cell gives out.

Continuous 8 kHz/16-bit audio is 16 kB/s -- the heaviest steady load Frame
can carry -- so this is the fastest honest way to characterise the battery.
Logs battery against elapsed time and throughput.

    python drain_test.py                 # run until it dies (or --max-min)
    python drain_test.py --max-min 20

Run UNDOCKED. If the reported level does not move at all, the glasses are
probably still on cradle power and the result means nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from datetime import datetime
from pathlib import Path

from brilliant_msg import BrilliantMsg, RxAudio

# Reading the firmware made the stakes of this script explicit: `shutdown()`
# is the ONLY code that arms a System OFF wake source, and a battery that
# simply collapses never runs it. A Frame that dies flat therefore has no
# wake source armed at all -- tapping can never bring it back, and only a
# dock/undock will. Running this to zero is what CREATES an unwakeable
# device, so it stops at a floor unless you ask for otherwise.
from park import SAFE_FLOOR_PCT


AUDIO_CTRL_MSG = 0x30
LUA_APP = Path(__file__).parent / "app" / "lua" / "audio_frame_app.lua"
BYTE_RATE = 16000


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


async def battery(frame: BrilliantMsg) -> float:
    try:
        r = await frame.ble.send_lua("print(frame.battery_level())",
                                     await_print=True, timeout=12)
        return float(str(r).strip())
    except Exception:
        return float("nan")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-min", type=float, default=90.0)
    ap.add_argument("--report", type=float, default=60.0,
                    help="seconds between battery readings (default 60)")
    ap.add_argument("--floor", type=float, default=SAFE_FLOOR_PCT,
                    help=f"stop at this %% (default {SAFE_FLOOR_PCT:.0f})")
    ap.add_argument("--to-zero", action="store_true",
                    help="run the cell all the way down. This is how a Frame "
                         "becomes unwakeable -- see park.py before using it.")
    ap.add_argument("--csv", type=Path, default=Path("out/drain_log.csv"))
    args = ap.parse_args()
    args.csv.parent.mkdir(parents=True, exist_ok=True)

    frame = BrilliantMsg()
    for i in range(1, 16):
        try:
            await frame.connect()
            break
        except Exception as exc:
            log(f"  connect {i}/15: {exc}")
            if i == 15:
                return 2
            await asyncio.sleep(3)

    rx = RxAudio(streaming=True)
    rows: list[tuple[float, float]] = []
    t0 = time.monotonic()

    try:
        await frame.ble.send_break_signal()
        await asyncio.sleep(0.5)
        await frame.ble.drain_print_channel()

        start_pct = await battery(frame)
        log(f"start battery {start_pct:.0f}%  --  streaming audio continuously")
        log("leave the glasses UNDOCKED; Ctrl-C stops early\n")
        rows.append((0.0, start_pct))

        await frame.upload_stdlua_libs(lib_names=["data", "audio"])
        await frame.upload_frame_app(local_filename=str(LUA_APP))
        await frame.start_frame_app()
        q = await rx.attach(frame)
        await frame.send_message(AUDIO_CTRL_MSG, bytes([1, 0, 16]))

        total = 0
        last_report = time.monotonic()
        while True:
            el = time.monotonic() - t0
            if el > args.max_min * 60:
                log("reached --max-min")
                break
            try:
                chunk = await asyncio.wait_for(q.get(), timeout=8.0)
            except asyncio.TimeoutError:
                log(f"t={el/60:5.1f} min  audio stalled -- link may be dying")
                break
            if chunk is None:
                log("end-of-stream")
                break
            total += len(chunk)

            now = time.monotonic()
            if now - last_report >= args.report:
                pct = await battery(frame)
                rows.append((now - t0, pct))
                drop = start_pct - pct
                mins = (now - t0) / 60
                rate = drop / (mins / 60) if mins > 0 and drop > 0 else 0.0
                proj = f"  -> ~{100/rate:.1f} h from full" if rate > 0 else ""
                log(f"t={mins:5.1f} min  battery {pct:5.1f}%  "
                    f"(-{drop:.0f}%)  {total/1024:7.0f} kB streamed"
                    f"  {total/(now-t0)/1000:.1f} kB/s{proj}")
                last_report = now

                if pct <= args.floor and not args.to_zero:
                    log(f"battery {pct:.0f}% has reached the {args.floor:.0f}% "
                        f"floor -- stopping and parking the display.")
                    log("DOCK IT NOW. A Frame that dies flat has no wake "
                        "source armed, so tapping cannot revive it.")
                    break
    except KeyboardInterrupt:
        log("stopped by user")
    except Exception as exc:
        log(f"LINK LOST after {(time.monotonic()-t0)/60:.1f} min: "
            f"{type(exc).__name__}: {exc}")
        log("(this is what a battery giving out looks like)")
    finally:
        try:
            await frame.send_message(AUDIO_CTRL_MSG, bytes([0, 0, 16]))
        except Exception:
            pass
        try:
            rx.detach(frame)
            await frame.stop_frame_app()
        except Exception:
            pass
        try:
            await frame.disconnect()
        except Exception:
            pass

        if len(rows) >= 2:
            args.csv.write_text(
                "elapsed_s,battery_pct\n"
                + "\n".join(f"{e:.1f},{p:.1f}" for e, p in rows),
                encoding="utf-8")
            mins = rows[-1][0] / 60
            drop = rows[0][1] - rows[-1][1]
            log(f"\n--- {mins:.1f} min under continuous 16 kB/s load ---")
            log(f"battery {rows[0][1]:.0f}% -> {rows[-1][1]:.0f}%  (dropped {drop:.0f}%)")
            if drop > 0 and mins > 0:
                rate = drop / (mins / 60)
                log(f"drain {rate:.0f} %/hour  ->  ~{100/rate:.1f} h runtime from full")
                log("")
                if 100 / rate >= 2.0:
                    log("VERDICT: healthy. Consistent with a ~210 mAh cell.")
                elif 100 / rate >= 0.75:
                    log("VERDICT: reduced capacity, but usable.")
                else:
                    log("VERDICT: badly degraded cell.")
            else:
                log("battery did not move -- either very low drain, or the")
                log("glasses are still on cradle power (check they are undocked).")
            log(f"wrote {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
