"""Terminal: follow Claude Code (and other console sessions) on the glasses,
approve its permission requests and answer it by voice. Talks only to the
local hub, g2/terminal.py on 127.0.0.1:8767; see g2/TERMINAL.md.

Session list: swipe moves, tap opens.
Session view: swipe scrolls, tap = voice reply, hold = approve (or Enter on
a waiting console; back to the list when nothing waits), swipe up while an
approval waits = deny. Double tap leaves (the launcher).
"""
from __future__ import annotations

import asyncio
import json
import os
import re

from g2.apps.base import App, AppContext, wrap

HUB = os.environ.get("G2_TERMINAL_URL", f"http://127.0.0.1:{os.environ.get('G2_TERMINAL_PORT', '8767')}")
W = 48
LIST_ROWS = 8
TEXT_ROWS = 7
SCREEN_ROWS = 9
GLYPH = {"waiting": "!", "busy": "*", "idle": "-", "ended": "x"}
YES = re.compile(r"^\W*(yes|yeah|yep|yup|approve|approved|allow|ok|okay|go ahead|do it|sure|confirm)\b", re.I)
NO = re.compile(r"^\W*(no|nope|deny|don't|do not|stop|reject)\b", re.I)
SPOKEN_KEYS = {"enter": "enter", "press enter": "enter", "return": "enter", "escape": "escape",
               "control c": "ctrl-c", "ctrl c": "ctrl-c", "interrupt": "ctrl-c", "tab": "tab"}


def age_text(seconds) -> str:
    s = max(0, int(seconds or 0))
    return f"{s}s" if s < 60 else f"{s // 60}m" if s < 3600 else f"{s // 3600}h" if s < 86400 else f"{s // 86400}d"


def fit(s: str, width: int = W) -> str:
    s = (s or "").replace("\t", " ")
    return s if len(s) <= width else s[:width - 1] + "~"


class HubClient:
    """WebSocket to the hub. Async context manager; ``async for msg in client``."""

    def __init__(self, base: str = HUB) -> None:
        self.url = base.rstrip("/").replace("http://", "ws://").replace("https://", "wss://") + "/ws"
        self._session = self._ws = None

    async def __aenter__(self):
        import aiohttp
        self._session = aiohttp.ClientSession()
        try:
            self._ws = await self._session.ws_connect(self.url, heartbeat=20, timeout=aiohttp.ClientWSTimeout(ws_close=2))
        except BaseException:              # includes a cancel (double tap) mid-connect
            await asyncio.shield(self._session.close())
            raise
        return self

    async def __aexit__(self, *exc):
        if self._ws is not None:
            await self._ws.close()
        await self._session.close()

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        import aiohttp
        async for m in self._ws:
            if m.type == aiohttp.WSMsgType.TEXT:
                try:
                    yield json.loads(m.data)
                except ValueError:
                    continue
            elif m.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break

    async def send(self, msg: dict) -> None:
        await self._ws.send_str(json.dumps(msg))


