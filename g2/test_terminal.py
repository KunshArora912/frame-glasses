"""Terminal Mode tests: transcript parsing, hooks, the hub, managed sessions
and the Terminal app, all on fakes and temp dirs. No glasses, no Claude Code.

    python -m unittest g2.test_terminal
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from g2 import terminal as T  # noqa: E402
from g2 import terminal_hook as H  # noqa: E402
from g2.apps.terminal import HubClient, Terminal  # noqa: E402
from g2.fakes import Clock, FakeGateway, FakeSpeech  # noqa: E402
from g2.launcher import Launcher  # noqa: E402

NOW = 1_800_000_000.0


def iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".000Z"


def rec_user(text, t, cwd="C:\\work\\demo"):
    return {"type": "user", "cwd": cwd, "timestamp": iso(t), "sessionId": "s1",
            "message": {"role": "user", "content": text}}


def rec_result(t, cwd="C:\\work\\demo"):
    return {"type": "user", "cwd": cwd, "timestamp": iso(t),
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]}}


def rec_asst(t, text=None, tool=None, stop="end_turn", cwd="C:\\work\\demo", side=False):
    content = []
    if text:
        content.append({"type": "text", "text": text})
    if tool:
        content.append({"type": "tool_use", "id": "x", "name": tool[0], "input": tool[1]})
    return {"type": "assistant", "cwd": cwd, "timestamp": iso(t), "isSidechain": side,
            "message": {"role": "assistant", "content": content, "stop_reason": stop}}


def lines(*recs):
    return [json.dumps(r) for r in recs]


def check_screen(tc, s, label):
    rows = s.splitlines()
    tc.assertLessEqual(len(rows), 10, f"{label}: {len(rows)} rows")
    for line in rows:
        tc.assertLessEqual(len(line), 48, f"{label}: {line!r}")


# ===================================================================== parsing
class TranscriptTests(unittest.TestCase):
    def test_busy_while_a_tool_runs(self):
        info = T.parse_transcript_lines(lines(
            rec_user("fix the tests", NOW - 30),
            rec_asst(NOW - 20, text="Running them.", tool=("Bash", {"command": "pytest -q", "description": "run"}), stop="tool_use"),
        ), "s1", "C--work-demo", now=NOW)
        self.assertEqual(info.status, "busy")
        self.assertEqual(info.project, "demo")
        self.assertEqual(info.last_tool, "Bash pytest -q")
        self.assertEqual(info.last_text, "Running them.")
        self.assertAlmostEqual(info.last_activity, NOW - 20, delta=1)

    def test_idle_after_end_turn_with_title_and_text(self):
        info = T.parse_transcript_lines(lines(
            rec_user("hi", NOW - 60),
            rec_asst(NOW - 50, tool=("Edit", {"file_path": "C:\\work\\demo\\app.py"}), stop="tool_use"),
            rec_result(NOW - 45),
            rec_asst(NOW - 40, text="Done: edited app.py."),
        ) + [json.dumps({"type": "ai-title", "aiTitle": "Fix the app"})], "s1", now=NOW)
        self.assertEqual(info.status, "idle")
        self.assertEqual(info.title, "Fix the app")
        self.assertEqual(info.last_tool, "Edit app.py")
        self.assertEqual(info.last_text, "Done: edited app.py.")

    def test_tool_result_pending_reply_is_busy_but_stale_is_idle(self):
        recs = lines(rec_user("go", NOW - 30), rec_asst(NOW - 25, tool=("Read", {"file_path": "x.py"}), stop="tool_use"),
                     rec_result(NOW - 20))
        self.assertEqual(T.parse_transcript_lines(recs, "s1", now=NOW).status, "busy")
        self.assertEqual(T.parse_transcript_lines(recs, "s1", now=NOW + T.STALE_S + 60).status, "idle")

    def test_garbage_sidechains_and_long_text(self):
        recs = ["not json", "", "[1,2]"] + lines(
            rec_user("go", NOW - 30),
            rec_asst(NOW - 20, text="x" * 1000),
            rec_asst(NOW - 10, text="from a subagent", stop="tool_use", side=True),
        )
        info = T.parse_transcript_lines(recs, "s1", now=NOW)
        self.assertEqual(info.status, "idle")
        self.assertEqual(len(info.last_text), T.TEXT_CHARS)
        self.assertNotIn("subagent", info.last_text)

    def test_watcher_takes_newest_n_and_notices_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "C--work-a").mkdir()
            (root / "C--work-b").mkdir()
            for i, (d, sid) in enumerate([("C--work-a", "old"), ("C--work-a", "mid"), ("C--work-b", "new")]):
                p = root / d / f"{sid}.jsonl"
                p.write_text("\n".join(lines(rec_user("q", NOW), rec_asst(NOW, text=f"answer {sid}"))) + "\n", encoding="utf-8")
                os.utime(p, (NOW + i, NOW + i))
            w = T.TranscriptWatcher(root, limit=2, clock=lambda: NOW)
            infos, changed = w.scan()
            self.assertTrue(changed)
            self.assertEqual(set(infos), {"mid", "new"})
            self.assertEqual(infos["new"].last_text, "answer new")
            _, changed = w.scan()
            self.assertFalse(changed)
            p = root / "C--work-b" / "new.jsonl"
            with p.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec_asst(NOW + 5, text="later", tool=("Bash", {"command": "ls"}), stop="tool_use")) + "\n")
            os.utime(p, (NOW + 10, NOW + 10))
            infos, changed = w.scan()
            self.assertTrue(changed)
            self.assertEqual(infos["new"].last_text, "later")


# ==================================================================== registry
class RegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_permission_request_makes_session_wait_and_decision_resolves_it(self):
        clock = Clock(NOW)
        r = T.Registry(clock)
        r.apply_transcripts({"s1": T.parse_transcript_lines(lines(
            rec_user("go", NOW - 5), rec_asst(NOW - 4, tool=("Bash", {"command": "rm -rf build"}), stop="tool_use")), "s1", now=NOW)})
        self.assertEqual(r.summary(r.sessions["s1"])["status"], "busy")
        p = r.hook_event("PermissionRequest", {"session_id": "s1", "cwd": "C:\\work\\demo", "hook_event_name": "PermissionRequest",
                                               "tool_name": "Bash", "tool_input": {"command": "rm -rf build"}})
        sm = r.summary(r.sessions["s1"])
        self.assertEqual(sm["status"], "waiting")
        self.assertEqual(sm["pending"]["summary"], "Bash rm -rf build")
        self.assertEqual(r.ordered()[0].id, "s1")
        self.assertTrue(r.decide("s1", "allow"))
        self.assertEqual(await p.future, {"behavior": "allow"})
        self.assertEqual(r.summary(r.sessions["s1"])["status"], "busy")
        self.assertFalse(r.decide("s1", "deny"))            # nothing pending any more

    async def test_hook_events_drive_status_and_text(self):
        clock = Clock(NOW)
        r = T.Registry(clock)
        r.hook_event("UserPromptSubmit", {"session_id": "s2", "cwd": "C:\\x\\proj", "prompt": "hello"})
        self.assertEqual(r.summary(r.sessions["s2"])["status"], "busy")
        self.assertEqual(r.sessions["s2"].project, "proj")
        clock.t += 3
        r.hook_event("Stop", {"session_id": "s2", "last_assistant_message": "All done."})
        sm = r.summary(r.sessions["s2"])
        self.assertEqual((sm["status"], sm["last_text"]), ("idle", "All done."))

    async def test_input_is_read_only_unless_a_claude_console_shares_the_folder(self):
        r = T.Registry(Clock(NOW))
        r.hook_event("SessionStart", {"session_id": "s3", "cwd": "C:\\work\\demo"})
        ok, why = r.send_input("s3", "hello", enter=True)
        self.assertFalse(ok)
        self.assertIn("read-only", why)
        fake = FakePty()
        m = T.ManagedSession(["claude"], cwd="C:\\work\\demo", spawn=lambda *a, **k: fake)
        m.start()
        r.add_managed(m)
        ok, _ = r.send_input("s3", "hello", enter=True)
        self.assertTrue(ok)
        self.assertEqual(fake.written, ["hello\r"])
        self.assertEqual(r.summary(r.sessions["s3"])["input"], m.id)
        m.stop()


# ======================================================================= hooks
class HookSettingsTests(unittest.TestCase):
    def test_merge_is_non_destructive_and_idempotent(self):
        user_hook = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo mine"}]}
        settings = {"model": "x", "hooks": {"PreToolUse": [user_hook]}}
        new, changes = T.merge_hooks(settings, python="py.exe", script=Path("C:/r/g2/terminal_hook.py"))
        self.assertEqual(new["model"], "x")
        self.assertEqual(new["hooks"]["PreToolUse"][0], user_hook)
        self.assertEqual(len(new["hooks"]["PreToolUse"]), 2)
        for ev in T.HOOK_EVENTS:
            self.assertIn(ev, new["hooks"])
        perm = new["hooks"]["PermissionRequest"][0]["hooks"][0]
        self.assertEqual(perm["type"], "command")
        self.assertEqual(perm["timeout"], 60)
        self.assertIn("terminal_hook.py", perm["command"])
        self.assertTrue(perm["command"].endswith(" PermissionRequest"))
        self.assertNotIn("async", perm)                       # the decision hook must block
        self.assertTrue(new["hooks"]["Stop"][0]["hooks"][0]["async"])
        again, changes2 = T.merge_hooks(new, python="py.exe", script=Path("C:/r/g2/terminal_hook.py"))
        self.assertEqual(changes2, [])
        self.assertEqual(again, new)
        moved, changes3 = T.merge_hooks(new, python="other.exe", script=Path("C:/r/g2/terminal_hook.py"))
        self.assertTrue(all(c.startswith("updated") for c in changes3))
        self.assertEqual(len(moved["hooks"]["PreToolUse"]), 2)
        self.assertEqual(settings, {"model": "x", "hooks": {"PreToolUse": [user_hook]}})   # input untouched

    def test_unmerge_restores_and_files_get_backups(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            original = {"theme": "dark", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "beep"}]}]}}
            path.write_text(json.dumps(original), encoding="utf-8")
            T._rewrite_settings(path, lambda s: T.merge_hooks(s, python="py.exe"))
            installed = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(installed["hooks"]["Stop"]), 2)
            self.assertEqual(len(list(Path(tmp).glob("settings.json.bak-*"))), 1)
            self.assertEqual(T._rewrite_settings(path, lambda s: T.merge_hooks(s, python="py.exe")), [])
            self.assertEqual(len(list(Path(tmp).glob("settings.json.bak-*"))), 1)   # no change, no backup
            T._rewrite_settings(path, T.unmerge_hooks)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original)
            self.assertEqual(T.unmerge_hooks({"a": 1}), ({"a": 1}, []))


class HookScriptTests(unittest.TestCase):
    def test_permission_decision_round_trip_with_injected_answers(self):
        calls = []

        def poster(answer):
            def p(url, body, timeout):
                calls.append((url, body, timeout))
                return answer
            return p
        data = {"session_id": "s1", "hook_event_name": "PermissionRequest", "tool_name": "Bash", "tool_input": {"command": "ls"}}
        out = H.run_hook("PermissionRequest", data, hub="http://h", approval_timeout=7, poster=poster({"decision": {"behavior": "allow"}}))
        self.assertEqual(json.loads(out), {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}})
        self.assertEqual(calls[-1][0], "http://h/hook/PermissionRequest?timeout=7")
        self.assertEqual(calls[-1][1], data)
        out = H.run_hook("PermissionRequest", data, hub="http://h", poster=poster({"decision": {"behavior": "deny", "message": "use make"}}))
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["decision"], {"behavior": "deny", "message": "use make"})
        self.assertEqual(H.run_hook("PermissionRequest", data, hub="http://h", poster=poster({"decision": None})), "")
        self.assertEqual(H.run_hook("PermissionRequest", data, hub="http://h", poster=poster(None)), "")   # hub down
        self.assertEqual(H.run_hook("Stop", {"session_id": "s1"}, hub="http://h", poster=poster({"ok": True})), "")
        self.assertEqual(calls[-1][0], "http://h/hook/Stop")

    def test_main_never_fails_without_a_hub(self):
        import io
        old_in, old_out = sys.stdin, sys.stdout
        try:
            sys.stdin = io.TextIOWrapper(io.BytesIO(b'{"session_id": "z"}'))
            sys.stdout = io.StringIO()
            with unittest.mock.patch.object(H, "HUB", "http://127.0.0.1:9"):
                self.assertEqual(H.main(["Notification"]), 0)
            self.assertEqual(sys.stdout.getvalue(), "")
        finally:
            sys.stdin, sys.stdout = old_in, old_out


import unittest.mock  # noqa: E402


# ============================================================ managed sessions
class FakePty:
    def __init__(self, chunks=()):
        self.chunks = list(chunks)
        self.written: list[str] = []
        self.alive = True
        self._ev = __import__("threading").Event()

    def read(self, n):
        if self.chunks:
            return self.chunks.pop(0)
        self._ev.wait(5)
        raise EOFError

    def write(self, s):
        self.written.append(s)

    def isalive(self):
        return self.alive

    def terminate(self):
        self.alive = False
        self._ev.set()


class ManagedTests(unittest.TestCase):
    def test_pyte_renders_ansi_into_the_glasses_grid(self):
        data = ("\x1b[2J\x1b[H\x1b[1;32mgreen\x1b[0m plain\r\n"
                + "y" * 60 + "\r\n"
                + "\x1b[5;10Hplaced" + "\x1b[9;1H$ ")
        rows = T.render_ansi(data, 48, 9)
        self.assertEqual(len(rows), 9)
        self.assertTrue(all(len(r) <= 48 for r in rows))
        self.assertEqual(rows[0], "green plain")
        self.assertEqual(rows[1], "y" * 48)            # auto-wrap at the right edge
        self.assertEqual(rows[2], "y" * 12)
        self.assertEqual(rows[4], " " * 9 + "placed")
        self.assertEqual(rows[8], "$")

    def test_prompt_heuristic(self):
        self.assertTrue(T.prompt_pending(["output", "C:\\work>", ""], 10.0, 12.0))
        self.assertFalse(T.prompt_pending(["output", "C:\\work>"], 10.0, 11.0))         # not quiet long enough
        self.assertFalse(T.prompt_pending(["compiling 3/9 files"], 0.0, 99.0))           # no prompt shape
        self.assertTrue(T.prompt_pending(["Overwrite file? (y/n)"], 0.0, 5.0))
        self.assertTrue(T.prompt_pending(["PS C:\\Users\\me> "], 0.0, 5.0))
        self.assertTrue(T.prompt_pending(["> "], 0.0, 5.0))
        self.assertFalse(T.prompt_pending(["", ""], 0.0, 5.0))

    def test_managed_session_feeds_screen_history_and_keys(self):
        clock = Clock(0.0)
        fake = FakePty()
        m = T.ManagedSession(["powershell"], cols=48, rows=9, clock=clock, spawn=lambda *a, **k: fake)
        m.start()
        m.feed("".join(f"line {i}\r\n" for i in range(20)) + "PS C:\\> ")
        self.assertEqual(m.lines()[-1], "PS C:\\>")
        self.assertIn("line 0", m.history_lines())
        self.assertFalse(m.needs_input())
        clock.t += 2
        self.assertTrue(m.needs_input())
        m.write("dir", enter=True)
        m.write(key="ctrl-c")
        m.write(key="escape")
        m.write(key="y")
        self.assertEqual(fake.written, ["dir\r", "\x03", "\x1b", "y"])
        fake.alive = False
        self.assertFalse(m.alive)
        self.assertFalse(m.needs_input())


# ============================================================ server end to end
class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = T.TerminalServer(port=0, projects_dir=Path(self.tmp.name), scan_interval=0.05, approval_timeout=5)
        await self.server.start()
        self.base = f"http://127.0.0.1:{self.server.port}"

    async def asyncTearDown(self):
        await self.server.stop()
        self.tmp.cleanup()

    async def test_binds_localhost_and_hook_waits_for_a_ws_decision(self):
        import aiohttp
        self.assertEqual(self.server.host, "127.0.0.1")
        data = {"session_id": "s9", "cwd": "C:\\work\\demo", "hook_event_name": "PermissionRequest",
                "tool_name": "Bash", "tool_input": {"command": "git push"}}
        # without any client the hook returns at once (no decision -> normal prompt)
        out = await asyncio.to_thread(H.run_hook, "PermissionRequest", data, hub=self.base, approval_timeout=5)
        self.assertEqual(out, "")
        async with aiohttp.ClientSession() as http, http.ws_connect(self.base.replace("http", "ws") + "/ws") as ws:
            first = json.loads((await ws.receive()).data)
            self.assertEqual(first["type"], "state")
            hook = asyncio.create_task(asyncio.to_thread(H.run_hook, "PermissionRequest", data, hub=self.base, approval_timeout=5))
            while True:
                m = json.loads((await asyncio.wait_for(ws.receive(), 5)).data)
                if m["type"] == "state" and any(s.get("pending") for s in m["sessions"]):
                    break
            s = next(s for s in m["sessions"] if s["id"] == "s9")
            self.assertEqual(s["status"], "waiting")
            await ws.send_str(json.dumps({"type": "decide", "session": "s9", "behavior": "deny", "message": "not today"}))
            out = await asyncio.wait_for(hook, 10)
        self.assertEqual(json.loads(out), {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                                                  "decision": {"behavior": "deny", "message": "not today"}}})

    async def test_http_input_reaches_a_managed_session(self):
        import aiohttp
        fake = FakePty()
        m = T.ManagedSession(["cmd"], spawn=lambda *a, **k: fake)
        m.start(asyncio.get_running_loop())
        self.server.add_managed(m)
        async with aiohttp.ClientSession() as http:
            async with http.post(f"{self.base}/sessions/{m.id}/input", json={"text": "echo hi", "enter": True}) as r:
                self.assertEqual(r.status, 200)
            async with http.post(f"{self.base}/sessions/{m.id}/input", json={"key": "ctrl-c"}) as r:
                self.assertEqual(r.status, 200)
            async with http.post(f"{self.base}/sessions/nope/input", json={"text": "x"}) as r:
                self.assertEqual(r.status, 409)
            async with http.get(f"{self.base}/") as r:
                self.assertIn("G2 Terminal", await r.text())
        self.assertEqual(fake.written, ["echo hi\r", "\x03"])
        m.stop()


# =================================================================== the app
class FakeHub:
    """Stands in for HubClient: the test pushes messages, the app's sends are recorded."""

    def __init__(self):
        self.q: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.up = True

    def factory(self):
        hub = self

        class Client:
            async def __aenter__(self_inner):
                if not hub.up:
                    raise ConnectionRefusedError("hub down")
                return self_inner

            async def __aexit__(self_inner, *exc):
                return False

            def __aiter__(self_inner):
                return self_inner

            async def __anext__(self_inner):
                m = await hub.q.get()
                if m is None:
                    raise StopAsyncIteration
                return m

            async def send(self_inner, msg):
                hub.sent.append(msg)
        return Client()


