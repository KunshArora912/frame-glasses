"""Frame AI assistant: tap, ask aloud, read the answer on the glasses.

    python assistant.py                  # tap to ask
    python assistant.py --camera         # attach a photo to every question
    python assistant.py --text "..."     # skip the mic, for testing

Flow:  tap -> listen -> transcribe (local) -> ask -> render -> tap to page.
Two quick taps while an answer is showing starts a new question.

Needs ANTHROPIC_API_KEY for real answers; without one it runs on a local
fallback brain so the whole loop still works.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

from brilliant_msg import (BrilliantMsg, RxAudio, RxPhoto, RxTap,
                           TxCaptureSettings, TxCode, TxPlainText,
                           TxSprite)

from ai import display as disp
from ai.brain import make_brain
from ai.stt_client import SttClient
from park import park

TEXT_MSG = 0x0A
CLEAR_MSG = 0x43
AUDIO_CTRL_MSG = 0x30
TAP_CTRL_MSG = 0x41
CAPTURE_MSG = 0x0D
SPRITE_MSG = 0x20

LUA_APP = Path(__file__).parent / "app" / "lua" / "assistant_frame_app.lua"
STD_LIBS = ["data", "plain_text", "audio", "camera", "code", "tap",
            "sprite"]

RATE, DEPTH = 16000, 8          # 16 kB/s, 100% of realtime, no aliasing
LISTEN_MAX = 12.0
DOUBLE_TAP_WINDOW = 0.6

# One physical tap fires many IMU interrupts. RxTap groups them into
# gestures, but a straggler from the tap that STARTED recording can arrive
# just after we drain the queue -- and would instantly stop the recording.
# Ignore taps for this long, and never stop before MIN_LISTEN.
TAP_GRACE = 1.2
MIN_LISTEN = 2.0

# Spoken requests meaning "capture and keep", as opposed to "answer a
# question about what you can see".
PHOTO_INTENT = ("take a picture", "take a photo", "taking a picture",
                "take picture", "take photo", "snap a photo", "snap a picture",
                "photograph this", "capture this", "take a shot",
                "get a picture", "get a photo")


def wants_photo(text: str) -> bool:
    low = (text or "").lower()
    return any(phrase in low for phrase in PHOTO_INTENT)


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def open_on_laptop(path: Path) -> None:
    """Open a saved photo in the default viewer. Best effort."""
    try:
        if os.name == "nt":
            os.startfile(str(path))
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as exc:
        log(f"  could not open viewer: {exc}")


class Assistant:
    def __init__(self, frame: BrilliantMsg, stt: SttClient,
                 use_camera: bool = False,
                 photo_dir: Path | None = None) -> None:
        self.frame = frame
        self.stt = stt
        self.use_camera = use_camera
        self.photo_dir = photo_dir or (Path(__file__).parent / "photos")
        self.brain = make_brain(battery_getter=self._battery)
        self.rx_tap = RxTap(threshold=0.25)
        self.rx_audio = RxAudio(streaming=True)
        self.rx_photo = RxPhoto()
        self.tap_q = self.audio_q = self.photo_q = None
        self.pages: list[disp.Page] = []
        self.page_i = 0

    async def _battery(self) -> float:
        r = await self.frame.ble.send_lua("print(frame.battery_level())",
                                          await_print=True, timeout=8)
        return float(str(r).strip())

    # ---------------------------------------------------------- rendering

    async def clear(self) -> None:
        await self.frame.send_message(CLEAR_MSG, TxCode(value=1).pack())
        await asyncio.sleep(0.08)

    async def show_status(self, text: str, colour: str = "WHITE") -> None:
        await self.clear()
        for i, line in enumerate(disp.wrap(text)[:3]):
            await self.frame.send_message(
                TEXT_MSG,
                TxPlainText(text=line, x=disp.LEFT_MARGIN,
                            y=140 + i * disp.LINE_SPACING).pack())
            await asyncio.sleep(0.05)

    async def show_page(self, page: disp.Page, header: str | None) -> None:
        await self.clear()
        y = disp.TOP_MARGIN
        if header:
            await self.frame.send_message(
                TEXT_MSG,
                TxPlainText(text=disp.to_ascii(header)[:disp.CHARS_PER_LINE],
                            x=disp.LEFT_MARGIN, y=y, palette_offset=14).pack())
            y += disp.LINE_SPACING
            await asyncio.sleep(0.05)
        for line in page.lines:
            if not line:
                y += disp.LINE_SPACING
                continue
            if y > disp.MAX_Y:
                break
            await self.frame.send_message(
                TEXT_MSG,
                TxPlainText(text=line, x=disp.LEFT_MARGIN, y=y).pack())
            y += disp.LINE_SPACING
            await asyncio.sleep(0.05)
        if page.total > 1:
            await self.frame.send_message(
                TEXT_MSG,
                TxPlainText(text=f"{page.index + 1}/{page.total}",
                            x=560, y=disp.MAX_Y, palette_offset=2).pack())

    # ------------------------------------------------------------- input

    async def wait_tap(self, timeout: float | None = None) -> bool:
        try:
            await asyncio.wait_for(self.tap_q.get(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def drain_taps(self) -> int:
        n = 0
        while True:
            try:
                self.tap_q.get_nowait()
                n += 1
            except asyncio.QueueEmpty:
                return n

    async def listen(self) -> bytes:
        """Record until a second tap, or LISTEN_MAX seconds."""
        await self.drain_taps()
        while True:                               # drop stale audio
            try:
                self.audio_q.get_nowait()
            except asyncio.QueueEmpty:
                break

        await self.frame.send_message(
            AUDIO_CTRL_MSG, bytes([1, 1 if RATE == 16000 else 0, DEPTH]))
        chunks: list[bytes] = []
        t0 = time.monotonic()
        try:
            while time.monotonic() - t0 < LISTEN_MAX:
                try:
                    chunk = await asyncio.wait_for(self.audio_q.get(), timeout=0.2)
                    if chunk is None:
                        break
                    chunks.append(chunk)
                except asyncio.TimeoutError:
                    pass

                elapsed = time.monotonic() - t0
                if elapsed < TAP_GRACE:
                    await self.drain_taps()       # swallow the opening bounce
                    continue
                if elapsed >= MIN_LISTEN and not self.tap_q.empty():
                    await self.drain_taps()       # a genuine second tap
                    break
        finally:
            await self.frame.send_message(
                AUDIO_CTRL_MSG, bytes([0, 1 if RATE == 16000 else 0, DEPTH]))
            await asyncio.sleep(0.25)
            while True:
                try:
                    c = self.audio_q.get_nowait()
                    if c:
                        chunks.append(c)
                except asyncio.QueueEmpty:
                    break
        return b"".join(chunks)

    async def snap(self) -> bytes | None:
        try:
            await self.frame.send_message(
                CAPTURE_MSG,
                TxCaptureSettings(resolution=512, quality_index=3).pack())
            return await asyncio.wait_for(self.photo_q.get(), timeout=25.0)
        except Exception as exc:
            log(f"  camera failed: {exc}")
            return None

    # ------------------------------------------------------- photo mode

    async def show_image(self, jpeg: bytes) -> None:
        """Render a photo back onto the glasses, quantised to 16 colours."""
        sprite = TxSprite.from_image_bytes(jpeg, max_pixels=40000)
        await self.clear()
        await self.frame.send_message(SPRITE_MSG, sprite.pack())

    async def take_photo(self) -> None:
        """Capture at full resolution, show it, save it, offer to discard."""
        await self.show_status("capturing...", "GREEN")
        try:
            await self.frame.send_message(
                CAPTURE_MSG,
                TxCaptureSettings(resolution=720, quality_index=4).pack())
            jpeg = await asyncio.wait_for(self.photo_q.get(), timeout=30.0)
        except Exception as exc:
            log(f"  capture failed: {exc}")
            await self.show_status("capture failed", "ORANGE")
            await asyncio.sleep(2.0)
            return

        self.photo_dir.mkdir(parents=True, exist_ok=True)
        path = self.photo_dir / f"frame_{datetime.now():%Y%m%d_%H%M%S}.jpg"
        path.write_bytes(jpeg)
        log(f"  photo {len(jpeg)/1024:.1f} kB -> {path}")

        try:
            await self.show_image(jpeg)
        except Exception as exc:
            log(f"  preview failed: {exc}")
        open_on_laptop(path)

        await asyncio.sleep(3.5)
        await self.show_status("saved. tap=keep  2 taps=delete", "GREEN")

        if not await self.wait_tap(timeout=12.0):
            log("  kept (no input)")
            return
        t0 = time.monotonic()
        second = await self.wait_tap(timeout=DOUBLE_TAP_WINDOW)
        await self.drain_taps()
        if second and time.monotonic() - t0 < DOUBLE_TAP_WINDOW:
            try:
                path.unlink()
                log("  deleted")
                await self.show_status("deleted", "ORANGE")
            except Exception as exc:
                log(f"  delete failed: {exc}")
        else:
            log(f"  kept: {path}")
            await self.show_status("kept", "GREEN")
        await asyncio.sleep(1.5)

    # -------------------------------------------------------------- loop

    async def one_question(self, forced_text: str | None = None) -> None:
        image = None
        if forced_text is None:
            await self.show_status("listening...", "GREEN")
            log("listening...")
            if self.use_camera:
                image = await self.snap()
                if image:
                    log(f"  photo {len(image)/1024:.1f} kB")
            pcm = await self.listen()
            secs = len(pcm) / (RATE * DEPTH // 8)
            log(f"  captured {len(pcm)/1024:.1f} kB ({secs:.1f}s)")

            await self.show_status("thinking...", "SKYBLUE")
            # Out-of-process: ctranslate2's native backend segfaults inside
            # the Bluetooth process, because bleak's WinRT backend has already
            # initialised a COM apartment on this thread.
            try:
                question, level = await self.stt.transcribe(pcm, RATE, DEPTH)
            except Exception as exc:
                log(f"  stt failed: {type(exc).__name__}: {exc}")
                await self.show_status("speech failed", "ORANGE")
                await asyncio.sleep(2.0)
                return
            log(f"  heard: {question!r}  ({level:.0f} dBFS)")
            if not question:
                hint = ("too short - hold on" if secs < MIN_LISTEN
                        else "didn't catch that")
                await self.show_status(hint, "ORANGE")
                await asyncio.sleep(2.0)
                return
        else:
            question = forced_text
            await self.show_status("thinking...", "SKYBLUE")

        if wants_photo(question):
            await self.take_photo()
            return

        try:
            answer = await self.brain.ask(question, image=image)
        except Exception as exc:
            log(f"  brain error: {type(exc).__name__}: {exc}")
            await self.show_status("brain unreachable", "ORANGE")
            await asyncio.sleep(2.5)
            return
        log(f"  answer: {answer!r}")

        self.pages = disp.paginate(answer)
        self.page_i = 0
        header = question[:disp.CHARS_PER_LINE]
        await self.show_page(self.pages[0], header)

        # tap to page; two quick taps to ask again
        while True:
            if not await self.wait_tap(timeout=90.0):
                return
            t = time.monotonic()
            second = await self.wait_tap(timeout=DOUBLE_TAP_WINDOW)
            if second and time.monotonic() - t < DOUBLE_TAP_WINDOW:
                await self.drain_taps()
                return                                   # double tap = new
            self.page_i = (self.page_i + 1) % len(self.pages)
            await self.show_page(self.pages[self.page_i], header)

    async def run(self, once_text: str | None = None) -> None:
        self.tap_q = await self.rx_tap.attach(self.frame)
        self.audio_q = await self.rx_audio.attach(self.frame)
        self.photo_q = await self.rx_photo.attach(self.frame)
        await self.frame.send_message(TAP_CTRL_MSG, TxCode(value=1).pack())

        log(f"brain: {self.brain.name}   camera: {self.use_camera}")

        if once_text is not None:
            await self.one_question(forced_text=once_text)
            return

        await self.show_status("tap to ask", "WHITE")
        log("ready -- tap the glasses to ask a question (Ctrl-C to stop)")
        while True:
            if not await self.wait_tap(timeout=None):
                continue
            await self.drain_taps()
            await self.one_question()
            await self.show_status("tap to ask", "WHITE")


async def connect(frame: BrilliantMsg, attempts: int = 20) -> bool:
    for i in range(1, attempts + 1):
        try:
            await frame.connect()
            return True
        except Exception as exc:
            log(f"  connect {i}/{attempts}: {exc}")
            await asyncio.sleep(3)
    return False


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--camera", action="store_true",
                    help="attach a photo to every question")
    ap.add_argument("--text", help="skip the mic and ask this instead")
    ap.add_argument("--photos", type=Path, default=None,
                    help="where to save photos (default ./photos)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)

    stt = SttClient()
    frame = BrilliantMsg()
    log("connecting...")
    if not await connect(frame):
        log("could not connect -- tap the glasses or dock/undock them")
        return 2

    try:
        log(f"connected (payload {frame.ble.max_lua_payload()})")
        await frame.ble.send_break_signal()
        await asyncio.sleep(0.5)
        await frame.ble.drain_print_channel()
        await frame.upload_stdlua_libs(lib_names=STD_LIBS)
        await frame.upload_frame_app(local_filename=str(LUA_APP))
        await frame.start_frame_app()

        log("starting speech worker (loads the model once)...")
        if not await stt.start():
            log("speech worker failed to start")
            return 1
        log("  speech worker ready")

        assistant = Assistant(frame, stt, use_camera=args.camera,
                              photo_dir=args.photos)
        await assistant.run(once_text=args.text)
        return 0
    except KeyboardInterrupt:
        log("stopped")
        return 0
    except Exception as exc:
        log(f"error: {type(exc).__name__}: {exc}")
        return 1
    finally:
        await stt.stop()
        try:
            await frame.send_message(AUDIO_CTRL_MSG, bytes([0, 1, DEPTH]))
            await frame.stop_frame_app()
        except Exception:
            pass
        await park(frame)
        await frame.disconnect()
        log("disconnected")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
