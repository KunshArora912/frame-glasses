"""Tests for the native Teleprompt / Translate host (g2/native_apps.py), the settings CLI
(g2/settings_cli.py) and the hidden board fallbacks (Settings, Teleprompt, Translate,
Conversate) with the fakes (g2/fakes.py) and the native FakeTransport. No glasses needed.

    python -m unittest g2.test_apps g2.test_launcher
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from g2 import apps as registry  # noqa: E402
from g2 import settings_cli  # noqa: E402
from g2.fakes import Clock, FakeBrain, FakeGateway, FakeSpeech  # noqa: E402
from g2.launcher import Launcher  # noqa: E402
from g2.native import apps as na  # noqa: E402
from g2.native import session  # noqa: E402
from g2.native import settings as ns  # noqa: E402
from g2.native import transport  # noqa: E402
from g2.native.framing import MagicAllocator  # noqa: E402
from g2.native.pb import message_field, read_bytes, read_messages, read_string, read_varint, varint_field  # noqa: E402
from g2.native_apps import NativeApps  # noqa: E402

APPS = {"settings", "teleprompter", "translate", "conversate"}


def native_responder(reject_sids=()):
    """Bonded-glasses fake plus COMM_RESP replies for the Teleprompter (0x06) and Translate (0x05) channels."""
    base = transport.fake_glasses_responder()

    def respond(side, p, raw):
        if p.sid in (na.SID_TELEPROMPTER, na.SID_TRANSLATE):
            cmd, magic = read_varint(p.payload, 1, -1), read_varint(p.payload, 2, 0)
            err = varint_field(1, 1) if p.sid in reject_sids else b""
            if p.sid == na.SID_TELEPROMPTER and cmd == na.TP_CMD_CONTROL:
                return [(side, transport.rx_frame(p.sid, na.tp_package(na.TP_CMD_COMM_RESP, magic, err)))]
            if p.sid == na.SID_TRANSLATE and cmd in (na.TR_CMD_CTRL, na.TR_CMD_RESULT):
                return [(side, transport.rx_frame(p.sid, na.tr_package(na.TR_CMD_COMM_RESP, magic, err)))]
            return None
        return base(side, p, raw)
    return respond


def app_sync_push(foreground: int, background: int = 1) -> bytes:
    return transport.rx_frame(session.SID_APP_SYNC, varint_field(1, 1) + varint_field(2, 9)
                              + message_field(3, varint_field(1, background) + varint_field(2, foreground)))


def tr_result(payload: bytes) -> tuple[str, str]:
    sub = read_bytes(payload, 4) or b""
    return read_string(sub, 1) or "", read_string(sub, 2) or ""


def tp_file_list(payload: bytes) -> list[tuple[bytes, str]]:
    sub = read_bytes(payload, 4) or b""
    return [(read_bytes(m, 1) or b"", read_string(m, 2) or "") for m in read_messages(sub, 1)]


async def wait_for(pred, timeout=1.0):
    for _ in range(int(timeout / 0.01)):
        if pred():
            return True
        await asyncio.sleep(0.01)
    return pred()


class NativeCtx:
    """The launcher's ctx plus ``native`` (what the hub provides)."""

    def __init__(self, ctx, native):
        self._ctx, self.native = ctx, native

    def __getattr__(self, name):
        return getattr(self._ctx, name)


def check_screen(tc: unittest.TestCase, s: str, label: str) -> None:
    tc.assertLessEqual(len(s.splitlines()), 10, f"{label}: {len(s.splitlines())} rows")
    for line in s.splitlines():
        tc.assertLessEqual(len(line), 48, f"{label}: {line!r}")


# ═══════════════════════════════════════════════════════════ native host

class NativeAppsCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.gw = FakeGateway()
        self.speech = FakeSpeech(self.gw)
        self.clock = Clock()
        self.brain = FakeBrain("- first point\n- second point\n- third point")
        self.apps = None

    async def asyncTearDown(self):
        if self.apps is not None:
            await self.apps.stop()
        if getattr(self, "g", None) is not None:
            await self.g.stop()
        self.tmp.cleanup()

    async def make(self, responder=None, **kw):
        self.ft = transport.FakeTransport(responder or native_responder())
        self.g = transport.Glasses(self.ft, MagicAllocator(), clock=self.clock)
        self.g.time_scale = 0.01
        await self.g.start()
        kw.setdefault("speech", self.speech)
        kw.setdefault("brain", self.brain)
        self.apps = NativeApps(self.g, scripts_dir=self.root / "scripts", transcripts_dir=self.root / "tr", clock=self.clock, **kw)
        self.apps.ACK_TIMEOUT_S = 0.3
        self.apps.attach()
        return self.apps

    def write(self, name, text):
        (self.root / "scripts").mkdir(exist_ok=True)
        (self.root / "scripts" / name).write_text(text, encoding="utf-8")

    def tp(self):
        return [na.tp_parse(p) for p in self.ft.payloads(sid=na.SID_TELEPROMPTER)]

    def tr(self):
        return [(na.tr_parse(p), p) for p in self.ft.payloads(sid=na.SID_TRANSLATE)]

    def push(self, frame):
        self.ft.push("right", frame)