def transcript_session(sid="s1", project="demo", status="idle", text="Hello. " * 30, pending=None, age=42):
    return {"id": sid, "kind": "transcript", "project": project, "title": "t", "status": status, "age_s": age,
            "last_text": text, "last_tool": "Bash pytest -q", "pending": pending}


def managed_session(sid="term-1", needs=False, n=30):
    hist = [f"old {i}" for i in range(n)]
    return {"id": sid, "kind": "managed", "project": "frame", "status": "waiting" if needs else "busy", "age_s": 1,
            "last_text": "", "last_tool": "", "pending": None, "command": "powershell",
            "screen": [f"row {i}" for i in range(8)] + ["PS C:\\>"], "history": hist, "needs_input": needs}


class AppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.gw = FakeGateway()
        self.speech = FakeSpeech(self.gw, text="use npm instead")
        self.hub = FakeHub()
        self.app = Terminal()
        self.app.client_factory = self.hub.factory
        self.app.min_redraw_s = 0
        self.app.retry_s = 0.01
        self.la = Launcher(self.gw, self.speech, None, None, apps=[self.app], clock=Clock(),
                           data_dir=Path(self.tmp.name), transcripts_dir=Path(self.tmp.name) / "tr")

    async def asyncTearDown(self):
        await self.la.stop()
        self.tmp.cleanup()

    async def pump(self, n=20):
        for _ in range(n):
            await asyncio.sleep(0)

    async def state(self, *sessions):
        await self.hub.q.put({"type": "state", "sessions": list(sessions)})
        await self.pump()

    async def go(self, *gestures):
        for g in gestures:
            await self.gw.emit("touch", g, 1)
            if self.app._voice_task is not None:
                await self.app._voice_task
                self.app._voice_task = None
            await self.pump()

    async def test_shows_a_clear_message_when_the_hub_is_not_running(self):
        self.hub.up = False
        await self.la.open("terminal")
        await asyncio.sleep(0.05)
        s = self.gw.shown[-1]
        self.assertIn("Terminal hub is not running.", s)
        self.assertIn("python g2/terminal.py serve", s)
        check_screen(self, s, "offline")
        self.hub.up = True                       # server comes back: the app reconnects by itself
        await asyncio.sleep(0.05)
        await self.state(transcript_session())
        self.assertIn("demo", self.gw.shown[-1])

    async def test_list_moves_and_opens_a_transcript_session(self):
        await self.la.open("terminal")
        await self.state(transcript_session("a", "alpha", "busy"), transcript_session("b", "beta", "idle", text="Short reply."))
        s = self.gw.shown[-1]
        self.assertIn(">* alpha", s)
        self.assertIn(" - beta", s)
        check_screen(self, s, "list")
        await self.go("swipe_down")
        self.assertIn(">- beta", self.gw.shown[-1])
        await self.go("single_tap")
        s = self.gw.shown[-1]
        self.assertTrue(s.splitlines()[0].startswith("- beta"))
        self.assertIn("tool: Bash pytest -q", s)
        self.assertIn("Short reply.", s)
        self.assertEqual(len(s.splitlines()), 10)
        check_screen(self, s, "session")
        await self.go("long_press_release")                   # nothing pending: back to the list
        self.assertIn(">- beta", self.gw.shown[-1])

    async def test_transcript_view_scrolls(self):
        await self.la.open("terminal")
        long = " ".join(f"w{i:03d}" for i in range(80))
        await self.state(transcript_session(text=long))
        await self.go("single_tap")
        first = self.gw.shown[-1]
        self.assertIn("w000", first)
        await self.go("swipe_down")
        self.assertNotIn("w000", self.gw.shown[-1])
        self.assertIn("w079", self.gw.shown[-1])
        await self.go("swipe_up")
        self.assertEqual(self.gw.shown[-1], first)

    async def test_approve_with_hold_and_deny_with_swipe_up(self):
        await self.la.open("terminal")
        pend = {"id": "perm-1", "tool_name": "Bash", "summary": "Bash rm -rf build", "age_s": 2}
        await self.state(transcript_session(status="waiting", pending=pend))
        self.assertIn("ask", self.gw.shown[-1])
        await self.go("single_tap")
        s = self.gw.shown[-1]
        self.assertIn("APPROVE? Bash rm -rf build", s)
        self.assertIn("hold allow  up deny", s)
        await self.go("long_press_release")
        self.assertEqual(self.hub.sent[-1], {"type": "decide", "session": "s1", "behavior": "allow"})
        self.assertIn("approved", self.gw.shown[-1])
        await self.go("swipe_up")
        self.assertEqual(self.hub.sent[-1]["behavior"], "deny")
        self.assertEqual(self.app.view, "session")

    async def test_voice_reply_answers_an_approval(self):
        await self.la.open("terminal")
        pend = {"id": "perm-1", "tool_name": "Bash", "summary": "Bash npm i", "age_s": 2}
        await self.state(transcript_session(status="waiting", pending=pend))
        await self.go("single_tap", "single_tap")
        self.assertEqual(self.hub.sent[-1], {"type": "decide", "session": "s1", "behavior": "deny", "message": "use npm instead"})
        self.assertEqual(self.gw.mic_calls, [True, False])
        self.speech.text_out = "Yes, go ahead."
        await self.go("single_tap")
        self.assertEqual(self.hub.sent[-1], {"type": "decide", "session": "s1", "behavior": "allow"})

    async def test_voice_reply_types_into_a_managed_session_and_hold_sends_enter(self):
        await self.la.open("terminal")
        await self.state(managed_session(needs=True))
        self.assertIn("pty", self.gw.shown[-1])
        await self.go("single_tap")
        s = self.gw.shown[-1]
        rows = s.splitlines()
        self.assertEqual(len(rows), 10)
        self.assertEqual(rows[-1], "PS C:\\>")
        self.assertIn("waiting", rows[0])
        await self.go("single_tap")
        self.assertEqual(self.hub.sent[-1], {"type": "input", "session": "term-1", "text": "use npm instead", "enter": True})
        self.speech.text_out = "Control C."
        await self.go("single_tap")
        self.assertEqual(self.hub.sent[-1], {"type": "input", "session": "term-1", "key": "ctrl-c"})
        await self.go("long_press_release")
        self.assertEqual(self.hub.sent[-1], {"type": "input", "session": "term-1", "key": "enter"})
        await self.go("swipe_up")                                # scroll back into history
        self.assertIn("old 2", self.gw.shown[-1])
        await self.state(managed_session(needs=True))            # live updates keep the scroll position
        self.assertIn("old 2", self.gw.shown[-1])

    async def test_read_only_ack_is_shown(self):
        await self.la.open("terminal")
        await self.state(transcript_session())
        await self.go("single_tap", "single_tap")
        await self.hub.q.put({"type": "ack", "ok": False, "message": "read-only: use terminal.py run"})
        await self.pump()
        self.assertIn("read-only: use terminal.py run", self.gw.shown[-1])

    async def test_every_screen_fits_the_grid(self):
        await self.la.open("terminal")
        check_screen(self, self.gw.shown[-1], "connecting")
        many = [transcript_session(f"s{i}", "a-very-long-project-name-that-goes-on-and-on" * 2, ["busy", "idle", "waiting"][i % 3],
                                   text="z" * 400, age=i * 4000) for i in range(12)]
        await self.state(*many)
        check_screen(self, self.gw.shown[-1], "long list")
        await self.go(*["swipe_down"] * 11)
        self.assertIn(">", "".join(line[:1] for line in self.gw.shown[-1].splitlines()))
        check_screen(self, self.gw.shown[-1], "list bottom")
        await self.go("single_tap")
        check_screen(self, self.gw.shown[-1], "session")
        wide = managed_session()
        wide["screen"] = ["x" * 120] * 9
        await self.state(wide)
        check_screen(self, self.gw.shown[-1], "gone")
        await self.go("long_press_release", "single_tap")
        check_screen(self, self.gw.shown[-1], "wide managed")
        await self.state()
        check_screen(self, self.gw.shown[-1], "empty")


