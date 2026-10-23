"""TEMPLATE: copy me to g2/apps/<yourname>.py (or run: python g2/dev.py new <yourname>).

Everything you need is on `ctx`; see g2/apps/base.py and g2/DEVELOPING.md.
Keep lines under 48 characters and screens under 10 rows.
"""
from __future__ import annotations

from g2.apps.base import App, AppContext


class MyApp(App):
    name = "My App"                       # menu label
    blurb = "one line that says what it does"
    order = 50                            # lower = higher in the menu

    def __init__(self) -> None:
        self.count = 0

    async def open(self, ctx: AppContext) -> None:
        """Called when the user taps your entry. Draw your first screen."""
        self.count = 0
        await ctx.show(ctx.screen(self.name, ["Hello.", "", "Tap to count."], "tap = count   double tap = exit"))

    async def touch(self, ctx: AppContext, gesture: str) -> None:
        """single_tap, swipe_up, swipe_down, long_press_release."""
        if gesture == "single_tap":
            self.count += 1
            await ctx.show(ctx.screen(self.name, [f"tapped {self.count} times"], "tap = count   double tap = exit"))
        elif gesture in ("swipe_up", "swipe_down"):
            await ctx.show(ctx.screen(self.name, [f"swiped {gesture[6:]}"], "tap = count   double tap = exit"))

    async def close(self, ctx: AppContext) -> None:
        """Called on exit, park, or link loss: stop anything you started."""

    # ---- things you can do -----------------------------------------------
    # Voice:   pcm = await ctx.speech.capture_utterance(); text, _ = await ctx.speech.transcribe(pcm)
    # Captions: live = ctx.speech.captioner(callback); await live.start(); ... await live.stop()
    # Brain:   answer = await ctx.brain.ask("question")
    # Notes:   ctx.store.add("text"); ctx.store.pending()
    # Battery: ctx.gw.battery, ctx.gw.charging, ctx.gw.firmware
    # Layout:  await ctx.show_layout([{"type": "text", "text": "...", "x": 16, "y": 12,
    #              "width": 544, "height": 264, "capture_events": True, "container_name": "main"}])
    # Long job: ctx.run(self._job(ctx)) runs it in the background and cancels it on exit.
