"""Notes: speak a note, keep it in thoughts.json (shared with the Frame todo
tool). Swipe scrolls the list; tap records a new one."""
from __future__ import annotations

import asyncio

from g2.apps.base import App, AppContext


class Notes(App):
    name = "Notes"
    blurb = "speak a note, keep it"
    order = 30

    def __init__(self) -> None:
        self.scroll = 0
        self.status = ""
        self._ctx: AppContext | None = None

    def text(self, ctx: AppContext, hint: str = "tap new   swipe scroll   2 taps exit") -> str:
        items = ctx.store.pending() if ctx.store is not None else []
        rows = ctx.BODY_ROWS - (1 if self.status else 0)
        if not items:
            body = ["no notes yet"]
        else:
            items = list(reversed(items))
            self.scroll = max(0, min(self.scroll, max(0, len(items) - rows)))
            body = [f"{t.when}  {t.summary[:ctx.COLS - 14]}" for t in items[self.scroll:self.scroll + rows]]
        if self.status:
            body = body + [self.status[:ctx.COLS]]
        return ctx.screen(self.name, body, hint)

    def redraw_text(self):
        return self.text(self._ctx) if self._ctx else None

    async def open(self, ctx: AppContext) -> None:
        self._ctx = ctx
        self.scroll, self.status = 0, ""
        await ctx.show(self.text(ctx))

    async def touch(self, ctx: AppContext, gesture: str) -> None:
        if gesture == "swipe_down":
            self.scroll += 1
        elif gesture == "swipe_up":
            self.scroll = max(0, self.scroll - 1)
        elif gesture == "single_tap":
            if ctx.busy:
                return
            if not ctx.speech_ready:
                self.status = "speech-to-text is not running"
            else:
                ctx.run(self._take(ctx), "g2-note")
                return
        else:
            return
        await ctx.show(self.text(ctx))

    async def on_link(self, ctx: AppContext, ready: bool) -> None:
        if not ready and ctx.busy:
            self.status = "interrupted"

    async def _take(self, ctx: AppContext) -> None:
        self.status = "listening ... speak your note"
        await ctx.show(self.text(ctx, "double tap = cancel"))
        try:
            t0 = ctx.clock()
            pcm = await ctx.speech.capture_utterance(max_s=30)
            if not pcm:
                self.status = "nothing heard"
            else:
                self.status = "transcribing ..."
                await ctx.show(self.text(ctx, "double tap = cancel"))
                text, _ = await ctx.speech.transcribe(pcm)
                thought = ctx.store.add(text, seconds=ctx.clock() - t0) if (text and ctx.store is not None) else None
                self.status = f"saved: {thought.summary}" if thought else "could not make out words"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            ctx.log.exception("note failed")
            self.status = f"failed: {type(exc).__name__}"
        await ctx.show(self.text(ctx))