class Terminal(App):
    name = "Terminal"
    blurb = "Claude Code on the glasses"
    order = 15

    client_factory = staticmethod(HubClient)     # tests swap this
    retry_s = 2.0
    min_redraw_s = 0.5                            # the display is slow; 4 Hz updates collapse to 2 Hz

    def __init__(self) -> None:
        self._reset()

    def _reset(self) -> None:
        self.sessions: list[dict] = []
        self.connected = False
        self.ever_connected = False
        self.error = ""
        self.view = "list"           # list | session
        self.sel = 0
        self.open_id: str | None = None
        self.top = 0                 # first visible line in the session view
        self.follow = True
        self.notice = ""
        self.voice = ""              # "" | listening | transcribing
        self._client = None
        self._voice_task: asyncio.Task | None = None
        self._late: asyncio.Task | None = None
        self._last = ""
        self._last_at = 0.0
        self._ctx: AppContext | None = None
        self._last_text_seen = ""

    # ------------------------------------------------------------ lifecycle
    async def open(self, ctx: AppContext) -> None:
        self._reset()
        self._ctx = ctx
        await self.draw(ctx, force=True)
        self._connect(ctx)

    async def close(self, ctx: AppContext) -> None:
        for t in (self._voice_task, self._late):
            if t is not None and not t.done():
                t.cancel()
        self._client = None
        self.connected = False

    async def on_link(self, ctx: AppContext, ready: bool) -> None:
        if ready and (ctx.busy is False):
            self._connect(ctx)      # the launcher cancelled our connection when the glasses went away

    def redraw_text(self):
        return self._last or None

    def _connect(self, ctx: AppContext) -> None:
        ctx.run(self._connection(ctx), "g2-terminal")

    async def _connection(self, ctx: AppContext) -> None:
        while True:
            try:
                async with self.client_factory() as client:
                    self._client = client
                    self.connected, self.ever_connected, self.error = True, True, ""
                    async for msg in client:
                        await self._on_message(ctx, msg)
                self.error = "hub closed the connection"
            except asyncio.CancelledError:
                self._client = None
                raise
            except Exception as exc:
                self.error = type(exc).__name__
            self._client = None
            self.connected = False
            await self.draw(ctx)
            await asyncio.sleep(self.retry_s)

    async def _on_message(self, ctx: AppContext, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "state":
            self.sessions = list(msg.get("sessions") or [])
            if self.sessions:
                self.sel = max(0, min(self.sel, len(self.sessions) - 1))
            await self.draw(ctx)
        elif kind == "ack":
            if not msg.get("ok"):
                self.notice = str(msg.get("message") or "failed")
                await self.draw(ctx, force=True)

    async def send(self, msg: dict) -> bool:
        if self._client is None:
            self.notice = "not connected to the hub"
            return False
        try:
            await self._client.send(msg)
            return True
        except Exception as exc:
            self.notice = f"send failed: {type(exc).__name__}"
            return False

    # -------------------------------------------------------------- lookup
    def current(self) -> dict | None:
        if self.view == "session":
            return next((s for s in self.sessions if s.get("id") == self.open_id), None)
        return self.sessions[self.sel] if self.sessions and self.sel < len(self.sessions) else None

    # ------------------------------------------------------------ rendering
    def offline_lines(self) -> list[str]:
        if not self.ever_connected and not self.error:
            return ["Connecting to the terminal hub ..."]
        return ["Terminal hub is not running.", "", "On the laptop:", "  python g2/terminal.py serve",
                "or a session:", "  python g2/terminal.py run -- claude", "", "retrying every few seconds"]

    def list_text(self, ctx: AppContext) -> str:
        header = ctx.header(self.name, "tap open  swipe move")
        if not self.connected:
            return "\n".join([header, ""] + self.offline_lines())
        if not self.sessions:
            return "\n".join([header, "", "No sessions yet.", "",
                              "Start Claude Code anywhere (hooks", "installed), or on the laptop:",
                              "  python g2/terminal.py run -- claude"])
        start = max(0, min(self.sel - LIST_ROWS + 1, len(self.sessions) - LIST_ROWS))
        rows = []
        for i, s in enumerate(self.sessions[start:start + LIST_ROWS], start):
            st = s.get("status", "idle")
            tag = "ask" if s.get("pending") else ("pty" if s.get("kind") == "managed" else "")
            rows.append(f"{'>' if i == self.sel else ' '}{GLYPH.get(st, '?')} {fit(s.get('project') or '?', 30):<30} "
                        f"{age_text(s.get('age_s')):>4} {tag}".rstrip())
        n = len(self.sessions)
        status = f"{n} session{'s' if n != 1 else ''}  ! waits  * busy  - idle"
        return "\n".join([header, fit(status)] + rows)

    def history(self, s: dict) -> list[str]:
        if s.get("kind") == "managed":
            return [fit(x) for x in (s.get("history") or []) + (s.get("screen") or [])]
        return wrap(s.get("last_text") or "(no reply yet)", W)

    def window(self, s: dict) -> list[str]:
        lines = self.history(s)
        rows = SCREEN_ROWS if s.get("kind") == "managed" else TEXT_ROWS
        max_top = max(0, len(lines) - rows)
        if s.get("kind") == "managed" and self.follow:
            self.top = max_top
        self.top = max(0, min(self.top, max_top))
        out = lines[self.top:self.top + rows]
        return out + [""] * (rows - len(out)) if s.get("kind") != "managed" else out

    def session_text(self, ctx: AppContext) -> str:
        s = self.current()
        if s is None:
            return "\n".join([ctx.header(self.name, "hold = back"), "", "That session is gone."])
        st = s.get("status", "idle")
        right = f"{st} {age_text(s.get('age_s'))}"
        head = f"{GLYPH.get(st, '?')} {fit(s.get('project') or '?', W - len(right) - 3)}"
        header = f"{head:<{W - len(right)}}{right}"
        pending = s.get("pending")
        if self.voice:
            body = ["Listening ... speak your reply" if self.voice == "listening" else "Transcribing ...", ""]
            if pending:
                body += ["say yes to allow, no to deny,", "anything else denies with your words"]
            return "\n".join([header, ""] + body)
        if s.get("kind") == "managed":
            if self.notice:
                header = fit(self.notice)
            elif pending:
                header = fit(f"APPROVE? {pending.get('summary', '')}")
            elif s.get("needs_input"):
                header = fit(f"{head}  waiting: tap say, hold enter")
            return "\n".join([header] + self.window(s))
        if self.notice:
            status = self.notice
        elif pending:
            status = f"APPROVE? {pending.get('summary', '')}"
        elif s.get("last_tool"):
            status = f"tool: {s['last_tool']}"
        else:
            status = s.get("title") or ""
        hint = "hold allow  up deny  tap reply" if pending else "tap reply  swipe scroll  hold back"
        return "\n".join([header, fit(status)] + self.window(s) + [hint])

    def text(self, ctx: AppContext) -> str:
        return self.session_text(ctx) if self.view == "session" else self.list_text(ctx)

    async def draw(self, ctx: AppContext, force: bool = False) -> None:
        txt = self.text(ctx)
        if txt == self._last and not force:
            return
        loop = asyncio.get_running_loop()
        wait = self.min_redraw_s - (loop.time() - self._last_at)
        if not force and wait > 0:
            if self._late is None or self._late.done():
                self._late = asyncio.create_task(self._late_draw(ctx, wait))
            return
        self._last, self._last_at = txt, loop.time()
        await ctx.show(txt)

    async def _late_draw(self, ctx: AppContext, wait: float) -> None:
        await asyncio.sleep(wait)
        await self.draw(ctx, force=True)

    # ------------------------------------------------------------- gestures
    async def touch(self, ctx: AppContext, gesture: str) -> None:
        self.notice = ""
        if not self.connected and gesture == "single_tap":
            self._connect(ctx)                     # retry right now
            return
        if self.view == "list":
            await self._touch_list(ctx, gesture)
        else:
            await self._touch_session(ctx, gesture)

    async def _touch_list(self, ctx: AppContext, gesture: str) -> None:
        n = len(self.sessions)
        if gesture == "swipe_down":
            self.sel = min(self.sel + 1, max(n - 1, 0))
        elif gesture == "swipe_up":
            self.sel = max(self.sel - 1, 0)
        elif gesture == "single_tap" and n:
            self.view, self.open_id = "session", self.sessions[self.sel].get("id")
            self.top, self.follow = 0, True
        await self.draw(ctx, force=True)

    async def _touch_session(self, ctx: AppContext, gesture: str) -> None:
        s = self.current()
        if self.voice:
            return
        if s is None:
            if gesture == "long_press_release":
                self.view = "list"
            await self.draw(ctx, force=True)
            return
        pending = s.get("pending")
        managed = s.get("kind") == "managed"
        rows = SCREEN_ROWS if managed else TEXT_ROWS
        if gesture == "long_press_release":
            if pending:
                if await self.send({"type": "decide", "session": s["id"], "behavior": "allow"}):
                    self.notice = f"approved: {pending.get('summary', '')}"
            elif managed and s.get("needs_input"):
                if await self.send({"type": "input", "session": s["id"], "key": "enter"}):
                    self.notice = "sent: Enter"
            else:
                self.view = "list"
        elif gesture == "swipe_up":
            if pending:
                if await self.send({"type": "decide", "session": s["id"], "behavior": "deny",
                                    "message": "Denied from the glasses."}):
                    self.notice = f"denied: {pending.get('summary', '')}"
            else:
                self.follow = False
                self.top = max(0, self.top - rows)
        elif gesture == "swipe_down":
            self.top += rows
            self.follow = self.top >= len(self.history(s)) - rows
        elif gesture == "single_tap":
            if not ctx.speech_ready:
                self.notice = "speech-to-text is not running"
            else:
                self._voice_task = asyncio.create_task(self._voice_reply(ctx, s["id"]), name="g2-terminal-voice")
                return
        await self.draw(ctx, force=True)

    async def _voice_reply(self, ctx: AppContext, sid: str) -> None:
        try:
            self.voice = "listening"
            await self.draw(ctx, force=True)
            pcm = await ctx.speech.capture_utterance()
            if not pcm:
                self.voice, self.notice = "", "nothing heard"
                await self.draw(ctx, force=True)
                return
            self.voice = "transcribing"
            await self.draw(ctx, force=True)
            text, _level = await ctx.speech.transcribe(pcm)
            self.voice = ""
            text = (text or "").strip()
            if not text:
                self.notice = "could not make out words"
            else:
                await self.reply(sid, text)
            await self.draw(ctx, force=True)
        except asyncio.CancelledError:
            self.voice = ""
            raise
        except Exception as exc:
            ctx.log.exception("terminal voice reply failed")
            self.voice, self.notice = "", f"failed: {type(exc).__name__}"
            await self.draw(ctx, force=True)

    async def reply(self, sid: str, text: str) -> None:
        s = next((x for x in self.sessions if x.get("id") == sid), None) or {"id": sid}
        pending = s.get("pending")
        if pending:
            if YES.match(text):
                ok = await self.send({"type": "decide", "session": sid, "behavior": "allow"})
                self.notice = "approved" if ok else self.notice
            else:
                msg = "Denied from the glasses." if NO.match(text) and len(text.split()) <= 2 else text
                ok = await self.send({"type": "decide", "session": sid, "behavior": "deny", "message": msg})
                self.notice = fit(f"denied: {text}") if ok else self.notice
            return
        key = SPOKEN_KEYS.get(re.sub(r"[^a-z ]", "", text.lower()).strip())
        if key:
            ok = await self.send({"type": "input", "session": sid, "key": key})
            self.notice = f"sent: {key}" if ok else self.notice
        else:
            ok = await self.send({"type": "input", "session": sid, "text": text, "enter": True})
            self.notice = fit(f"sent: {text}") if ok else self.notice