class NativeTeleprompterTests(NativeAppsCase):
    async def test_menu_launch_lists_files_starts_and_serves_pages(self):
        apps = await self.make()
        self.write("talk.txt", "\n".join(f"line {i}" for i in range(1, 25)))
        self.write("notes.md", "# auto\nshort one")
        self.write("ignored.pdf", "x")
        # the glasses opened Teleprompt from their menu
        self.push(app_sync_push(na.SID_TELEPROMPTER))
        self.push(transport.rx_frame(na.SID_TELEPROMPTER, na.tp_package(na.TP_CMD_FILE_LIST_REQUEST, 3, b"")))
        self.assertTrue(await wait_for(lambda: any(m.cmd == na.TP_CMD_FILE_LIST for m in self.tp())))
        self.assertEqual(apps.foreground, na.SID_TELEPROMPTER)
        fl = [p for p in self.ft.payloads(sid=na.SID_TELEPROMPTER) if read_varint(p, 1, -1) == na.TP_CMD_FILE_LIST][0]
        self.assertEqual(tp_file_list(fl), [(b"0", "notes"), (b"1", "talk")])
        self.assertTrue((self.root / "scripts" / "README.txt").exists() is False)   # folder existed: no README
        # the user picked "talk"
        self.push(transport.rx_frame(na.SID_TELEPROMPTER, na.tp_package(na.TP_CMD_FILE_SELECT, 4, b"\x0a\x01" + b"1")))
        self.assertTrue(await wait_for(lambda: apps.current == "teleprompter"))
        start = [m for m in self.tp() if m.cmd == na.TP_CMD_CONTROL and m.value == na.TP_CTRL_START]
        self.assertEqual(len(start), 1)
        self.assertEqual(apps.script_name, "talk")
        self.assertEqual(len(apps.pages), 3)                # 24 lines, 9 per page
        # the glasses ask for page 1, then report a scroll position
        self.push(transport.rx_frame(na.SID_TELEPROMPTER, na.tp_package(na.TP_CMD_PAGE_DATA_REQUEST, 5, varint_field(1, 1))))
        self.assertTrue(await wait_for(lambda: apps.served == 1))
        page = [m for m in self.tp() if m.cmd == na.TP_CMD_PAGE_DATA]
        self.assertEqual(len(page), 1)
        raw = [p for p in self.ft.payloads(sid=na.SID_TELEPROMPTER) if read_varint(p, 1, -1) == na.TP_CMD_PAGE_DATA][0]
        sub = read_bytes(raw, 5)
        self.assertEqual(read_varint(sub, 1, 0), 1)
        self.assertEqual(read_bytes(sub, 3), na.tp_page_text(apps.pages[1]))
        self.push(transport.rx_frame(na.SID_TELEPROMPTER, na.tp_package(na.TP_CMD_PAGE_SCROLL_SYNC, 6, varint_field(1, 2) + varint_field(2, 4))))
        self.assertTrue(await wait_for(lambda: (apps.page, apps.line) == (2, 4)))
        # heartbeats carry the position (6 s scaled to 60 ms)
        self.assertTrue(await wait_for(lambda: any(m.cmd == na.TP_CMD_HEARTBEAT for m in self.tp()), 1.5))
        hb = [p for p in self.ft.payloads(sid=na.SID_TELEPROMPTER) if read_varint(p, 1, -1) == na.TP_CMD_HEARTBEAT][-1]
        self.assertEqual(read_varint(read_bytes(hb, 13), 1, 0), 2)
        # the glasses end the session themselves: no CLOSE from us
        self.push(transport.rx_frame(na.SID_TELEPROMPTER, na.tp_package(na.TP_CMD_END_SESSION_REQUEST, 7, b"")))
        self.assertTrue(await wait_for(lambda: apps.current is None))
        self.assertFalse(any(m.cmd == na.TP_CMD_CONTROL and m.value == na.TP_CTRL_CLOSE for m in self.tp()))
        self.assertEqual(apps.pages, [])

    async def test_auto_header_starts_auto_mode_and_close_sends_close(self):
        apps = await self.make()
        self.write("auto.txt", "# auto\none\ntwo")
        res = await apps.open("teleprompter", path=self.root / "scripts" / "auto.txt")
        self.assertTrue(res["ok"])
        raw = self.ft.payloads(sid=na.SID_TELEPROMPTER)[0]
        settings = read_bytes(read_bytes(raw, 3), 2)
        self.assertEqual(read_varint(settings, 1, 0), na.TP_MODE_AUTO)
        self.assertEqual(read_varint(settings, 7, 0), NativeApps.AUTO_SCROLL_MS)
        self.assertEqual(read_varint(settings, 4, 0), 1)       # pages
        self.assertEqual(read_varint(settings, 5, 0), 2)       # lines
        await apps.close()
        last = self.tp()[-1]
        self.assertEqual((last.cmd, last.value), (na.TP_CMD_CONTROL, na.TP_CTRL_CLOSE))
        self.assertIsNone(apps.current)

    async def test_start_rejected_is_retried_once_then_reported(self):
        apps = await self.make(native_responder(reject_sids={na.SID_TELEPROMPTER}))
        res = await apps.teleprompter_start("hello")
        self.assertEqual((res["acked"], res["ok"], res["error"]), (True, False, 1))
        self.assertEqual(len([m for m in self.tp() if m.value == na.TP_CTRL_START]), 2)
        self.assertIsNone(apps.current)
        self.assertIsNone(apps._hb)

    async def test_scripts_folder_created_with_readme_and_empty_open(self):
        apps = await self.make()
        files = apps.scripts()
        self.assertEqual([p.name for p in files], ["README.txt"])
        self.assertTrue(await wait_for(lambda: True))
        apps.scripts_dir = self.root / "empty"
        apps.scripts_dir.mkdir()
        res = await apps.open("teleprompter")
        self.assertFalse(res["ok"])
        self.assertIn("no scripts", res["detail"])


