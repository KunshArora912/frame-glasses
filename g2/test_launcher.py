"""Launcher, app and speech tests with a fake gateway. No glasses needed.

    python -m unittest g2.test_launcher     (or: python g2/dev.py test)
"""
from __future__ import annotations

import array
import asyncio
import math
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from g2 import stt  # noqa: E402
from g2 import apps as registry  # noqa: E402
from g2.fakes import Clock, FakeBrain, FakeGateway, FakeSpeech  # noqa: E402
from g2.launcher import Launcher, paginate  # noqa: E402
from notes.store import Store  # noqa: E402

CORE = {"assistant", "transcribe", "notes", "status"}


def make(tmp):
    gw = FakeGateway()
    speech = FakeSpeech(gw)
    store = Store(Path(tmp) / "thoughts.json")
    clock = Clock()
    apps = registry.discover(only=CORE)
    la = Launcher(gw, speech, FakeBrain(), store, apps=apps, clock=clock, idle_blank_s=60,
                  transcripts_dir=Path(tmp) / "tr", data_dir=Path(tmp))
    return gw, speech, store, clock, la


async def settle(la):
    if la.busy is not None:
        try:
            await la.busy
        except asyncio.CancelledError:
            pass


class LauncherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.gw, self.speech, self.store, self.clock, self.la = make(self.tmp.name)
        await self.la.home()

    async def asyncTearDown(self):
        await self.la.stop()
        self.tmp.cleanup()

    async def go(self, *gestures):
        for g in gestures:
            await self.gw.emit("touch", g, 1)

    def test_home_lists_menu_with_marker_and_battery(self):
        s = self.gw.shown[-1]
        for name, _ in self.la.menu:
            self.assertIn(name, s)
        self.assertIn("> Assistant", s)
        self.assertIn("battery 96% charging", s)
        els = self.gw.layouts[-1]
        self.assertEqual(sum(1 for e in els if e.get("capture_events")), 1)
        self.assertTrue(all(len(e["container_name"]) <= 16 for e in els))
        self.assertTrue(all(e["x"] + e["width"] <= 576 and e["y"] + e["height"] <= 288 for e in els))

    def test_menu_order_and_discovery(self):
        self.assertEqual([n for n, _ in self.la.menu], ["Assistant", "Transcribe", "Notes", "Status"])
        self.assertNotIn("_template", [getattr(a, "module", "") for a in self.la.apps])

    async def test_swipes_move_selection_and_clamp(self):
        await self.go("swipe_down")
        self.assertIn("> Transcribe", self.gw.shown[-1])
        await self.go(*["swipe_down"] * 10)
        self.assertIn("> Status", self.gw.shown[-1])
        await self.go(*["swipe_up"] * 10)
        self.assertIn("> Assistant", self.gw.shown[-1])

    async def test_tap_opens_status_and_double_tap_returns_home(self):
        await self.go("swipe_down", "swipe_down", "swipe_down", "single_tap")
        self.assertEqual(self.la.screen, "status")
        self.assertIn("firmware 2.3.2.14", self.gw.shown[-1])
        self.assertIn("L on  R on", self.gw.shown[-1])
        await self.go("double_tap")
        self.assertEqual(self.la.screen, "home")
        self.assertIn("> Status", self.gw.shown[-1])

    async def test_idle_blanks_and_any_touch_restores_home_without_opening(self):
        self.clock.t += 61
        await self.la.tick()
        self.assertEqual(self.la.screen, "blank")
        self.assertEqual(self.gw.shown[-1], " ")
        await self.go("single_tap")
        self.assertEqual(self.la.screen, "home")
        self.assertIn("> Assistant", self.gw.shown[-1])

    async def test_reconnect_redraws_current_screen(self):
        await self.go("swipe_down", "swipe_down", "swipe_down", "single_tap")     # status
        n = len(self.gw.shown)
        self.gw.ready = False
        await self.gw.emit("connection", False, "recovering")
        self.assertEqual(len(self.gw.shown), n)
        self.gw.ready = True
        await self.gw.emit("connection", True, "ready")
        self.assertEqual(len(self.gw.shown), n + 1)
        self.assertIn("Status", self.gw.shown[-1])

    async def test_notes_flow_saves_and_shows(self):
        await self.go("swipe_down", "swipe_down", "single_tap")
        self.assertIn("no notes yet", self.gw.shown[-1])
        await self.go("single_tap")
        await settle(self.la)
        self.assertEqual(len(self.store.pending()), 1)
        self.assertIn("saved:", self.gw.shown[-1])
        self.assertIn("buy milk tomorrow", self.gw.shown[-1].lower())
        self.assertEqual(self.gw.mic_calls, [True, False])

    async def test_assistant_asks_and_pages(self):
        await self.go("single_tap")
        self.assertIn("Tap, then ask", self.gw.shown[-1])
        await self.go("single_tap")
        await settle(self.la)
        s = self.gw.shown[-1]
        self.assertIn("You: buy milk tomorrow", s)
        self.assertIn("forty-two", s)
        self.assertIn(" 1/2", s)
        await self.go("single_tap")
        self.assertIn(" 2/2", self.gw.shown[-1])
        await self.go("double_tap")
        self.assertEqual(self.la.screen, "home")

    async def test_transcribe_streams_to_screen_and_file_and_stops_mic(self):
        await self.go("swipe_down", "single_tap")
        self.assertEqual(self.gw.mic_calls, [True])
        await self.speech.captions("hello there")
        await self.speech.captions("general kenobi")
        self.assertIn("hello there general kenobi", self.gw.shown[-1])
        await self.gw.emit("connection", True, "ready")
        self.assertEqual(self.gw.mic_calls[-1], True)
        await self.go("double_tap")
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
        await self.go("single_tap", "single_tap")
        await asyncio.sleep(0)
        self.assertEqual(self.la.app.state, "listening")
        await self.go("double_tap")
        self.assertEqual(self.la.screen, "home")
        self.assertEqual(self.gw.mic_calls, [True, False])

    async def test_page_shutdown_means_user_exited_so_go_home(self):
        await self.go("swipe_down", "swipe_down", "swipe_down", "single_tap")     # status
        await self.gw.emit("reinit", "page_shutdown")
        self.assertEqual(self.la.screen, "home")
        await self.go("swipe_up", "swipe_up", "single_tap")                        # transcribe
        self.assertEqual(self.gw.mic_calls[-1], True)
        await self.gw.emit("reinit", "page_shutdown")
        self.assertEqual(self.gw.mic_calls[-1], False)
        self.assertEqual(self.la.screen, "home")

    async def test_parked_waits_for_relaunch_and_ignores_touches(self):
        await self.go("swipe_down", "single_tap")                                  # transcribe
        n = len(self.gw.shown)
        await self.gw.emit("reinit", "page_parked")
        self.assertEqual(self.la.screen, "parked")
        self.assertEqual(self.gw.mic_calls[-1], False)
        await self.go("single_tap")
        await self.la.tick()
        await self.gw.emit("connection", True, "ready")
        self.assertEqual(len(self.gw.shown), n)
        await self.gw.emit("reinit", "dashboard_launch")
        self.assertEqual(self.la.screen, "home")
        self.assertIn("> Assistant", self.gw.shown[-1])

    async def test_every_screen_fits_the_grid(self):
        def check(label):
            s = self.gw.shown[-1]
            self.assertLessEqual(len(s.splitlines()), 10, label)
            for line in s.splitlines():
                self.assertLessEqual(len(line), 48, f"{label}: {line!r}")
        await self.go("swipe_down", "swipe_down", "swipe_down", "single_tap"); check("status")
        await self.go("double_tap", "swipe_up", "single_tap"); check("notes")
        await self.go("single_tap"); await settle(self.la); check("notes saved")
        await self.go("double_tap", "swipe_up", "single_tap"); check("transcribe")
        await self.speech.captions("word " * 30); check("transcribe captions")
        await self.go("double_tap", "swipe_up", "single_tap"); check("assistant")
        await self.go("single_tap"); await settle(self.la); check("assistant answer p1")
        await self.go("single_tap"); check("assistant answer p2")

    async def test_home_after_long_session_is_not_blanked_by_next_tick(self):
        await self.go("single_tap")
        self.clock.t += 600
        await self.gw.emit("reinit", "page_shutdown")
        self.assertEqual(self.la.screen, "home")
        await self.la.tick()
        self.assertEqual(self.la.screen, "home")

    async def test_surface_notices_are_not_gestures(self):
        n = len(self.gw.shown)
        await self.gw.emit("touch", "foreground_exit", None)
        await self.gw.emit("touch", "foreground_enter", None)
        self.assertEqual(len(self.gw.shown), n)

    async def test_open_by_name_and_unknown_app(self):
        self.assertTrue(await self.la.open("notes"))
        self.assertEqual(self.la.screen, "notes")
        self.assertFalse(await self.la.open("nope"))

    async def test_app_exception_does_not_kill_the_launcher(self):
        from g2.apps.base import App

        class Broken(App):
            name = "Broken"

            async def open(self, ctx):
                raise RuntimeError("boom")

            async def touch(self, ctx, gesture):
                raise RuntimeError("bang")
        self.la.apps.append(Broken())
        self.assertTrue(await self.la.open("broken"))
        self.assertIn("failed to open", self.gw.shown[-1])
        await self.go("single_tap")               # swallowed, logged
        await self.go("double_tap")
        self.assertEqual(self.la.screen, "home")

    def test_paginate_respects_rows_and_width(self):
        pages = paginate("word " * 400)
        self.assertTrue(all(len(p) <= 8 for p in pages))
        self.assertTrue(all(len(line) <= 48 for p in pages for line in p))


