"""Run every remaining Frame SDK demo back to back.

Covers the examples the live camera/audio programs don't: sprites, sprite
movement, rasterised text blocks, IMU streaming, tap events, camera exposure
telemetry, and photographing the room then displaying it back on the glasses.

    python showcase.py              # everything
    python showcase.py --skip tap   # omit the interactive stage

Each stage is independent: one failing does not stop the rest.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import math
import time
from datetime import datetime
from pathlib import Path

from brilliant_msg import (BrilliantMsg, RxAutoExpResult, RxIMU, RxMeteringData,
                           RxPhoto, RxTap, TxAutoExpSettings, TxCaptureSettings,
                           TxCode, TxManualExpSettings, TxSprite, TxSpriteCoords,
                           TxTextSpriteBlock)
from park import park

SPRITE_MSG = 0x20
COORDS_MSG = 0x21
TEXT_BLOCK_MSG = 0x23
IMU_CTRL_MSG = 0x40
TAP_CTRL_MSG = 0x41
CAPTURE_MSG = 0x0D
AUTOEXP_SET_MSG = 0x0E
MANUALEXP_SET_MSG = 0x0F
EXP_CTRL_MSG = 0x42

LUA_APP = Path(__file__).parent / "app" / "lua" / "showcase_frame_app.lua"
STD_LIBS = ["data", "sprite", "sprite_coords", "text_sprite_block",
            "imu", "tap", "camera", "code"]


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def make_target_png(size: int = 120) -> bytes:
    """A concentric-ring target as a 4-colour indexed PNG.

    Generated rather than shipped so the demo has no asset dependencies, and
    rings make sprite movement obvious on a 640x400 display.
    """
    from PIL import Image

    img = Image.new("P", (size, size), 0)
    img.putpalette([0, 0, 0, 255, 255, 255, 255, 165, 0, 135, 206, 235] + [0] * 244)
    px = img.load()
    c = size / 2
    for y in range(size):
        for x in range(size):
            d = math.hypot(x - c, y - c)
            if d > c:
                px[x, y] = 0
            else:
                px[x, y] = 1 + int(d / (c / 3)) % 3
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


async def connect(frame: BrilliantMsg, attempts: int = 20) -> bool:
    for i in range(1, attempts + 1):
        try:
            await frame.connect()
            return True
        except Exception as exc:
            log(f"  connect {i}/{attempts}: {exc}")
            await asyncio.sleep(3)
    return False


# --------------------------------------------------------------- stages

async def stage_sprite(frame: BrilliantMsg) -> str:
    png = make_target_png()
    sprite = TxSprite.from_indexed_png_bytes(png)
    log(f"  sprite {sprite.width}x{sprite.height}, {sprite.num_colors} colours, "
        f"{sprite.bpp}bpp")
    await frame.send_message(SPRITE_MSG, sprite.pack())
    await asyncio.sleep(2.0)
    return f"{sprite.width}x{sprite.height} {sprite.num_colors}-colour sprite shown"


async def stage_sprite_move(frame: BrilliantMsg) -> str:
    """Move the already-sent sprite without resending the pixels."""
    path = [(1, 1), (260, 1), (500, 1), (500, 140), (500, 270),
            (260, 270), (1, 270), (1, 140), (260, 140)]
    for x, y in path:
        await frame.send_message(COORDS_MSG,
                                 TxSpriteCoords(code=SPRITE_MSG, x=x, y=y).pack())
        await asyncio.sleep(0.45)
    return f"sprite moved through {len(path)} positions (coords only, no pixels)"


async def stage_text_block(frame: BrilliantMsg) -> str:
    text = "Frame SDK\nrasterised text\n0123456789\nv25.278.1113"
    tsb = TxTextSpriteBlock(width=560, line_height=40, font_size=32,
                            max_display_lines=4, text=text)
    # create_text_sprites(text) returns the rendered sprites; the block
    # header goes first, then each sprite under the same message code.
    sprites = tsb.create_text_sprites(text)
    await frame.send_message(TEXT_BLOCK_MSG, tsb.pack())
    await asyncio.sleep(0.4)
    for spr in sprites:
        await frame.send_message(TEXT_BLOCK_MSG, spr.pack())
        await asyncio.sleep(0.25)
    await asyncio.sleep(1.5)
    return f"rasterised {len(sprites)} text sprites"


async def stage_imu(frame: BrilliantMsg, secs: float) -> str:
    rx = RxIMU(smoothing_samples=3)
    q = await rx.attach(frame)
    try:
        await frame.send_message(IMU_CTRL_MSG, TxCode(value=1).pack())
        log(f"  move the glasses around for {secs:.0f}s...")
        t0 = time.monotonic()
        n = 0
        last = None
        while time.monotonic() - t0 < secs:
            try:
                d = await asyncio.wait_for(q.get(), timeout=4.0)
            except asyncio.TimeoutError:
                break
            n += 1
            last = d
            if n % 10 == 0:
                try:
                    log(f"    pitch {d.pitch:7.1f}  roll {d.roll:7.1f}  "
                        f"compass {d.compass}")
                except Exception:
                    log(f"    {d}")
        await frame.send_message(IMU_CTRL_MSG, TxCode(value=0).pack())
        rate = n / max(time.monotonic() - t0, 1e-6)
        return f"{n} IMU samples ({rate:.1f}/s); last={last}"
    finally:
        rx.detach(frame)


async def stage_tap(frame: BrilliantMsg, secs: float) -> str:
    rx = RxTap(threshold=0.3)
    q = await rx.attach(frame)
    try:
        await frame.send_message(TAP_CTRL_MSG, TxCode(value=1).pack())
        log(f"  >>> TAP THE GLASSES for {secs:.0f}s <<<")
        t0 = time.monotonic()
        taps = 0
        while time.monotonic() - t0 < secs:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            taps += 1
            log(f"    tap #{taps}: {ev}")
        await frame.send_message(TAP_CTRL_MSG, TxCode(value=0).pack())
        return f"{taps} debounced tap events (RxTap threshold 0.3)"
    finally:
        rx.detach(frame)


async def stage_camera_sprite(frame: BrilliantMsg) -> str:
    """Photograph the room, then show it back on the glasses' own display."""
    rx = RxPhoto()
    q = await rx.attach(frame)
    try:
        await asyncio.sleep(3.0)                       # auto-exposure settle
        await frame.send_message(
            CAPTURE_MSG,
            TxCaptureSettings(resolution=512, quality_index=4).pack())
        jpeg = await asyncio.wait_for(q.get(), timeout=30.0)
        log(f"  captured {len(jpeg)/1024:.1f} kB")
        Path("out").mkdir(exist_ok=True)
        Path("out/showcase_capture.jpg").write_bytes(jpeg)

        # quantise to the display's 16-colour palette and send it back
        sprite = TxSprite.from_image_bytes(jpeg, max_pixels=40000)
        log(f"  quantised to {sprite.width}x{sprite.height}, "
            f"{sprite.num_colors} colours, {sprite.bpp}bpp")
        await frame.send_message(SPRITE_MSG, sprite.pack())
        await asyncio.sleep(3.0)
        return (f"photo {len(jpeg)/1024:.1f} kB -> "
                f"{sprite.width}x{sprite.height} sprite on the glasses")
    finally:
        rx.detach(frame)


