"""Capture frames from Frame and package them for the remote viewer.

Writes one JSON document per frame into a staging directory, ready to be
pushed into the Artifact database as a single batch. Keeps the base64 out
of the conversation entirely -- the batch references these files by path.

    .venv/Scripts/python.exe app/push_frames.py --frames 12

Each document: {seq, ts, b64, bytes, luma, stddev, res, quality}
Plus status.json: device/firmware/battery/capture metadata.
"""

from __future__ import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from frame_config import FRAME_ADDR
import annotations

import argparse
import asyncio
import base64
import io
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path

from brilliant_msg import BrilliantMsg, RxPhoto, TxCaptureSettings
import sys
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
from park import park

CAPTURE_SETTINGS_MSG = 0x0D
LUA_APP = Path(__file__).parent / "lua" / "camera_frame_app.lua"
QUALITY_NAMES = ["VERY_LOW", "LOW", "MEDIUM", "HIGH", "VERY_HIGH"]


def luma_stats(jpeg: bytes) -> tuple[float, float]:
    from PIL import Image, ImageStat

    stat = ImageStat.Stat(Image.open(io.BytesIO(jpeg)).convert("L"))
    return round(stat.mean[0], 1), round(stat.stddev[0], 1)


def boost(jpeg: bytes) -> tuple[bytes, float]:
    """Stretch the histogram so a dark frame is actually inspectable.

    The sensor is already at max shutter and max gain, so this is purely a
    viewing aid -- like a field monitor's exposure assist. It reveals real
    structure (and real noise); it does not add information.
    """
    from PIL import Image, ImageOps, ImageStat

    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    out = ImageOps.autocontrast(img, cutoff=(0.5, 0.5))
    buf = io.BytesIO()
    out.save(buf, format="JPEG", quality=72)
    mean = ImageStat.Stat(out.convert("L")).mean[0]
    return buf.getvalue(), round(mean, 1)


async def connect_with_retry(frame: BrilliantMsg, attempts: int = 10) -> bool:
    for attempt in range(1, attempts + 1):
        try:
            await frame.connect()
            return True
        except Exception as exc:
            print(f"  connect {attempt}/{attempts}: {exc}")
            if attempt < attempts:
                await asyncio.sleep(2.0)
    return False


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=int, default=12)
    parser.add_argument("--resolution", type=int, default=320)
    parser.add_argument("--quality", type=int, default=3, choices=range(5))
    parser.add_argument("--warmup", type=float, default=4.0)
    parser.add_argument("--out", type=Path, default=Path("out/push"))
    parser.add_argument("--start-seq", type=int, default=1)
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    for stale in args.out.glob("*.json"):
        stale.unlink()

    frame = BrilliantMsg()
    if not await connect_with_retry(frame):
        print("Could not connect -- tap the glasses to wake them.")
        return 2

    rx_photo = RxPhoto()
    written: list[dict] = []
    battery = None
    firmware = None

    try:
        print(f"connected (type={frame.ble.type})")
        try:
            firmware = await frame.ble.send_lua(
                "print(frame.FIRMWARE_VERSION)", await_print=True, timeout=10)
            battery = await frame.ble.send_lua(
                "print(frame.battery_level())", await_print=True, timeout=10)
        except Exception:
            pass

        await frame.upload_stdlua_libs(lib_names=["data", "camera"])
        await frame.upload_frame_app(local_filename=str(LUA_APP))
        frame.attach_print_response_handler()
        await frame.start_frame_app()
        photo_queue = await rx_photo.attach(frame)

        settings = TxCaptureSettings(
            resolution=args.resolution, quality_index=args.quality).pack()

        if args.warmup > 0:
            print(f"warming auto-exposure {args.warmup:.0f}s...")
            await asyncio.sleep(args.warmup)

        for i in range(args.frames):
            seq = args.start_seq + i
            t0 = time.monotonic()
            await frame.send_message(CAPTURE_SETTINGS_MSG, settings)
            try:
                jpeg = await asyncio.wait_for(photo_queue.get(), timeout=25.0)
            except asyncio.TimeoutError:
                print(f"frame {seq}: timeout")
                break
            dt = time.monotonic() - t0
            mean, sd = luma_stats(jpeg)
            boosted, boost_luma = boost(jpeg)
            doc = {
                "seq": seq,
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "b64": base64.b64encode(jpeg).decode("ascii"),
                "b64_boost": base64.b64encode(boosted).decode("ascii"),
                "boost_luma": boost_luma,
                "bytes": len(jpeg),
                "luma": mean,
                "stddev": sd,
                "res": args.resolution,
                "quality": QUALITY_NAMES[args.quality],
                "secs": round(dt, 2),
            }
            path = args.out / f"frame_{seq:04d}.json"
            path.write_text(json.dumps(doc), encoding="utf-8")
            written.append({"seq": seq, "bytes": len(jpeg),
                            "b64": len(doc["b64"]), "luma": mean})
            print(f"  #{seq}  {len(jpeg) / 1024:.1f} kB jpeg  luma {mean}+/-{sd}"
                  f"  -> boosted luma {boost_luma}  ({dt:.2f}s)")

        status = {
            "pushed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "count": len(written),
            "latest_seq": written[-1]["seq"] if written else 0,
            "device": "Frame F5",
            "address": FRAME_ADDR,
            "firmware": (firmware or "unknown").strip(),
            "battery": (battery or "").strip(),
            "res": args.resolution,
            "quality": QUALITY_NAMES[args.quality],
            "mean_luma": round(sum(w["luma"] for w in written) / len(written), 1)
            if written else 0,
        }
        (args.out / "status.json").write_text(json.dumps(status), encoding="utf-8")

        total_b64 = sum(w["b64"] for w in written)
        print(f"\n{len(written)} frames staged in {args.out}")
        print(f"total base64 payload: {total_b64 / 1024:.1f} kB")
        print(f"mean luma: {status['mean_luma']}  "
              f"({'UNDEREXPOSED - dark scene' if status['mean_luma'] < 30 else 'exposed'})")
        return 0
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}")
        return 1
    finally:
        rx_photo.detach(frame)
        try:
            frame.detach_print_response_handler()
            await frame.stop_frame_app()
        except Exception:
            pass
        await park(frame)
        await frame.disconnect()
        print("disconnected")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
