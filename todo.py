"""Speak a thought, keep it.

    python todo.py                  # tap the glasses, speak, it is saved
    python todo.py --list           # what is outstanding
    python todo.py --add "..."      # no glasses needed
    python todo.py --done <id>

Tap the frames, say the thought, tap again (or just stop talking). It is
transcribed locally -- nothing leaves the laptop -- tidied, and appended to
`thoughts.json`. Three taps quits.

This reuses the assistant's device app unchanged: it already renders text,
forwards taps and streams the microphone, which is the whole job here.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path

from brilliant_msg import BrilliantMsg, RxAudio, RxTap, TxCode, TxPlainText

from ai import display as disp
from ai.stt_client import SttClient
from notes.store import Store, clean
from park import park

TEXT_MSG = 0x0A
CLEAR_MSG = 0x43
AUDIO_CTRL_MSG = 0x30
TAP_CTRL_MSG = 0x41

LUA_APP = Path(__file__).parent / "app" / "lua" / "assistant_frame_app.lua"
STD_LIBS = ["data", "plain_text", "audio", "camera", "code", "tap", "sprite"]

RATE, DEPTH = 16000, 8
LISTEN_MAX = 20.0           # a thought can be longer than a question

# Hard-won on this hardware: one physical tap fires several IMU interrupts,
# and the taps that WAKE the glasses arrive while the app is still
# uploading. See FRAME-NOTES and the navigation HUD for the measurements.
TAP_GRACE = 1.2
MIN_LISTEN = 2.0
STARTUP_GRACE = 6.0
QUIT_TAPS = 3
GESTURE_WINDOW = 1.6


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


class Capture:
    def __init__(self, frame: BrilliantMsg, stt: SttClient,
                 store: Store) -> None:
        self.frame = frame
        self.stt = stt
        self.store = store
        self.rx_tap = RxTap()
        self.rx_audio = RxAudio(streaming=True)
        self.tap_q = self.audio_q = None
        self._taps_live_at = 0.0

    # --------------------------------------------------------- the panel

    async def clear(self) -> None:
        await self.frame.send_message(CLEAR_MSG, TxCode(value=1).pack())
        await asyncio.sleep(0.08)

    async def show(self, *lines: str, colour: str = "WHITE") -> None:
        await self.clear()
        y = 110
        for line in lines[:4]:
            for wrapped in disp.wrap(line)[:2]:
                await self.frame.send_message(
                    TEXT_MSG,
                    TxPlainText(text=wrapped, x=disp.LEFT_MARGIN, y=y).pack())
                y += disp.LINE_SPACING
                await asyncio.sleep(0.04)

    # ------------------------------------------------------------- taps

    async def drain_taps(self) -> int:
        n = 0
        while self.tap_q is not None:
            try:
                self.tap_q.get_nowait()
                n += 1
            except asyncio.QueueEmpty:
                break
        return n

    async def wait_gesture(self) -> int:
        """Block until taps arrive, then return how many were in the burst."""
        await asyncio.wait_for(self.tap_q.get(), timeout=None)
        taps = 1 + await self.drain_taps()
        until = time.monotonic() + GESTURE_WINDOW
        while time.monotonic() < until:
            try:
                await asyncio.wait_for(
                    self.tap_q.get(),
                    timeout=max(0.05, until - time.monotonic()))
                taps += 1
            except asyncio.TimeoutError:
                break
        return taps + await self.drain_taps()

    # ---------------------------------------------------------- listening

    async def listen(self) -> bytes:
        await self.drain_taps()
        while True:
            try:
                self.audio_q.get_nowait()
            except asyncio.QueueEmpty:
                break

        await self.frame.send_message(AUDIO_CTRL_MSG, bytes([1, 1, DEPTH]))
        chunks: list[bytes] = []
        t0 = time.monotonic()
        try:
            while time.monotonic() - t0 < LISTEN_MAX:
                try:
                    chunk = await asyncio.wait_for(self.audio_q.get(),
                                                   timeout=0.2)
                    if chunk is None:
                        break
                    chunks.append(chunk)
                except asyncio.TimeoutError:
                    pass
                elapsed = time.monotonic() - t0
                if elapsed < TAP_GRACE:
                    await self.drain_taps()   # the tap that started us
                    continue
                if elapsed >= MIN_LISTEN and not self.tap_q.empty():
                    await self.drain_taps()
                    break
        finally:
            await self.frame.send_message(AUDIO_CTRL_MSG, bytes([0, 1, DEPTH]))
            await asyncio.sleep(0.25)
            while True:
                try:
                    leftover = self.audio_q.get_nowait()
                    if leftover:
                        chunks.append(leftover)
                except asyncio.QueueEmpty:
                    break
        return b"".join(chunks)

    async def one_thought(self) -> None:
        await self.show("listening...", colour="GREEN")
        log("listening...")
        pcm = await self.listen()
        secs = len(pcm) / (RATE * DEPTH // 8)
        log(f"  captured {len(pcm) / 1024:.1f} kB ({secs:.1f}s)")

        await self.show("saving...")
        try:
            text, level = await self.stt.transcribe(pcm, RATE, DEPTH)
        except Exception as exc:
            log(f"  stt failed: {type(exc).__name__}: {exc}")
            await self.show("speech failed", colour="ORANGE")
            await asyncio.sleep(2.0)
            return

        log(f"  heard: {text!r}  ({level:.0f} dBFS)")
        if not text.strip():
            hint = "too short" if secs < MIN_LISTEN else "didn't catch that"
            await self.show(hint, colour="ORANGE")
            await asyncio.sleep(2.0)
            return

        thought = self.store.add(text, seconds=secs, confidence=f"{level:.0f}")
        if thought is None:
            await self.show("nothing to save", colour="ORANGE")
            await asyncio.sleep(2.0)
            return

        log(f"  saved [{thought.id}] {thought.text!r}")
        await self.show("saved:", thought.text,
                        f"{len(self.store.pending())} open")
        await asyncio.sleep(3.0)

    async def run(self) -> int:
        self.tap_q = await self.rx_tap.attach(self.frame)
        self.audio_q = await self.rx_audio.attach(self.frame)
        await self.frame.send_message(TAP_CTRL_MSG, TxCode(value=1).pack())
        await asyncio.sleep(0.4)
        dropped = await self.drain_taps()
        if dropped:
            log(f"  ignored {dropped} wake-up tap(s)")
        self._taps_live_at = time.monotonic() + STARTUP_GRACE

        open_now = len(self.store.pending())
        await self.show("tap to capture", f"{open_now} open")
        log(f"ready -- tap to capture a thought ({open_now} open, "
            f"{QUIT_TAPS} taps to quit, Ctrl-C also works)")

        while True:
            taps = await self.wait_gesture()
            if time.monotonic() < self._taps_live_at:
                log(f"  ignored {taps} tap(s) during startup")
                continue
            if taps >= QUIT_TAPS:
                log(f"  {taps} taps -- quitting")
                await self.show("bye")
                await asyncio.sleep(1.0)
                return 0
            await self.one_thought()
            await self.show("tap to capture",
                            f"{len(self.store.pending())} open")


async def report_battery(frame, when: str) -> float | None:
    """Read and LOG the battery. Every connection to these glasses should
    leave a number behind -- not knowing the charge at the moment something
    went wrong has already cost two misdiagnoses on this unit."""
    try:
        reply = await frame.ble.send_lua("print(frame.battery_level())",
                                         await_print=True, timeout=6)
        level = float(str(reply).strip())
        log(f"battery {level:.0f}% ({when})")
        return level
    except Exception as exc:
        log(f"battery unreadable ({when}): {type(exc).__name__}: {exc}")
        return None


async def connect(frame: BrilliantMsg, attempts: int = 20) -> bool:
    for i in range(1, attempts + 1):
        try:
            await frame.connect()
            return True
        except Exception as exc:
            log(f"  connect {i}/{attempts}: {exc}")
            await asyncio.sleep(3)
    return False


def print_list(store: Store, show_done: bool) -> None:
    items = store.all() if show_done else store.pending()
    if not items:
        print("nothing captured yet")
        return
    for t in items:
        mark = "x" if t.done else " "
        print(f"  [{mark}] {t.id}  {t.when}  {t.text}")
    print(f"\n{len(store.pending())} open, {len(store)} total")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="print and exit")
    ap.add_argument("--all", action="store_true", help="include done items")
    ap.add_argument("--add", help="add a thought without the glasses")
    ap.add_argument("--done", help="mark a thought done by id")
    ap.add_argument("--file", type=Path, default=None)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)

    store = Store(args.file)

    if args.add:
        thought = store.add(args.add)
        print(f"saved [{thought.id}] {thought.text}" if thought
              else "nothing to save")
        return 0
    if args.done:
        matches = [t for t in store.all() if t.id.startswith(args.done)]
        if not matches:
            print(f"no thought starting {args.done!r}")
            return 1
        store.apply_done([t.id for t in store.all() if t.done]
                         + [matches[0].id])
        print(f"done: {matches[0].text}")
        return 0
    if args.list or args.all:
        print_list(store, args.all)
        return 0

    stt = SttClient()
    frame = BrilliantMsg()
    log("connecting...")
    if not await connect(frame):
        log("could not connect -- tap the glasses or dock/undock them")
        return 2

    try:
        log(f"connected (payload {frame.ble.max_lua_payload()})")
        await report_battery(frame, "on connect")
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

        return await Capture(frame, stt, store).run()
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
