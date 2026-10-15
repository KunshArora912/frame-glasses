"""Turning taps into intentions.

Frame's only input is one IMU tap sensor, and it is noisy: a single
physical tap fires several interrupts, and the taps that WAKE the glasses
arrive while the app is still uploading. Both facts were measured the hard
way -- two hardware navigation runs ended themselves within seconds of
starting, each reporting a "double tap" nobody made.

So gestures are counted against a TIME WINDOW, never against the host's
poll interval: how many taps a burst appears to contain must not depend on
where the loop happened to look.

`Gesture` is deliberately about intent rather than mechanism. When the
Mudra band turns out to be reachable, it becomes a second InputSource
emitting the same four values and not one app changes.
"""

from __future__ import annotations

import asyncio
import enum
import time

# A burst is over once this long passes with no further tap.
GESTURE_WINDOW = 1.0
# Taps arriving this soon after start-up are the wake-up gesture echoing.
STARTUP_GRACE = 6.0


class Gesture(enum.Enum):
    NEXT = "next"         # one tap: move down / advance
    PREV = "prev"         # move up -- only an input with a real D-pad
                          # emits this; taps never do
    SELECT = "select"     # two taps: open / confirm
    BACK = "back"         # three taps: leave the app
    QUIT = "quit"         # four or more: shut the shell down


# How many taps mean what. Kept as data so an input source with real
# discrete gestures can map onto the same intents directly.
_BY_COUNT = {1: Gesture.NEXT, 2: Gesture.SELECT, 3: Gesture.BACK}


def gesture_for(taps: int) -> Gesture | None:
    if taps <= 0:
        return None
    return _BY_COUNT.get(taps, Gesture.QUIT)


class TapInput:
    """Collects tap events from the glasses into whole gestures."""

    name = "taps"

    def __init__(self, queue: asyncio.Queue) -> None:
        self.queue = queue
        self._live_at = time.monotonic() + STARTUP_GRACE

    def arm(self, grace: float = STARTUP_GRACE) -> None:
        self._live_at = time.monotonic() + grace

    @property
    def armed(self) -> bool:
        return time.monotonic() >= self._live_at

    async def drain(self) -> int:
        n = 0
        while True:
            try:
                self.queue.get_nowait()
                n += 1
            except asyncio.QueueEmpty:
                return n

    async def next_gesture(self, timeout: float | None = None) -> Gesture | None:
        """Wait for a burst of taps and return what it meant.

        Returns None on timeout, or when the burst arrived during start-up
        and was therefore the wake-up gesture rather than an instruction.
        """
        try:
            await asyncio.wait_for(self.queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

        taps = 1 + await self.drain()
        until = time.monotonic() + GESTURE_WINDOW
        while True:
            remaining = until - time.monotonic()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(self.queue.get(), timeout=remaining)
                taps += 1
                until = time.monotonic() + GESTURE_WINDOW   # extend the burst
            except asyncio.TimeoutError:
                break
        taps += await self.drain()

        if not self.armed:
            return None
        return gesture_for(taps)
