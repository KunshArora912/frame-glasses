import sys, os; sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio, sys, time
from bleak import BleakClient, BleakScanner
sys.path.insert(0, '.')
from frame_config import FRAME_ADDR as ADDR; TX='7a230002-5475-a6a4-654c-8431f6ad49c4'; RX='7a230003-5475-a6a4-654c-8431f6ad49c4'
def log(m): print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)
async def main():
    d = None
    for _ in range(12):
        d = await BleakScanner.find_device_by_address(ADDR, timeout=5)
        if d: break
    log(f"adv: {d}")
    if not d: return 2
    c = BleakClient(d, timeout=20, winrt={'use_cached_services': False})
    try:
        await c.connect()
        await c.start_notify(RX, lambda h,v: None); log("existing bond OK")
    except Exception as e:
        log(f"existing bond unusable ({type(e).__name__}); clearing Windows side")
        try: await c.disconnect()
        except Exception: pass
        await BleakClient(ADDR).unpair(); await asyncio.sleep(2)
        for attempt in range(1,7):
            try:
                c = BleakClient(d, timeout=20, winrt={'use_cached_services': False}); await c.connect()
                try:
                    await c.start_notify(RX, lambda h,v: None); log(f"bond ok (attempt {attempt})"); break
                except Exception: pass
                await c.pair(protection_level=1); await c.start_notify(RX, lambda h,v: None); log(f"bonded via pair() (attempt {attempt})"); break
            except Exception as e:
                log(f"  attempt {attempt}: {type(e).__name__}: {str(e)[:60]}")
                try: await c.disconnect()
                except Exception: pass
                await asyncio.sleep(3)
        else:
            log("could not bond"); return 1
    out=[]
    await c.stop_notify(RX); await c.start_notify(RX, lambda h,v: out.append(bytes(v).decode('utf-8','replace')))
    async def lua(s, wait=1.0):
        out.clear(); await c.write_gatt_char(TX, s.encode(), response=True); await asyncio.sleep(wait); return ''.join(out)
    await c.write_gatt_char(TX, b'\x03', response=True); await asyncio.sleep(0.5)
    log("fw: " + await lua("print(frame.FIRMWARE_VERSION)"))
    log("battery: " + await lua("print(frame.battery_level())"))
    log("DIAG:\n" + await lua("print(frame.boot_diag())", 2.0))
    await c.disconnect()
asyncio.run(main())
