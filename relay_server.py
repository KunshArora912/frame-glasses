"""Relay between the phone (holding the Bluetooth link) and the laptop apps.

    python relay_server.py              # listens on 0.0.0.0:8765

One process, one port:

    GET  /            the phone web app (static files from phone/)
    WS   /phone       the phone's bridge: it owns the Bluetooth link to Frame
    WS   /app         one laptop program at a time (through frame_relay.py)

Messages are JSON text. Payloads are base64.

    phone -> server   {"t":"hello","name":"Frame F5","mtu":247}
                      {"t":"ble","state":"connected"|"disconnected","name":...}
                      {"t":"notify","d":"<b64>"}             RX notification
                      {"t":"wrote","id":n}                   write done
                      {"t":"error","id":n,"msg":"..."}
    server -> phone   {"t":"write","id":n,"d":"<b64>","r":true|false}

    app -> server     {"t":"connect"}  -> {"t":"connected","name":...,"mtu":n}
                                          or {"t":"error","msg":...}
                      {"t":"write","id":n,"d":"<b64>","r":bool}
                      {"t":"disconnect"}
    server -> app     {"t":"notify","d":"<b64>"}, {"t":"wrote","id":n},
                      {"t":"error",...}, {"t":"ble","state":"disconnected"}

The phone keeps its Bluetooth link across app sessions, so switching apps is
instant and costs no re-bonding. For HTTPS (Web Bluetooth needs a secure
origin) put Tailscale Serve or `adb reverse` in front; see phone/README.md.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import sys
from datetime import datetime
from pathlib import Path

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Response
from websockets.datastructures import Headers

STATIC = Path(__file__).parent / "phone"
HOST, PORT = "0.0.0.0", 8765


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


class Relay:
    def __init__(self) -> None:
        self.phone = None          # websocket of the phone bridge
        self.phone_name = None
        self.phone_mtu = 247
        self.ble_up = False
        self.app = None            # websocket of the attached laptop program

    # ---- phone side ------------------------------------------------------
    async def phone_session(self, ws) -> None:
        if self.phone is not None:
            log("second phone connected; replacing the old bridge")
            try:
                await self.phone.close()
            except Exception:
                pass
        self.phone = ws
        log("phone bridge connected")
        try:
            async for raw in self._messages(ws):
                try:
                    m = json.loads(raw)
                except Exception:
                    continue
                t = m.get("t")
                if t == "hello":
                    self.phone_name = m.get("name")
                    self.phone_mtu = int(m.get("mtu") or 247)
                    log(f"phone says Frame is {self.phone_name!r}, mtu {self.phone_mtu}")
                elif t == "ble":
                    self.ble_up = m.get("state") == "connected"
                    if self.ble_up:
                        self.phone_name = m.get("name") or self.phone_name
                    log(f"phone bluetooth {m.get('state')} ({self.phone_name})")
                    if not self.ble_up and self.app is not None:
                        await self._send(self.app, {"t": "ble", "state": "disconnected"})
                elif t in ("notify", "wrote", "error"):
                    if self.app is not None:
                        await self._send(self.app, m)
        finally:
            if self.phone is ws:
                self.phone = None
                self.ble_up = False
                log("phone bridge gone")
                if self.app is not None:
                    await self._send(self.app, {"t": "ble", "state": "disconnected"})

    # ---- app side --------------------------------------------------------
    async def app_session(self, ws) -> None:
        if self.app is not None:
            await self._send(ws, {"t": "error", "msg": "another program is attached"})
            await ws.close()
            return
        self.app = ws
        peer = "laptop program"
        try:
            async for raw in self._messages(ws):
                try:
                    m = json.loads(raw)
                except Exception:
                    continue
                t = m.get("t")
                if t == "connect":
                    if self.phone is None or not self.ble_up:
                        await self._send(ws, {"t": "error", "msg": "phone not bridged to Frame"})
                    else:
                        log(f"{peer} attached to {self.phone_name}")
                        await self._send(ws, {"t": "connected", "name": self.phone_name, "mtu": self.phone_mtu})
                elif t == "write":
                    if self.phone is None:
                        await self._send(ws, {"t": "error", "id": m.get("id"), "msg": "no phone"})
                    else:
                        await self._send(self.phone, m)
                elif t == "disconnect":
                    log(f"{peer} detached")
                    break
        finally:
            if self.app is ws:
                self.app = None

    @staticmethod
    async def _messages(ws):
        """Iterate incoming messages; a peer that drops without a close frame just ends the loop."""
        try:
            async for raw in ws:
                yield raw
        except ConnectionClosed:
            return

    @staticmethod
    async def _send(ws, m: dict) -> None:
        try:
            await ws.send(json.dumps(m))
        except Exception:
            pass

    # ---- dispatch --------------------------------------------------------
    async def handler(self, ws) -> None:
        path = ws.request.path.split("?")[0]
        if path == "/phone":
            await self.phone_session(ws)
        elif path == "/app":
            await self.app_session(ws)
        else:
            await ws.close()


def static_response(connection, request):
    """Serve phone/ for plain GETs; let WebSocket upgrades through."""
    if "upgrade" in request.headers.get("Connection", "").lower():
        return None
    path = request.path.split("?")[0]
    if path in ("/", ""):
        path = "/index.html"
    f = (STATIC / path.lstrip("/")).resolve()
    if STATIC.resolve() not in f.parents or not f.is_file():
        return Response(404, "Not Found", Headers([("Content-Type", "text/plain")]), b"not found")
    ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
    if f.suffix == ".webmanifest":
        ctype = "application/manifest+json"
    body = f.read_bytes()
    return Response(200, "OK", Headers([("Content-Type", ctype), ("Cache-Control", "no-cache")]), body)


async def main() -> None:
    relay = Relay()
    async with serve(relay.handler, HOST, PORT, process_request=static_response, max_size=4 * 1024 * 1024):
        log(f"relay on http://0.0.0.0:{PORT}  (phone page at /, phone ws at /phone, apps at /app)")
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