async def stage_exposure(frame: BrilliantMsg, secs: float = 8.0) -> str:
    """Exposure + metering telemetry via the message layer.

    NOT via send_lua: the showcase frame app owns stdout, so an awaited
    REPL print never comes back while an app is running. That was the
    original failure here.
    """
    rx_exp = RxAutoExpResult()
    rx_met = RxMeteringData()
    q_exp = await rx_exp.attach(frame)
    q_met = await rx_met.attach(frame)
    try:
        await frame.send_message(AUTOEXP_SET_MSG,
                                 TxAutoExpSettings(metering_index=1, exposure=0.12).pack())
        await asyncio.sleep(0.3)
        await frame.send_message(EXP_CTRL_MSG, TxCode(value=1).pack())

        t0 = time.monotonic()
        n_exp = n_met = 0
        last_exp = last_met = None
        while time.monotonic() - t0 < secs:
            try:
                last_exp = await asyncio.wait_for(q_exp.get(), timeout=3.0)
                n_exp += 1
                if n_exp % 8 == 0:
                    log(f"    shutter {last_exp['shutter']:.0f}  "
                        f"gain {last_exp['analog_gain']:.1f}  "
                        f"err {last_exp['error']:+.3f}")
            except asyncio.TimeoutError:
                pass
            try:
                last_met = q_met.get_nowait()
                n_met += 1
            except asyncio.QueueEmpty:
                pass

        await frame.send_message(EXP_CTRL_MSG, TxCode(value=0).pack())
        await asyncio.sleep(0.3)

        # and prove manual control lands too
        await frame.send_message(
            MANUALEXP_SET_MSG,
            TxManualExpSettings(manual_shutter=8000, manual_analog_gain=64).pack())
        await asyncio.sleep(0.5)

        log(f"    metering sample: {last_met}")
        return (f"{n_exp} autoexp results, {n_met} metering samples; "
                f"shutter={last_exp['shutter']:.0f} gain={last_exp['analog_gain']:.1f}"
                if last_exp else f"{n_exp} autoexp, {n_met} metering")
    finally:
        rx_exp.detach(frame)
        rx_met.detach(frame)


