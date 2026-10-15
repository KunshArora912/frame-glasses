"""Transcribing speech as it happens, rather than after it stops.

`SttClient` is batch: hand it a recording, get text back. That is right for
"tap, ask a question, read the answer" because the question has a clear
end. It is wrong for captions, for a teleprompter following your voice, and
for rewinding what someone just said -- all of which need words while the
talking is still going on.

This wraps the same warm worker in a sliding window:

    audio in  ->  ring buffer  ->  every STEP seconds, transcribe the last
                                   WINDOW seconds  ->  emit what is new

WHY A SLIDING WINDOW AND NOT CHUNKS. Transcribing fixed chunks cuts words
in half at every boundary, and Whisper hallucinates confidently on a
fragment that starts mid-word. Overlapping windows mean every word is seen
whole at least once; the cost is that each window re-transcribes audio
already seen, which is why only the NEW tail of each result is emitted.

WHY NOT VAD. Voice activity detection would cut cleanly at pauses, but
Frame's microphone FIFO holds only 2.048 s and never recovers once it laps
-- so the host must keep draining regardless of whether anyone is speaking.
Given we are paying for the audio anyway, a timer is simpler and has no
threshold to tune wrong in a noisy room.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import datetime

# How much audio each pass looks at, and how often a pass happens. 6 s is
# long enough for Whisper to have context to work with; 2 s keeps the lag
# between speaking and seeing it short enough to read along with.
WINDOW_SECONDS = 6.0
STEP_SECONDS = 2.0

# Below this the window is silence and not worth a transcription pass --
# the model will invent text from room tone given the chance.
QUIET_DBFS = -45.0


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def new_tail(previous: str, current: str) -> str:
    """The part of `current` that was not already in `previous`.

    Windows overlap, so each result repeats most of the one before it.
    Comparing on words rather than characters survives the model changing
    its mind about punctuation or capitalisation between passes, which it
    does constantly.
    """
    old = previous.split()
    new = current.split()
    if not old:
        return current.strip()

    # Find the longest suffix of `old` that prefixes `new`, and take what
    # follows it. Bounded by the window, so this stays cheap.
    most = min(len(old), len(new))
    for size in range(most, 0, -1):
        if [w.lower().strip(".,!?") for w in old[-size:]] == \
           [w.lower().strip(".,!?") for w in new[:size]]:
            return " ".join(new[size:]).strip()
    return " ".join(new).strip()


class Listener:
    """Feeds audio in, calls back with text as it is recognised.

    The callback receives only NEW words, so a caption view can append and
    a teleprompter can match without seeing the same phrase twice.
    """

    def __init__(self, stt, rate: int = 16000, depth: int = 8,
                 window: float = WINDOW_SECONDS,
                 step: float = STEP_SECONDS) -> None:
        self.stt = stt
        self.rate = rate
        self.depth = depth
        self.window = window
        self.step = step
        self.bytes_per_second = rate * depth // 8
        self._audio = deque()
        self._held = 0
        self._last_text = ""
        self._task: asyncio.Task | None = None
        self._callback = None
        self._running = False
        self.transcript: list[str] = []

    # --------------------------------------------------------------- feed

    def feed(self, chunk: bytes) -> None:
        """Push audio in. Safe to call from the BLE receive path."""
        if not chunk:
            return
        self._audio.append(chunk)
        self._held += len(chunk)
        limit = int(self.window * self.bytes_per_second)
        while self._held > limit and len(self._audio) > 1:
            self._held -= len(self._audio.popleft())

    def _window_bytes(self) -> bytes:
        return b"".join(self._audio)

    # -------------------------------------------------------------- hooks

    async def start(self, callback) -> None:
        self._callback = callback
        self._last_text = ""
        self.transcript = []
        self._running = True
        self._task = asyncio.create_task(self._pump(), name="listener")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._audio.clear()
        self._held = 0

    @property
    def text(self) -> str:
        return " ".join(self.transcript)

    # --------------------------------------------------------------- pump

    async def _pump(self) -> None:
        while self._running:
            await asyncio.sleep(self.step)

            # Not enough audio yet to be worth a pass.
            if self._held < int(self.bytes_per_second * 1.5):
                continue

            audio = self._window_bytes()
            try:
                text, level = await self.stt.transcribe(audio, self.rate,
                                                        self.depth)
            except Exception as exc:
                log(f"  listener: {type(exc).__name__}: {exc}")
                continue

            if level < QUIET_DBFS or not text.strip():
                continue

            fresh = new_tail(self._last_text, text)
            self._last_text = text
            if not fresh:
                continue

            self.transcript.append(fresh)
            if self._callback is not None:
                try:
                    result = self._callback(fresh)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as exc:
                    log(f"  listener callback failed: {type(exc).__name__}: {exc}")
