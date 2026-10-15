"""Run Lua lines on the Frame and print what comes back.
Usage: python wake/lua_exec.py "print(frame.battery_level())" ["..."]
Each argument is sent as one line; replies are printed under it.
"""
import sys, os; sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio, time
from bleak import BleakClient, BleakScanner
from frame_config import FRAME_ADDR as ADDR
TX = '7a230002-5475-a6a4-654c-8431f6ad49c4'; RX = '7a230003-5475-a6a4-654c-8431f6ad49c4'

def log(m): print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)

async def main(lines):
    d = None
    for _ in range(6):
        d = await BleakScanner.find_device_by_address(ADDR, timeout=5)
        if d: break
    if not d:
        log("not advertising (asleep or out of range)"); return 2
    c = BleakClient(d, timeout=20, winrt={'use_cached_services': False})
    await c.connect()
    out = []
    await c.start_notify(RX, lambda h, v: out.append(bytes(v).decode('utf-8', 'replace')))
    await c.write_gatt_char(TX, b'\x03', response=True); await asyncio.sleep(0.4)
    for s in lines:
        out.clear()
        await c.write_gatt_char(TX, s.encode(), response=True)
        await asyncio.sleep(1.2)
        log(f"> {s}\n  {''.join(out).strip()}")
    await c.disconnect()
    return 0

if __name__ == '__main__':
    sys.exit(asyncio.run(main(sys.argv[1:])))
