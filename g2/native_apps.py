"""Host side of the glasses' own full-screen apps: Teleprompt (SID 0x06) and Translate (SID 0x05).

The user picks the entry in the firmware's menu, the firmware opens its own full-screen app and
talks to us; this controller answers. Same shape as the hub's ``hub/native_apps.py``:
``on_frame(side, parsed)`` for every inbound frame, ``open(name)`` / ``close()``, content methods,
one heartbeat task, one current app.

    apps = NativeApps(glasses, scripts_dir=Path("g2/data/teleprompter"), speech=speech)
    apps.attach()                      # glasses.on(apps.on_frame)

How a menu selection surfaces (docs/native-protocol.md section 4-5):
  * SID 0x0D push ``{3: {1 bg, 2 fg}}`` names the foreground SID (LIVE);
  * Teleprompt sends ``FILE_LIST_REQUEST`` -> we answer ``FILE_LIST`` with the scripts in
    ``scripts_dir`` -> ``FILE_SELECT {fileId}`` -> we ``START`` and answer ``PAGE_DATA_REQUEST``s;
  * Translate sends ``LANG_SWITCH_REQ`` / notify; we ``OPEN`` a session (also on the 0x0D push),
    heart-beat every 4 s and push ``RESULT`` frames: the laptop's speech-to-text captions each
    utterance, argostranslate (if installed) translates it; without it the caption is shown as is.
  * "Conversate" mode = the same Translate surface with src == dst (live transcript), transcript
    saved to a file and a brain summary written (and pushed) when the session ends. The firmware's
    real Conversate channel (menu id 11 / SID 0x0B) is not decoded.

Confidence: every SID 0x05 / 0x06 sequence here is DECODED (never captured live); the 0x0D push
is LIVE. Frames the hardware check should watch: tp_file_list, tp_start, tp_page, tp_heartbeat,
tp_close, tp_mode_switch_response, tr_open, tr_text, tr_heartbeat, tr_lang_switch_response, tr_close.

Standalone (no launcher):  python g2/native_apps.py [--gateway URL] [--scripts DIR] [--pair EN>ES]
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

HERE = Path(__file__).resolve().parent
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))

from g2.native import apps as na  # noqa: E402
from g2.native import session  # noqa: E402
from g2.native.pb import read_varint  # noqa: E402

DEFAULT_SCRIPTS = HERE / "data" / "teleprompter"
DEFAULT_TRANSCRIPTS = HERE / "transcripts"
SID_CONVERSATE = 0x0B                                   # menu id 11; channel not decoded
PAIRS: list[tuple[str, str]] = [("EN", "ES"), ("ES", "EN"), ("EN", "FR"), ("FR", "EN"), ("EN", "DE"), ("DE", "EN")]
LANG_NAMES = {"EN": "English", "ES": "Spanish", "FR": "French", "DE": "German", "IT": "Italian", "PT": "Portuguese"}
MAX_FILES = 20
SUMMARY_PROMPT = "Summarize in 3 short bullet points: "

README = """\
Teleprompt reads .txt and .md files from this folder.

Pick "Teleprompt" in the glasses menu: the glasses ask the laptop for this
list, you choose a file on the glasses, and the firmware's own teleprompter
shows it (swipe to scroll, tap to pause, double tap to leave).

A file whose first line is "# auto" scrolls by itself (one line every 3 s).
"""

Translator = Callable[[str, str, str], Awaitable[str]]


def argos_available() -> bool:
    return importlib.util.find_spec("argostranslate") is not None


def make_argos_translator() -> Translator:
    """A translator over the installed argostranslate packages (imported lazily, never installed)."""
    import argostranslate.translate as at  # type: ignore

    async def translate(text: str, src: str, dst: str) -> str:
        def work() -> str:
            fn = getattr(at, "get_translation_from_codes", None)
            if fn is not None:
                tr = fn(src.lower(), dst.lower())
                if tr is None:
                    raise LookupError(f"argos package {src.lower()}>{dst.lower()} not installed")
                return tr.translate(text)
            return at.translate(text, src.lower(), dst.lower())
        return await asyncio.to_thread(work)

    return translate


def install_hint(src: str = "en", dst: str = "es") -> str:
    return f"pip install argostranslate; argospm install translate-{src.lower()}_{dst.lower()}"


def load_script(path: Path) -> tuple[str, bool]:
    """(text, auto_scroll) with a leading ``# auto`` line removed."""
    text = path.read_text(encoding="utf-8", errors="replace").replace("\r", "")
    first, _, rest = text.partition("\n")
    if first.strip().lower() in ("# auto", "# native"):
        return rest, first.strip().lower() == "# auto"
    return text, False


