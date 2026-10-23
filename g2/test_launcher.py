"""Launcher and speech tests with a fake gateway. No glasses needed.

    python -m unittest g2.test_launcher
"""
from __future__ import annotations

import array
import asyncio
import inspect
import math
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from g2 import stt  # noqa: E402
from g2.launcher import MENU, Launcher, paginate  # noqa: E402
from notes.store import Store  # noqa: E402


class FakeGateway:
    def __init__(self):
        self.handlers = {}
        self.shown = []
        self.mic_calls = []
        self.ready = True
        self.phase = "ready"
        self.battery, self.charging, self.firmware = 96, True, "2.3.2.14"
        self.gateway_up = True
        self.last_gesture = None
        self.base = "http://fake"
        self.state = {"left": {"connected": True}, "right": {"connected": True}}

    def on(self, kind, fn):
        self.handlers.setdefault(kind, []).append(fn)

    async def emit(self, kind, *args):
        for fn in self.handlers.get(kind, []):
            r = fn(*args)
            if inspect.isawaitable(r):
                await r

    async def text(self, s):
        self.shown.append(s)

    async def mic(self, on):
        self.mic_calls.append(on)

    async def status(self):
        return {"glasses": self.state}


class FakeSpeech:
    def __init__(self, gw, text="buy milk tomorrow", pcm=b"\x01\x00" * 16000):
        self.gw, self.text_out, self.pcm = gw, text, pcm
        self.available = True
        self.model = "fake"
        self.captions = None

    async def capture_utterance(self, **kw):
        await self.gw.mic(True)
        await self.gw.mic(False)
        return self.pcm

    async def transcribe(self, pcm):
        return self.text_out, -25.0

    def captioner(self, callback):
        speech = self

        class Live:
            async def start(self_inner):
                speech.captions = callback
                await speech.gw.mic(True)

            async def resume(self_inner):
                await speech.gw.mic(True)

            async def stop(self_inner):
                speech.captions = None
                await speech.gw.mic(False)
        return Live()


class FakeBrain:
    async def ask(self, q, image=None):
        return "The answer is forty-two. " * 12      # long enough to paginate


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(tmp):
    gw = FakeGateway()
    speech = FakeSpeech(gw)
    store = Store(Path(tmp) / "thoughts.json")
    clock = Clock()
    la = Launcher(gw, speech, FakeBrain(), store, clock=clock, idle_blank_s=60,
                  transcripts_dir=Path(tmp) / "tr")
    return gw, speech, store, clock, la


async def settle(la):
    if la.busy is not None:
        await la.busy


class LauncherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.gw, self.speech, self.store, self.clock, self.la = make(self.tmp.name)
        await self.la.home()

    async def asyncTearDown(self):
        await self.la.stop()
        self.tmp.cleanup()

    def test_home_lists_menu_with_marker_and_battery(self):
        s = self.gw.shown[-1]
        for name, _ in MENU:
            self.assertIn(name, s)
        self.assertIn("> Assistant", s)
        self.assertIn("battery 96% charging", s)
        self.assertLessEqual(len(s.splitlines()), 10)
        self.assertLessEqual(len(s.encode()), 1000)

    async def test_swipes_move_selection_and_clamp(self):
        await self.gw.emit("touch", "swipe_down", 1)
        self.assertIn("> Transcribe", self.gw.shown[-1])
        for _ in range(10):
            await self.gw.emit("touch", "swipe_down", 1)
        self.assertIn("> Status", self.gw.shown[-1])
        for _ in range(10):
            await self.gw.emit("touch", "swipe_up", 1)
        self.assertIn("> Assistant", self.gw.shown[-1])

    async def test_tap_opens_status_and_double_tap_returns_home(self):
        for _ in range(3):
            await self.gw.emit("touch", "swipe_down", 1)
        await self.gw.emit("touch", "single_tap", 1)
        self.assertEqual(self.la.screen, "status")
        self.assertIn("firmware 2.3.2.14", self.gw.shown[-1])
        self.assertIn("L on  R on", self.gw.shown[-1])
        await self.gw.emit("touch", "double_tap", 1)
        self.assertEqual(self.la.screen, "home")
        self.assertIn("> Status", self.gw.shown[-1])

    async def test_idle_blanks_and_any_touch_restores_home_without_opening(self):
        self.clock.t += 61
        await self.la.tick()
        self.assertEqual(self.la.screen, "blank")
        self.assertEqual(self.gw.shown[-1], " ")
        await self.gw.emit("touch", "single_tap", 1)
        self.assertEqual(self.la.screen, "home")
        self.assertIn("> Assistant", self.gw.shown[-1])

    async def test_reconnect_redraws_current_screen(self):
        for _ in range(3):
            await self.gw.emit("touch", "swipe_down", 1)
        await self.gw.emit("touch", "single_tap", 1)          # status
        n = len(self.gw.shown)
        self.gw.ready = False
        await self.gw.emit("connection", False, "recovering")
        self.assertEqual(len(self.gw.shown), n)                 # nothing sent while away
        self.gw.ready = True
        await self.gw.emit("connection", True, "ready")
        self.assertEqual(len(self.gw.shown), n + 1)
        self.assertIn("Status", self.gw.shown[-1])

    async def test_notes_flow_saves_and_shows(self):
        for _ in range(2):
            await self.gw.emit("touch", "swipe_down", 1)
        await self.gw.emit("touch", "single_tap", 1)          # open Notes
        self.assertIn("no notes yet", self.gw.shown[-1])
        await self.gw.emit("touch", "single_tap", 1)          # new note
        await settle(self.la)
        self.assertEqual(len(self.store.pending()), 1)
        self.assertIn("saved:", self.gw.shown[-1])
        self.assertIn("buy milk tomorrow", self.gw.shown[-1].lower())
        self.assertEqual(self.gw.mic_calls, [True, False])

    async def test_assistant_asks_and_pages(self):
        await self.gw.emit("touch", "single_tap", 1)          # open Assistant
        self.assertIn("Tap, then ask", self.gw.shown[-1])
        await self.gw.emit("touch", "single_tap", 1)          # ask
        await settle(self.la)
        s = self.gw.shown[-1]
        self.assertIn("You: buy milk tomorrow", s)
        self.assertIn("forty-two", s)
        self.assertIn("page 1/", s)
        await self.gw.emit("touch", "single_tap", 1)          # next page
        self.assertIn("page 2/", self.gw.shown[-1])
        await self.gw.emit("touch", "double_tap", 1)
        self.assertEqual(self.la.screen, "home")

    async def test_transcribe_streams_to_screen_and_file_and_stops_mic(self):
        await self.gw.emit("touch", "swipe_down", 1)
        await self.gw.emit("touch", "single_tap", 1)          # open Transcribe
        self.assertEqual(self.gw.mic_calls, [True])
        await self.speech.captions("hello there")
        await self.speech.captions("general kenobi")
        self.assertIn("hello there general kenobi", self.gw.shown[-1])
        # link drops and returns: mic re-armed, screen redrawn
        await self.gw.emit("connection", True, "ready")
        self.assertEqual(self.gw.mic_calls[-1], True)
        await self.gw.emit("touch", "double_tap", 1)
        self.assertEqual(self.gw.mic_calls[-1], False)
        files = list((Path(self.tmp.name) / "tr").glob("*.txt"))
        self.assertEqual(len(files), 1)
        body = files[0].read_text(encoding="utf-8")
        self.assertIn("hello there", body)
        self.assertIn("general kenobi", body)

    async def test_double_tap_cancels_a_listening_assistant(self):
        slow = asyncio.Event()

        async def capture(**kw):
            await self.gw.mic(True)
            try:
                await slow.wait()
            finally:
                await self.gw.mic(False)
        self.speech.capture_utterance = capture
        await self.gw.emit("touch", "single_tap", 1)          # open
        await self.gw.emit("touch", "single_tap", 1)          # ask -> listening
        await asyncio.sleep(0)
        self.assertEqual(self.la.assistant_state, "listening")
        await self.gw.emit("touch", "double_tap", 1)
        self.assertEqual(self.la.screen, "home")
        self.assertEqual(self.gw.mic_calls, [True, False])

    async def test_page_shutdown_means_user_exited_so_go_home(self):
        for _ in range(3):
            await self.gw.emit("touch", "swipe_down", 1)
        await self.gw.emit("touch", "single_tap", 1)          # status
        await self.gw.emit("reinit", "page_shutdown")
        self.assertEqual(self.la.screen, "home")
        self.assertIn("> Status", self.gw.shown[-1])
        # a transcribe session must stop its mic on exit too
        await self.gw.emit("touch", "swipe_up", 1); await self.gw.emit("touch", "swipe_up", 1)
        await self.gw.emit("touch", "single_tap", 1)          # transcribe
        self.assertEqual(self.gw.mic_calls[-1], True)
        await self.gw.emit("reinit", "page_shutdown")
        self.assertEqual(self.gw.mic_calls[-1], False)
        self.assertEqual(self.la.screen, "home")

    def test_paginate_respects_rows_and_width(self):
        pages = paginate("word " * 400)
        self.assertTrue(all(len(p) <= 8 for p in pages))
        self.assertTrue(all(len(line) <= 48 for p in pages for line in p))


def tone(seconds: float, amp: float, freq: float = 440.0) -> bytes:
    n = int(16000 * seconds)
    a = array.array("h", (int(amp * 32767 * math.sin(2 * math.pi * freq * i / 16000)) for i in range(n)))
    return a.tobytes()


class EndpointerTests(unittest.TestCase):
    def test_finds_speech_between_silences(self):
        quiet = tone(2.0, 0.002)                 # about -54 dBFS, like the room
        voice = tone(1.5, 0.1)                   # about -20 dBFS
        pcm = quiet + voice + tone(3.0, 0.002)
        rng = stt.find_utterance(pcm)
        self.assertIsNotNone(rng)
        start, end = rng
        self.assertLess(abs(start - len(quiet)) / 32000, 0.3)              # within 300 ms
        self.assertGreater(end, len(quiet) + len(voice))                    # includes trailing silence
        self.assertLess(end, len(quiet) + len(voice) + 32000 * 1.6)        # stopped about 1.2 s after

    def test_timeout_when_nobody_speaks(self):
        self.assertIsNone(stt.find_utterance(tone(8.0, 0.002)))

    def test_dbfs_scale(self):
        self.assertAlmostEqual(stt.dbfs(tone(0.5, 1.0)), -3.0, delta=0.2)
        self.assertLess(stt.dbfs(b"\x00\x00" * 100), -100)


if __name__ == "__main__":
    unittest.main()
