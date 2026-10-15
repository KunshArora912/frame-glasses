"""Frame connection diagnostic.

Answers one question: can this laptop talk to the Frame glasses right now,
and if not, why? Uses bleak directly (no SDK) so it works regardless of
which brilliant-* packages are installed.

    python diagnose.py

Exit codes: 0 = controllable, 1 = bond slot taken, 2 = not advertising.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime

from bleak import BleakClient, BleakScanner

FRAME_SERVICE = "7a230001-5475-a6a4-654c-8431f6ad49c4"
TX_CHAR = "7a230002-5475-a6a4-654c-8431f6ad49c4"
RX_CHAR = "7a230003-5475-a6a4-654c-8431f6ad49c4"

RESET_STEPS = """
Frame holds exactly ONE Bluetooth bond. If it is paired to your phone, this
laptop cannot pair until you clear it. To hand the bond to this laptop:

  1. Turn Bluetooth OFF on the phone (or "forget" Frame there).
  2. Dock Frame on the cradle, cradle plugged into power (white LED on).
  3. SIM tool on the cradle button -- press and HOLD.
  4. Still holding, lift Frame out of the cradle. THEN release.
  5. Pair via Windows Settings > Bluetooth & devices > Add device > "Frame F5".
     Do NOT try to pair from Python -- Frame drives the pairing ceremony
     itself, so bleak's client.pair() always fails.

Step 4 order matters: releasing while still docked sends the case-detect pin
low->high, which is the interrupt that powers Frame off.
"""


def looks_like_frame(device, adv) -> bool:
    uuids = [u.lower() for u in (adv.service_uuids or [])]
    name = (adv.local_name or device.name or "").lower()
    return FRAME_SERVICE in uuids or "frame" in name


def stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


async def find(timeout: float = 15.0):
    print(f"[{stamp()}] scanning {timeout:.0f}s for Frame...")
    return await BleakScanner.find_device_by_filter(looks_like_frame, timeout=timeout)


async def interrogate(device) -> int:
    replies: list[bytes] = []

    def on_notify(_handle, data: bytearray) -> None:
        replies.append(bytes(data))
        print(f"[{stamp()}]   <- {bytes(data)!r}")

    async with BleakClient(device, timeout=20.0) as client:
        print(f"[{stamp()}] connected, MTU={getattr(client, 'mtu_size', '?')}")

        try:
            await client.start_notify(RX_CHAR, on_notify)
        except Exception as exc:
            print(f"[{stamp()}] FAILED to subscribe: {type(exc).__name__}: {exc}")
            print(
                "\nDIAGNOSIS: Frame's characteristics need an encrypted link, and\n"
                "this host has no bond. The single bond slot belongs to another\n"
                "device (most likely your phone)."
            )
            print(RESET_STEPS)
            return 1

        print(f"[{stamp()}] encrypted link established -- writes permitted\n")

        # If a companion app (e.g. Noa) left a long-running main.lua on the
        # device, run_lua() never returns and the REPL is never reached, so
        # plain Lua strings sit in the buffer unexecuted. 0x03 is the break
        # signal: it aborts main.lua and drops into the REPL.
        print(f"[{stamp()}] -> break signal (0x03)")
        await client.write_gatt_char(TX_CHAR, b"\x03", response=True)
        await asyncio.sleep(1.0)

        for lua in (
            "print(frame.FIRMWARE_VERSION)",
            "print(frame.HARDWARE_VERSION)",
            "print(frame.battery_level())",
            "print(frame.bluetooth.max_length())",
        ):
            print(f"[{stamp()}] -> {lua}")
            await client.write_gatt_char(TX_CHAR, lua.encode(), response=True)
            await asyncio.sleep(1.5)

        # Frame is double-buffered: nothing appears until show(). Also keep
        # y <= 352 -- the per-glyph fit check is (y + 48 <= 400) and it fails
        # SILENTLY, so text just vanishes with no error.
        print(f"[{stamp()}] -> drawing to display")
        await client.write_gatt_char(
            TX_CHAR,
            b"frame.display.text('FRAME OK',170,180);frame.display.show();print('drawn')",
            response=True,
        )
        await asyncio.sleep(2.0)

        print(f"\nDIAGNOSIS: Frame is fully controllable from this laptop.")
        print(f"Replies received: {replies}")
        return 0


async def main() -> int:
    device = await find()
    if device is None:
        print(
            "\nDIAGNOSIS: Frame is not advertising.\n"
            "  - It sleeps aggressively; a tap on the frame wakes it.\n"
            "  - It is powered OFF whenever docked in the cradle.\n"
            "  - A phone holding the connection will also hide it, since Frame\n"
            "    accepts only one connection at a time."
        )
        return 2

    print(f"[{stamp()}] found {device.name!r} @ {device.address}")
    try:
        return await interrogate(device)
    except Exception as exc:
        print(f"[{stamp()}] error: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
