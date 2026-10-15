"""The Mudra band as an input source for the shell.

Frame's own input is one noisy tap sensor. A neural wristband gives four
discrete gestures, a six-way D-pad and analog pressure, which is the
difference between cycling through a menu one tap at a time and actually
pointing at the thing you want.

WHY THE WEBSOCKET AND NOT BLE DIRECTLY. There are three documented routes
to this band on Windows: OS-level BLE HID, the official `mudra-sdk` over
bleak, and the Mudra Companion app's localhost WebSocket. We take the
WebSocket, for one reason that is specific to this project: Frame already
owns a bleak connection, and the band's raw streams run at 1125 Hz. Having
one Python process hold two concurrent BLE connections, one of them
high-rate, is a class of bug this project has already paid for once (the
ctranslate2 segfault came from exactly this kind of in-process contention).
Letting Companion.exe own the band's radio and handing us JSON over
localhost removes the whole category.

It also costs the glasses nothing: the band pairs to Windows, not to Frame,
so Frame's single bond slot is untouched.

PROTOCOL. Vendor-specified, from the official `mudra-skills` repo:
  * ws://127.0.0.1:8766
  * subscribe one signal per command: {"command":"subscribe","signal":"gesture"}
    -- plural or array forms are rejected as invalid_signal
  * no unsolicited frames on connect; send {"command":"get_status"} first
  * the band counts as connected only when status reports BOTH a firmware
    and a serial_number
  * subscriptions are per-socket, so re-subscribe after any reconnect
  * never open two sockets
  * signals are mutually exclusive in groups: gesture XOR pressure,
    nav_direction XOR navigation

We subscribe to `gesture` and `nav_direction` only. The raw EMG and IMU
bundles would be pointless traffic for a menu and are licensed separately.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import datetime

from shell.gestures import Gesture

WS_URL = "ws://127.0.0.1:8766"
STATUS_EVERY = 2.0
RECONNECT_DELAY = 3.0

# The band's documented discrete gestures, mapped onto shell intents.
# `tap` is the cheap frequent action, matching the tap-sensor language, so
# muscle memory carries across whichever input is in use.
_BY_GESTURE = {
    "tap": Gesture.SELECT,
    "double_tap": Gesture.BACK,
    "twist": Gesture.NEXT,
    "double_twist": Gesture.QUIT,
}

# The six-way D-pad. Down/Right advance, Up/Left go back a step; rolls are
# left unmapped rather than guessed at.
_BY_DIRECTION = {
    "down": Gesture.NEXT,
    "right": Gesture.NEXT,
    "up": Gesture.PREV,
    "left": Gesture.PREV,
    # "roll left"/"roll right" are left unmapped rather than guessed at.
}


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def intent_for(frame: dict) -> Gesture | None:
    """Map one Companion frame onto a shell intent, or None to ignore it."""
    kind = frame.get("type")
    data = frame.get("data") or {}
    if kind == "gesture":
        return _BY_GESTURE.get(str(data.get("type", "")).lower())
    if kind == "nav_direction":
        direction = str(data.get("direction", "")).strip().lower()
        return _BY_DIRECTION.get(direction)
    return None


def band_present(status: dict) -> bool:
    """Companion reports a band only when firmware AND serial are both set."""
    device = (status.get("data") or status).get("device") or {}
    return bool(device.get("firmware")) and bool(device.get("serial_number"))


class MudraInput:
    """Emits the same Gesture values as TapInput, from the band instead.

    Deliberately interchangeable with TapInput: the launcher asks for the
    next gesture and does not care which wrist or which radio produced it.
    """

    name = "mudra"

    def __init__(self, url: str = WS_URL) -> None:
        self.url = url
        self.queue: asyncio.Queue = asyncio.Queue()
        self.connected = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._pump(), name="mudra-ws")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self.connected = False

    async def drain(self) -> int:
        n = 0
        while True:
            try:
                self.queue.get_nowait()
                n += 1
            except asyncio.QueueEmpty:
                return n

    async def next_gesture(self, timeout: float | None = None) -> Gesture | None:
        if timeout is not None and timeout <= 0:
            try:
                return self.queue.get_nowait()
            except asyncio.QueueEmpty:
                return None
        try:
            return await asyncio.wait_for(self.queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    # ------------------------------------------------------------ the pump

    async def _pump(self) -> None:
        import websockets

        while True:
            try:
                async with websockets.connect(self.url) as socket:
                    # Nothing arrives unsolicited, so ask first.
                    await socket.send(json.dumps({"command": "get_status"}))
                    # One command per signal: arrays are rejected.
                    for signal in ("gesture", "nav_direction"):
                        await socket.send(json.dumps({"command": "subscribe",
                                                      "signal": signal}))
                    log("mudra: connected to Companion")
                    await self._listen(socket)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.connected:
                    log(f"mudra: lost Companion ({type(exc).__name__})")
                self.connected = False
            await asyncio.sleep(RECONNECT_DELAY)

    async def _listen(self, socket) -> None:
        import websockets

        last_status = 0.0
        loop = asyncio.get_running_loop()
        while True:
            now = loop.time()
            if now - last_status >= STATUS_EVERY:
                last_status = now
                await socket.send(json.dumps({"command": "get_status"}))

            try:
                raw = await asyncio.wait_for(socket.recv(), timeout=STATUS_EVERY)
            except asyncio.TimeoutError:
                continue
            except websockets.ConnectionClosed:
                return

            try:
                frame = json.loads(raw)
            except (TypeError, ValueError):
                continue

            if frame.get("type") == "status" or "device" in frame:
                present = band_present(frame)
                if present != self.connected:
                    log(f"mudra: band {'connected' if present else 'away'}")
                self.connected = present
                continue

            intent = intent_for(frame)
            if intent is not None:
                await self.queue.put(intent)


async def probe(url: str = WS_URL, seconds: float = 6.0) -> dict:
    """Is Companion running, and is a band on the wrist? Diagnostic only."""
    import websockets

    out = {"companion": False, "band": False, "error": "", "signals": []}
    try:
        async with websockets.connect(url, open_timeout=3.0) as socket:
            out["companion"] = True
            await socket.send(json.dumps({"command": "get_status"}))
            for signal in ("gesture", "nav_direction"):
                await socket.send(json.dumps({"command": "subscribe",
                                              "signal": signal}))
            deadline = asyncio.get_running_loop().time() + seconds
            while asyncio.get_running_loop().time() < deadline:
                try:
                    raw = await asyncio.wait_for(socket.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                try:
                    frame = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                out["signals"].append(frame.get("type"))
                if band_present(frame):
                    out["band"] = True
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


if __name__ == "__main__":
    # python -m shell.mudra : is Mudra Link streaming, and is a band on the wrist?
    import sys

    async def _cli() -> int:
        log("probing Mudra Link at " + WS_URL + " for 6 s ...")
        r = await probe()
        if not r["companion"]:
            log("Mudra Link is not streaming: open Mudra Link > Advanced settings > "
                "Mudra Studio and turn it on (" + r["error"] + ")")
            return 2
        log("Mudra Link stream: yes   band on wrist: " + ("yes" if r["band"] else "no")
            + "   frames seen: " + ", ".join(sorted(set(str(x) for x in r["signals"]))))
        if "--simulate" in sys.argv:
            import websockets
            async with websockets.connect(WS_URL) as socket:
                await socket.send(json.dumps({"command": "trigger_gesture", "data": {"type": "tap"}}))
                log("sent a simulated tap")
        return 0

    sys.exit(asyncio.run(_cli()))
