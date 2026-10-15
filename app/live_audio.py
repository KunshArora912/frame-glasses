"""Live microphone stream from Frame to this laptop's speakers.

Plays audio as it arrives and writes a WAV on exit.

    .venv/Scripts/python.exe app/live_audio.py                  # 5s clip, 8kHz/16bit
    .venv/Scripts/python.exe app/live_audio.py --seconds 15
    .venv/Scripts/python.exe app/live_audio.py --rate 16000 --no-play
    .venv/Scripts/python.exe app/live_audio.py --seconds 0      # until Ctrl-C

Byte rate is rate * depth / 8. Measured sustained link throughput is
~25 kB/s, not the 40 kB/s the docs imply:

    8000 Hz x  8 bit =  8 kB/s   leaves the most room for video
    8000 Hz x 16 bit = 16 kB/s   default; measured 100% of realtime
   16000 Hz x  8 bit = 16 kB/s   same rate, but no aliasing (see below)
   16000 Hz x 16 bit = 32 kB/s   measured only ~78% of realtime -> drops

Firmware facts worth knowing (from lua_libraries/microphone.c):
  * The PDM always runs at 16 kHz into a 32768-sample int16 FIFO, so the
    buffer is ALWAYS 2.048 s regardless of the rate/depth you pick. That is
    the longest the device may go without a read() before it overflows --
    silently, and it never recovers to realtime afterwards.
  * 8 kHz is naive decimation with NO anti-alias filter (the firmware says
    so), so content above 4 kHz folds back into the band. At equal byte
    rate, 16000/8 is cleaner than 8000/16.
  * 8-bit is just the top byte of the int16 -- signed, no dither, ~48 dB.

Samples arrive as signed PCM, which sounddevice's int8/int16 dtypes take
verbatim. RxAudio.to_wav_bytes does the signed->unsigned fixup that 8-bit
WAV needs, so never offset the samples yourself.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

from brilliant_msg import BrilliantMsg, RxAudio
import sys
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
from park import park

try:
    from audio_out import Player
except ImportError:
    from app.audio_out import Player

# must match AUDIO_CTRL_MSG in lua/audio_frame_app.lua
AUDIO_CTRL_MSG = 0x30

LUA_APP = Path(__file__).parent / "lua" / "audio_frame_app.lua"


def control(start: bool, rate: int, depth: int) -> bytes:
    """[command, rate_code, bit_depth] -- see audio_frame_app.lua"""
    return bytes([1 if start else 0, 1 if rate == 16000 else 0, depth])


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
    parser.add_argument("--seconds", type=float, default=5.0,
                        help="0 = stream until Ctrl-C")
    parser.add_argument("--no-play", action="store_true", help="capture only, no speakers")
    parser.add_argument("--wav", type=Path, default=Path("out/mic.wav"))
    args = parser.parse_args()

    byte_rate = args.rate * args.depth // 8
    print(f"{args.rate} Hz x {args.depth} bit = {byte_rate / 1000:.0f} kB/s "
          f"(link sustains ~25 kB/s measured)\n")

    frame = BrilliantMsg()
    if not await connect_with_retry(frame):
        print("\nCould not connect. Tap the glasses to wake them (they sleep into")
        print("SYSTEM OFF and are unreachable over Bluetooth), then retry.")
        return 2

    rx_audio = RxAudio(streaming=True)
    attached = False
    player = None
    chunks: list[bytes] = []

    try:
        print(f"connected (type={frame.ble.type})")
        await frame.upload_stdlua_libs(lib_names=["data", "audio"])
        await frame.upload_frame_app(local_filename=str(LUA_APP))
        frame.attach_print_response_handler()
        await frame.start_frame_app()

        audio_queue = await rx_audio.attach(frame)
        attached = True

        if not args.no_play:
            try:
                player = Player(args.rate, args.depth)
                print(f"live playback on ({player.latency_ms:.0f} ms latency)")
            except Exception as exc:
                print(f"(no playback: {type(exc).__name__}: {exc})")

        await frame.send_message(AUDIO_CTRL_MSG, control(True, args.rate, args.depth))

        limit = args.seconds if args.seconds > 0 else float("inf")
        print(f"\nrecording {'until Ctrl-C' if limit == float('inf') else f'{limit:.0f}s'}...\n")

        total = 0
        t0 = time.monotonic()
        last_report = t0

        while True:
            elapsed = time.monotonic() - t0
            if elapsed >= limit:
                break
            try:
                chunk = await asyncio.wait_for(audio_queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                print("  (no audio for 5s -- stopping)")
                break
            if chunk is None:          # end-of-stream sentinel
                print("  (end of stream)")
                break

            chunks.append(chunk)
            total += len(chunk)
            if player:
                try:
                    player.write(chunk)
                except Exception as exc:
                    print(f"  (playback stopped: {type(exc).__name__}: {exc})")
                    player.close()
                    player = None

            now = time.monotonic()
            if now - last_report >= 1.0:
                got = total / (now - t0)
                print(f"  {total / 1024:6.1f} kB  {got / 1000:5.1f} kB/s  "
                      f"({got / byte_rate * 100:3.0f}% of realtime)")
                last_report = now

        await frame.send_message(AUDIO_CTRL_MSG, control(False, args.rate, args.depth))
        await asyncio.sleep(0.5)

        elapsed = time.monotonic() - t0
        pcm = b"".join(chunks)
        print(f"\n{len(pcm) / 1024:.1f} kB in {elapsed:.1f}s "
              f"= {len(pcm) / elapsed / 1000:.1f} kB/s "
              f"({len(pcm) / elapsed / byte_rate * 100:.0f}% of realtime)")

        if pcm:
            # to_wav_bytes already does the signed->unsigned fixup that 8-bit
            # WAV requires -- do NOT offset it here as well.
            wav = RxAudio.to_wav_bytes(pcm, sample_rate=args.rate,
                                       bits_per_sample=args.depth, channels=1)
            args.wav.parent.mkdir(parents=True, exist_ok=True)
            args.wav.write_bytes(wav)
            secs = len(pcm) / byte_rate
            print(f"wrote {args.wav} ({len(wav) / 1024:.1f} kB, {secs:.1f}s of audio)")
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
        # detach unconditionally -- unregister is a safe no-op for an
        # unknown subscriber, so the flag only risks leaking a handler
        rx_audio.detach(frame)
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