class NativeTranslateTests(NativeAppsCase):
    async def test_menu_launch_opens_captions_only_and_ends_on_notify_close(self):
        import g2.native_apps as mod
        self.assertFalse(mod.argos_available(), "these tests assume argostranslate is not installed")
        apps = await self.make()
        self.push(app_sync_push(na.SID_TRANSLATE))
        self.assertTrue(await wait_for(lambda: apps.current == "translate" and self.gw.mic_calls == [True]))
        msgs = self.tr()
        self.assertEqual(msgs[0][0].cmd, na.TR_CMD_CTRL)
        open_sub = read_bytes(msgs[0][1], 3)
        self.assertEqual(read_string(open_sub, 2), "EN>ES")
        self.assertEqual(read_string(open_sub, 6), "EN")
        self.assertEqual(read_string(open_sub, 7), "ES")
        langs = [(read_string(m, 1), read_string(m, 2)) for m in read_messages(open_sub, 5)]
        self.assertIn(("EN", "English"), langs)
        self.assertEqual(apps.engine, "captions only")
        notice = [tr_result(p) for m, p in msgs if m.cmd == na.TR_CMD_RESULT]
        self.assertEqual(len(notice), 1)
        self.assertIn("argospm install translate-en_es", notice[0][0])
        await self.speech.captions("hola que tal")
        results = [tr_result(p) for m, p in self.tr() if m.cmd == na.TR_CMD_RESULT]
        self.assertEqual(results[-1], ("hola que tal", " "))       # source only, nothing invented
        self.assertTrue(await wait_for(lambda: any(m.cmd == na.TR_CMD_HEARTBEAT for m, _ in self.tr()), 1.5))
        self.push(transport.rx_frame(na.SID_TRANSLATE, na.tr_package(na.TR_CMD_NOTIFY, 8, varint_field(1, na.TR_CLOSE))))
        self.assertTrue(await wait_for(lambda: apps.current is None))
        self.assertEqual(self.gw.mic_calls, [True, False])
        self.assertFalse(any(m.cmd == na.TR_CMD_CTRL and m.value == na.TR_CLOSE for m, _ in self.tr()))
        body = next((self.root / "tr").glob("translate_*.txt")).read_text(encoding="utf-8")
        self.assertIn("hola que tal", body)
        self.assertIn("# ended", body)

    async def test_translator_lang_switch_and_pair_change(self):
        async def fake_translate(text, src, dst):
            return f"[{src}>{dst}] {text.upper()}"
        apps = await self.make(translator=fake_translate)
        res = await apps.open("translate", src="EN", dst="FR")
        self.assertTrue(res["ok"])
        self.assertEqual(apps.engine, "argos")
        self.assertEqual(len([m for m, _ in self.tr() if m.cmd == na.TR_CMD_RESULT]), 0)   # nothing wrong: no notice
        await self.speech.captions("good morning")
        src, dst = [tr_result(p) for m, p in self.tr() if m.cmd == na.TR_CMD_RESULT][-1]
        self.assertEqual((src, dst), ("good morning", "[EN>FR] GOOD MORNING"))
        # the glasses switch the source language from their in-app menu
        self.push(transport.rx_frame(na.SID_TRANSLATE, na.tr_package(na.TR_CMD_LANG_SWITCH_REQ, 9, b"\x0a\x02DE")))
        self.assertTrue(await wait_for(lambda: any(m.cmd == na.TR_CMD_LANG_SWITCH_RSP for m, _ in self.tr())))
        self.assertEqual((apps.src, apps.dst), ("DE", "FR"))
        # a pair change from the laptop re-opens the session
        n = len(self.tr())
        await apps.set_pair(0)
        cmds = [(m.cmd, m.value) for m, _ in self.tr()[n:] if m.cmd == na.TR_CMD_CTRL]
        self.assertEqual(cmds, [(na.TR_CMD_CTRL, na.TR_CLOSE), (na.TR_CMD_CTRL, na.TR_OPEN)])
        self.assertEqual(apps.current, "translate")
        files = sorted((self.root / "tr").glob("translate_*.txt"))
        self.assertEqual(len(files), 2)                       # one per session, no overwrite
        self.assertIn("good morning || [EN>FR] GOOD MORNING", files[0].read_text(encoding="utf-8"))

    async def test_open_rejected_means_no_session_and_no_heartbeat(self):
        apps = await self.make(native_responder(reject_sids={na.SID_TRANSLATE}))
        res = await apps.open("translate")
        self.assertEqual((res["acked"], res["ok"]), (True, False))
        self.assertIsNone(apps.current)
        self.assertIsNone(apps._hb)
        self.assertEqual(self.gw.mic_calls, [])
        await asyncio.sleep(0.1)
        self.assertEqual([m.cmd for m, _ in self.tr()], [na.TR_CMD_CTRL])

    async def test_foreground_change_ends_the_session(self):
        apps = await self.make()
        await apps.open("translate")
        self.push(app_sync_push(0x01))                         # back on the dashboard
        self.assertTrue(await wait_for(lambda: apps.current is None))
        self.assertEqual(self.gw.mic_calls, [True, False])

    async def test_conversate_mode_transcribes_and_summarizes(self):
        apps = await self.make(menu_translate_mode="conversate")
        self.push(transport.rx_frame(na.SID_TRANSLATE, na.tr_package(na.TR_CMD_LANG_SWITCH_REQ, 9, b"")))
        self.assertTrue(await wait_for(lambda: apps.current == "conversate"))
        opened = [p for m, p in self.tr() if m.cmd == na.TR_CMD_CTRL][0]
        self.assertEqual(read_string(read_bytes(opened, 3), 2), "EN>EN")
        self.assertTrue(any(m.cmd == na.TR_CMD_LANG_SWITCH_RSP for m, _ in self.tr()))
        await self.speech.captions("we ship on friday")
        await self.speech.captions("and tell the team")
        results = [tr_result(p) for m, p in self.tr() if m.cmd == na.TR_CMD_RESULT]
        self.assertEqual(results[-1], ("and tell the team", "and tell the team"))
        await apps.close()
        results = [tr_result(p) for m, p in self.tr() if m.cmd == na.TR_CMD_RESULT]
        self.assertEqual(results[-1][0], "Summary")
        self.assertIn("- first point", results[-1][1])
        last = self.tr()[-1][0]
        self.assertEqual((last.cmd, last.value), (na.TR_CMD_CTRL, na.TR_CLOSE))
        body = next((self.root / "tr").glob("conversate_*.txt")).read_text(encoding="utf-8")
        self.assertIn("we ship on friday", body)
        self.assertIn("## Summary\n- first point", body)

    async def test_conversate_on_glasses_menu_is_not_decoded(self):
        apps = await self.make()
        with self.assertLogs("g2.native_apps", level="WARNING") as cm:
            self.push(app_sync_push(0x0B))
            self.assertTrue(await wait_for(lambda: apps.foreground == 0x0B))
            await asyncio.sleep(0.02)
        self.assertTrue(any("SID 0x0B" in line for line in cm.output))
        self.assertIsNone(apps.current)


