"""Low-latency PCM playback for Frame audio.

Callback mode with a jitter buffer, deliberately: RawOutputStream.write()
BLOCKS until the buffer is consumed, which stalls the asyncio loop and starves
bleak's notification handler -- i.e. it costs you audio you were trying to
play. Here the PortAudio thread pulls from a bytearray and the coroutine only
appends.

WASAPI shared mode refuses 8/16 kHz outright (the endpoint runs at 48 kHz), so
we pass WasapiSettings(auto_convert=True): measured 26-30 ms latency versus
91-120 ms on MME/DirectSound.

Frame emits SIGNED PCM -- int8 for 8-bit, int16 little-endian for 16-bit --
which sounddevice's 'int8'/'int16' dtypes take verbatim. No conversion needed.
"""

from __future__ import annotations

import threading


class Player:
    def __init__(self, rate: int, depth: int, prime_ms: int = 250) -> None:
        import sounddevice as sd

        self._sd = sd
        self.width = depth // 8
        self.dtype = {8: "int8", 16: "int16"}[depth]
        self.rate = rate
        self._prime_bytes = rate * self.width * prime_ms // 1000
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._started = False
        self.underruns = 0

        device, extra = None, None
        try:
            idx = next(i for i, h in enumerate(sd.query_hostapis())
                       if "WASAPI" in h["name"])
            device = sd.query_hostapis()[idx]["default_output_device"]
            extra = sd.WasapiSettings(auto_convert=True)
        except (StopIteration, Exception):
            pass

        self.stream = sd.RawOutputStream(
            samplerate=rate, channels=1, dtype=self.dtype, device=device,
            extra_settings=extra, latency="low", callback=self._callback,
        )
        self.latency_ms = float(self.stream.latency) * 1000

    def _callback(self, outdata, frames, time_info, status) -> None:
        want = frames * self.width
        with self._lock:
            have = min(want, len(self._buf))
            outdata[:have] = self._buf[:have]
            del self._buf[:have]
        if have < want:
            # Underrun: emit silence. Never raise from the audio thread.
            outdata[have:] = b"\x00" * (want - have)
            self.underruns += 1

    def write(self, pcm: bytes) -> None:
        """Append raw signed PCM. Starts playback once primed."""
        with self._lock:
            self._buf.extend(pcm)
            buffered = len(self._buf)
        if not self._started and buffered >= self._prime_bytes:
            self.stream.start()
            self._started = True

    def close(self) -> None:
        try:
            if self._started:
                self.stream.stop()
            self.stream.close()
        except Exception:
            pass