# ------------------------------------------------------------------ main

async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--skip", nargs="*", default=[],
                    help="stage names to skip (tap, imu, camera, ...)")
    ap.add_argument("--imu-secs", type=float, default=12.0)
    ap.add_argument("--tap-secs", type=float, default=12.0)
    args = ap.parse_args()

    frame = BrilliantMsg()
    log("connecting...")
    if not await connect(frame):
        log("could not connect -- tap the glasses or dock/undock them")
        return 2

    results: list[tuple[str, bool, str]] = []
    try:
        log(f"connected (payload {frame.ble.max_lua_payload()})")
        await frame.ble.send_break_signal()
        await asyncio.sleep(0.5)
        await frame.ble.drain_print_channel()

        log("uploading showcase app...")
        await frame.upload_stdlua_libs(lib_names=STD_LIBS)
        await frame.upload_frame_app(local_filename=str(LUA_APP))
        frame.attach_print_response_handler()
        await frame.start_frame_app()

        stages = [
            ("sprite", stage_sprite(frame)),
            ("sprite-move", stage_sprite_move(frame)),
            ("text-block", stage_text_block(frame)),
            ("imu", stage_imu(frame, args.imu_secs)),
            ("tap", stage_tap(frame, args.tap_secs)),
            ("exposure", stage_exposure(frame, 8.0)),
            ("camera-sprite", stage_camera_sprite(frame)),
        ]

        for i, (name, coro) in enumerate(stages, 1):
            if name in args.skip:
                coro.close()
                results.append((name, True, "skipped"))
                log(f"\nSTAGE {i}/{len(stages)}  {name}: skipped")
                continue
            log(f"\nSTAGE {i}/{len(stages)}  {name}")
            try:
                out = await coro
                results.append((name, True, out))
                log(f"  OK: {out}")
            except Exception as exc:
                results.append((name, False, f"{type(exc).__name__}: {exc}"))
                log(f"  FAILED: {type(exc).__name__}: {exc}")

        print("\n" + "=" * 62)
        for name, ok, detail in results:
            print(f"  [{'OK  ' if ok else 'FAIL'}] {name:<15}{detail}")
        print("=" * 62)
        failed = sum(1 for _, ok, _ in results if not ok)
        print(f"  {len(results) - failed}/{len(results)} stages succeeded")
        return 1 if failed else 0
    finally:
        try:
            frame.detach_print_response_handler()
            await frame.stop_frame_app()
        except Exception:
            pass
        await park(frame)
        await frame.disconnect()
        log("disconnected")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
