"""Async client for the local G2 gateway (men-g2-ble-gateway) on this laptop.

    gw = G2Gateway()
    gw.on("touch", lambda gesture, source: ...)
    await gw.start()
    await gw.text("hello")

Event callbacks (sync or async):
    touch(gesture, source)        single_tap | double_tap | swipe_up | swipe_down
    battery(level, charging)
    connection(ready, phase)      BLE link to the arms, as the gateway sees it
    reinit(reason)                gateway rebuilt its page state; redraw
    audio(pcm16)                  decoded mic audio, 16 kHz mono, when mic is on
    gateway(up)                   our WebSocket to the gateway came or went

The WebSocket reconnects by itself if the gateway restarts, and the gateway
reconnects to the arms by itself when they leave and return; the launcher just
redraws on connection(ready=True).
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
import time
from typing import Any, Callable

log = logging.getLogger("g2.gateway")


class GatewayError(RuntimeError):
    pass


class G2Gateway:
    def __init__(self, base: str = "http://127.0.0.1:8765", decoder: Any = None) -> None:
        self.base = base.rstrip("/")
        self.ws_url = self.base.replace("http", "ws", 1) + "/ws"
        self.decoder = decoder
        self._handlers: dict[str, list[Callable]] = {}
        self._session = None
        self._task: asyncio.Task | None = None
        self.state: dict = {}
        self.ready = False
        self.phase = "unknown"
        self.battery: int | None = None
        self.charging: bool | None = None
        self.firmware: str | None = None
        self.last_gesture: str | None = None
        self.gateway_up = False
        self.last_event_at = 0.0
        self._announced = False

    # ------------------------------------------------------------ events
    def on(self, kind: str, fn: Callable) -> None:
        self._handlers.setdefault(kind, []).append(fn)

    async def _emit(self, kind: str, *args) -> None:
        for fn in list(self._handlers.get(kind, [])):
            try:
                r = fn(*args)
                if inspect.isawaitable(r):
                    await r
            except Exception:
                log.exception("handler for %s failed", kind)

    # --------------------------------------------------------- lifecycle
    async def start(self) -> None:
        import aiohttp
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        self._task = asyncio.create_task(self._ws_loop(), name="g2-gateway-ws")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._session:
            await self._session.close()
            self._session = None

    async def _ws_loop(self) -> None:
        import aiohttp
        delay = 1.0
        while True:
            try:
                async with self._session.ws_connect(self.ws_url, heartbeat=20) as ws:
                    delay = 1.0
                    self.gateway_up = True
                    self._announced = False
                    await self._emit("gateway", True)
                    # Read and dispatch on separate tasks: the gateway drops a
                    # client whose socket backs up (128 events, ~6 s of mic
                    # audio), so a slow handler must never stall the reader.
                    queue: asyncio.Queue = asyncio.Queue()
                    consumer = asyncio.create_task(self._consume(queue), name="g2-gateway-dispatch")
                    try:
                        async for m in ws:
                            if m.type != aiohttp.WSMsgType.TEXT:
                                continue
                            try:
                                e = json.loads(m.data)
                            except ValueError:
                                continue
                            if e.get("kind") == "glasses.mic_audio" and queue.qsize() > 100:
                                continue                      # shed audio before text events
                            queue.put_nowait(e)
                    finally:
                        consumer.cancel()
                        try:
                            await consumer
                        except (asyncio.CancelledError, Exception):
                            pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("gateway ws down: %s", exc)
            if self.gateway_up:
                self.gateway_up = False
                self._set_ready(False, "gateway_down")
                await self._emit("connection", False, "gateway_down")
                await self._emit("gateway", False)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 10.0)

    async def _consume(self, queue: asyncio.Queue) -> None:
        while True:
            e = await queue.get()
            try:
                await self._dispatch(e)
            except Exception:
                log.exception("bad event")

    def _set_ready(self, ready: bool, phase: str) -> bool:
        changed = ready != self.ready
        self.ready, self.phase = ready, phase
        return changed

    async def _dispatch(self, e: dict) -> None:
        kind, d = e.get("kind"), e.get("data") or {}
        self.last_event_at = time.time()
        if kind == "status.snapshot":
            g = d.get("glasses") or {}
            self.state = g
            self.battery = g.get("battery_level", self.battery)
            self.charging = g.get("charging", self.charging)
            self.firmware = g.get("firmware_version") or self.firmware
            changed = self._set_ready(bool(g.get("ready")), g.get("phase", "unknown"))
            # The gateway sends a snapshot after every display write, so only
            # the first one per WebSocket session and real ready-state changes
            # reach the app; otherwise redraw -> snapshot -> redraw loops.
            if changed or not self._announced:
                self._announced = True
                await self._emit("connection", self.ready, self.phase)
        elif kind == "connection.state":
            if self._set_ready(bool(d.get("ready")), d.get("phase", "unknown")):
                await self._emit("connection", self.ready, self.phase)
        elif kind == "glasses.touch":
            self.last_gesture = d.get("gesture")
            await self._emit("touch", d.get("gesture"), d.get("source"))
        elif kind == "glasses.battery":
            self.battery = d.get("level", self.battery)
            if d.get("charging") is not None:
                self.charging = d.get("charging")
            await self._emit("battery", self.battery, self.charging)
        elif kind == "glasses.firmware":
            self.firmware = d.get("right") or d.get("left") or self.firmware
        elif kind == "glasses.mic_audio":
            if self.decoder is not None and self._handlers.get("audio"):
                raw = base64.b64decode(d.get("data_base64", ""))
                pcm = self.decoder.decode(raw, d.get("frame_size"))
                if pcm:
                    await self._emit("audio", pcm)
        elif kind == "system.reinitialize":
            await self._emit("reinit", d.get("reason"))
        elif kind == "glasses.dashboard":
            await self._emit("dashboard", d)

    # ----------------------------------------------------------- actions
    async def _post(self, path: str, body: dict) -> dict:
        if self._session is None:
            raise GatewayError("not started")
        try:
            async with self._session.post(self.base + path, json=body) as r:
                data = await r.json(content_type=None)
                if r.status >= 400:
                    raise GatewayError(f"{path} -> {r.status}: {str(data)[:200]}")
                return data
        except GatewayError:
            raise
        except Exception as exc:
            raise GatewayError(f"{path}: {type(exc).__name__}: {exc}") from exc

    async def text(self, s: str) -> None:
        if len(s.encode("utf-8")) > 1000:
            s = s.encode("utf-8")[:990].decode("utf-8", "ignore")
        await self._post("/api/display", {"text": s or " "})

    async def layout(self, elements: list[dict], **extra) -> None:
        await self._post("/api/display", {"elements": elements, **extra})

    # Even's own screens keep text off the canvas edge. One inset text
    # container (16 px sides, 12 px top) looks like theirs and still captures
    # taps; the gateway updates it in place when only the text changes.
    INSET = dict(x=16, y=12, width=544, height=264)

    async def text_inset(self, s: str, name: str = "main") -> None:
        if len(s.encode("utf-8")) > 1000:
            s = s.encode("utf-8")[:990].decode("utf-8", "ignore")
        await self.layout([{"type": "text", "text": s or " ", "padding": 0,
                            "capture_events": True, "container_name": name, **self.INSET}])

    async def clear(self) -> None:
        await self._post("/api/display", {"clear": True})

    async def page(self, *, shutdown: bool = False, create: bool = False, exit_mode: int = 1) -> dict:
        """POST /api/page.  ``shutdown=True`` drops our page so the firmware's own dashboard shows
        (exit_mode 1 = the firmware's system-exit path; the gateway parks and emits reinit page_parked);
        ``create=True`` brings the page back (reinit page_create).  Exactly one of the two."""
        if shutdown == create:
            raise ValueError("page(): pass exactly one of shutdown=True or create=True")
        body = {"create": True} if create else {"shutdown": True, "exit_mode": int(exit_mode)}
        return await self._post("/api/page", body)

    async def mic(self, on: bool) -> None:
        await self._post("/api/mic", {"enabled": bool(on)})

    async def status(self) -> dict:
        if self._session is None:
            raise GatewayError("not started")
        try:
            async with self._session.get(self.base + "/api/status") as r:
                data = await r.json(content_type=None)
        except Exception as exc:
            raise GatewayError(f"status: {type(exc).__name__}: {exc}") from exc
        g = data.get("glasses") or {}
        self.state = g
        self.battery = g.get("battery_level", self.battery)
        self.charging = g.get("charging", self.charging)
        self.firmware = g.get("firmware_version") or self.firmware
        self._set_ready(bool(g.get("ready")), g.get("phase", "unknown"))
        return data
