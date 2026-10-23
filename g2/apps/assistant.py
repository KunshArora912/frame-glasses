"""Assistant: tap, ask by voice, read the answer. Speech-to-text runs on the
laptop; the brain is offline unless you opt in to Claude (see --brain)."""
from __future__ import annotations

import asyncio

from g2.apps.base import App, AppContext


class Assistant(App):
    name = "Assistant"
    blurb = "ask by voice, read the answer"
    order = 10

    def __init__(self) -> None:
        self.state = "idle"        # idle | listening | thinking | answer
        self.pages: list[list[str]] = []
        self.page = 0
        self.question = ""
        self._last = ""

    def text(self, ctx: AppContext, body: list[str], hint: str = "tap = ask      double tap = exit") -> str:
        if self.state == "answer" and len(self.pages) > 1:
            hint = f"tap = more  {self.page + 1}/{len(self.pages)}   double tap = exit"
        self._last = ctx.screen(self.name, body, hint)
        return self._last

    def redraw_text(self):
        return self._last or None

    async def open(self, ctx: AppContext) -> None:
        self.pages, self.page, self.question, self.state = [], 0, "", "idle"
        if not ctx.speech_ready:
            await ctx.show(self.text(ctx, ["speech-to-text is not running on the laptop"], "double tap = exit"))
            return
        await ctx.show(self.text(ctx, ["Tap, then ask your question.", "", f"brain: {ctx.brain_name(ctx.brain)}"]))

    async def touch(self, ctx: AppContext, gesture: str) -> None:
        if gesture != "single_tap" or self.state in ("listening", "thinking") or not ctx.speech_ready:
            return
        if self.state == "answer" and self.page < len(self.pages) - 1:
            self.page += 1
            await ctx.show(self.text(ctx, self.pages[self.page]))
            return
        ctx.run(self._ask(ctx), "g2-ask")

    async def close(self, ctx: AppContext) -> None:
        self.state = "idle"

    async def on_link(self, ctx: AppContext, ready: bool) -> None:
        if not ready and self.state in ("listening", "thinking"):
            self.state = "idle"
            self.text(ctx, ["Interrupted: glasses went away.", "", "Tap to try again."])

    async def _ask(self, ctx: AppContext) -> None:
        self.state = "listening"
        await ctx.show(self.text(ctx, ["Listening ...", "", "speak now; it stops when you pause"], "double tap = cancel"))
        try:
            pcm = await ctx.speech.capture_utterance()
            if not pcm:
                self.state = "idle"
                await ctx.show(self.text(ctx, ["Nothing heard.", "", "Tap to try again."]))
                return
            self.state = "thinking"
            await ctx.show(self.text(ctx, ["Heard you. Transcribing ..."], "double tap = cancel"))
            text, _level = await ctx.speech.transcribe(pcm)
            if not text:
                self.state = "idle"
                await ctx.show(self.text(ctx, ["Could not make out words.", "", "Tap to try again."]))
                return
            self.question = text
            await ctx.show(self.text(ctx, [f"You: {text}", "", "Thinking ..."], "double tap = cancel"))
            answer = await ctx.brain.ask(text) if ctx.brain else "no brain configured"
            self.pages = ctx.paginate(f"You: {text}\n\n{answer}")
            self.page = 0
            self.state = "answer"
            await ctx.show(self.text(ctx, self.pages[0], "tap = ask again   double tap = exit"))
        except asyncio.CancelledError:
            self.state = "idle"
            raise
        except Exception as exc:
            ctx.log.exception("assistant failed")
            self.state = "idle"
            await ctx.show(self.text(ctx, [f"Failed: {type(exc).__name__}", *ctx.wrap(str(exc))[:2], "", "Tap to try again."]))
