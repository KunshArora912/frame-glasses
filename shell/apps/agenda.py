"""What is next, and how long you have.

The useful question a calendar answers on a HUD is not "what does my week
look like" -- six 26-character rows cannot answer that and shouldn't try.
It is "what is next and am I late". So the first row is the countdown, in
the units a person actually thinks in, and the rest is the day.

Events come from a JSON file the laptop refreshes, not from a live API
call: the glasses hold the only Bluetooth connection and a network stall
would freeze the panel. A stale file still answers the question.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from ai.display import to_ascii
from shell.app import App
from shell.gestures import Gesture

DEFAULT_PATH = Path(__file__).parent.parent.parent / "agenda.json"

# Anything further out than this is "later", not "next".
HORIZON_HOURS = 14


def parse_when(value: str) -> datetime | None:
    if not value:
        return None
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        return None
    # All-day events arrive naive; treat them as local midnight.
    if when.tzinfo is None:
        return when
    return when.astimezone().replace(tzinfo=None)


def countdown(delta: timedelta) -> str:
    """How long until it, in the units a person actually thinks in."""
    total = int(delta.total_seconds())
    if total < 0:
        past = -total
        if past < 3600:
            return f"started {past // 60} min ago"
        return f"started {past // 3600}h ago"
    if total < 60:
        return "now"
    if total < 3600:
        return f"in {total // 60} min"
    if total < HORIZON_HOURS * 3600:
        hours, mins = divmod(total // 60, 60)
        return f"in {hours}h {mins:02d}"
    return "later"


class Agenda(App):
    def __init__(self, path: Path | None = None) -> None:
        super().__init__(name="agenda", label="Day ahead", tick_seconds=20.0)
        self.path = path or DEFAULT_PATH
        self.events: list[dict] = []
        self.offset = 0

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.events = []
            return
        events = []
        for row in raw if isinstance(raw, list) else []:
            when = parse_when(str(row.get("start", "")))
            if when is None:
                continue
            events.append({"when": when,
                           "title": to_ascii(str(row.get("title", "")))[:24],
                           "where": to_ascii(str(row.get("where", "")))[:24],
                           "all_day": bool(row.get("all_day"))})
        self.events = sorted(events, key=lambda e: e["when"])

    def upcoming(self, now: datetime | None = None) -> list[dict]:
        now = now or datetime.now()
        # Keep something that started in the last 15 minutes: "you are late
        # for it" is the most useful thing a glance can tell you.
        floor = now - timedelta(minutes=15)
        return [e for e in self.events if e["when"] >= floor]

    def view(self, now: datetime | None = None) -> list[str]:
        now = now or datetime.now()
        ahead = self.upcoming(now)
        if not ahead:
            return ["", "nothing left today", "",
                    now.strftime("%a %d %b  %H:%M")]

        index = min(self.offset, len(ahead) - 1)
        event = ahead[index]
        rows = [countdown(event["when"] - now),
                event["title"],
                event["where"] or event["when"].strftime("%H:%M"),
                ""]
        for following in ahead[index + 1:index + 3]:
            rows.append(f"{following['when']:%H:%M}  {following['title'][:18]}")
        if len(ahead) > 1:
            rows.append(f"{index + 1}/{len(ahead)}   tap for next")
        return rows[:7]

    async def draw(self) -> None:
        await self.screen.page(self.view(), heading=True)

    async def start(self) -> None:
        self.load()
        self.offset = 0
        await self.draw()

    async def tick(self) -> None:
        self.load()                 # the laptop may have refreshed it
        await self.draw()

    async def gesture(self, gesture: Gesture) -> bool:
        ahead = self.upcoming()
        if gesture is Gesture.NEXT and ahead:
            self.offset = (self.offset + 1) % len(ahead)
            await self.draw()
        elif gesture is Gesture.SELECT:
            self.offset = 0
            self.load()
            await self.draw()
        return True
