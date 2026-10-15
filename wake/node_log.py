import sys, os; sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio, time
from bleak import BleakClient, BleakScanner
from frame_config import FRAME_ADDR as ADDR; TX='7a230002-5475-a6a4-654c-8431f6ad49c4'; RX='7a230003-5475-a6a4-654c-8431f6ad49c4'
async def main():
    d = await BleakScanner.find_device_by_address(ADDR, timeout=10)
    async with BleakClient(d, timeout=20, winrt={'use_cached_services': False}) as c:
        out=[]
        await c.start_notify(RX, lambda h,v: out.append(bytes(v).decode('utf-8','replace')))
        async def lua(s, wait=0.25):
            out.clear(); await c.write_gatt_char(TX, s.encode(), response=True); await asyncio.sleep(wait); return ''.join(out).strip()
        await c.write_gatt_char(TX, b'\x03', response=True); await asyncio.sleep(0.3)
        print(time.strftime('%H:%M:%S'), 'stay_awake =', await lua("print(frame.stay_awake())"), flush=True)
        last=None; t0=time.time(); lastprint=0
        while time.time()-t0 < 1800:
            r = await lua("print(frame.case_detect(), frame.amux(1), frame.amux(3), frame.amux(9))", 0.2)
            cd = r.split('\t')[0] if r else '?'
            now=time.time()
            if cd != last or now-lastprint > 3:
                print(time.strftime('%H:%M:%S'), 'cd=%s  chgin_amux,batt_amux,sys_amux = %s' % (cd, r.replace('\t',', ')), flush=True)
                last=cd; lastprint=now
            await asyncio.sleep(0.05)
asyncio.run(main())
