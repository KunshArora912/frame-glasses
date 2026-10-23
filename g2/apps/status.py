"""Status: glasses, gateway, speech and brain at a glance. Tap refreshes."""
from __future__ import annotations

import time

from g2.apps.base import App, AppContext


class Status(App):
    name = "Status"
    blurb = "glasses, gateway, speech, brain"
    order = 90

    def text(self, ctx: AppContext) -> str:
        gw = ctx.gw
        st = getattr(gw, "state", {}) or {}
        left, right = st.get("left") or {}, st.get("right") or {}
        up = int(ctx.uptime())
        body = [
            f"glasses   {'ready' if getattr(gw, 'ready', False) else 'away'} ({getattr(gw, 'phase', '?')})   "
            f"L {'on' if left.get('connected') else 'off'}  R {'on' if right.get('connected') else 'off'}",
            f"{ctx.battery_text(getattr(gw, 'battery', None), getattr(gw, 'charging', None))}   firmware {getattr(gw, 'firmware', None) or '?'}",
            f"gateway   {'up' if getattr(gw, 'gateway_up', True) else 'down'}   {getattr(gw, 'base', '')}",
            f"speech    {('ready, ' + ctx.speech.model) if ctx.speech_ready else 'off'}",
            f"brain     {ctx.brain_name(ctx.brain)}",
            f"launcher  up {up // 3600:d}h {up % 3600 // 60:02d}m   last gesture {getattr(gw, 'last_gesture', None) or '-'}",
            "all data stays on this laptop",
        ]
        return ctx.screen(self.name, body, "tap = refresh   double tap = exit")

    async def open(self, ctx: AppContext) -> None:
        t0 = time.time()
        try:
            if hasattr(ctx.gw, "status"):
                await ctx.gw.status()
        except Exception as exc:
            ctx.log.warning("status query failed: %s", exc)
        if time.time() - t0 > 1.0:
            ctx.log.warning("status query took %.1f s", time.time() - t0)
        await ctx.show(self.text(ctx))

    async def touch(self, ctx: AppContext, gesture: str) -> None:
        if gesture == "single_tap":
            await self.open(ctx)