# ══════════════════════════════════════════════════════════ settings CLI

class SettingsCliTests(unittest.TestCase):
    def run_cli(self, *argv, responder=None):
        out: list[str] = []
        ft = transport.FakeTransport(responder or transport.fake_glasses_responder())
        rc = settings_cli.main(["--timeout", "0.3", *argv], transport_factory=lambda base: ft, out=out.append)
        return rc, out, ft

    def test_dry_run_prints_frames_and_sends_nothing(self):
        rc, out, ft = self.run_cli("set", "brightness", "20", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertEqual(ft.writes, [])
        self.assertTrue(out[0].startswith("sid 0x09 payload 0801"))
        self.assertIn("(LIVE)", out[0])
        self.assertTrue(any(line.startswith("frame aa21") for line in out))
        self.assertIn("'level': 20", out[-1])
        rc, out, _ = self.run_cli("set", "height", "10", "--dry-run")
        self.assertIn("(DECODED)", out[0])
        self.assertIn("'display_height': 10", out[-1])

    def test_show_reads_snapshot(self):
        rc, out, ft = self.run_cli("show")
        self.assertEqual(rc, 0)
        text = "\n".join(out)
        self.assertIn("brightness 30", text)
        self.assertIn("height     10  (DECODED set)", text)
        self.assertIn("battery    87%", text)
        self.assertEqual([read_varint(p, 1, -1) for p in ft.payloads(sid=ns.SID_SETTINGS)], [ns.CMD_READ])

    def test_set_applies_and_reads_back(self):
        rc, out, ft = self.run_cli("set", "angle", "40")
        self.assertEqual(rc, 0)
        sets = [ns.parse(p).set for p in ft.payloads(sid=ns.SID_SETTINGS) if read_varint(p, 1, -1) == ns.CMD_SET]
        self.assertEqual(sets, [{"head_up": {"enabled": 1, "angle": 40, "calibration_switch": 0, "calibration": 0}}])
        self.assertIn("set angle 40: acked (LIVE)", out)
        self.assertIn("readback angle: 25", out)                 # the fake always reports 25
        rc, out, ft = self.run_cli("set", "timeout", "never")
        mod = [p for p in ft.payloads(sid=ns.SID_MODULE) if read_varint(p, 1, -1) == ns.MOD_CMD_SET_TIMEOUT]
        self.assertEqual(ns.parse_module(mod[0]).timeout, ns.TIMEOUT_NEVER)

    def test_bad_values_and_keys(self):
        self.assertEqual(settings_cli.main(["set", "brightness", "99", "--dry-run"]), 2)
        self.assertEqual(settings_cli.main(["set", "wear", "maybe", "--dry-run"]), 2)
        self.assertEqual(settings_cli.main(["set", "nope", "1", "--dry-run"]), 2)
        rc, out, _ = self.run_cli("keys")
        self.assertTrue(any(line.startswith("height") and line.endswith("DECODED") for line in out))


# ═══════════════════════════════════════════════════════ board fallbacks

class AppCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.gw = FakeGateway()
        self.speech = FakeSpeech(self.gw)
        self.clock = Clock()
        self.brain = FakeBrain("- first point\n- second point\n- third point")
        self.la = Launcher(self.gw, self.speech, self.brain, None, apps=registry.discover(only=APPS),
                           clock=self.clock, transcripts_dir=Path(self.tmp.name) / "tr", data_dir=Path(self.tmp.name))
        self.glasses = None

    async def asyncTearDown(self):
        await self.la.stop()
        if self.glasses is not None:
            await self.glasses.stop()
        self.tmp.cleanup()

    async def with_native(self, responder=None):
        self.ft = transport.FakeTransport(responder or transport.fake_glasses_responder())
        self.glasses = transport.Glasses(self.ft, MagicAllocator(), clock=self.clock)
        self.glasses.time_scale = 0.01
        await self.glasses.start()
        self.la.ctx = NativeCtx(self.la.ctx, self.glasses)
        return self.glasses

    async def go(self, *gestures):
        for g in gestures:
            await self.gw.emit("touch", g, 1)
            await self.settle()

    async def settle(self):
        if self.la.busy is not None and not self.la.busy.done():
            try:
                await asyncio.wait_for(asyncio.shield(self.la.busy), 5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        await asyncio.sleep(0)

    @property
    def screen(self) -> str:
        return self.gw.shown[-1]

    def check(self, label: str) -> None:
        check_screen(self, self.screen, label)

    def set_payloads(self, sid=ns.SID_SETTINGS):
        return self.ft.payloads(sid=sid)

    def sets(self, sid=ns.SID_SETTINGS):
        return [ns.parse(p) for p in self.set_payloads(sid) if read_varint(p, 1, -1) == ns.CMD_SET]


class SettingsBoardTests(AppCase):
    async def test_hidden_and_without_native_shows_a_message(self):
        self.assertNotIn("Settings", [n for n, _ in self.la.menu])
        await self.la.open("settings")
        self.assertIn("native link is not up", self.screen)
        self.check("settings no native")
        n = len(self.gw.shown)
        await self.go("swipe_down", "single_tap")
        self.assertEqual(len(self.gw.shown), n)

    async def test_reads_snapshot_on_open(self):
        await self.with_native()
        await self.la.open("settings")
        await self.settle()
        s = self.screen
        self.assertIn("> brightness", s)
        self.assertRegex(s, r"brightness\s+30")
        self.assertRegex(s, r"display height\s+10  DECODED, untested")
        self.assertRegex(s, r"head-up display\s+on")
        self.check("settings list")
        reads = [p for p in self.set_payloads() if read_varint(p, 1, -1) == ns.CMD_READ]
        self.assertEqual(len(reads), 1)
        self.assertEqual(read_varint(self.set_payloads(ns.SID_MODULE)[0], 1, -1), ns.MOD_CMD_READ_TIMEOUT)

    async def test_edit_brightness_applies_and_rereads(self):
        await self.with_native()
        await self.la.open("settings")
        await self.settle()
        await self.go("single_tap", "swipe_down", "swipe_down")        # edit, 30 -> 40
        self.assertRegex(self.screen, r"\* brightness\s+40")
        self.check("settings editing")
        await self.go("single_tap")                                    # apply
        sets = self.sets()
        self.assertEqual(sets[0].set, {"brightness": {"auto": 0, "level": 40, "left_calibration": 0, "right_calibration": 0}})
        reads = [p for p in self.set_payloads() if read_varint(p, 1, -1) == ns.CMD_READ]
        self.assertEqual(len(reads), 2, "re-read after apply")
        self.assertIn("brightness: acked, readback 30", self.screen)   # the fake always reports 30
        self.check("settings applied")

    async def test_scroll_to_the_end_and_edit_display_timeout_uses_sid_0x20(self):
        await self.with_native()
        await self.la.open("settings")
        await self.settle()
        await self.go(*["swipe_down"] * 12)
        self.assertIn("> display timeout", self.screen)
        self.assertNotIn("brightness", self.screen)
        self.check("settings bottom")
        await self.go("single_tap", "swipe_down", "swipe_down", "single_tap")      # tap: ? -> 3; swipes: 5, 10
        mod = [p for p in self.set_payloads(ns.SID_MODULE) if read_varint(p, 1, -1) == ns.MOD_CMD_SET_TIMEOUT]
        self.assertEqual(ns.parse_module(mod[0]).timeout, 10)
        self.assertEqual(self.sets(), [])
        self.assertIn("display timeout: acked", self.screen)

    async def test_head_up_angle_flags_units_and_decoded_builders(self):
        await self.with_native()
        await self.la.open("settings")
        await self.settle()
        await self.go(*["swipe_down"] * 5, "single_tap", "swipe_down", "single_tap")      # angle 25 -> 30
        await self.go("swipe_down", "single_tap", "swipe_down", "single_tap")             # wear on -> off
        await self.go("swipe_down", "single_tap", "swipe_down", "single_tap")             # silent off -> on
        await self.go("swipe_down", "single_tap", "swipe_down", "single_tap")             # time 12h -> 24h
        await self.go(*["swipe_up"] * 6, "single_tap", "swipe_up", "long_press_release")  # height: cancel
        self.assertIn("cancelled", self.screen)
        await self.go("single_tap", "swipe_up", "single_tap")                              # height 10 -> 9
        self.assertEqual([s.set for s in self.sets()], [
            {"head_up": {"enabled": 1, "angle": 30, "calibration_switch": 0, "calibration": 0}},
            {"wear_detection": 0},
            {"silent_mode": 1},
            {"units": {"unit_format": 0, "distance_unit": 0, "time_format": 0, "date_format": 0, "temperature_unit": 1}},
            {"display_height": 9},
        ])
        self.check("settings after edits")

    async def test_no_reply_is_reported(self):
        await self.with_native(responder=lambda side, p, raw: None)
        self.la.find_app("settings").timeout_s = 0.05
        await self.la.open("settings")
        await self.settle()
        self.assertIn("no reply from the glasses", self.screen)
        self.check("settings no reply")


class TeleprompterBoardTests(AppCase):
    def folder(self) -> Path:
        return Path(self.tmp.name) / "teleprompter"

    def write(self, name: str, text: str) -> Path:
        self.folder().mkdir(exist_ok=True)
        p = self.folder() / name
        p.write_text(text, encoding="utf-8")
        return p

    async def test_first_open_creates_folder_with_readme(self):
        self.assertNotIn("Teleprompt", [n for n, _ in self.la.menu])
        await self.la.open("teleprompter")
        self.assertTrue((self.folder() / "README.txt").exists())
        self.assertIn("> README.txt", self.screen)
        self.check("teleprompter list")
        await self.go("single_tap")
        self.assertIn("Teleprompt reads .txt and .md files", self.screen)
        self.check("teleprompter readme page 1")

    async def test_paging_with_swipes_and_tap_back(self):
        self.write("talk.txt", "\n".join(f"line {i}" for i in range(1, 21)))
        self.write("notes.md", "# hi")
        self.write("skip.pdf", "x")
        await self.la.open("teleprompter")
        self.assertIn("> notes.md", self.screen)
        self.assertNotIn("skip.pdf", self.screen)
        await self.go("swipe_down", "single_tap")
        self.assertIn("line 1\n", self.screen)
        self.assertIn("line 8", self.screen)
        self.assertNotIn("line 9", self.screen)
        self.assertIn("1/3", self.screen)
        self.check("talk p1")
        await self.go("swipe_down")
        self.assertIn("line 9", self.screen)
        self.assertIn("2/3", self.screen)
        await self.go("swipe_down", "swipe_down", "swipe_down")
        self.assertIn("3/3", self.screen)
        self.assertIn("line 20", self.screen)
        self.check("talk p3")
        await self.go("swipe_up", "swipe_up", "swipe_up")
        self.assertIn("1/3", self.screen)
        await self.go("single_tap")
        self.assertIn("> talk.txt", self.screen)

    async def test_auto_scroll_with_fake_clock(self):
        self.write("talk.txt", "\n".join(f"line {i}" for i in range(1, 13)))
        await self.la.open("teleprompter")
        await self.go("single_tap")
        app = self.la.app
        await self.go("long_press_release")
        self.assertTrue(app.scrolling)
        self.assertIn("hold stop", self.screen)
        self.assertIn("3s", self.screen)
        self.check("scrolling")
        self.assertFalse(await app.scroll_tick(self.la.ctx))      # not due yet
        self.clock.t += 3
        self.assertTrue(await app.scroll_tick(self.la.ctx))
        self.assertNotIn("line 1\n", self.screen)
        self.assertIn("line 2", self.screen)
        await self.go("swipe_down")                               # faster: 2 s, re-armed from now
        self.assertEqual(app.interval, 2)
        self.assertIn("2s", self.screen)
        self.clock.t += 1
        self.assertFalse(await app.scroll_tick(self.la.ctx))
        self.clock.t += 1
        self.assertTrue(await app.scroll_tick(self.la.ctx))
        self.assertIn("line 3\n", self.screen)
        await self.go("swipe_up", "swipe_up")                     # slower: 4 s
        self.assertEqual(app.interval, 4)
        self.clock.t += 3
        self.assertFalse(await app.scroll_tick(self.la.ctx))
        await self.go("long_press_release")                       # stop
        self.assertFalse(app.scrolling)
        self.assertIn("hold auto", self.screen)
        self.clock.t += 10
        self.assertFalse(await app.scroll_tick(self.la.ctx))
        await self.go("double_tap")
        self.assertIsNone(app._scroller)

    async def test_native_flag_without_native_falls_back_to_the_page(self):
        self.write("native.txt", "# native\nhello from the firmware path")
        await self.la.open("teleprompter")
        await self.go("single_tap")
        self.assertIn("hello from the firmware path", self.screen)
        self.assertNotIn("# native", self.screen)
        self.assertEqual(self.la.app.mode, "read")
        self.assertEqual(self.la.app.status, "no native link; showing page")

    async def test_native_flag_with_native_uses_the_controller(self):
        await self.with_native(native_responder())
        self.write("native.txt", "# native\n" + "\n".join(f"line {i}" for i in range(1, 25)))
        await self.la.open("teleprompter")
        await self.go("single_tap")
        app = self.la.app
        self.assertEqual(app.mode, "native")
        self.assertIn("firmware teleprompter", self.screen)
        self.assertIn("3 pages", self.screen)
        self.check("native teleprompter")
        self.ft.push("right", transport.rx_frame(na.SID_TELEPROMPTER, na.tp_package(na.TP_CMD_PAGE_DATA_REQUEST, 7, varint_field(1, 1))))
        self.assertTrue(await wait_for(lambda: "served page 1" in self.screen))
        self.check("native teleprompter served")
        await self.go("single_tap")                                # stop -> CLOSE
        last = na.tp_parse(self.ft.payloads(sid=na.SID_TELEPROMPTER)[-1])
        self.assertEqual((last.cmd, last.value), (na.TP_CMD_CONTROL, na.TP_CTRL_CLOSE))
        self.assertEqual(app.mode, "list")
        self.assertIsNone(app.native)

    async def test_native_start_rejected_falls_back(self):
        await self.with_native(native_responder(reject_sids={na.SID_TELEPROMPTER}))
        self.write("native.txt", "# native\nfallback text")
        await self.la.open("teleprompter")
        await self.go("single_tap")
        self.assertEqual(self.la.app.mode, "read")
        self.assertIn("fallback text", self.screen)
        self.assertIn("native teleprompter failed", self.la.app.status)


class TranslateBoardTests(AppCase):
    async def test_fallback_without_argos_still_captions(self):
        self.assertNotIn("Translate", [n for n, _ in self.la.menu])
        await self.la.open("translate")
        self.assertEqual(self.gw.mic_calls, [True])
        self.assertIn("EN > ES   captions only", self.screen)
        self.assertIn("pip install argostranslate", self.screen)
        self.check("translate open")
        await self.speech.captions("hola que tal")
        self.assertIn("hola que tal", self.screen)
        self.check("translate caption")
        await self.go("swipe_down")
        self.assertIn("ES > EN", self.screen)
        await self.go("swipe_up", "swipe_up")
        self.assertIn("DE > EN", self.screen)
        await self.go("single_tap")                      # pause
        self.assertEqual(self.gw.mic_calls[-1], False)
        self.assertIn("paused", self.screen)
        await self.go("single_tap")                      # resume
        self.assertEqual(self.gw.mic_calls[-1], True)
        await self.go("double_tap")
        self.assertEqual(self.gw.mic_calls[-1], False)
        files = list((Path(self.tmp.name) / "tr").glob("translate_*.txt"))
        self.assertEqual(len(files), 1)
        self.assertIn("en>es | hola que tal", files[0].read_text(encoding="utf-8"))

    async def test_injected_translator_and_missing_package(self):
        app = self.la.find_app("translate")

        async def fake_translate(text, src, dst):
            return f"[{src}>{dst}] {text.upper()}"
        app.translator = fake_translate
        await self.la.open("translate")
        self.assertIn("EN > ES   argos", self.screen)
        await self.speech.captions("good morning")
        self.assertIn("[en>es] GOOD MORNING", self.screen)
        self.check("translate translated")
        await self.speech.captions("word " * 40)
        self.check("translate long")
        await self.go("double_tap")
        body = next((Path(self.tmp.name) / "tr").glob("translate_*.txt")).read_text(encoding="utf-8")
        self.assertIn("good morning || [en>es] GOOD MORNING", body)

        async def missing(text, src, dst):
            raise LookupError("no package")
        app.translator = missing
        await self.la.open("translate")
        await self.speech.captions("hello")
        self.assertIn("argospm install translate-en_es", self.screen)
        self.check("translate missing package")


class ConversateBoardTests(AppCase):
    async def test_transcript_summary_and_file(self):
        self.assertNotIn("Conversate", [n for n, _ in self.la.menu])
        await self.la.open("conversate")
        self.assertEqual(self.gw.mic_calls, [True])
        self.check("conversate open")
        await self.speech.captions("we should ship on friday")
        await self.speech.captions("and tell the team")
        self.assertIn("we should ship on friday and tell the team", self.screen)
        self.check("conversate live")
        await self.go("single_tap")
        self.assertIn("Summary", self.screen)
        self.assertIn("- first point", self.screen)
        self.check("conversate summary")
        await self.speech.captions("more talk")                # keeps recording while the summary shows
        self.assertIn("Summary", self.screen)
        await self.go("single_tap")                             # back to live
        self.assertIn("more talk", " ".join(self.screen.split()))
        await self.go("double_tap")
        self.assertEqual(self.gw.mic_calls[-1], False)
        body = next((Path(self.tmp.name) / "tr").glob("conversate_*.txt")).read_text(encoding="utf-8")
        self.assertIn("## Transcript", body)
        self.assertIn("more talk", body)
        self.assertIn("## Summary\n- first point", body)

    async def test_summary_before_speech_long_summary_and_no_speech(self):
        self.brain.answer = "point " * 120
        await self.la.open("conversate")
        await self.go("single_tap")
        self.assertIn("nothing to summarize yet", self.screen)
        await self.speech.captions("hello")
        await self.go("single_tap")
        self.assertIn("1/2", self.screen)
        self.check("conversate summary p1")
        await self.go("single_tap")
        self.assertIn("2/2", self.screen)
        self.check("conversate summary p2")
        await self.go("single_tap")
        self.assertIn("Conversate", self.screen.splitlines()[0])
        await self.go("double_tap")
        self.la.speech = None
        await self.la.open("conversate")
        self.assertIn("speech-to-text is not running", self.screen)
        await self.la.open("translate")
        self.assertIn("speech-to-text is not running", self.screen)


class RegistryTests(unittest.TestCase):
    def test_fallback_apps_are_hidden_with_orders(self):
        apps = registry.discover(only=APPS)
        self.assertEqual([(a.name, a.order, a.hidden) for a in apps],
                         [("Conversate", 25, True), ("Teleprompt", 40, True), ("Translate", 50, True), ("Settings", 80, True)])


if __name__ == "__main__":
    unittest.main()
