"""Exercise every subsystem on battery power, measuring drain per workload.

More informative than idle battery polling: it proves each subsystem works
off-dock AND shows what each one actually costs, which is what you need to
plan a real application against a 210 mAh cell.

    python workout.py            # full run
    python workout.py --quick    # shorter camera/audio segments

Run it with the glasses UNDOCKED. Reports a per-stage drain table at the end.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import time
from datetime import datetime
from pathlib import Path

from brilliant_msg import (BrilliantMsg, RxAudio, RxPhoto, TxCaptureSettings,
                           TxPlainText)
from park import park

CAPTURE_MSG = 0x0D
AUDIO_CTRL_MSG = 0x30
TEXT_MSG = 0x0A
LUA = Path(__file__).parent / "app" / "lua"


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


class Marks:
    """Battery readings tagged with the stage that preceded them."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, float, float]] = []
        self.t0 = time.monotonic()

    def add(self, stage: str, pct: float) -> None:
        self.rows.append((stage, time.monotonic() - self.t0, pct))
        log(f"    battery {pct:5.1f}%   [{stage}]")

    def table(self) -> str:
        if len(self.rows) < 2:
            return "not enough readings"
        out = [f"{'stage':<26}{'elapsed':>9}{'batt%':>8}{'drop':>7}"]
        out.append("-" * 50)
        for i, (stage, el, pct) in enumerate(self.rows):
            drop = "" if i == 0 else f"{self.rows[i-1][2] - pct:+.0f}"
            out.append(f"{stage:<26}{el/60:8.1f}m{pct:8.1f}{drop:>7}")
        total = self.rows[0][2] - self.rows[-1][2]
        mins = self.rows[-1][1] / 60
        out.append("-" * 50)
        out.append(f"total: {total:.0f}% over {mins:.1f} min")
        if total > 0 and mins > 0:
            rate = total / (mins / 60)
            out.append(f"rate : {rate:.0f} %/hour  ->  "
                       f"~{100/rate:.1f} h from full at this load")
        return "\n".join(out)


async def battery(frame: BrilliantMsg) -> float:
    try:
        r = await frame.ble.send_lua("print(frame.battery_level())",
                                     await_print=True, timeout=12)
        return float(str(r).strip())
    except Exception:
        return float("nan")


async def connect_with_retry(frame: BrilliantMsg, attempts: int = 20) -> bool:
    for i in range(1, attempts + 1):
        try:
            await frame.connect()
            return True
        except Exception as exc:
            log(f"  connect {i}/{attempts}: {exc}")
            await asyncio.sleep(3)
    return False


async def stage_display(frame: BrilliantMsg, marks: Marks) -> None:
    log("STAGE 1/5  display: palette, brightness, bitmap")
    lua = frame.ble.send_lua
    colours = ["WHITE", "RED", "GREEN", "SKYBLUE", "ORANGE", "YELLOW"]
    for i, c in enumerate(colours):
        await lua(f"frame.display.text('{c}',60,{1 + i * 48},{{color='{c}'}})",
                  await_print=False)
        await asyncio.sleep(0.15)
    await lua("frame.display.show();print('ok')", await_print=True, timeout=12)
    await asyncio.sleep(1.5)
    for b in (-2, 0, 2):
        await lua(f"frame.display.set_brightness({b})", await_print=False)
        await asyncio.sleep(0.4)
    await lua("frame.display.set_brightness(0);print('ok')", await_print=True, timeout=12)
    marks.add("display", await battery(frame))


async def stage_imu(frame: BrilliantMsg, marks: Marks) -> None:
    log("STAGE 2/5  IMU: orientation + raw, 5 samples")
    for _ in range(5):
        r = await frame.ble.send_lua(
            "local d=frame.imu.direction() local a=frame.imu.raw() "
            "print(string.format('roll %.0f pitch %.0f | accel %.0f,%.0f,%.0f',"
            "d.roll,d.pitch,a.accelerometer.x,a.accelerometer.y,a.accelerometer.z))",
            await_print=True, timeout=12)
        log(f"    {r}")
        await asyncio.sleep(0.8)
    marks.add("IMU", await battery(frame))


