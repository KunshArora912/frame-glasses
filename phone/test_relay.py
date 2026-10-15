"""Exercise relay_server.py + frame_relay.py with a fake phone and a fake Frame.

    python relay_server.py            # must already be running
    .venv/Scripts/python.exe phone/test_relay.py

The fake phone speaks the same WebSocket protocol as phone/app.js and
answers like Frame's Lua REPL for a few known lines. Then the real SDK
(brilliant_ble) is run through frame_relay and checked end to end: connect,
send_lua with await_print, a data-channel packet, the MTU, and disconnect.
"""
import asyncio, base64, json, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from websockets.asyncio.client import connect
import frame_relay

RELAY_PHONE = "ws://127.0.0.1:8765/phone"
REPLIES = {
    "print(1)": b"1\n",
    "print(nil)": b"nil\n",
    "print(frame.battery_level())": b"87.0\n",
    "print(frame.bluetooth.max_length())": b"243\n",
    "frame.bluetooth.send(string.char(1,2,3))": bytes([1, 2, 3]),  # data channel echo
}


async def fake_phone(stop: asyncio.Event, seen: list):
    async with connect(RELAY_PHONE) as ws:
        await ws.send(json.dumps({"t": "hello", "name": "Frame 42", "mtu": 247}))
        await ws.send(json.dumps({"t": "ble", "state": "connected", "name": "Frame 42", "mtu": 247}))
        async def reader():
            async for raw in ws:
                m = json.loads(raw)
                if m.get("t") == "write":
                    data = base64.b64decode(m["d"])
                    seen.append(data)
                    await ws.send(json.dumps({"t": "wrote", "id": m["id"]}))
                    reply = REPLIES.get(data.decode("utf-8", "replace"))
                    if reply:
                        await ws.send(json.dumps({"t": "notify", "d": base64.b64encode(reply).decode()}))
        r = asyncio.create_task(reader())
        await stop.wait()
        r.cancel()


async def main() -> int:
    stop = asyncio.Event()
    seen: list[bytes] = []
    phone = asyncio.create_task(fake_phone(stop, seen))
    await asyncio.sleep(0.5)

    frame_relay.install()
    from brilliant_ble import BrilliantBle
    fails = 0
    def check(name, cond, info=""):
        nonlocal fails
        print(("ok   " if cond else "FAIL ") + name + ("" if cond else f"  -- {info}"))
        fails += 0 if cond else 1

    data_seen = []
    f = BrilliantBle()
    name = await f.connect(timeout=5, data_response_handler=lambda d: data_seen.append(bytes(d)))
    check("connect through relay", name == "Frame 42", name)
    check("mtu -> lua payload 244", f.max_lua_payload() == 244, f.max_lua_payload())
    r = await f.send_lua("print(frame.battery_level())", await_print=True, timeout=5)
    check("send_lua round trip", str(r).strip() == "87.0", r)
    await f.send_break_signal()
    check("break byte forwarded", b"\x03" in seen, seen[-3:])
    await f.send_lua("frame.bluetooth.send(string.char(1,2,3))", await_print=False)
    await asyncio.sleep(0.3)
    check("data channel delivered to data handler", data_seen and data_seen[-1] == bytes([2, 3]), data_seen)
    await f.disconnect()
    check("disconnected", not f.is_connected())

    # second session must work after the first detached
    f2 = BrilliantBle()
    name = await f2.connect(timeout=5)
    r = await f2.send_lua("print(1)", await_print=True, timeout=5)
    check("second session reattaches", str(r).strip() == "1", r)
    await f2.disconnect()

    stop.set()
    await phone
    print("\nFAILS:", fails)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
