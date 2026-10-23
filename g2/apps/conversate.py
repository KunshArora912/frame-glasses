"""Conversate (hidden fallback; the native version is NativeApps.conversate_open in g2/native_apps.py): live transcription (like Transcribe) plus, on tap, a local
summary of the conversation so far from ``ctx.brain``. Double tap ends the
session and writes transcript + summary to <transcripts_dir>/conversate_<time>.txt.

Everything stays on the laptop: the brain is offline unless --brain opted in.
No native frames are sent.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from g2.apps.base import App, AppContext

HINT_LIVE = "tap summary  2 taps end"
HINT_SUMMARY = "tap back  2 taps end"
PROMPT = "Summarize in 3 short bullet points: "


class Conversate(App):
    name = "Conversate"
    blurb = "live transcript + summary on tap"
    order = 25
    hidden = True                 # fallback: live transcript on the firmware Translate surface lives in g2/native_apps.py

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.entries: list[str] = []          # raw caption chunks, for the summary
        self.file: Path | None = None
        self.live = None
        self.summary = ""
        self.pages: list[list[str]] = []
        self.page = 0
        self.view = "live"                    # live | summary
        self.status = ""
        self._ctx: AppContext | None = None
        self._last = ""

    # ---- screens ----------------------------------------------------------
    def text(self, ctx: AppContext) -> str:
        if self.view == "summary" and self.pages:
            n = len(self.pages)
            hint = HINT_SUMMARY if n == 1 else f"tap more  {self.page + 1}/{n}  2 taps end"
            self._last = ctx.screen("Summary", self.pages[self.page], hint)
            return self._last
        rows = ctx.BODY_ROWS - (1 if self.status else 0)
        body = self.lines[-rows:] or ["listening ..."]
        if self.status:
            body = body + [self.status[:ctx.COLS]]
        self._last = ctx.screen(self.name, body, HINT_LIVE)
        return self._last

    def redraw_text(self):
        return self._last or None

    # ---- lifecycle --------------------------------------------------------
    async def open(self, ctx: AppContext) -> None:
        self._ctx = ctx
        self.lines, self.entries, self.summary, self.pages, self.page = [], [], "", [], 0
        self.view, self.status = "live", ""
        if not ctx.speech_ready:
            self._last = ctx.screen(self.name, ["speech-to-text is not running on the laptop"])
            await ctx.show(self._last)
            return
        ctx.transcripts_dir.mkdir(parents=True, exist_ok=True)
        self.file = ctx.transcripts_dir / f"conversate_{ctx.now():%Y-%m-%d_%H%M%S}.txt"
        self.file.write_text(f"# G2 conversation {ctx.now():%Y-%m-%d %H:%M:%S}\n\n## Transcript\n", encoding="utf-8")
        self.live = ctx.speech.captioner(self._on_caption)
        await self.live.start()
        await ctx.show(self.text(ctx))

    async def _on_caption(self, fresh: str) -> None:
        ctx = self._ctx
        if not fresh or ctx is None or self.file is None:
            return
        fresh = fresh.strip()
        self.entries.append(fresh)
        with self.file.open("a", encoding="utf-8") as f:
            f.write(f"[{ctx.now():%H:%M:%S}] {fresh}\n")
        joined = (self.lines[-1] + " " + fresh) if self.lines else fresh
        self.lines = self.lines[:-1] + ctx.wrap(joined)
        if self.live is not None and self.view == "live":
            await ctx.show(self.text(ctx))

    async def touch(self, ctx: AppContext, gesture: str) -> None:
        if gesture != "single_tap" or self.live is None:
            return
        if self.view == "summary":
            if self.page < len(self.pages) - 1:
                self.page += 1
            else:
                self.view, self.page = "live", 0
            await ctx.show(self.text(ctx))
            return
        if ctx.busy:
            return
        if not self.entries:
            self.status = "nothing to summarize yet"
            await ctx.show(self.text(ctx))
            return
        if ctx.brain is None:
            self.status = "no brain configured"
            await ctx.show(self.text(ctx))
            return
        ctx.run(self._summarize(ctx), "g2-summary")

    async def _summarize(self, ctx: AppContext) -> None:
        self.status = "summarizing ..."
        await ctx.show(self.text(ctx))
        try:
            answer = await ctx.brain.ask(PROMPT + " ".join(self.entries))
            self.summary = (answer or "").strip() or "(empty summary)"
            self.pages = ctx.paginate(self.summary)
            self.page, self.view, self.status = 0, "summary", ""
        except asyncio.CancelledError:
            self.status = ""
            raise
        except Exception as exc:
            ctx.log.exception("summary failed")
            self.status = f"summary failed: {type(exc).__name__}"
        await ctx.show(self.text(ctx))

    async def close(self, ctx: AppContext) -> None:
        live, self.live = self.live, None
        if live is not None:
            try:
                await live.stop()
            except Exception as exc:
                ctx.log.warning("stopping captions: %s", exc)
        if self.file is not None:
            try:
                with self.file.open("a", encoding="utf-8") as f:
                    f.write("\n## Summary\n")
                    f.write((self.summary or "(no summary requested)") + "\n")
            except OSError as exc:
                ctx.log.warning("writing summary: %s", exc)
            self.file = None

    async def on_link(self, ctx: AppContext, ready: bool) -> None:
        if ready and self.live is not None:
            try:
                await self.live.resume()
            except Exception as exc:
                ctx.log.warning("mic resume failed: %s", exc)