class NativeApps:
    """One controller per process; ``current`` is 'translate' | 'teleprompter' | 'conversate' | None."""

    ACK_TIMEOUT_S = 3.0
    AUTO_SCROLL_MS = 3000

    def __init__(self, glasses, *, scripts_dir: Path | str | None = None, speech=None, translator: Translator | None = None,
                 brain=None, transcripts_dir: Path | str | None = None, pairs: list[tuple[str, str]] | None = None,
                 menu_translate_mode: str = "translate", clock: Callable[[], float] = time.time,
                 now: Callable[[], datetime] = datetime.now, log: logging.Logger | None = None,
                 spawn: Callable[[Awaitable, str], asyncio.Task] | None = None,
                 on_change: Callable[[str], Any] | None = None) -> None:
        self.g = glasses
        self.scripts_dir = Path(scripts_dir) if scripts_dir else DEFAULT_SCRIPTS
        self.transcripts_dir = Path(transcripts_dir) if transcripts_dir else DEFAULT_TRANSCRIPTS
        self.speech = speech
        self.translator = translator
        self.brain = brain
        self.pairs = list(pairs or PAIRS)
        self.pair = 0
        self.menu_translate_mode = menu_translate_mode   # what a menu-launched Translate does
        self.clock, self.now = clock, now
        self.log = log or logging.getLogger("g2.native_apps")
        self._spawn = spawn or (lambda coro, name: asyncio.ensure_future(coro))
        self.on_change = on_change
        self.current: str | None = None
        self.foreground: int | None = None
        self._attached = False
        self._hb: asyncio.Task | None = None
        self._tasks: list[asyncio.Task] = []
        # teleprompter
        self.pages: list[list[str]] = []
        self.page = 0
        self.line = 0
        self.served: int | None = None
        self.script_name = ""
        self._files: list[Path] = []
        # translate / conversate
        self.live = None
        self.engine = ""
        self.file: Path | None = None
        self.entries: list[tuple[str, str]] = []
        self.summary = ""
        self.last_error = ""
        self.count = 0

    # ── plumbing ───────────────────────────────────────────────────────────────────────
    def attach(self) -> None:
        if not self._attached:
            self.g.on(self.on_frame)
            self._attached = True

    def detach(self) -> None:
        self._attached = False
        try:
            listeners = getattr(self.g, "_listeners", None)
            if listeners is not None and self.on_frame in listeners:
                listeners.remove(self.on_frame)
        except Exception:
            pass

    @property
    def src(self) -> str:
        return self.pairs[self.pair][0]

    @property
    def dst(self) -> str:
        return self.pairs[self.pair][1]

    def status(self) -> dict[str, Any]:
        return {"current": self.current, "foreground": self.foreground, "pages": len(self.pages), "page": self.page,
                "served": self.served, "script": self.script_name, "pair": f"{self.src}>{self.dst}", "engine": self.engine,
                "utterances": self.count, "last_error": self.last_error, "summary": self.summary}

    def _task(self, coro, name: str) -> asyncio.Task:
        t = self._spawn(coro, name)
        self._tasks.append(t)
        self._tasks = [x for x in self._tasks if not x.done()]
        return t

    async def _changed(self, what: str) -> None:
        if self.on_change is not None:
            try:
                r = self.on_change(what)
                if asyncio.iscoroutine(r):
                    await r
            except Exception:
                self.log.exception("on_change failed")

    async def _sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds * getattr(self.g, "time_scale", 1.0))

    async def _send(self, sid: int, payload: bytes, wait_cmd: int | None = None, timeout: float | None = None):
        """Send one payload; when ``wait_cmd`` is given wait for that command on ``sid`` (or None on timeout)."""
        if wait_cmd is None:
            return await self.g.send_pb(sid, payload)
        pred = lambda p: p.sid == sid and read_varint(p.payload, 1, -1) == wait_cmd  # noqa: E731
        return await self.g.send_pb(sid, payload, expect=pred, timeout=timeout or self.ACK_TIMEOUT_S)

    @staticmethod
    def _result(reply, error: int | None) -> dict[str, Any]:
        if reply is None:
            return {"acked": False, "ok": False, "error": None, "detail": "the glasses did not acknowledge the command"}
        if error:
            return {"acked": True, "ok": False, "error": error, "detail": f"the glasses rejected the command (error {error})"}
        return {"acked": True, "ok": True, "error": 0, "detail": None}

    def _start_heartbeat(self, sid: int, interval: float, build: Callable[[], bytes]) -> None:
        self._stop_heartbeat()

        async def loop():
            try:
                while True:
                    await self._sleep(interval)
                    await self.g.send_pb(sid, build())
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.log.warning("heartbeat stopped: %s", e)
        self._hb = self._spawn(loop(), f"g2-native-{sid:#x}-heartbeat")

    def _stop_heartbeat(self) -> None:
        t, self._hb = self._hb, None
        if t is not None and not t.done():
            t.cancel()

    async def _switch_to(self, name: str) -> None:
        if self.current and self.current != name:
            await self.close()
        self.current = name

    # ── teleprompter ───────────────────────────────────────────────────────────────────
    def scripts(self) -> list[Path]:
        d = self.scripts_dir
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            (d / "README.txt").write_text(README, encoding="utf-8")
        self._files = sorted([p for p in d.iterdir() if p.is_file() and p.suffix.lower() in (".txt", ".md")],
                             key=lambda p: p.name.lower())[:MAX_FILES]
        return self._files

    def file_list_entries(self) -> list[tuple[bytes, str]]:
        return [(str(i).encode(), p.stem[:na.TP_FILENAME_MAX_CHARS]) for i, p in enumerate(self.scripts())]

    def script_by_id(self, file_id: bytes) -> Path | None:
        try:
            i = int(file_id.decode("utf-8", "replace").strip() or "-1")
        except ValueError:
            return None
        files = self._files or self.scripts()
        return files[i] if 0 <= i < len(files) else None

    async def teleprompter_start(self, text: str, mode: int = na.TP_MODE_MANUAL, scroll_interval_ms: int = 0,
                                 name: str = "") -> dict[str, Any]:
        """START (retried once on COMM_RESP FAIL like the official app), then heartbeat every 6 s."""
        await self._switch_to("teleprompter")
        self.pages = na.tp_paginate(text)
        if not self.pages:
            self.current = None
            return {"acked": False, "ok": False, "error": None, "detail": "the script is empty"}
        self.page, self.line, self.served, self.script_name = 0, 0, None, name
        total_pages, total_lines = len(self.pages), sum(len(p) for p in self.pages)
        reply = err = None
        for _attempt in range(2):
            payload = na.tp_start(self.g.alloc.next_magic(), total_pages, total_lines, mode, scroll_interval_ms=scroll_interval_ms)
            reply = await self._send(na.SID_TELEPROMPTER, payload, na.TP_CMD_COMM_RESP)
            err = na.tp_parse(reply.payload).error if reply is not None else None
            if not err:
                break
        res = self._result(reply, err)
        if res["acked"] and not res["ok"]:
            self.current = None
            self.pages = []
            return res
        self._start_heartbeat(na.SID_TELEPROMPTER, na.TP_HEARTBEAT_INTERVAL_S,
                              lambda: na.tp_heartbeat(self.g.alloc.next_magic(), self.page, self.line))
        self.log.info("teleprompter: %d page(s), %d line(s)%s", total_pages, total_lines, "" if reply else " (no reply yet)")
        await self._changed("teleprompter")
        return res

    async def teleprompter_file(self, path: Path) -> dict[str, Any]:
        text, auto = load_script(path)
        mode = na.TP_MODE_AUTO if auto else na.TP_MODE_MANUAL
        return await self.teleprompter_start(text, mode, self.AUTO_SCROLL_MS if auto else 0, name=path.stem)

    async def _serve_page(self, page_id: int) -> None:
        if self.current != "teleprompter":
            return
        if 0 <= page_id < len(self.pages):
            await self.g.send_pb(na.SID_TELEPROMPTER, na.tp_page(self.g.alloc.next_magic(), page_id, self.pages[page_id]))
            self.served = self.page = page_id
            await self._changed("page")
        else:
            self.log.warning("teleprompter: glasses asked for page %d, script has %d", page_id, len(self.pages))

    async def teleprompter_pause(self) -> None:
        await self.g.send_pb(na.SID_TELEPROMPTER, na.tp_pause(self.g.alloc.next_magic()))

    async def teleprompter_resume(self) -> None:
        await self.g.send_pb(na.SID_TELEPROMPTER, na.tp_resume(self.g.alloc.next_magic()))

    # ── translate / conversate ─────────────────────────────────────────────────────────
    def _lang_list(self) -> list[tuple[str, str]]:
        seen: dict[str, str] = {}
        for a, b in self.pairs:
            seen.setdefault(a, LANG_NAMES.get(a, a))
            seen.setdefault(b, LANG_NAMES.get(b, b))
        return list(seen.items())

    async def translate_open(self, src: str | None = None, dst: str | None = None, mode: str = "translate") -> dict[str, Any]:
        """CTRL OPEN; on COMM_RESP OK start the 4 s heartbeat and the laptop's captions."""
        await self._switch_to(mode)
        if mode == "conversate" and not (src and dst):
            src = dst = (src or dst or self.src)     # transcript: same language both sides
        if src and dst:
            key = (src.upper(), dst.upper())
            if key not in self.pairs:
                self.pairs.append(key)
            self.pair = self.pairs.index(key)
        src, dst = self.src, self.dst
        reply = await self._send(na.SID_TRANSLATE, na.tr_open(self.g.alloc.next_magic(), src, dst, lang_list=self._lang_list()),
                                 na.TR_CMD_COMM_RESP)
        err = na.tr_parse(reply.payload).error if reply is not None else None
        res = self._result(reply, err)
        if res["acked"] and not res["ok"]:
            self.current = None                      # no session: do not heart-beat a rejected OPEN
            return res
        self._start_heartbeat(na.SID_TRANSLATE, na.TR_HEARTBEAT_INTERVAL_S, lambda: na.tr_heartbeat(self.g.alloc.next_magic()))
        self.entries, self.summary, self.count, self.last_error = [], "", 0, ""
        if self.translator is None and argos_available():
            try:
                self.translator = make_argos_translator()
            except Exception as e:
                self.log.warning("argostranslate import failed: %s", e)
        self.engine = "transcript" if mode == "conversate" else ("argos" if self.translator is not None else "captions only")
        self.transcripts_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{mode}_{self.now():%Y-%m-%d_%H%M%S}"
        self.file = self.transcripts_dir / f"{stem}.txt"
        n = 1
        while self.file.exists():                   # two sessions in one second
            n += 1
            self.file = self.transcripts_dir / f"{stem}_{n}.txt"
        self.file.write_text(f"# G2 {mode} {self.now():%Y-%m-%d %H:%M:%S} {src}>{dst} ({self.engine})\n", encoding="utf-8")
        notice = None
        if self.speech is None or not getattr(self.speech, "available", True):
            notice = "speech-to-text is not running on the laptop"
        elif mode == "translate" and self.translator is None:
            notice = "captions only: " + install_hint(src, dst)
        if notice:
            self.last_error = notice
            await self.translate_text(notice, " ")
        if self.speech is not None and getattr(self.speech, "available", True):
            try:
                self.live = self.speech.captioner(self._on_caption)
                await self.live.start()
            except Exception as e:
                self.log.warning("captions failed to start: %s", e)
                self.live = None
        self.log.info("%s open %s>%s (%s)", mode, src, dst, self.engine)
        await self._changed(mode)
        return res

    async def conversate_open(self, lang: str = "EN") -> dict[str, Any]:
        return await self.translate_open(lang, lang, mode="conversate")

    async def translate_text(self, src_text: str, dst_text: str, final: bool = True) -> dict[str, Any]:
        """RESULT {srcText, dstText}; a blank dst_text (" ") shows the source only."""
        if self.current not in ("translate", "conversate"):
            res = await self.translate_open()
            if not res["ok"] and res["acked"]:
                return res
        reply = await self._send(na.SID_TRANSLATE, na.tr_text(self.g.alloc.next_magic(), src_text, dst_text, final), na.TR_CMD_COMM_RESP)
        err = na.tr_parse(reply.payload).error if reply is not None else None
        return self._result(reply, err)

    async def _on_caption(self, fresh: str) -> None:
        fresh = (fresh or "").strip()
        if not fresh or self.current not in ("translate", "conversate"):
            return
        translated = fresh
        if self.current == "translate" and self.translator is not None:
            try:
                translated = (await self.translator(fresh, self.src, self.dst)).strip() or fresh
                self.last_error = ""
            except LookupError:
                self.last_error = "missing: " + install_hint(self.src, self.dst)
                translated = fresh
            except Exception as e:
                self.log.warning("translation failed: %s", e)
                self.last_error = f"translation failed: {type(e).__name__}"
                translated = fresh
        self.entries.append((fresh, translated))
        self.count += 1
        if self.file is not None:
            with self.file.open("a", encoding="utf-8") as f:
                f.write(f"[{self.now():%H:%M:%S}] {fresh}" + (f" || {translated}" if translated != fresh else "") + "\n")
        try:
            await self.translate_text(fresh, translated if translated != fresh else (" " if self.current == "translate" else fresh))
        except Exception as e:
            self.log.warning("push failed: %s", e)
        await self._changed("caption")

    async def set_pair(self, index: int) -> None:
        self.pair = index % len(self.pairs)
        if self.current == "translate":
            await self.close()
            await self.translate_open()

    async def summarize(self) -> str:
        """Brain summary of the transcript so far; pushed to the surface while it is open, always saved."""
        if not self.entries or self.brain is None:
            return ""
        text = " ".join(src for src, _ in self.entries)
        try:
            self.summary = (await self.brain.ask(SUMMARY_PROMPT + text) or "").strip()
        except Exception as e:
            self.log.warning("summary failed: %s", e)
            self.summary = ""
            return ""
        if self.current == "conversate" and self.summary:
            try:
                await self.translate_text("Summary", self.summary[:na.TR_TEXT_MAX_BYTES // 2])
            except Exception as e:
                self.log.warning("summary push failed: %s", e)
        await self._changed("summary")
        return self.summary

    async def _stop_captions(self) -> None:
        live, self.live = self.live, None
        if live is not None:
            try:
                await live.stop()
            except Exception as e:
                self.log.warning("stopping captions: %s", e)

    async def _finish_session_file(self) -> None:
        if self.file is None:
            return
        try:
            with self.file.open("a", encoding="utf-8") as f:
                if self.summary:
                    f.write("\n## Summary\n" + self.summary + "\n")
                f.write(f"\n# ended {self.now():%H:%M:%S}, {self.count} utterance(s)\n")
        except OSError as e:
            self.log.warning("writing session file: %s", e)
        self.file = None

    # ── open / close ───────────────────────────────────────────────────────────────────
    async def open(self, name: str, **kw) -> dict[str, Any]:
        if name == "teleprompter":
            path = kw.get("path")
            if path is None:
                files = self.scripts()
                path = files[0] if files else None
            if path is None:
                return {"acked": False, "ok": False, "error": None, "detail": f"no scripts in {self.scripts_dir}"}
            return await self.teleprompter_file(Path(path))
        if name == "translate":
            return await self.translate_open(kw.get("src"), kw.get("dst"))
        if name == "conversate":
            return await self.conversate_open(kw.get("lang", "EN"))
        raise KeyError(name)

    async def close(self) -> None:
        name, self.current = self.current, None
        self._stop_heartbeat()
        if name is None:
            return
        if name in ("translate", "conversate"):
            await self._stop_captions()
            if name == "conversate" and not self.summary:
                self.current = name                   # so the summary can still be pushed
                await self.summarize()
                self.current = None
            await self._finish_session_file()
            payload = na.tr_close(self.g.alloc.next_magic())
            sid = na.SID_TRANSLATE
        else:
            self.pages = []
            payload = na.tp_close(self.g.alloc.next_magic())
            sid = na.SID_TELEPROMPTER
        try:
            await self.g.send_pb(sid, payload)
        except Exception as e:
            self.log.warning("%s close: %s", name, e)
        await self._changed("closed")

    async def _ended(self, name: str) -> None:
        """The glasses left the app themselves: stop our side without sending CLOSE."""
        if self.current != name:
            return
        self.current = None
        self._stop_heartbeat()
        if name in ("translate", "conversate"):
            await self._stop_captions()
            if name == "conversate":
                await self.summarize()
            await self._finish_session_file()
        else:
            self.pages = []
        self.log.info("%s closed on the glasses", name)
        await self._changed("ended")

    async def stop(self) -> None:
        await self.close()
        self.detach()
        for t in self._tasks:
            if not t.done():
                t.cancel()

    # ── inbound ────────────────────────────────────────────────────────────────────────
    def on_frame(self, side: str, p) -> None:
        """Glasses.on listener. Must not block: replies are scheduled."""
        try:
            if p.sid == session.SID_APP_SYNC:
                self._on_app_sync(p)
            elif p.sid == na.SID_TELEPROMPTER:
                self._on_teleprompter(na.tp_parse(p.payload))
            elif p.sid == na.SID_TRANSLATE:
                self._on_translate(na.tr_parse(p.payload))
        except Exception:
            self.log.exception("native app frame handling failed")

    def _on_app_sync(self, p) -> None:
        a = session.parse_app_sync(p.payload)
        if a.foreground is None:
            return
        fg, self.foreground = a.foreground, a.foreground
        cur_sid = {"translate": na.SID_TRANSLATE, "conversate": na.SID_TRANSLATE, "teleprompter": na.SID_TELEPROMPTER}.get(self.current or "")
        if self.current and fg not in (cur_sid, 0):
            self._task(self._ended(self.current), "g2-native-ended")
        elif fg == na.SID_TRANSLATE and self.current is None:
            mode = "conversate" if self.menu_translate_mode == "conversate" else "translate"
            self._task(self.translate_open(mode=mode), "g2-native-translate-open")
        elif fg == SID_CONVERSATE:
            self.log.warning("Conversate picked on the glasses: its channel (SID 0x0B) is not decoded; "
                             "use Translate with menu_translate_mode='conversate'")

    def _on_teleprompter(self, m) -> None:
        if m.cmd == na.TP_CMD_FILE_LIST_REQUEST:                       # opened from the glasses menu
            entries = self.file_list_entries()
            self._task(self.g.send_pb(na.SID_TELEPROMPTER, na.tp_file_list(self.g.alloc.next_magic(), entries)), "g2-tp-filelist")
        elif m.cmd == na.TP_CMD_FILE_SELECT:
            path = self.script_by_id(m.file_id or b"")
            if path is not None:
                self._task(self.teleprompter_file(path), "g2-tp-start")
            else:
                self.log.warning("teleprompter: unknown file id %r", m.file_id)
        elif m.cmd == na.TP_CMD_PAGE_DATA_REQUEST and m.page is not None:
            self._task(self._serve_page(m.page), "g2-tp-page")
        elif m.cmd in (na.TP_CMD_PAGE_SCROLL_SYNC, na.TP_CMD_HEARTBEAT):
            if m.page is not None:
                self.page = m.page
            if m.line is not None:
                self.line = m.line
        elif m.cmd == na.TP_CMD_MODE_SWITCH_REQ:
            self._task(self.g.send_pb(na.SID_TELEPROMPTER, na.tp_mode_switch_response(self.g.alloc.next_magic())), "g2-tp-mode")
        elif m.is_close:
            self._task(self._ended("teleprompter"), "g2-tp-ended")

    def _on_translate(self, m) -> None:
        if m.cmd == na.TR_CMD_LANG_SWITCH_REQ:
            self._task(self.g.send_pb(na.SID_TRANSLATE, na.tr_lang_switch_response(self.g.alloc.next_magic())), "g2-tr-lang")
            key = (m.lang_key or "").strip().upper()
            if key and key != self.src:
                for i, (a, b) in enumerate(self.pairs):
                    if a == key and b == self.dst:
                        self.pair = i
                        break
                else:
                    self.pairs.append((key, self.dst))
                    self.pair = len(self.pairs) - 1
                self.log.info("translate: glasses switched source to %s", key)
            if self.current is None:
                mode = "conversate" if self.menu_translate_mode == "conversate" else "translate"
                self._task(self.translate_open(mode=mode), "g2-native-translate-open")
        elif m.is_close:
            self._task(self._ended(self.current or "translate"), "g2-tr-ended")
        elif m.cmd == na.TR_CMD_NOTIFY and m.value in (na.TR_PAUSE, na.TR_RESUME):
            self.log.info("translate: glasses %s", "paused" if m.value == na.TR_PAUSE else "resumed")


# ── standalone runner ───────────────────────────────────────────────────────────────────


async def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Host the glasses' Teleprompt / Translate apps from the laptop")
    ap.add_argument("--gateway", default="http://127.0.0.1:8765")
    ap.add_argument("--scripts", default=str(DEFAULT_SCRIPTS))
    ap.add_argument("--pair", default="EN>ES", help="default Translate pair, e.g. EN>ES")
    ap.add_argument("--menu-translate", choices=["translate", "conversate"], default="translate",
                    help="what a Translate picked on the glasses does")
    ap.add_argument("--open", choices=["teleprompter", "translate", "conversate"], help="open one app right away")
    ap.add_argument("--no-stt", action="store_true", help="no laptop speech-to-text (Translate shows a notice)")
    ap.add_argument("--model", default="base.en")
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.v else logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    from g2.native.transport import GatewayRawTransport, Glasses
    glasses = Glasses(GatewayRawTransport(a.gateway), log=logging.getLogger("g2.native").info)
    gw = speech = brain = None
    if not a.no_stt:
        try:
            from g2.gateway_client import G2Gateway
            from g2.lc3dec import Decoder
            from g2.stt import Speech
            gw = G2Gateway(a.gateway, decoder=Decoder())
            await gw.start()
            speech = Speech(gw, model=a.model)
            if not await speech.start():
                speech = None
        except Exception as e:
            logging.getLogger("g2.native_apps").warning("speech unavailable (%s)", e)
            speech = None
    try:
        from ai.brain import make_brain
        brain = make_brain()
    except Exception:
        brain = None
    src, _, dst = a.pair.upper().partition(">")
    pairs = [(src, dst or src)] + [p for p in PAIRS if p != (src, dst)]
    apps = NativeApps(glasses, scripts_dir=a.scripts, speech=speech, brain=brain, pairs=pairs, menu_translate_mode=a.menu_translate)
    await glasses.start()
    apps.attach()
    apps.scripts()
    print(f"hosting Teleprompt ({apps.scripts_dir}) and Translate ({src}>{dst or src}); pick them in the glasses menu. Ctrl-C stops.")
    try:
        if a.open:
            print(a.open, await apps.open(a.open))
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await apps.stop()
        await glasses.stop()
        if speech is not None:
            await speech.stop()
        if gw is not None:
            await gw.stop()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(_main()))
    except KeyboardInterrupt:
        pass
