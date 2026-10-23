"""The tiny API every G2 app is written against.

An app is one file in g2/apps/ containing a subclass of App (or a module-level
APP instance). The launcher finds it, puts it in the menu, opens it on a tap,
forwards gestures, and closes it when the user double-taps out.

    from g2.apps.base import App

    class Hello(App):
        name = "Hello"                 # menu label (12 chars fit nicely)
        blurb = "says hello"           # one line under the menu
        order = 50                     # menu position, lower first

        async def open(self, ctx):
            await ctx.show(ctx.screen("Hello", ["Hello from the laptop."], "tap = again"))

        async def touch(self, ctx, gesture):
            if gesture == "single_tap":
                await ctx.show(ctx.screen("Hello", [f"tapped at {ctx.now():%H:%M:%S}"], "tap = again"))

Gestures that reach touch(): single_tap, swipe_up, swipe_down,
long_press_release. A double tap is taken by the firmware as "leave the app";
the launcher calls close() and shows the home screen (or Even's dashboard).

The display: 576 x 288 px, about 48 columns by 10 rows of one fixed font, no
colours, no font sizes. ctx.screen() builds the standard header/body/hint
string; ctx.show_layout() places several text boxes (see DEVELOPING.md).
"""
from __future__ import annotations

import asyncio
import logging
import textwrap
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Coroutine

COLS = 48
ROWS = 10
BODY_ROWS = 8
GESTURES = ("single_tap", "swipe_up", "swipe_down", "long_press_release")


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


def brain_name(brain) -> str:
    if brain is None:
        return "none"
    n = type(brain).__name__
    return {"ClaudeBrain": "Claude (API, leaves the laptop)",
            "ClaudeCliBrain": "Claude CLI (leaves the laptop)",
            "OfflineBrain": "offline"}.get(n, n)


class App:
    name: str = "App"
    blurb: str = ""
    order: int = 50
    hidden: bool = False          # True: not in the menu (openable via dev.py push)

    @property
    def key(self) -> str:
        return self.name.lower().replace(" ", "_")

    async def open(self, ctx: "AppContext") -> None:
        await ctx.show(ctx.screen(self.name, ["nothing here yet"], "double tap = exit"))

    async def touch(self, ctx: "AppContext", gesture: str) -> None:
        pass

    async def close(self, ctx: "AppContext") -> None:
        """Stop mics, timers, files. Called on exit, park, and link loss."""

    async def on_link(self, ctx: "AppContext", ready: bool) -> None:
        """Glasses went away (False) or came back (True). Redraw is automatic."""

    def redraw_text(self) -> str | None:
        """Return the current screen to re-send after a reconnect; None = last shown."""
        return None


class AppContext:
    """What an app gets to work with. One per launcher; `app` is the current app."""

    COLS, ROWS, BODY_ROWS = COLS, ROWS, BODY_ROWS

    def __init__(self, launcher) -> None:
        self._l = launcher
        self.log = logging.getLogger("g2.app")

    # ---- services -------------------------------------------------------
    @property
    def gw(self):
        return self._l.gw

    @property
    def speech(self):
        return self._l.speech

    @property
    def brain(self):
        return self._l.brain

    @property
    def store(self):
        return self._l.store

    @property
    def clock(self) -> Callable[[], float]:
        return self._l.clock

    @property
    def now(self) -> Callable[[], datetime]:
        return self._l.now

    @property
    def data_dir(self) -> Path:
        return self._l.data_dir

    @property
    def transcripts_dir(self) -> Path:
        return self._l.transcripts_dir

    @property
    def speech_ready(self) -> bool:
        return self.speech is not None and getattr(self.speech, "available", False)

    # ---- display --------------------------------------------------------
    async def show(self, text: str) -> None:
        await self._l.show(text)

    async def show_layout(self, elements: list[dict]) -> None:
        await self._l.show_layout(elements)

    def header(self, title: str, hint: str) -> str:
        return f"{title:<12}{hint}"[:COLS]

    def screen(self, title: str, body: list[str], hint: str = "double tap = exit") -> str:
        return "\n".join([self.header(title, hint), ""] + list(body)[:BODY_ROWS])

    wrap = staticmethod(wrap)
    paginate = staticmethod(paginate)
    battery_text = staticmethod(battery_text)
    brain_name = staticmethod(brain_name)

    # ---- control --------------------------------------------------------
    def run(self, coro: Coroutine[Any, Any, Any], name: str = "app-task") -> asyncio.Task:
        """Run a long job (listening, thinking). Cancelled automatically on exit."""
        return self._l.run_busy(coro, name)

    @property
    def busy(self) -> bool:
        return self._l.busy is not None and not self._l.busy.done()

    async def exit(self) -> None:
        await self._l.home()

    def uptime(self) -> float:
        return self.clock() - self._l.started_at
