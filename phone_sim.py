"""Stand in for the phone, using the laptop's own Bluetooth radio.

    python phone_sim.py            # bridges the real glasses to relay_server.py

Does exactly what phone/app.js does -- holds the Bluetooth link to Frame and
forwards bytes to ws://127.0.0.1:8765/phone -- so the whole relay path
(relay_server.py, frame_relay.py, the apps) can be tested without a phone.
Useful again later for comparing "through the phone" against "direct".
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import time

from bleak import BleakClient, BleakScanner
from websockets.asyncio.client import connect

from frame_config import FRAME_ADDR

TX = '7a230002-5475-a6a4-654c-8431f6ad49c4'
RX = '7a230003-5475-a6a4-654c-8431f6ad49c4'
RELAY = 'ws://127.0.0.1:8765/phone'


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] phone_sim: {m}", flush=True)


async def main() -> int:
    d = None
    for _ in range(6):
        d = await BleakScanner.find_device_by_address(FRAME_ADDR, timeout=5)
        if d:
            break
    if not d:
        log("glasses not advertising (asleep?)")
        return 2
    ws = await connect(RELAY, max_size=4 * 1024 * 1024)
    log("linked to relay")
    dropped = asyncio.Event()
    loop = asyncio.get_running_loop()

    async def send(m: dict) -> None:
        await ws.send(json.dumps(m))

    client = BleakClient(d, timeout=20, winrt={'use_cached_services': False},
                         disconnected_callback=lambda c: loop.call_soon_threadsafe(dropped.set))
    await client.connect()
    await client.start_notify(RX, lambda h, v: asyncio.ensure_future(send({"t": "notify", "d": base64.b64encode(bytes(v)).decode()})))
    name = d.name or "Frame"
    log(f"bluetooth connected to {name}")
    await send({"t": "hello", "name": name, "mtu": client.mtu_size})
    await send({"t": "ble", "state": "connected", "name": name, "mtu": client.mtu_size})

    async def writes() -> None:
        async for raw in ws:
            m = json.loads(raw)
            if m.get("t") == "write":
                try:
                    await client.write_gatt_char(TX, base64.b64decode(m["d"]), response=m.get("r", True))
                    await send({"t": "wrote", "id": m.get("id")})
                except Exception as e:
                    await send({"t": "error", "id": m.get("id"), "msg": str(e)[:80]})

    writer = asyncio.create_task(writes())
    await dropped.wait()
    log("bluetooth link dropped")
    await send({"t": "ble", "state": "disconnected"})
    writer.cancel()
    await ws.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
