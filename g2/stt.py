"""Local speech for the G2: utterance capture with endpointing, batch
transcription, and live captions. Everything runs on this laptop
(faster-whisper through the Frame project's warm worker process).

    speech = Speech(gateway)            # gateway: g2.gateway_client.G2Gateway
    await speech.start()
    pcm = await speech.capture_utterance()      # mic on, wait for speech, stop at silence
    text, level = await speech.transcribe(pcm)

The G2 mic floor in a quiet room is about -52 dBFS; speech lands around
-30 .. -15 dBFS. The endpointer tracks the floor and treats anything 12 dB
above it (and above -42 dBFS) as voice.
"""
from __future__ import annotations

import array
import asyncio
import math
import time
from typing import Callable

RATE = 16_000
DEPTH = 16
BLOCK_MS = 100
BLOCK_BYTES = RATE * 2 * BLOCK_MS // 1000      # 3200

VOICE_ABOVE_FLOOR_DB = 12.0
VOICE_MIN_DBFS = -42.0


def dbfs(pcm: bytes) -> float:
    if len(pcm) < 2:
        return -120.0
    a = array.array("h")
    a.frombytes(pcm[: len(pcm) // 2 * 2])
    rms = math.sqrt(sum(x * x for x in a) / len(a)) / 32768.0
    return 20 * math.log10(max(rms, 1e-6))


class Endpointer:
    """Decides, block by block, when an utterance has started and ended."""

    def __init__(self, start_timeout: float = 6.0, silence_s: float = 1.2,
                 max_s: float = 15.0, floor: float = -52.0) -> None:
        self.start_timeout = start_timeout
        self.silence_s = silence_s
        self.max_s = max_s
        self.floor = floor
        self.started: float | None = None
        self.last_voice: float | None = None
        self.elapsed = 0.0
        self.peak = -120.0

    def feed(self, level: float, dt: float = BLOCK_MS / 1000) -> str:
        """Returns 'wait', 'voice', 'silence', 'done' or 'timeout'."""
        self.elapsed += dt
        self.peak = max(self.peak, level)
        if level < self.floor:
            self.floor = level
        else:
            self.floor += (level - self.floor) * 0.02          # slow upward drift
        voiced = level > max(self.floor + VOICE_ABOVE_FLOOR_DB, VOICE_MIN_DBFS)
        if self.started is None:
            if voiced:
                self.started = self.elapsed
                self.last_voice = self.elapsed
                return "voice"
            return "timeout" if self.elapsed >= self.start_timeout else "wait"
        if voiced:
            self.last_voice = self.elapsed
        if self.elapsed - self.started >= self.max_s:
            return "done"
        if self.elapsed - (self.last_voice or self.started) >= self.silence_s:
            return "done"
        return "voice" if voiced else "silence"


def find_utterance(pcm: bytes, **kw) -> tuple[int, int] | None:
    """Offline helper (and test hook): byte range of the first utterance."""
    ep = Endpointer(**kw)
    start = None
    for i in range(0, len(pcm) - BLOCK_BYTES + 1, BLOCK_BYTES):
        state = ep.feed(dbfs(pcm[i:i + BLOCK_BYTES]))
        if state == "voice" and start is None:
            start = max(0, i - BLOCK_BYTES * 2)
        if state in ("done", "timeout"):
            return (start, i + BLOCK_BYTES) if start is not None else None
    return (start, len(pcm)) if start is not None else None


class Speech:
    def __init__(self, gateway, model: str = "base.en", stt=None) -> None:
        self.gw = gateway
        self.model = model
        self.stt = stt
        self._chunks: list[bytes] = []
        self._listening = False
        self._wake: asyncio.Event | None = None
        self._live = None                     # active Listener for captions
        gateway.on("audio", self._on_audio)

    @property
    def available(self) -> bool:
        return self.stt is not None and getattr(self.stt, "running", True)

    async def start(self) -> bool:
        if self.stt is None:
            from ai.stt_client import SttClient
            self.stt = SttClient(self.model)
        if hasattr(self.stt, "start"):
            return bool(await self.stt.start())
        return True

    async def stop(self) -> None:
        if self.stt is not None and hasattr(self.stt, "stop"):
            await self.stt.stop()

    def _on_audio(self, pcm: bytes) -> None:
        if self._listening:
            self._chunks.append(pcm)
            if self._wake is not None:
                self._wake.set()
        if self._live is not None:
            self._live.feed(pcm)

    async def capture_utterance(self, start_timeout: float = 6.0, silence_s: float = 1.2,
                                max_s: float = 15.0,
                                on_state: Callable[[str, float], None] | None = None) -> bytes | None:
        """Turn the mic on, wait for speech, return the PCM once it ends.

        None when nobody spoke within start_timeout, or the link dropped."""
        ep = Endpointer(start_timeout, silence_s, max_s)
        self._chunks, self._listening, self._wake = [], True, asyncio.Event()
        kept = bytearray()
        pending = bytearray()
        await self.gw.mic(True)
        t_last = time.time()
        try:
            while True:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    if time.time() - t_last > 3.0:          # mic stream died
                        return bytes(kept) if ep.started is not None else None
                    continue
                self._wake.clear()
                t_last = time.time()
                while self._chunks:
                    pending += self._chunks.pop(0)
                while len(pending) >= BLOCK_BYTES:
                    block = bytes(pending[:BLOCK_BYTES])
                    del pending[:BLOCK_BYTES]
                    state = ep.feed(dbfs(block))
                    kept += block
                    if on_state:
                        on_state(state, ep.elapsed)
                    if state == "timeout":
                        return None
                    if state == "done":
                        return bytes(kept)
                    if ep.started is None and len(kept) > BLOCK_BYTES * 5:
                        del kept[:-BLOCK_BYTES * 3]            # keep a little pre-roll
        finally:
            self._listening = False
            try:
                await self.gw.mic(False)
            except Exception:
                pass

    async def transcribe(self, pcm: bytes) -> tuple[str, float]:
        if self.stt is None:
            raise RuntimeError("speech worker not started")
        text, level = await self.stt.transcribe(pcm, RATE, DEPTH)
        return text.strip(), level

    # -------------------------------------------------------------- live
    def captioner(self, callback, window: float = 6.0, step: float = 2.0) -> "Live":
        """Frame-style sliding-window captions on this mic; fresh words go to
        callback(str). Returns an object with start()/stop()/transcript."""
        from ai.listener import Listener
        return Live(self, Listener(self.stt, rate=RATE, depth=DEPTH, window=window, step=step), callback)


class Live:
    def __init__(self, speech: Speech, listener, callback) -> None:
        self.speech, self.listener, self.callback = speech, listener, callback

    async def start(self) -> None:
        await self.listener.start(self.callback)
        self.speech._live = self.listener
        await self.speech.gw.mic(True)

    async def resume(self) -> None:
        """After the glasses reconnect the mic flag on the device is gone."""
        await self.speech.gw.mic(True)

    async def stop(self) -> None:
        self.speech._live = None
        try:
            await self.speech.gw.mic(False)
        except Exception:
            pass
        await self.listener.stop()

    @property
    def transcript(self) -> list[str]:
        return self.listener.transcript