async def stage_camera(frame: BrilliantMsg, marks: Marks, frames: int) -> None:
    log(f"STAGE 3/5  camera: {frames} frames at 720px VERY_HIGH")
    rx = RxPhoto()
    await frame.upload_stdlua_libs(lib_names=["data", "camera"])
    await frame.upload_frame_app(local_filename=str(LUA / "camera_frame_app.lua"))
    frame.attach_print_response_handler()
    await frame.start_frame_app()
    q = await rx.attach(frame)
    try:
        settings = TxCaptureSettings(resolution=720, quality_index=4).pack()
        await asyncio.sleep(4.0)                      # let auto-exposure settle
        out = Path("out/workout")
        out.mkdir(parents=True, exist_ok=True)
        total = 0
        for i in range(frames):
            t0 = time.monotonic()
            await frame.send_message(CAPTURE_MSG, settings)
            jpeg = await asyncio.wait_for(q.get(), timeout=30.0)
            dt = time.monotonic() - t0
            total += len(jpeg)
            (out / f"w_{i+1:02d}.jpg").write_bytes(jpeg)
            try:
                from PIL import Image, ImageStat
                st = ImageStat.Stat(Image.open(io.BytesIO(jpeg)).convert("L"))
                extra = f"  luma {st.mean[0]:.0f}+/-{st.stddev[0]:.0f}"
            except Exception:
                extra = ""
            log(f"    #{i+1}  {len(jpeg)/1024:.1f} kB  {dt:.2f}s{extra}")
        log(f"    {total/1024:.1f} kB total")
    finally:
        rx.detach(frame)
        frame.detach_print_response_handler()
        await frame.stop_frame_app()
    marks.add("camera", await battery(frame))


async def stage_audio(frame: BrilliantMsg, marks: Marks, secs: float) -> None:
    log(f"STAGE 4/5  microphone: {secs:.0f}s at 8kHz/16bit")
    rx = RxAudio(streaming=True)
    await frame.upload_stdlua_libs(lib_names=["data", "audio"])
    await frame.upload_frame_app(local_filename=str(LUA / "audio_frame_app.lua"))
    frame.attach_print_response_handler()
    await frame.start_frame_app()
    q = await rx.attach(frame)
    try:
        await frame.send_message(AUDIO_CTRL_MSG, bytes([1, 0, 16]))
        t0 = time.monotonic()
        got = 0
        while time.monotonic() - t0 < secs:
            try:
                chunk = await asyncio.wait_for(q.get(), timeout=5.0)
            except asyncio.TimeoutError:
                break
            if chunk is None:
                break
            got += len(chunk)
        await frame.send_message(AUDIO_CTRL_MSG, bytes([0, 0, 16]))
        el = time.monotonic() - t0
        log(f"    {got/1024:.1f} kB in {el:.1f}s = {got/el/1000:.1f} kB/s "
            f"({got/el/16000*100:.0f}% of realtime)")
    finally:
        rx.detach(frame)
        frame.detach_print_response_handler()
        await frame.stop_frame_app()
    marks.add("microphone", await battery(frame))


async def stage_text(frame: BrilliantMsg, marks: Marks) -> None:
    log("STAGE 5/5  message layer: TxPlainText")
    await frame.upload_stdlua_libs(lib_names=["data", "plain_text"])
    await frame.upload_frame_app(local_filename=str(LUA / "plain_text_frame_app.lua"))
    frame.attach_print_response_handler()
    await frame.start_frame_app()
    try:
        for i, t in enumerate(("ON BATTERY", "ALL SYSTEMS GO", "v25.278.1113")):
            await frame.send_message(TEXT_MSG,
                                     TxPlainText(text=t, x=60, y=60 + i * 60).pack())
            await asyncio.sleep(1.5)
    finally:
        frame.detach_print_response_handler()
        await frame.stop_frame_app()
    marks.add("message layer", await battery(frame))


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()
    frames = 3 if args.quick else 6
    audio_secs = 8.0 if args.quick else 20.0

    frame = BrilliantMsg()
    log("connecting -- glasses should be UNDOCKED")
    if not await connect_with_retry(frame):
        log("could not connect")
        return 2

    marks = Marks()
    try:
        await frame.ble.send_break_signal()
        await asyncio.sleep(0.5)
        await frame.ble.drain_print_channel()
        fw = await frame.ble.send_lua("print(frame.FIRMWARE_VERSION)",
                                      await_print=True, timeout=12)
        log(f"firmware {fw}   payload {frame.ble.max_lua_payload()}")
        marks.add("start", await battery(frame))

        await stage_display(frame, marks)
        await stage_imu(frame, marks)
        await stage_camera(frame, marks, frames)
        await stage_audio(frame, marks, audio_secs)
        await stage_text(frame, marks)

        log("\n" + marks.table())
        return 0
    except Exception as exc:
        log(f"\nSTOPPED: {type(exc).__name__}: {exc}")
        log("(a dropped link here usually means the battery gave out)")
        log("\n" + marks.table())
        return 1
    finally:
        try:
            await park(frame)
            await frame.disconnect()
        except Exception:
            pass
        log("disconnected")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
