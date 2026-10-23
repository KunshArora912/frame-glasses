"""Transcribe: live captions on the glasses, saved to g2/transcripts/<date>.txt
on the laptop. Tap inserts a paragraph break. Double tap stops."""
from __future__ import annotations

from pathlib import Path

from g2.apps.base import App, AppContext


class Transcribe(App):
    name = "Transcribe"
    blurb = "live captions, saved to a file"
    order = 20

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.file: Path | None = None
        self.live = None
        self._ctx: AppContext | None = None

    def text(self, ctx: AppContext, hint: str = "double tap = stop") -> str:
        body = self.lines[-ctx.BODY_ROWS:] or ["listening ..."]
        return ctx.screen(self.name, body, hint)

    def redraw_text(self):
        return self.text(self._ctx) if self._ctx else None

    async def open(self, ctx: AppContext) -> None:
        self._ctx = ctx
        self.lines = []
        if not ctx.speech_ready:
            await ctx.show(ctx.screen(self.name, ["speech-to-text is not running on the laptop"]))
            return
        ctx.transcripts_dir.mkdir(parents=True, exist_ok=True)
        self.file = ctx.transcripts_dir / f"{ctx.now():%Y-%m-%d_%H%M%S}.txt"
        self.file.write_text(f"# G2 transcript {ctx.now():%Y-%m-%d %H:%M:%S}\n", encoding="utf-8")
        self.live = ctx.speech.captioner(self._on_caption)
        await self.live.start()
        await ctx.show(self.text(ctx))

    async def _on_caption(self, fresh: str) -> None:
        ctx = self._ctx
        if not fresh or ctx is None or self.file is None:
            return
        with self.file.open("a", encoding="utf-8") as f:
            f.write(f"[{ctx.now():%H:%M:%S}] {fresh}\n")
        joined = (self.lines[-1] + " " + fresh) if self.lines else fresh
        self.lines = self.lines[:-1] + ctx.wrap(joined)
        if self.live is not None:
            await ctx.show(self.text(ctx))

    async def touch(self, ctx: AppContext, gesture: str) -> None:
        if gesture == "single_tap" and self.live is not None and self.file is not None:
            with self.file.open("a", encoding="utf-8") as f:
                f.write("\n")
            self.lines.append("")
            await ctx.show(self.text(ctx))

    async def close(self, ctx: AppContext) -> None:
        live, self.live = self.live, None
        if live is not None:
            try:
                await live.stop()
            except Exception as exc:
                ctx.log.warning("stopping captions: %s", exc)

    async def on_link(self, ctx: AppContext, ready: bool) -> None:
        if ready and self.live is not None:
            try:
                await self.live.resume()      # the device forgets the mic flag on reconnect
            except Exception as exc:
                ctx.log.warning("mic resume failed: %s", exc)
