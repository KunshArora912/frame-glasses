"""Live camera stream from Frame to a window on this laptop.

Requests a frame, waits for the JPEG, shows it, repeats. The request/response
loop is its own flow control -- there is no point running ahead of the link,
which sustains ~25 kB/s measured (the docs' ~40 is optimistic).

    .venv/Scripts/python.exe app/live_camera.py
    .venv/Scripts/python.exe app/live_camera.py --resolution 512 --quality 4
    .venv/Scripts/python.exe app/live_camera.py --frames 20 --no-window --save out/

Counter-intuitively, shrinking the image barely helps. MEASURED on this unit:

    256px MEDIUM     2.0 kB/frame   1.68 fps    3.4 kB/s
    512px VERY_HIGH  6.8 kB/frame   1.44 fps   10.0 kB/s

A 3.4x larger image costs only 14% of the frame rate, because the limit is
per-frame LATENCY (capture + device loop + round trip, ~0.65 s), not
bandwidth -- 10 kB/s is nowhere near the ~25 kB/s the link sustains. So the
defaults here are 512px VERY_HIGH: the quality is nearly free.

To actually gain frame rate, attack latency (LOOP_SLEEP in the Lua app), not
image size. Only at 720px VERY_HIGH (~50-100 kB) does bandwidth start to bite.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import time
from pathlib import Path

from brilliant_msg import BrilliantMsg, RxPhoto, TxCaptureSettings
import sys
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
from park import park

# must match CAPTURE_SETTINGS_MSG in lua/camera_frame_app.lua
CAPTURE_SETTINGS_MSG = 0x0D

LUA_APP = Path(__file__).parent / "lua" / "camera_frame_app.lua"

QUALITY_NAMES = ["VERY_LOW", "LOW", "MEDIUM", "HIGH", "VERY_HIGH"]


def brightness(jpeg: bytes) -> str:
    """Mean/stddev luma -- makes auto-exposure convergence visible.

    A near-black frame reads as mean~14 stddev~2; a correctly exposed indoor
    scene is usually mean 60-160 with stddev well above 20.
    """
    try:
        from PIL import Image, ImageStat

        stat = ImageStat.Stat(Image.open(io.BytesIO(jpeg)).convert("L"))
        return f"luma {stat.mean[0]:.0f}+/-{stat.stddev[0]:.0f}"
    except Exception:
        return ""


class Viewer:
    """Minimal Tk viewer. Uses pillow + tkinter, both already present."""

    def __init__(self, title: str = "Frame camera") -> None:
        import tkinter as tk
        from PIL import ImageTk  # noqa: F401  (import proves availability)

        self._tk = tk
        self.root = tk.Tk()
        self.root.title(title)
        self.label = tk.Label(self.root)
        self.label.pack()
        self.status = tk.Label(self.root, text="waiting for first frame...", anchor="w")
        self.status.pack(fill="x")
        self._photo = None
        self.closed = False
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self) -> None:
        self.closed = True

    def pump(self) -> None:
        """Keep Tk responsive between frames so X is noticed promptly."""
        try:
            self.root.update_idletasks()
            self.root.update()
        except Exception:
            self.closed = True

    def show(self, jpeg: bytes, status: str) -> None:
        from PIL import Image, ImageTk

        img = Image.open(io.BytesIO(jpeg))
        self._photo = ImageTk.PhotoImage(img)
        self.label.configure(image=self._photo)
        self.status.configure(text=status)
        self.root.update_idletasks()
        self.root.update()

    def close(self) -> None:
        try:
            self.root.destroy()
        except Exception:
            pass


async def connect_with_retry(frame: BrilliantMsg, attempts: int = 8) -> bool:
    """Frame sleeps into SYSTEM OFF and is then unreachable; and the bond
    typically lands only after ~3 rejected attempts. So: retry."""
    for attempt in range(1, attempts + 1):
        try:
            await frame.connect()
            return True
        except Exception as exc:
            print(f"  connect attempt {attempt}/{attempts}: {exc}")
            if attempt < attempts:
                await asyncio.sleep(2.0)
    return False


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resolution", type=int, default=512,
                        help="even, 100-720 (default 512 -- see note below)")
    parser.add_argument("--quality", type=int, default=4, choices=range(5),
                        help="0=VERY_LOW .. 4=VERY_HIGH (default 4)")
    parser.add_argument("--pan", type=int, default=0, help="-140..140")
    parser.add_argument("--frames", type=int, default=0, help="0 = until window closed")
    parser.add_argument("--timeout", type=float, default=20.0, help="per-frame timeout (s)")
    parser.add_argument("--no-window", action="store_true", help="headless, stats only")
    parser.add_argument("--save", type=Path, default=None, help="directory to write JPEGs")
    parser.add_argument("--warmup", type=float, default=3.0,
                        help="seconds to let auto-exposure converge first (default 3)")
    args = parser.parse_args()

    if args.resolution % 2 or not (100 <= args.resolution <= 720):
        parser.error("--resolution must be even and between 100 and 720")
    if args.save:
        args.save.mkdir(parents=True, exist_ok=True)

    viewer = None
    if not args.no_window:
        try:
            viewer = Viewer()
        except Exception as exc:
            print(f"(no window: {type(exc).__name__}: {exc}) -- continuing headless")

    frame = BrilliantMsg()
    if not await connect_with_retry(frame):
        print("\nCould not connect. Tap the glasses to wake them (they sleep into")
        print("SYSTEM OFF and are unreachable), then retry. See diagnose.py.")
        if viewer:
            viewer.close()
        return

    rx_photo = RxPhoto()
    attached = False
    try:
        print(f"connected (type={frame.ble.type}, max_payload={frame.ble.max_data_payload()})")
        await frame.print_short_text("Camera starting...")
        await frame.upload_stdlua_libs(lib_names=["data", "camera"])
        await frame.upload_frame_app(local_filename=str(LUA_APP))
        frame.attach_print_response_handler()
        await frame.start_frame_app()

        photo_queue = await rx_photo.attach(frame)
        attached = True

        settings = TxCaptureSettings(
            resolution=args.resolution, quality_index=args.quality, pan=args.pan
        ).pack()

        # Auto-exposure converges over many iterations on the device. Capturing
        # immediately after start gives near-black frames (mean ~14/255), so let
        # the device-side loop step the algorithm before the first request.
        if args.warmup > 0:
            print(f"warming up auto-exposure for {args.warmup:.1f}s...")
            await asyncio.sleep(args.warmup)

        print(
            f"\nstreaming {args.resolution}x{args.resolution} "
            f"{QUALITY_NAMES[args.quality]}  (Ctrl-C to stop)\n"
        )

        n = 0
        total_bytes = 0
        t_start = time.monotonic()

        while True:
            if viewer is not None and viewer.closed:
                print("window closed")
                break
            if args.frames and n >= args.frames:
                break

            if viewer is not None:
                # otherwise a window close is only noticed when a frame lands
                viewer.pump()

            t0 = time.monotonic()
            await frame.send_message(CAPTURE_SETTINGS_MSG, settings)
            try:
                jpeg = await asyncio.wait_for(photo_queue.get(), timeout=args.timeout)
            except asyncio.TimeoutError:
                print(f"frame {n + 1}: timed out after {args.timeout}s")
                break

            dt = time.monotonic() - t0
            n += 1
            total_bytes += len(jpeg)
            elapsed = time.monotonic() - t_start
            status = (
                f"#{n}  {len(jpeg) / 1024:.1f} kB  {dt:.2f}s  "
                f"{len(jpeg) / dt / 1024:.1f} kB/s  avg {n / elapsed:.2f} fps"
                f"  {brightness(jpeg)}"
            )
            print(status)

            if args.save:
                (args.save / f"frame_{n:04d}.jpg").write_bytes(jpeg)
            if viewer is not None:
                try:
                    viewer.show(jpeg, status)
                except Exception as exc:
                    print(f"(viewer error: {type(exc).__name__}: {exc})")
                    viewer.closed = True

        if n:
            elapsed = time.monotonic() - t_start
            print(
                f"\n{n} frames in {elapsed:.1f}s -- {n / elapsed:.2f} fps, "
                f"{total_bytes / 1024:.1f} kB total, "
                f"{total_bytes / elapsed / 1024:.1f} kB/s sustained"
            )
    except KeyboardInterrupt:
        print("\ninterrupted")
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}")
    finally:
        # detach unconditionally: unregister is a safe no-op for an
        # unknown subscriber, and the flag could leave one attached
        rx_photo.detach(frame)
        try:
            frame.detach_print_response_handler()
            await frame.stop_frame_app()
        except Exception:
            pass
        await park(frame)
        await frame.disconnect()
        if viewer:
            viewer.close()
        print("disconnected")


if __name__ == "__main__":
    asyncio.run(main())