class TemplateAndToolsTests(unittest.IsolatedAsyncioTestCase):
    async def test_template_app_runs(self):
        import importlib
        mod = importlib.import_module("g2.apps._template")
        app = mod.MyApp()
        gw = FakeGateway()
        la = Launcher(gw, None, None, None, apps=[app], clock=Clock())
        await la.open(app.key)
        self.assertIn("Hello.", gw.shown[-1])
        await gw.emit("touch", "single_tap", 1)
        self.assertIn("tapped 1 times", gw.shown[-1])
        await la.stop()

    def test_render_produces_png(self):
        from g2.render import render
        img = render("Hello\nworld")
        self.assertEqual(img.size, (576 * 2, 288 * 2))

    def test_dev_new_and_list(self):
        import g2.dev as dev
        with tempfile.TemporaryDirectory() as tmp:
            old = dev.APPS
            try:
                dev.APPS = Path(tmp)
                (Path(tmp) / "_template.py").write_text((old / "_template.py").read_text(encoding="utf-8"), encoding="utf-8")
                rc = dev.cmd_new(type("A", (), {"name": "Weather Now", "title": None, "blurb": "rain or not", "force": False})())
                self.assertEqual(rc, 0)
                src = (Path(tmp) / "weather_now.py").read_text(encoding="utf-8")
                self.assertIn('name = "Weather Now"', src)
                self.assertIn('blurb = "rain or not"', src)
                self.assertIn("class WeatherNow(App)", src)
            finally:
                dev.APPS = old

    def test_layout_checker(self):
        import g2.dev as dev
        good = [{"type": "text", "text": "a", "x": 16, "y": 12, "width": 544, "height": 264, "capture_events": True, "container_name": "main"}]
        self.assertEqual(dev._check_layout(good), [])
        bad = [{"type": "text", "text": "a", "x": 500, "y": 12, "width": 100, "height": 300, "capture_events": True, "container_name": "a-very-long-container-name"},
               {"type": "text", "text": "b", "x": 0, "y": 0, "width": 10, "height": 10, "capture_events": True}]
        errs = dev._check_layout(bad)
        self.assertTrue(any("capture_events" in e for e in errs))
        self.assertTrue(any("off the canvas" in e for e in errs))
        self.assertTrue(any("longer than 16" in e for e in errs))


