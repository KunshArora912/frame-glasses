"""Client for the out-of-process speech-to-text worker.

Keeps one warm subprocess so the model loads once. See stt_server.py for
why transcription cannot live in the Bluetooth process.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


class SttClient:
    def __init__(self, model: str = "base.en") -> None:
        self.model = model
        self._proc: asyncio.subprocess.Process | None = None
        self._tmp = Path(tempfile.gettempdir()) / "frame_stt.pcm"

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self, timeout: float = 120.0) -> bool:
        """Spawn the worker and wait for it to report the model is loaded."""
        root = Path(__file__).resolve().parent.parent
        self._proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", "-m", "ai.stt_server", self.model,
            cwd=str(root),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            line = await asyncio.wait_for(self._proc.stdout.readline(),
                                          timeout=timeout)
        except asyncio.TimeoutError:
            log.error("stt worker did not become ready")
            await self.stop()
            return False
        try:
            msg = json.loads(line.decode().strip() or "{}")
        except json.JSONDecodeError:
            msg = {}
        if not msg.get("ready"):
            log.error("stt worker failed: %s", msg.get("error"))
            await self.stop()
            return False
        return True

    async def transcribe(self, pcm: bytes, rate: int, depth: int,
                         timeout: float = 60.0) -> tuple[str, float]:
        if not self.running:
            raise RuntimeError("stt worker is not running")
        # The pipe is a FIFO with no request ids. If an earlier call was
        # cancelled while waiting, its reply is still coming: drain it first
        # or this call would read the previous utterance's text as its own.
        owed = getattr(self, "_owed", 0)
        while owed:
            await asyncio.wait_for(self._proc.stdout.readline(), timeout=timeout)
            owed -= 1
            self._owed = owed
        self._tmp.write_bytes(pcm)
        req = json.dumps({"pcm": str(self._tmp), "rate": rate,
                          "depth": depth}) + "\n"
        self._proc.stdin.write(req.encode())
        await self._proc.stdin.drain()
        self._owed = owed + 1

        line = await asyncio.wait_for(self._proc.stdout.readline(),
                                      timeout=timeout)
        self._owed -= 1
        if not line:
            raise RuntimeError("stt worker closed unexpectedly")
        msg = json.loads(line.decode().strip())
        if not msg.get("ok"):
            raise RuntimeError(msg.get("error", "unknown stt error"))
        return msg.get("text", ""), float(msg.get("dbfs", -120.0))

    async def stop(self) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.returncode is None:
                self._proc.stdin.write(b'{"quit":true}\n')
                await self._proc.stdin.drain()
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass
        finally:
            self._proc = None
