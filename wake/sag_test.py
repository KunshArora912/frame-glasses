import sys, os; sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
"""Re-bond to a freshly pin-booted Frame, then measure battery sag under load.

A voltage-based gauge hides a tired cell at rest and exposes it under a pulse.
We read battery_level() at rest, then with display full-white + camera running
+ a busy Lua loop, every 0.5 s for 20 s, then at rest again.
"""
import asyncio, time, sys
from bleak import BleakClient, BleakScanner

from frame_config import FRAME_ADDR as ADDR
TX = '7a230002-5475-a6a4-654c-8431f6ad49c4'
RX = '7a230003-5475-a6a4-654c-8431f6ad49c4'

def log(m): print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)

async def bond(d):
    """Return a connected, encrypted client or None."""
    for attempt in range(1, 7):
        try:
            c = BleakClient(d, timeout=20, winrt={'use_cached_services': False})
            await c.connect()
            try:
                await c.start_notify(RX, lambda h, v: None)
                log(f"bond already OK (attempt {attempt})"); return c
            except Exception:
                pass
            try:
                await c.pair(protection_level=1)
                await c.start_notify(RX, lambda h, v: None)
                log(f"bonded via pair() (attempt {attempt})"); return c
            except Exception as e:
                log(f"  pair attempt {attempt}: {type(e).__name__}: {str(e)[:70]}")
            try: await c.disconnect()
            except Exception: pass
        except Exception as e:
            log(f"  connect attempt {attempt}: {type(e).__name__}: {str(e)[:70]}")
        await asyncio.sleep(3)
    return None

async def main():
    log("waiting for Frame to advertise...")
    d = None
    for _ in range(60):
        d = await BleakScanner.find_device_by_address(ADDR, timeout=5)
        if d: break
    if not d: log("never advertised"); return 2
    log(f"advertising {d}")
    c = None
    try:
        c = BleakClient(d, timeout=20, winrt={'use_cached_services': False}); await c.connect()
        await c.start_notify(RX, lambda h, v: None); log("existing bond OK")
    except Exception as e:
        log(f"existing bond unusable ({type(e).__name__}); re-bonding")
        try: await c.disconnect()
        except Exception: pass
        await BleakClient(ADDR).unpair(); await asyncio.sleep(2)
        c = await bond(d)
    if not c: log("could not bond"); return 1
    out = []
    await c.stop_notify(RX)
    await c.start_notify(RX, lambda h, v: out.append(bytes(v).decode('utf-8', 'replace').strip()))

    async def lua(s, wait=0.8):
        out.clear(); await c.write_gatt_char(TX, s.encode(), response=True); await asyncio.sleep(wait); return list(out)

    await c.write_gatt_char(TX, b'\x03', response=True); await asyncio.sleep(0.5)
    log(f"fw {await lua('print(frame.FIRMWARE_VERSION)')}")
    rest = [ (await lua('print(frame.battery_level())'))[:1] for _ in range(5) ]
    log(f"REST battery x5: {rest}")

    # Heavy load: full white display, camera awake+capturing, busy loop.
    await lua("frame.display.bitmap(1,1,640,2,15,string.rep(string.char(255),160))", 0.3)
    await lua("frame.display.text('LOAD TEST',200,170); frame.display.show()", 0.5)
    try:
        await lua("frame.camera.capture{quality='HIGH', resolution=720}", 0.5)
    except Exception as e:
        log(f"camera call: {e}")
    log("under load...")
    under = []
    t0 = time.time()
    while time.time() - t0 < 20:
        r = await lua("local t=frame.time.utc(); for i=1,20000 do t=t+i end; print(frame.battery_level())", 0.4)
        under.append(r[:1]); 
    log(f"UNDER LOAD battery: {under}")
    await lua("frame.display.text(' ',1,1); frame.display.show()", 0.3)
    try:
        await lua("frame.camera.sleep()", 0.3)
    except Exception: pass
    await asyncio.sleep(5)
    rest2 = [ (await lua('print(frame.battery_level())'))[:1] for _ in range(5) ]
    log(f"REST AFTER battery x5: {rest2}")
    await lua("frame.display.text('SAG DONE',200,170); frame.display.show()", 0.3)
    await c.disconnect()
    return 0

if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