def tone(seconds: float, amp: float, freq: float = 440.0) -> bytes:
    n = int(16000 * seconds)
    a = array.array("h", (int(amp * 32767 * math.sin(2 * math.pi * freq * i / 16000)) for i in range(n)))
    return a.tobytes()


class EndpointerTests(unittest.TestCase):
    def test_finds_speech_between_silences(self):
        quiet = tone(2.0, 0.002)
        voice = tone(1.5, 0.1)
        pcm = quiet + voice + tone(3.0, 0.002)
        rng = stt.find_utterance(pcm)
        self.assertIsNotNone(rng)
        start, end = rng
        self.assertLess(abs(start - len(quiet)) / 32000, 0.3)
        self.assertGreater(end, len(quiet) + len(voice))
        self.assertLess(end, len(quiet) + len(voice) + 32000 * 1.6)

    def test_sustained_speech_is_not_cut_by_floor_drift(self):
        quiet, voice = tone(2.0, 0.002), tone(8.0, 0.1)
        start, end = stt.find_utterance(quiet + voice + tone(3.0, 0.002))
        self.assertGreaterEqual(end, len(quiet) + len(voice))

    def test_timeout_when_nobody_speaks(self):
        self.assertIsNone(stt.find_utterance(tone(8.0, 0.002)))

    def test_dbfs_scale(self):
        self.assertAlmostEqual(stt.dbfs(tone(0.5, 1.0)), -3.0, delta=0.2)
        self.assertLess(stt.dbfs(b"\x00\x00" * 100), -100)


if __name__ == "__main__":
    unittest.main()
