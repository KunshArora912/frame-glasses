"""Watch for the Frame waking up. Logs the moment it starts advertising, then
connects, reads seconds-since-boot, charger voltage, battery and the retained
boot log, disconnects, and keeps watching for the next wake.
Usage: python wake/wake_watch.py
"""
import sys, os; sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio, time
from bleak import BleakClient, BleakScanner
from frame_config import FRAME_ADDR as ADDR
TX = '7a230002-5475-a6a4-654c-8431f6ad49c4'; RX = '7a230003-5475-a6a4-654c-8431f6ad49c4'

def log(m): print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)

async def read_state(d):
    c = BleakClient(d, timeout=20, winrt={'use_cached_services': False})
    await c.connect()
    out = []
    await c.start_notify(RX, lambda h, v: out.append(bytes(v).decode('utf-8', 'replace')))
    await c.write_gatt_char(TX, b'\x03', response=True); await asyncio.sleep(0.4)
    async def lua(s, wait=1.2):
        out.clear(); await c.write_gatt_char(TX, s.encode(), response=True); await asyncio.sleep(wait); return ''.join(out).strip()
    log("  " + await lua("print('uptime_s', frame.time.utc(), 'chgin_v', frame.amux(1), 'batt', frame.battery_level(), 'cd', frame.case_detect())"))
    log("  boot log:\n" + await lua("print(frame.boot_diag())", 2.0))
    await c.disconnect()

async def main():
    log("watching for the glasses to advertise (Ctrl-C to stop)")
    seen = False
    while True:
        d = await BleakScanner.find_device_by_address(ADDR, timeout=3)
        if d and not seen:
            log("ADVERTISING -> awake")
            seen = True
            try:
                await read_state(d)
            except Exception as e:
                log(f"  read failed: {type(e).__name__}: {str(e)[:80]}")
        elif not d and seen:
            log("gone quiet -> asleep (or connected elsewhere)")
            seen = False
        await asyncio.sleep(1)

if __name__ == '__main__':
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
