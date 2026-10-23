"""Translate (hidden fallback; the firmware Translate app is hosted by g2/native_apps.py): live two-way captions. The laptop's speech-to-text listens, each
fresh chunk is translated locally with argostranslate when it is installed
(nothing is downloaded or installed by this app), and the glasses show the
source line and the translated line. Swipe switches the language pair, tap
pauses. The session is saved to <transcripts_dir>/translate_<time>.txt.

Without argostranslate the app still captions and shows how to install it.
No native frames are sent; the native Translate channel (apps.tr_*) is DECODED
only and not used here.
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from typing import Awaitable, Callable

from g2.apps.base import App, AppContext

PAIRS: list[tuple[str, str]] = [("en", "es"), ("es", "en"), ("en", "fr"), ("fr", "en"), ("en", "de"), ("de", "en")]
LANG_NAMES = {"en": "English", "es": "Spanish", "fr": "French", "de": "German"}
HINT_LIVE = "swipe pair  tap pause  2 taps exit"
HINT_PAUSED = "swipe pair  tap resume  2 taps exit"
SRC_ROWS, DST_ROWS = 3, 3

Translator = Callable[[str, str, str], Awaitable[str]]


def argos_available() -> bool:
    return importlib.util.find_spec("argostranslate") is not None


def make_argos_translator() -> Translator:
    """A translator over the installed argostranslate packages (imported lazily)."""
    import argostranslate.translate as at  # type: ignore

    async def translate(text: str, src: str, dst: str) -> str:
        def work() -> str:
            fn = getattr(at, "get_translation_from_codes", None)
            if fn is not None:
                tr = fn(src, dst)
                if tr is None:
                    raise LookupError(f"argos package {src}>{dst} not installed")
                return tr.translate(text)
            return at.translate(text, src, dst)
        return await asyncio.to_thread(work)

    return translate


class Translate(App):
    name = "Translate"
    blurb = "live captions, translated on the laptop"
    order = 50
    hidden = True                 # fallback: the real Translate is the firmware app hosted by g2/native_apps.py

    def __init__(self) -> None:
        self.pair = 0
        self.src_lines: list[str] = []
        self.dst_lines: list[str] = []
        self.live = None
        self.paused = False
        self.file: Path | None = None
        self.translator: Translator | None = None     # tests may inject one
        self.engine = ""                               # "argos" | "captions only"
        self.status = ""
        self._ctx: AppContext | None = None
        self._last = ""

    # ---- screens ----------------------------------------------------------
    @property
    def src(self) -> str:
        return PAIRS[self.pair][0]

    @property
    def dst(self) -> str:
        return PAIRS[self.pair][1]

    def text(self, ctx: AppContext) -> str:
        head = f"{self.src.upper()} > {self.dst.upper()}   {self.engine}"
        if self.paused:
            head += "   paused"
        body = [head[:ctx.COLS]]
        body += (self.src_lines[-SRC_ROWS:] or ["listening ..." if not self.paused else ""])
        body += [""] * (SRC_ROWS - min(SRC_ROWS, len(self.src_lines[-SRC_ROWS:]) or 1))
        body.append("")
        dst = self.dst_lines[-DST_ROWS:]
        body += dst or [self.status[:ctx.COLS]]
        self._last = ctx.screen(self.name, body, HINT_PAUSED if self.paused else HINT_LIVE)
        return self._last

    def redraw_text(self):
        return self._last or None

    # ---- lifecycle --------------------------------------------------------
    async def open(self, ctx: AppContext) -> None:
        self._ctx = ctx
        self.src_lines, self.dst_lines, self.paused, self.status = [], [], False, ""
        if not ctx.speech_ready:
            self._last = ctx.screen(self.name, ["speech-to-text is not running on the laptop"])
            await ctx.show(self._last)
            return
        if self.translator is None and argos_available():
            try:
                self.translator = make_argos_translator()
            except Exception as exc:
                ctx.log.warning("argostranslate import failed: %s", exc)
        if self.translator is not None:
            self.engine = "argos"
        else:
            self.engine = "captions only"
            self.status = "install: pip install argostranslate"
        ctx.transcripts_dir.mkdir(parents=True, exist_ok=True)
        self.file = ctx.transcripts_dir / f"translate_{ctx.now():%Y-%m-%d_%H%M%S}.txt"
        self.file.write_text(f"# G2 translate session {ctx.now():%Y-%m-%d %H:%M:%S} ({self.engine})\n", encoding="utf-8")
        self.live = ctx.speech.captioner(self._on_caption)
        await self.live.start()
        await ctx.show(self.text(ctx))

    async def _on_caption(self, fresh: str) -> None:
        ctx = self._ctx
        if not fresh or ctx is None or self.live is None or self.paused:
            return
        fresh = fresh.strip()
        self.src_lines = (self.src_lines + ctx.wrap(fresh))[-12:]
        translated = ""
        if self.translator is not None:
            try:
                translated = (await self.translator(fresh, self.src, self.dst)).strip()
            except LookupError:
                self.status = f"missing: argospm install translate-{self.src}_{self.dst}"[:ctx.COLS]
            except Exception as exc:
                ctx.log.warning("translation failed: %s", exc)
                self.status = f"translation failed: {type(exc).__name__}"[:ctx.COLS]
        if translated:
            self.dst_lines = (self.dst_lines + ctx.wrap(translated))[-12:]
        if self.file is not None:
            with self.file.open("a", encoding="utf-8") as f:
                f.write(f"[{ctx.now():%H:%M:%S}] {self.src}>{self.dst} | {fresh}" + (f" || {translated}" if translated else "") + "\n")
        if self.live is not None:
            await ctx.show(self.text(ctx))

    async def touch(self, ctx: AppContext, gesture: str) -> None:
        if self.live is None:
            return
        if gesture in ("swipe_down", "swipe_up"):
            self.pair = (self.pair + (1 if gesture == "swipe_down" else -1)) % len(PAIRS)
            self.src_lines, self.dst_lines = [], []
            if self.engine != "argos":
                self.status = "install: pip install argostranslate"
            else:
                self.status = ""
            if self.file is not None:
                with self.file.open("a", encoding="utf-8") as f:
                    f.write(f"\n# pair {self.src}>{self.dst}\n")
        elif gesture == "single_tap":
            self.paused = not self.paused
            try:
                if self.paused:
                    await self.live.stop()
                else:
                    self.live = ctx.speech.captioner(self._on_caption)
                    await self.live.start()
            except Exception as exc:
                ctx.log.warning("pause/resume failed: %s", exc)
        else:
            return
        await ctx.show(self.text(ctx))

    async def close(self, ctx: AppContext) -> None:
        live, self.live = self.live, None
        if live is not None and not self.paused:
            try:
                await live.stop()
            except Exception as exc:
                ctx.log.warning("stopping captions: %s", exc)
        self.paused = False

    async def on_link(self, ctx: AppContext, ready: bool) -> None:
        if ready and self.live is not None and not self.paused:
            try:
                await self.live.resume()
            except Exception as exc:
                ctx.log.warning("mic resume failed: %s", exc)
