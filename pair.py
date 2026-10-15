"""Pair a freshly-reset Frame to this laptop, and prove it.

    python pair.py            # waits for the glasses, then bonds
    python pair.py --wait 30  # minutes to wait for them to appear

After the cradle pin (which runs `bluetooth_unpair()` on the device) Frame
holds no bond, and neither should Windows -- run the Windows-side unpair
first, or Windows keeps offering a key the glasses no longer have and every
write fails `Insufficient Authentication` forever.

Then the ceremony is Frame's to drive, not ours: it repeatedly requests
authentication and Windows completes Just Works on its own after a few
rejected attempts. Measured on this unit: success on the 4th try, twice.
`bleak`'s `client.pair()` always fails here for the same reason, so this
script simply connects, subscribes, and retries until the subscribe is
allowed -- that moment IS the bond landing.

On success it reads the battery and firmware, draws PAIRED on the panel,
and leaves the display on, because the next step is usually a diagnostic
that needs the glasses awake. It will tell you to dock when it exits.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from datetime import datetime

from bleak import BleakClient, BleakScanner

FRAME_SERVICE = "7a230001-5475-a6a4-654c-8431f6ad49c4"
TX_CHAR = "7a230002-5475-a6a4-654c-8431f6ad49c4"
RX_CHAR = "7a230003-5475-a6a4-654c-8431f6ad49c4"

ATTEMPTS = 8
RETRY_DELAY = 3.0


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def is_frame(device, adv) -> bool:
    name = (adv.local_name or device.name or "").lower()
    if "update" in name:
        return False
    uuids = [u.lower() for u in (adv.service_uuids or [])]
    return FRAME_SERVICE in uuids or "frame" in name


async def wait_for_frame(minutes: float):
    log(f"waiting up to {minutes:.0f} min for Frame to advertise...")
    log("  (hold the cradle pin until the screen lights, UNDOCK while "
        "still holding, then release)")
    deadline = time.monotonic() + minutes * 60
    quiet = 0
    while time.monotonic() < deadline:
        device = await BleakScanner.find_device_by_filter(is_frame, timeout=6.0)
        if device is not None:
            log(f"advertising: {device.name!r} @ {device.address}")
            return device
        quiet += 1
        if quiet % 10 == 0:
            log(f"  nothing yet ({(time.monotonic() - deadline + minutes * 60) / 60:.0f} min)")
        await asyncio.sleep(2.0)
    return None


async def try_bond(device) -> tuple[bool, list[bytes]]:
    """One connect + subscribe. Subscribe succeeding means the link is
    encrypted, which means the bond exists."""
    replies: list[bytes] = []

    def on_notify(_h, data: bytearray) -> None:
        replies.append(bytes(data))

    async with BleakClient(device, timeout=20.0,
                           winrt={"use_cached_services": False}) as client:
        try:
            await client.start_notify(RX_CHAR, on_notify)
        except Exception as exc:
            return False, [str(exc).encode()]

        await client.write_gatt_char(TX_CHAR, b"\x03", response=True)
        await asyncio.sleep(0.8)
        for lua in ("print(frame.FIRMWARE_VERSION)",
                    "print(frame.battery_level())"):
            await client.write_gatt_char(TX_CHAR, lua.encode(), response=True)
            await asyncio.sleep(1.2)
        await client.write_gatt_char(
            TX_CHAR,
            b"frame.display.text('PAIRED',200,170);frame.display.show();"
            b"print('drawn')",
            response=True)
        await asyncio.sleep(1.5)
        return True, replies


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--wait", type=float, default=30.0, help="minutes")
    args = ap.parse_args()

    device = await wait_for_frame(args.wait)
    if device is None:
        log("Frame never advertised -- it did not boot, or died on undock")
        return 2

    await asyncio.sleep(1.5)
    for attempt in range(1, ATTEMPTS + 1):
        log(f"bond attempt {attempt}/{ATTEMPTS}")
        try:
            ok, replies = await try_bond(device)
        except Exception as exc:
            log(f"  {type(exc).__name__}: {exc}")
            ok, replies = False, []
        if ok:
            text = [r.decode("utf-8", "replace").strip() for r in replies]
            log("BONDED -- encrypted link, writes permitted")
            for line in text:
                if line:
                    log(f"  <- {line}")
            battery = next((t for t in text
                            if t.replace(".", "", 1).isdigit()), None)
            if battery:
                log(f"BATTERY {float(battery):.0f}% right after boot")
            log("display left on for the next test. DOCK IT when done.")
            return 0
        log(f"  not yet ({replies[0][:60].decode('utf-8', 'replace') if replies else 'rejected'})")
        await asyncio.sleep(RETRY_DELAY)

    log("bond never landed after 8 attempts -- is the Windows side unpaired?")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
