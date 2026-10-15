"""Live audio + video stream from Frame: speakers plus a camera window.

Continuous microphone audio, with a photo every few seconds, sharing the one
Bluetooth link.

    .venv/Scripts/python.exe app/live_av.py
    .venv/Scripts/python.exe app/live_av.py --seconds 30 --photo-interval 2
    .venv/Scripts/python.exe app/live_av.py --no-play --no-window --seconds 20

Measured link budget: ~25 kB/s sustained, NOT the 40 kB/s theoretical max.
  8 kHz x 16 bit audio  = 16 kB/s   <- default, runs at 100% of realtime
  256px MEDIUM JPEG     = ~2 kB each
So a photo every 2-3s costs under 1 kB/s on average and fits alongside audio.
16 kHz x 16 bit audio alone is 32 kB/s and already drops to ~78% of realtime,
so do not combine that with video.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import time
from pathlib import Path

from brilliant_msg import (BrilliantMsg, RxAudio, RxPhoto, TxCaptureSettings)
import sys
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
from park import park

try:
    from audio_out import Player
except ImportError:
    from app.audio_out import Player

AUDIO_CTRL_MSG = 0x30
CAPTURE_SETTINGS_MSG = 0x0D

LUA_APP = Path(__file__).parent / "lua" / "av_frame_app.lua"
QUALITY_NAMES = ["VERY_LOW", "LOW", "MEDIUM", "HIGH", "VERY_HIGH"]


def control(start: bool, rate: int, depth: int) -> bytes:
    return bytes([1 if start else 0, 1 if rate == 16000 else 0, depth])


class Viewer:
    def __init__(self, title: str = "Frame A/V") -> None:
        import tkinter as tk
        from PIL import ImageTk  # noqa: F401

        self.root = tk.Tk()
        self.root.title(title)
        self.label = tk.Label(self.root)
        self.label.pack()
        self.status = tk.Label(self.root, text="waiting...", anchor="w")
        self.status.pack(fill="x")
        self._photo = None
        self.closed = False
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    def _close(self) -> None:
        self.closed = True

    def pump(self) -> None:
        """Keep Tk responsive between frames."""
        try:
            self.root.update_idletasks()
            self.root.update()
        except Exception:
            self.closed = True

    def show(self, jpeg: bytes, status: str) -> None:
        from PIL import Image, ImageTk

        self._photo = ImageTk.PhotoImage(Image.open(io.BytesIO(jpeg)))
        self.label.configure(image=self._photo)
        self.status.configure(text=status)
        self.pump()

    def close(self) -> None:
        try:
            self.root.destroy()
        except Exception:
            pass


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
    parser.add_argument("--rate", type=int, default=8000, choices=(8000, 16000))
    parser.add_argument("--depth", type=int, default=16, choices=(8, 16))
    parser.add_argument("--resolution", type=int, default=256)
    parser.add_argument("--quality", type=int, default=2, choices=range(5))
    parser.add_argument("--photo-interval", type=float, default=3.0,
                        help="seconds between photos (default 3)")
    parser.add_argument("--seconds", type=float, default=20.0, help="0 = until Ctrl-C")
    parser.add_argument("--no-play", action="store_true")
    parser.add_argument("--no-window", action="store_true")
    parser.add_argument("--wav", type=Path, default=Path("out/av.wav"))
    parser.add_argument("--save", type=Path, default=None)
    parser.add_argument("--prime-ms", type=int, default=700,
                        help="playback jitter buffer, ms (default 700)")
    args = parser.parse_args()

    byte_rate = args.rate * args.depth // 8
    print(f"audio {args.rate}Hz/{args.depth}bit = {byte_rate / 1000:.0f} kB/s, "
          f"photo {args.resolution}px {QUALITY_NAMES[args.quality]} "
          f"every {args.photo_interval:.0f}s  (link sustains ~25 kB/s)\n")
    if args.save:
        args.save.mkdir(parents=True, exist_ok=True)

    viewer = None
    if not args.no_window:
        try:
            viewer = Viewer()
        except Exception as exc:
            print(f"(no window: {type(exc).__name__}: {exc})")

    frame = BrilliantMsg()
    if not await connect_with_retry(frame):
        print("\nCould not connect. Tap the glasses to wake them, then retry.")
        if viewer:
            viewer.close()
        return 2

    rx_audio = RxAudio(streaming=True)
    rx_photo = RxPhoto()
    attached = False
    player = None
    chunks: list[bytes] = []

    try:
        print(f"connected (type={frame.ble.type})")
        await frame.upload_stdlua_libs(lib_names=["data", "audio", "camera"])
        await frame.upload_frame_app(local_filename=str(LUA_APP))
        frame.attach_print_response_handler()
        await frame.start_frame_app()

        audio_queue = await rx_audio.attach(frame)
        photo_queue = await rx_photo.attach(frame)
        attached = True

        if not args.no_play:
            try:
                # Bigger jitter buffer than audio-alone: a camera capture
                # blocks the device-side audio pump for its whole transfer,
                # and a 250ms buffer underruns across that gap.
                player = Player(args.rate, args.depth, prime_ms=args.prime_ms)
                print(f"live playback on ({player.latency_ms:.0f} ms latency)")
            except Exception as exc:
                print(f"(no playback: {type(exc).__name__}: {exc})")

        cam_settings = TxCaptureSettings(
            resolution=args.resolution, quality_index=args.quality
        ).pack()

        await frame.send_message(AUDIO_CTRL_MSG, control(True, args.rate, args.depth))

        limit = args.seconds if args.seconds > 0 else float("inf")
        print(f"\nstreaming {'until Ctrl-C' if limit == float('inf') else f'{limit:.0f}s'}...\n")

        t0 = time.monotonic()
        audio_bytes = 0
        photos = 0
        photo_bytes = 0
        next_photo = t0 + 1.0          # let audio settle first
        last_report = t0
        pending_photo = False

        while True:
            now = time.monotonic()
            if now - t0 >= limit:
                break
            if viewer is not None and viewer.closed:
                print("window closed")
                break

            # request a photo on schedule
            if not pending_photo and now >= next_photo:
                await frame.send_message(CAPTURE_SETTINGS_MSG, cam_settings)
                pending_photo = True

            # drain audio without blocking
            drained = 0
            while True:
                try:
                    chunk = audio_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if chunk is None:
                    print("  (audio end-of-stream)")
                    break
                chunks.append(chunk)
                audio_bytes += len(chunk)
                drained += len(chunk)
                if player:
                    try:
                        player.write(chunk)
                    except Exception:
                        player.close()
                        player = None

            # collect a photo if one has arrived
            try:
                jpeg = photo_queue.get_nowait()
            except asyncio.QueueEmpty:
                jpeg = None
            if jpeg is not None:
                photos += 1
                photo_bytes += len(jpeg)
                pending_photo = False
                next_photo = time.monotonic() + args.photo_interval
                if args.save:
                    (args.save / f"av_{photos:04d}.jpg").write_bytes(jpeg)
                if viewer is not None:
                    try:
                        viewer.show(jpeg, f"photo #{photos}  {len(jpeg) / 1024:.1f} kB")
                    except Exception:
                        viewer.closed = True

            if viewer is not None:
                viewer.pump()

            now = time.monotonic()
            if now - last_report >= 2.0:
                el = now - t0
                print(f"  t={el:4.1f}s  audio {audio_bytes / 1024:6.1f} kB "
                      f"({audio_bytes / el / byte_rate * 100:3.0f}% realtime)  "
                      f"photos {photos} ({photo_bytes / 1024:.1f} kB)  "
                      f"total {(audio_bytes + photo_bytes) / el / 1000:.1f} kB/s")
                last_report = now

            if drained == 0:
                await asyncio.sleep(0.005)

        await frame.send_message(AUDIO_CTRL_MSG, control(False, args.rate, args.depth))
        await asyncio.sleep(0.5)

        el = time.monotonic() - t0
        pcm = b"".join(chunks)
        print(f"\n--- {el:.1f}s ---")
        print(f"audio : {len(pcm) / 1024:.1f} kB, {len(pcm) / el / 1000:.1f} kB/s, "
              f"{len(pcm) / el / byte_rate * 100:.0f}% of realtime")
        print(f"photos: {photos} ({photo_bytes / 1024:.1f} kB, "
              f"{photos / el:.2f}/s)")
        print(f"link  : {(len(pcm) + photo_bytes) / el / 1000:.1f} kB/s combined")

        if pcm:
            # to_wav_bytes handles the 8-bit signed->unsigned fixup itself.
            wav = RxAudio.to_wav_bytes(pcm, sample_rate=args.rate,
                                       bits_per_sample=args.depth, channels=1)
            args.wav.parent.mkdir(parents=True, exist_ok=True)
            args.wav.write_bytes(wav)
            print(f"wrote {args.wav} ({len(pcm) / byte_rate:.1f}s of audio)")
        return 0
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 0
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}")
        return 1
    finally:
        try:
            await frame.send_message(AUDIO_CTRL_MSG,
                                     control(False, args.rate, args.depth))
        except Exception:
            pass
        if player:
            if player.underruns:
                print(f"({player.underruns} playback underruns)")
            player.close()
        # detach unconditionally -- unregister is a safe no-op, and the
        # flag could leak a handler if the second attach raised
        rx_audio.detach(frame)
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
    raise SystemExit(asyncio.run(main()))
