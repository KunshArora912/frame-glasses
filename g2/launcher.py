"""Home screen and apps for the Even G2, driven from the laptop through the
local gateway. No phone, no Even account, nothing leaves the machine.

    python g2/run.py                      # gateway + this launcher, supervised
    python g2/launcher.py [--brain offline|cli|api] [--model base.en] [--no-stt]

Gestures (right temple):
    swipe up / down   move the selection
    single tap        open / next page / ask again
    double tap        back to home (stops the mic)

Screens: Home (time, battery, menu), Assistant (ask by voice, answer on
screen), Transcribe (live captions saved under g2/transcripts/), Notes (speak
a note into thoughts.json), Status (glasses, gateway, speech, brain).

The display is one full-screen text container (about 50 columns by 10 rows).
Every screen is a string; the gateway skips Bluetooth traffic when the string
did not change, so periodic redraws are free.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import textwrap
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from g2.gateway_client import G2Gateway, GatewayError  # noqa: E402

log = logging.getLogger("g2.launcher")

COLS = 48            # safe width on the 50-column grid
ROWS = 10
BODY_ROWS = 8        # rows left after a header and a hint line
IDLE_BLANK_S = 120
TICK_S = 10

MENU = [
    ("Assistant", "ask by voice, read the answer"),
    ("Transcribe", "live captions, saved to a file"),
    ("Notes", "speak a note, keep it"),
    ("Status", "glasses, gateway, speech, brain"),
]


def wrap(text: str, width: int = COLS) -> list[str]:
    out: list[str] = []
    for para in (text or "").replace("\r", "").split("\n"):
        if not para.strip():
            out.append("")
            continue
        out.extend(textwrap.wrap(para, width=width, break_long_words=True) or [""])
    return out


def paginate(text: str, rows: int = BODY_ROWS) -> list[list[str]]:
    lines = wrap(text) or [""]
    return [lines[i:i + rows] for i in range(0, len(lines), rows)] or [[""]]


def battery_text(level, charging) -> str:
    if level is None:
        return "battery --"
    s = f"battery {level}%"
    if charging is True:
        s += " charging" if level < 100 else " charged"
    return s


class Launcher:
    def __init__(self, gw, speech=None, brain=None, store=None, *,
                 clock=time.time, now=datetime.now,
                 idle_blank_s: float = IDLE_BLANK_S,
                 transcripts_dir: Path | None = None) -> None:
        self.gw = gw
        self.speech = speech
        self.brain = brain
        self.store = store
        self.clock = clock
        self.now = now
        self.idle_blank_s = idle_blank_s
        self.transcripts_dir = transcripts_dir or (HERE / "transcripts")
        self.screen = "home"
        self.sel = 0
        self.last_input = clock()
        self.started_at = clock()
        self.shown = ""                      # last string sent
        self.busy: asyncio.Task | None = None
        self.note_scroll = 0
        self.pages: list[list[str]] = []
        self.page = 0
        self.question = ""
        self.assistant_state = "idle"        # idle | listening | thinking | answer
        self.live = None
        self.caption_lines: list[str] = []
        self.caption_file: Path | None = None
        self.status_note = ""
        self._ticker: asyncio.Task | None = None
        gw.on("touch", self.on_touch)
        gw.on("connection", self.on_connection)
        gw.on("battery", self.on_battery)
        gw.on("reinit", self.on_reinit)

    # ------------------------------------------------------------ output
    async def show(self, text: str) -> None:
        self.shown = text
        if not getattr(self.gw, "ready", True):
            return
        t0 = time.time()
        try:
            draw = getattr(self.gw, "text_inset", None) or self.gw.text
            await draw(text)
        except GatewayError as exc:
            log.warning("display failed: %s", exc)
        dt = time.time() - t0
        if dt > 1.0:
            log.warning("display write took %.1f s", dt)

    def header(self, title: str, hint: str) -> list[str]:
        return [f"{title:<20}{hint}"[:COLS + 2]]

    # ------------------------------------------------------------- home
    def home_text(self) -> str:
        t = self.now()
        top = f"{t:%H:%M}  {t:%a %d %b}"
        bat = battery_text(getattr(self.gw, "battery", None), getattr(self.gw, "charging", None))
        lines = [f"{top:<28}{bat}"[:COLS + 2], ""]
        for i, (name, blurb) in enumerate(MENU):
            mark = ">" if i == self.sel else " "
            lines.append(f"{mark} {name:<12}{blurb}")
        lines.append("")
        lines.append("swipe = move   tap = open   double tap = home")
        return "\n".join(lines)

    # Even's dashboard: a status area (time, date, battery) on the left and
    # one card on the right. Same arrangement here, with our menu as the card.
    def home_elements(self) -> list[dict]:
        t = self.now()
        bat = battery_text(getattr(self.gw, "battery", None), getattr(self.gw, "charging", None))
        status = f"{t:%H:%M}\n{t:%a %d %b}"
        menu = "\n".join(("> " if i == self.sel else "  ") + name for i, (name, _) in enumerate(MENU))
        blurb = MENU[self.sel][1]
        return [
            {"type": "text", "text": status, "x": 16, "y": 12, "width": 200, "height": 96,
             "padding": 0, "container_name": "status"},
            {"type": "text", "text": bat, "x": 16, "y": 116, "width": 200, "height": 60,
             "padding": 0, "container_name": "battery"},
            {"type": "text", "text": menu, "x": 232, "y": 12, "width": 328, "height": 164,
             "padding": 8, "border_width": 1, "border_radius": 4, "capture_events": True,
             "container_name": "menu"},
            {"type": "text", "text": blurb, "x": 232, "y": 184, "width": 328, "height": 40,
             "padding": 8, "container_name": "blurb"},
            {"type": "text", "text": "swipe  move     tap  open     double tap  exit",
             "x": 16, "y": 252, "width": 544, "height": 32, "padding": 0, "container_name": "hint"},
        ]

    async def show_home(self) -> None:
        self.shown = self.home_text()
        if not getattr(self.gw, "ready", True):
            return
        layout = getattr(self.gw, "layout", None)
        if layout is None:
            await self.show(self.home_text())
            return
        try:
            await layout(self.home_elements())
        except GatewayError as exc:
            log.warning("home layout failed: %s", exc)

    async def home(self) -> None:
        await self.leave_current()
        self.screen = "home"
        self.last_input = self.clock()   # arriving home counts as activity
        await self.show_home()

    async def leave_current(self) -> None:
        if self.busy and not self.busy.done():
            self.busy.cancel()
            try:
                await self.busy
            except (asyncio.CancelledError, Exception):
                pass
        self.busy = None
        if self.live is not None:
            live, self.live = self.live, None
            try:
                await live.stop()
            except Exception as exc:
                log.warning("stopping captions: %s", exc)
        self.assistant_state = "idle"

    async def blank(self) -> None:
        self.screen = "blank"
        await self.show(" ")

    # ------------------------------------------------------------ events
    async def on_touch(self, gesture: str, source=None) -> None:
        self.last_input = self.clock()
        log.info("touch %s (source %s) on %s", gesture, source, self.screen)
        if self.screen == "blank":
            await self.home()
            return
        if self.screen == "parked":
            return
        if gesture not in ("single_tap", "double_tap", "swipe_up", "swipe_down", "long_press_release"):
            return                       # foreground_enter/exit etc. are surface notices
        if gesture == "double_tap":
            if self.screen == "home":
                await self.show_home()
            else:
                await self.home()
            return
        handler = getattr(self, f"touch_{self.screen}", None)
        if handler:
            await handler(gesture)

    async def on_connection(self, ready: bool, phase: str) -> None:
        log.info("glasses %s (%s)", "ready" if ready else "away", phase)
        if ready:
            await self.redraw()
            if self.live is not None:
                try:
                    await self.live.resume()
                except Exception as exc:
                    log.warning("mic resume failed: %s", exc)
        elif self.busy and not self.busy.done():
            self.busy.cancel()
            self.assistant_state = "idle"
            if self.screen == "assistant":
                self.shown = self.assistant_text(["Interrupted: glasses went away.", "", "Tap to try again."])
            elif self.screen == "notes":
                self.status_note = "interrupted"
                self.shown = self.notes_text()

    async def on_reinit(self, reason=None) -> None:
        # The firmware treats a double tap as "exit this app". With the
        # gateway's park_on_exit the native dashboard stays visible
        # (page_parked) until the user picks us from the glasses menu
        # (dashboard_launch) or a program draws (display_request/page_create).
        # Without park_on_exit the gateway rebuilds the page at once
        # (page_shutdown); either way the user asked to leave: stop the mic.
        if reason == "page_parked":
            log.info("user left the app; parked behind the native dashboard")
            await self.leave_current()
            self.screen = "parked"
            self.sel = 0
            return
        if reason == "display_request":
            if self.screen == "parked":
                self.screen = "home"       # a program drew while parked; follow it
            return                         # otherwise it was our own write
        if reason in ("page_shutdown", "dashboard_launch", "page_create"):
            log.info("app (re)entered (%s); home", reason)
            await self.home()
            return
        log.info("gateway reinitialised (%s); redrawing", reason)
        await self.redraw()
        if self.live is not None:
            try:
                await self.live.resume()
            except Exception:
                pass

    async def on_battery(self, level, charging) -> None:
        if self.screen == "home":
            await self.show_home()

    async def redraw(self) -> None:
        if self.screen == "parked":
            return
        if self.screen == "home":
            await self.show_home()
        elif self.screen == "blank":
            await self.show(" ")
        elif self.shown:
            await self.show(self.shown)

    async def tick(self) -> None:
        if self.screen == "parked":
            return
        if self.screen == "home":
            if self.clock() - self.last_input >= self.idle_blank_s:
                await self.blank()
            else:
                await self.show_home()

    async def _ticker_loop(self) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            try:
                await self.tick()
            except Exception:
                log.exception("tick failed")

    async def start(self) -> None:
        self._ticker = asyncio.create_task(self._ticker_loop(), name="g2-ticker")
        await self.home()

    async def stop(self) -> None:
        if self._ticker:
            self._ticker.cancel()
        await self.leave_current()

    # ------------------------------------------------------------- home
    async def touch_home(self, gesture: str) -> None:
        if gesture == "swipe_down":
            self.sel = min(self.sel + 1, len(MENU) - 1)
        elif gesture == "swipe_up":
            self.sel = max(self.sel - 1, 0)
        elif gesture == "single_tap":
            await self.open(MENU[self.sel][0].lower())
            return
        await self.show_home()

    async def open(self, name: str) -> None:
        self.screen = name
        opener = getattr(self, f"open_{name}")
        await opener()

    # -------------------------------------------------------- assistant
    def assistant_text(self, body: list[str], hint: str = "tap = ask      double tap = exit") -> str:
        if self.assistant_state == "answer" and len(self.pages) > 1:
            hint = f"tap = more  {self.page + 1}/{len(self.pages)}   double tap = exit"
        return "\n".join([f"{'Assistant':<12}{hint}"[:COLS], ""] + body)

    async def open_assistant(self) -> None:
        self.pages, self.page, self.question = [], 0, ""
        if self.speech is None or not self.speech.available:
            await self.show(self.assistant_text(["speech-to-text is not running on the laptop"], "double tap = home"))
            return
        await self.show(self.assistant_text(["Tap, then ask your question.", "", f"brain: {brain_name(self.brain)}"]))

    async def touch_assistant(self, gesture: str) -> None:
        if gesture != "single_tap":
            return
        if self.assistant_state in ("listening", "thinking"):
            return
        if self.assistant_state == "answer" and self.page < len(self.pages) - 1:
            self.page += 1
            await self.show(self.assistant_text(self.pages[self.page], "tap = more   double tap = exit"))
            return
        self.busy = asyncio.create_task(self._ask(), name="g2-ask")

    async def _ask(self) -> None:
        self.assistant_state = "listening"
        await self.show(self.assistant_text(["Listening ...", "", "speak now; it stops when you pause"], "double tap = cancel"))
        try:
            pcm = await self.speech.capture_utterance()
            if not pcm:
                self.assistant_state = "idle"
                await self.show(self.assistant_text(["Nothing heard.", "", "Tap to try again."]))
                return
            self.assistant_state = "thinking"
            await self.show(self.assistant_text(["Heard you. Transcribing ..."], "double tap = cancel"))
            text, level = await self.speech.transcribe(pcm)
            if not text:
                self.assistant_state = "idle"
                await self.show(self.assistant_text(["Could not make out words.", "", "Tap to try again."]))
                return
            self.question = text
            await self.show(self.assistant_text([f"You: {text}", "", "Thinking ..."], "double tap = cancel"))
            answer = await self.brain.ask(text) if self.brain else "no brain configured"
            self.pages = paginate(f"You: {text}\n\n{answer}")
            self.page = 0
            self.assistant_state = "answer"
            hint = "tap = more   double tap = exit" if len(self.pages) > 1 else "tap = ask again   double tap = exit"
            await self.show(self.assistant_text(self.pages[0], hint))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("assistant failed")
            self.assistant_state = "idle"
            await self.show(self.assistant_text([f"Failed: {type(exc).__name__}", str(exc)[:COLS * 2], "", "Tap to try again."]))

    # ------------------------------------------------------- transcribe
    def transcribe_text(self, hint: str = "double tap = stop") -> str:
        head = f"{'Transcribe':<12}{hint}"[:COLS]
        body = self.caption_lines[-BODY_ROWS:] or ["listening ..."]
        return "\n".join([head, ""] + body)

    async def open_transcribe(self) -> None:
        self.caption_lines = []
        if self.speech is None or not self.speech.available:
            await self.show("Transcribe  double tap = exit\n\nspeech-to-text is not running on the laptop")
            return
        self.transcripts_dir.mkdir(parents=True, exist_ok=True)
        self.caption_file = self.transcripts_dir / f"{self.now():%Y-%m-%d_%H%M%S}.txt"
        self.caption_file.write_text(f"# G2 transcript {self.now():%Y-%m-%d %H:%M:%S}\n", encoding="utf-8")
        self.live = self.speech.captioner(self.on_caption)
        await self.live.start()
        await self.show(self.transcribe_text())

    async def on_caption(self, fresh: str) -> None:
        if not fresh:
            return
        with self.caption_file.open("a", encoding="utf-8") as f:
            f.write(f"[{self.now():%H:%M:%S}] {fresh}\n")
        joined = (self.caption_lines[-1] + " " + fresh) if self.caption_lines else fresh
        self.caption_lines = self.caption_lines[:-1] + wrap(joined)
        if self.screen == "transcribe":
            await self.show(self.transcribe_text())

    async def touch_transcribe(self, gesture: str) -> None:
        if gesture == "single_tap" and self.live is not None:
            # a tap drops a paragraph break into the file so notes stay readable
            with self.caption_file.open("a", encoding="utf-8") as f:
                f.write("\n")
            self.caption_lines.append("")
            await self.show(self.transcribe_text())

    # ------------------------------------------------------------ notes
    def notes_text(self, hint: str = "tap new   swipe scroll   2 taps exit") -> str:
        head = f"{'Notes':<12}{hint}"[:COLS]
        items = self.store.pending() if self.store is not None else []
        rows = BODY_ROWS - (1 if self.status_note else 0)
        if not items:
            body = ["no notes yet"]
        else:
            items = list(reversed(items))
            self.note_scroll = max(0, min(self.note_scroll, max(0, len(items) - rows)))
            body = [f"{t.when}  {t.summary[:COLS - 14]}" for t in items[self.note_scroll:self.note_scroll + rows]]
        lines = [head, ""] + body
        if self.status_note:
            lines.append(self.status_note[:COLS])
        return "\n".join(lines)

    async def open_notes(self) -> None:
        self.note_scroll, self.status_note = 0, ""
        await self.show(self.notes_text())

    async def touch_notes(self, gesture: str) -> None:
        if gesture == "swipe_down":
            self.note_scroll += 1
        elif gesture == "swipe_up":
            self.note_scroll = max(0, self.note_scroll - 1)
        elif gesture == "single_tap":
            if self.busy and not self.busy.done():
                return
            if self.speech is None or not self.speech.available:
                self.status_note = "speech-to-text is not running"
            else:
                self.busy = asyncio.create_task(self._take_note(), name="g2-note")
                return
        await self.show(self.notes_text())

    async def _take_note(self) -> None:
        self.status_note = "listening ... speak your note"
        await self.show(self.notes_text("double tap = cancel"))
        try:
            t0 = self.clock()
            pcm = await self.speech.capture_utterance(max_s=30)
            if not pcm:
                self.status_note = "nothing heard"
            else:
                self.status_note = "transcribing ..."
                await self.show(self.notes_text("double tap = cancel"))
                text, _ = await self.speech.transcribe(pcm)
                thought = self.store.add(text, seconds=self.clock() - t0) if (text and self.store is not None) else None
                self.status_note = f"saved: {thought.summary}" if thought else "could not make out words"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("note failed")
            self.status_note = f"failed: {type(exc).__name__}"
        await self.show(self.notes_text())

    # ----------------------------------------------------------- status
    def status_text(self) -> str:
        gw = self.gw
        st = getattr(gw, "state", {}) or {}
        left, right = st.get("left") or {}, st.get("right") or {}
        up = int(self.clock() - self.started_at)
        lines = [
            f"{'Status':<12}tap = refresh   double tap = exit",
            "",
            f"glasses   {'ready' if getattr(gw, 'ready', False) else 'away'} ({getattr(gw, 'phase', '?')})   "
            f"L {'on' if left.get('connected') else 'off'}  R {'on' if right.get('connected') else 'off'}",
            f"{battery_text(getattr(gw, 'battery', None), getattr(gw, 'charging', None))}   firmware {getattr(gw, 'firmware', None) or '?'}",
            f"gateway   {'up' if getattr(gw, 'gateway_up', True) else 'down'}   {getattr(gw, 'base', '')}",
            f"speech    {('ready, ' + self.speech.model) if (self.speech and self.speech.available) else 'off'}",
            f"brain     {brain_name(self.brain)}",
            f"launcher  up {up // 3600:d}h {up % 3600 // 60:02d}m   last gesture {getattr(gw, 'last_gesture', None) or '-'}",
            "all data stays on this laptop",
        ]
        return "\n".join(lines)

    async def open_status(self) -> None:
        t0 = time.time()
        try:
            if hasattr(self.gw, "status"):
                await self.gw.status()
        except Exception as exc:
            log.warning("status query failed: %s", exc)
        if time.time() - t0 > 1.0:
            log.warning("status query took %.1f s", time.time() - t0)
        await self.show(self.status_text())

    async def touch_status(self, gesture: str) -> None:
        if gesture == "single_tap":
            await self.open_status()


def brain_name(brain) -> str:
    if brain is None:
        return "none"
    n = type(brain).__name__
    return {"ClaudeBrain": "Claude (API, leaves the laptop)",
            "ClaudeCliBrain": "Claude CLI (leaves the laptop)",
            "OfflineBrain": "offline"}.get(n, n)


async def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--gateway", default=os.environ.get("G2_GATEWAY", "http://127.0.0.1:8765"))
    ap.add_argument("--model", default=os.environ.get("G2_STT_MODEL", "base.en"))
    ap.add_argument("--brain", default=os.environ.get("G2_BRAIN"), choices=[None, "offline", "cli", "api"])
    ap.add_argument("--no-stt", action="store_true", help="skip the speech worker (menu only)")
    ap.add_argument("--idle-blank", type=float, default=IDLE_BLANK_S)
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.v else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    decoder = None
    try:
        from g2.lc3dec import Decoder
        decoder = Decoder()
    except Exception as exc:
        log.warning("no LC3 decoder (%s); mic features disabled", exc)

    gw = G2Gateway(a.gateway, decoder=decoder)
    speech = None
    if decoder is not None and not a.no_stt:
        from g2.stt import Speech
        speech = Speech(gw, model=a.model)
    from ai.brain import make_brain
    from notes.store import Store
    async def battery_level() -> float:          # OfflineBrain awaits its getter
        if gw.battery is None:
            raise RuntimeError("battery unknown")
        return float(gw.battery)
    brain = make_brain(battery_getter=battery_level, prefer=a.brain)
    store = Store()
    launcher = Launcher(gw, speech, brain, store, idle_blank_s=a.idle_blank)

    await gw.start()
    if speech is not None:
        log.info("loading speech model %s ...", a.model)
        ok = await speech.start()
        log.info("speech %s", "ready" if ok else "FAILED (assistant/notes/transcribe disabled)")
        if not ok:
            speech = None
            launcher.speech = None
    # wait briefly for the first snapshot so the home screen has battery/time
    for _ in range(50):
        if gw.gateway_up:
            break
        await asyncio.sleep(0.1)
    await launcher.start()
    log.info("launcher running; Ctrl-C to stop")
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await launcher.stop()
        if speech is not None:
            await speech.stop()
        await gw.stop()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
