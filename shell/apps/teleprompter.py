"""Your script on the glasses while you film.

Meta ships a teleprompter on the Ray-Ban Display and you scroll it by
pinching your fingers, one card at a time. This one can follow your VOICE:
it listens to what you are actually saying, finds that place in the script,
and advances on its own. Hands free, eyes on the lens.

Two things make that work on hardware this small:

  * matching is done on a bag of words against a WINDOW of the script
    around where we already are, not against the whole text. Scripts repeat
    phrases ("and then", "so what I did"), and a global match jumps to the
    wrong verse. A forward-biased window cannot.
  * it only ever moves FORWARD. A mis-hear should cost you nothing; the
    worst case is that the script sits still for a sentence, which is also
    exactly what happens when you ad-lib.

The panel shows three lines: what you just said, what you are saying now,
and what comes next -- with the current line bright and the others dimmed.
"""

from __future__ import annotations

import re
import time

from ai.display import to_ascii, wrap
from shell.app import App
from shell.gestures import Gesture

# A line of script, sized for the panel's 26-character rows.
LINE_CHARS = 24

# How far ahead of the current line the matcher will look. Wide enough to
# catch a skipped sentence, narrow enough that a repeated phrase later in
# the script cannot win.
LOOK_AHEAD = 6
# Fraction of a line's words that must appear in what was heard.
MATCH_RATIO = 0.45

_WORD = re.compile(r"[a-z0-9']+")


def words(text: str) -> list[str]:
    return _WORD.findall(to_ascii(text or "").lower())


def split_script(text: str) -> list[str]:
    """Break a script into panel-width lines.

    Wrapping each SENTENCE separately is the obvious approach and it is
    wrong twice over: it strands tails on their own row ("back.",
    "dollars."), wasting a third of a three-line view, and a one-word row
    matches any sentence containing that word with a perfect score, so the
    voice follower jumps to it. Wrapping runs continuously instead, and
    any stranded tail is pulled back up into the previous row when it
    fits.
    """
    cleaned = " ".join(to_ascii(text or "").split())
    if not cleaned:
        return []

    lines = wrap(cleaned, LINE_CHARS)
    merged: list[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # A row with fewer than three words reads as a fragment; fold it
        # back if the row above has room.
        if (merged and len(line.split()) < 3
                and len(merged[-1]) + 1 + len(line) <= LINE_CHARS):
            merged[-1] = f"{merged[-1]} {line}"
        else:
            merged.append(line)
    return merged


def match_line(heard: str, lines: list[str], current: int) -> int:
    """Best place in the script for what was just heard. Never goes back."""
    said = set(words(heard))
    if not said:
        return current

    best, best_score = current, 0.0
    last = min(len(lines), current + LOOK_AHEAD + 1)
    for i in range(current, last):
        target = words(lines[i])
        if len(target) < 2:
            continue          # too short to identify a place in the script
        hits = sum(1 for w in target if w in said)
        score = hits / len(target)
        # Ties go to the EARLIER line: drifting ahead is worse than lagging,
        # because a reader who has lost their place looks down, not up.
        if score > best_score + 1e-9 and score >= MATCH_RATIO:
            best, best_score = i, score
    return best


class Teleprompter(App):
    """Reads a script file; follows your voice, or advances on a tap."""

    def __init__(self, script_path, listener_factory=None) -> None:
        super().__init__(name="teleprompter", label="Teleprompter",
                         tick_seconds=0.0)
        self.script_path = script_path
        self.listener_factory = listener_factory
        self.listener = None
        self.lines: list[str] = []
        self.index = 0
        self.started_at = 0.0
        self._following = False

    # ------------------------------------------------------------ script

    def load(self) -> None:
        try:
            raw = self.script_path.read_text(encoding="utf-8")
        except OSError:
            raw = ""
        self.lines = split_script(raw)

    # ------------------------------------------------------------ drawing

    def view(self) -> list[str]:
        """Previous, current and next lines, plus how far through you are."""
        if not self.lines:
            return ["", "no script yet", "",
                    f"write one to {self.script_path.name}"]
        previous = self.lines[self.index - 1] if self.index > 0 else ""
        current = self.lines[self.index]
        following = (self.lines[self.index + 1]
                     if self.index + 1 < len(self.lines) else "")
        after = (self.lines[self.index + 2]
                 if self.index + 2 < len(self.lines) else "")

        elapsed = time.monotonic() - self.started_at if self.started_at else 0
        progress = f"{self.index + 1}/{len(self.lines)}"
        mark = "voice" if self._following else "tap"
        # The CURRENT line must land on row 2: that is the row the device
        # draws bright when `emphasis` is set. Any padding above it shifts
        # the highlight onto the line you have already read.
        return [previous, current, following, after, "",
                f"{progress}  {int(elapsed // 60)}:{int(elapsed % 60):02d}  {mark}"]

    async def draw(self) -> None:
        # emphasis dims everything except the second row, which is where
        # the current line sits.
        rows = self.view()
        await self.screen.page(rows, emphasis=True)

    # -------------------------------------------------------------- hooks

    async def start(self) -> None:
        self.load()
        self.index = 0
        self.started_at = time.monotonic()
        self._following = False
        await self.draw()

        # Voice-following is a bonus, never a requirement: if the mic or
        # the model is unavailable the script still advances on a tap.
        shell = self.context.get("shell")
        if shell is None or self.listener_factory is None:
            return
        self.listener = await self.listener_factory()
        if self.listener is None:
            return
        await self.listener.start(self.on_heard)
        self._following = await shell.start_audio(self.listener)
        await self.draw()

    async def stop(self) -> None:
        shell = self.context.get("shell")
        if shell is not None:
            await shell.stop_audio()
        self.listener = None
        self._following = False

    async def on_heard(self, text: str) -> None:
        """Called by the listener with whatever it just transcribed."""
        if not self.lines:
            return
        moved = match_line(text, self.lines, self.index)
        if moved != self.index:
            self.index = moved
            await self.draw()

    async def gesture(self, gesture: Gesture) -> bool:
        if gesture is Gesture.NEXT:
            if self.index + 1 < len(self.lines):
                self.index += 1
                await self.draw()
        elif gesture is Gesture.SELECT:
            # Restart from the top -- the thing you want between takes.
            self.index = 0
            self.started_at = time.monotonic()
            await self.draw()
        return True
