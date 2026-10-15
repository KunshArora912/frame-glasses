"""Run any program in this project through the phone instead of the laptop radio.

    python frame_relay.py assistant.py            # same args as usual after it
    python frame_relay.py install_main.py
    FRAME_RELAY=ws://127.0.0.1:8765/app           # default; override to point elsewhere

What it does: before the program starts, the Bluetooth client inside the
brilliant_ble SDK is swapped for one that talks to relay_server.py over a
WebSocket. The phone's web page (phone/) holds the real Bluetooth link to
Frame and forwards bytes both ways. Nothing in the programs changes; every
send_lua / send_data / notification goes the same route.

Programs still see one device named whatever the phone reported, with the
MTU the phone reported (Frame: 247 -> 244 byte Lua payload).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import runpy
import sys

RELAY_URL = os.environ.get("FRAME_RELAY", "ws://127.0.0.1:8765/app")


class _Char:
    def __init__(self, uuid: str) -> None:
        self.uuid = uuid


class _Service:
    def __init__(self, chars: dict[str, _Char]) -> None:
        self._chars = chars

    def get_characteristic(self, uuid):
        return self._chars.get(uuid.lower())


class _Services:
    def __init__(self, service_uuid: str, chars: dict[str, _Char]) -> None:
        self._uuid = service_uuid.lower()
        self._svc = _Service(chars)

    def get_service(self, uuid):
        return self._svc if uuid.lower() == self._uuid else None


class _Device:
    def __init__(self, name: str) -> None:
        self.name = name
        self.address = "relay"


class RelayClient:
    """Quacks like bleak.BleakClient for the handful of calls the SDK makes."""

    def __init__(self, device, disconnected_callback=None, timeout=10, **_) -> None:
        import brilliant_ble.brilliant_ble as sdk
        self._device = device
        self._disconnected_callback = disconnected_callback
        self._ws = None
        self._connected = False
        self._mtu = 247
        self._notify_cb = {}
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0
        self._reader = None
        self._backend = self          # the SDK inspects its class name (BlueZ MTU workaround)
        B = sdk.BrilliantBle
        self.services = _Services(B._SERVICE_UUID, {
            B._TX_CHARACTERISTIC_UUID.lower(): _Char(B._TX_CHARACTERISTIC_UUID),
            B._RX_CHARACTERISTIC_UUID.lower(): _Char(B._RX_CHARACTERISTIC_UUID),
        })

    # -- lifecycle --
    async def connect(self):
        from websockets.asyncio.client import connect
        self._ws = await connect(RELAY_URL, max_size=4 * 1024 * 1024)
        await self._ws.send(json.dumps({"t": "connect"}))
        reply = json.loads(await asyncio.wait_for(self._ws.recv(), 10))
        if reply.get("t") != "connected":
            await self._ws.close()
            raise asyncio.TimeoutError(reply.get("msg", "relay refused"))
        self._device.name = reply.get("name") or self._device.name
        self._mtu = int(reply.get("mtu") or 247)
        self._connected = True
        self._reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self):
        try:
            async for raw in self._ws:
                m = json.loads(raw)
                t = m.get("t")
                if t == "notify":
                    data = bytearray(base64.b64decode(m["d"]))
                    for cb in list(self._notify_cb.values()):
                        r = cb(None, data)
                        if asyncio.iscoroutine(r):
                            await r
                elif t == "wrote":
                    f = self._pending.pop(m.get("id"), None)
                    if f and not f.done():
                        f.set_result(True)
                elif t == "error":
                    f = self._pending.pop(m.get("id"), None)
                    if f and not f.done():
                        f.set_exception(Exception(m.get("msg", "relay error")))
                elif t == "ble" and m.get("state") == "disconnected":
                    break
        except Exception:
            pass
        finally:
            was = self._connected
            self._connected = False
            for f in self._pending.values():
                if not f.done():
                    f.set_exception(Exception("link dropped"))
            self._pending.clear()
            if was and self._disconnected_callback:
                try:
                    self._disconnected_callback(self)
                except Exception:
                    pass

    async def disconnect(self):
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps({"t": "disconnect"}))
            except Exception:
                pass
            try:
                await self._ws.close()
            except Exception:
                pass
        self._connected = False
        if self._reader:
            self._reader.cancel()
            self._reader = None
        return True

    @property
    def is_connected(self):
        return self._connected

    @property
    def mtu_size(self):
        return self._mtu

    # -- GATT --
    async def start_notify(self, char, callback):
        uuid = char if isinstance(char, str) else char.uuid
        self._notify_cb[uuid.lower()] = callback

    async def stop_notify(self, char):
        uuid = char if isinstance(char, str) else char.uuid
        self._notify_cb.pop(uuid.lower(), None)

    async def write_gatt_char(self, char, data, response=True):
        if not self._connected:
            raise Exception("not connected")
        self._next_id += 1
        wid = self._next_id
        f = asyncio.get_running_loop().create_future()
        self._pending[wid] = f
        await self._ws.send(json.dumps({"t": "write", "id": wid,
                                        "d": base64.b64encode(bytes(data)).decode(), "r": bool(response)}))
        await asyncio.wait_for(f, 15)


class _Scanner:
    @staticmethod
    async def find_device_by_filter(_filterfunc, timeout=10, service_uuids=None, **_):
        # The phone decides which Frame it is bonded to; just hand back a stub.
        return _Device("Frame (via phone)")


def install() -> None:
    import brilliant_ble.brilliant_ble as sdk
    sdk.BleakScanner = _Scanner
    sdk.BleakClient = RelayClient


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    install()
    script = sys.argv[1]
    sys.argv = sys.argv[1:]
    sys.path.insert(0, os.path.dirname(os.path.abspath(script)) or ".")
    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main()