class AppAgainstRealHubTests(unittest.IsolatedAsyncioTestCase):
    async def test_app_follows_the_real_server_and_approves(self):
        tmp = tempfile.TemporaryDirectory()
        server = T.TerminalServer(port=0, projects_dir=Path(tmp.name), scan_interval=0.05, approval_timeout=5)
        await server.start()
        gw = FakeGateway()
        app = Terminal()
        app.min_redraw_s = 0
        base = f"http://127.0.0.1:{server.port}"
        app.client_factory = lambda: HubClient(base)
        la = Launcher(gw, FakeSpeech(gw), None, None, apps=[app], clock=Clock(), data_dir=Path(tmp.name))
        try:
            await la.open("terminal")
            data = {"session_id": "live1", "cwd": "C:\\work\\shop", "hook_event_name": "PermissionRequest",
                    "tool_name": "Write", "tool_input": {"file_path": "C:\\work\\shop\\cart.py"}}
            for _ in range(100):
                if server.clients:
                    break
                await asyncio.sleep(0.02)
            hook = asyncio.create_task(asyncio.to_thread(H.run_hook, "PermissionRequest", data, hub=base, approval_timeout=5))
            for _ in range(200):
                if "shop" in (gw.shown[-1] if gw.shown else ""):
                    break
                await asyncio.sleep(0.02)
            self.assertIn("! shop", gw.shown[-1])
            await gw.emit("touch", "single_tap", 1)
            self.assertIn("APPROVE? Write cart.py", gw.shown[-1])
            await gw.emit("touch", "long_press_release", 1)
            out = await asyncio.wait_for(hook, 10)
            self.assertEqual(json.loads(out)["hookSpecificOutput"]["decision"], {"behavior": "allow"})
        finally:
            await la.stop()
            await server.stop()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
