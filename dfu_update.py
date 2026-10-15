"""Nordic Secure DFU over BLE for Brilliant Labs Frame.

Flashes a Frame firmware .zip (application.dat init packet + application.bin
image) to the glasses. Ported from Brilliant's own AR Studio implementation
(ar-studio-for-vscode/src/nordicdfu.ts), which is the known-good reference for
this device.

    python dfu_update.py --dry-run          # enter DFU, read state, DO NOT write
    python dfu_update.py                    # full flash

SAFETY
  * The glasses must be UNDOCKED and charged. The documented brick report
    (noa-flutter #163, "stuck at 99%") is caused by the device being docked:
    it powers off when the update completes and the flow hangs.
  * Nothing else may hold the BLE link -- Frame accepts one connection.
  * You CANNOT brick the glasses with a protocol error here. An
    application-only Secure DFU never writes the MBR, SoftDevice, bootloader
    or settings page, and the image is signature- and hash-validated before
    activation. The worst realistic outcome is a device sitting in the
    bootloader advertising 0xFE59 as "Frame Update", re-flashable over BLE
    as often as you like.
  * BUT there is a point of commitment. This image is 560 KB, which does not
    fit alongside the running app in the nRF52840 DFU region, so the
    bootloader invalidates bank 0 and overwrites in place -- and it decides
    that at the EXECUTE of the INIT PACKET, before any firmware byte is
    sent. After that point there is no old firmware to fall back to: the
    flash must be carried to completion. Do not interrupt it.
  * The one genuinely dangerous event is losing power during activation,
    which is a power problem, not a protocol one -- hence "undocked and
    charged" above.

Deviation from the reference, deliberate: on a CRC mismatch AR Studio retries
the chunk with `chk--` but never rewinds `fileOffset`, so the retry resends
from the wrong position. This implementation rewinds to the chunk start.
"""

from __future__ import annotations

import argparse
import asyncio
import time
import zipfile
import zlib
from pathlib import Path

from bleak import BleakClient, BleakScanner

# Nordic Secure DFU
DFU_SERVICE = "0000fe59-0000-1000-8000-00805f9b34fb"
DFU_CONTROL = "8ec90001-f315-4f60-9fb8-838830daea50"
DFU_PACKET = "8ec90002-f315-4f60-9fb8-838830daea50"

# Frame application service (to trigger the reboot into the bootloader)
FRAME_SERVICE = "7a230001-5475-a6a4-654c-8431f6ad49c4"
FRAME_TX = "7a230002-5475-a6a4-654c-8431f6ad49c4"

# control point opcodes
OP_CREATE = 0x01
OP_SET_PRN = 0x02
OP_CALC_CRC = 0x03
OP_EXECUTE = 0x04
OP_SELECT = 0x06
RESPONSE = 0x60
RESULT_OK = 0x01

OBJ_COMMAND = 0x01   # the init packet (.dat)
OBJ_DATA = 0x02      # the firmware image (.bin)

PACKET_SIZE = 100    # matches the reference; proven on this device
PACKET_DELAY = 0.010   # the ONLY flow control: no PRN, no MTU backpressure
MAX_CHUNK_RETRIES = 3
CREATE_ERASE_SETTLE = 0.10   # flash page erase queued by CREATE is async

assert PACKET_SIZE % 4 == 0, "flash writes must be word-aligned in length"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class DfuError(RuntimeError):
    """Transport-level failure: no answer, malformed frame, bad state."""


class DfuRemoteError(DfuError):
    """The device answered with a non-OK result code.

    Distinct from DfuError because a rejection is NEVER tolerable: the final
    EXECUTE is where the bootloader runs the ECDSA signature check and the
    SHA-256 image hash, and a rejection there means the firmware was refused.
    """


class DfuTarget:
    def __init__(self, client: BleakClient) -> None:
        self.client = client
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()

    def _on_notify(self, _handle, data: bytearray) -> None:
        self._queue.put_nowait(bytes(data))

    async def start(self) -> None:
        await self.client.start_notify(DFU_CONTROL, self._on_notify)

    async def control(self, payload: list[int], timeout: float = 20.0) -> bytes:
        """Write a control-point command and await its response."""
        while not self._queue.empty():          # drop anything stale
            self._queue.get_nowait()

        await self.client.write_gatt_char(DFU_CONTROL, bytes(payload), response=True)

        # Skip frames that do not echo our opcode rather than failing on them.
        #
        # NOTE: this does NOT make us safe against Packet Receipt
        # Notifications. The bootloader re-labels a PRN as a CRC response
        # (p_res->request = NRF_DFU_OP_CRC_GET), so while we are awaiting
        # CALC_CRC a PRN is indistinguishable from the real answer and would
        # be consumed as one. PRN is disabled explicitly in main() before any
        # transfer; this loop is belt-and-braces for frames arriving while a
        # CREATE or EXECUTE is outstanding.
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DfuError(f"no response to opcode 0x{payload[0]:02x}")
            try:
                resp = await asyncio.wait_for(self._queue.get(), timeout=remaining)
            except asyncio.TimeoutError as exc:
                raise DfuError(f"no response to opcode 0x{payload[0]:02x}") from exc

            if len(resp) < 3 or resp[0] != RESPONSE:
                log(f"  (ignoring malformed frame {resp.hex()})")
                continue
            if resp[1] != payload[0]:
                log(f"  (ignoring unsolicited frame for opcode 0x{resp[1]:02x})")
                continue
            if resp[2] != RESULT_OK:
                # result 0x0B is EXT_ERROR: byte 3 carries the real cause,
                # which is how signature/version rejections usually arrive.
                ext = ""
                if resp[2] == 0x0B and len(resp) > 3:
                    ext = f" (extended error 0x{resp[3]:02x})"
                raise DfuRemoteError(
                    f"opcode 0x{payload[0]:02x} rejected with result "
                    f"0x{resp[2]:02x}{ext}")
            return resp

    async def packet(self, data: bytes) -> None:
        await self.client.write_gatt_char(DFU_PACKET, data, response=False)

    async def select(self, obj_type: int) -> tuple[int, int, int]:
        r = await self.control([OP_SELECT, obj_type])
        max_size = int.from_bytes(r[3:7], "little")
        offset = int.from_bytes(r[7:11], "little")
        crc = int.from_bytes(r[11:15], "little")
        return max_size, offset, crc

    async def transfer(self, data: bytes, obj_type: int, label: str) -> None:
        max_size, offset, crc = await self.select(obj_type)
        total = len(data)
        log(f"{label}: {total} bytes | device max_size={max_size} offset={offset} crc={crc}")
        if max_size <= 0:
            raise DfuError(f"device reported max_size={max_size}")
        if offset != 0:
            # Harmless: CREATE(COMMAND) memsets the whole progress struct, and
            # the command object is always transferred first, so the device is
            # back at zero before any image byte flows. Do NOT abort here --
            # the old advice was to dock the glasses, which is precisely the
            # condition behind the "stuck at 99%" brick report.
            log(f"  note: device reports a stale offset of {offset} bytes; "
                "CREATE resets all progress, continuing from byte 0")

        # M4: the command object must fit in a single DFU object.
        if obj_type == OBJ_COMMAND and total > max_size:
            raise DfuError(
                f"init packet is {total}B but the device accepts a single "
                f"command object of at most {max_size}B")

        file_offset = 0
        chunk_index = 0
        t0 = time.monotonic()

        while file_offset < total:
            chunk_start = file_offset
            chunk_size = min(max_size, total - chunk_start)

            for attempt in range(1, MAX_CHUNK_RETRIES + 1):
                file_offset = chunk_start          # rewind on retry (ref bug)

                await self.control(
                    [OP_CREATE, obj_type,
                     chunk_size & 0xFF, (chunk_size >> 8) & 0xFF,
                     (chunk_size >> 16) & 0xFF, (chunk_size >> 24) & 0xFF])

                if obj_type == OBJ_DATA:
                    # CREATE queues a flash page erase with a NULL callback,
                    # so the response arrives BEFORE the ~85 ms erase
                    # completes. Packets sent into that window occupy the
                    # 17-buffer pool until their flash write lands. Nordic's
                    # Android library inserts the same delay
                    # (prepareObjectDelay, 400 ms by default).
                    await asyncio.sleep(CREATE_ERASE_SETTLE)

                # Back off on each retry: if the cause was buffer overrun,
                # resending at the same rate will just fail again.
                delay = PACKET_DELAY * attempt

                end = chunk_start + chunk_size
                while file_offset < end:
                    n = min(PACKET_SIZE, end - file_offset)
                    await self.packet(data[file_offset:file_offset + n])
                    file_offset += n
                    if delay:
                        await asyncio.sleep(delay)

                # CRC is cumulative over the file from byte 0
                expected = zlib.crc32(data[:file_offset]) & 0xFFFFFFFF
                r = await self.control([OP_CALC_CRC])
                got_offset = int.from_bytes(r[3:7], "little")
                got_crc = int.from_bytes(r[7:11], "little")

                if got_crc == expected and got_offset == file_offset:
                    is_final = (obj_type == OBJ_DATA and file_offset >= total)
                    try:
                        await self.control([OP_EXECUTE], timeout=30.0)
                    except DfuRemoteError:
                        # The device answered and said NO -- signature or hash
                        # validation failed. Never report this as success.
                        raise
                    except Exception as exc:
                        if not is_final:
                            raise
                        # The last EXECUTE activates the image, and the
                        # bootloader can reset before answering. Treat a
                        # timeout/disconnect HERE as success: the image is
                        # already committed. Raising would report a good
                        # flash as a failure and invite a needless re-flash.
                        log(f"  final EXECUTE did not answer "
                            f"({type(exc).__name__}) -- device is rebooting, "
                            "treating as success")
                    break

                log(f"  chunk {chunk_index} attempt {attempt}: CRC/offset mismatch "
                    f"(device {got_offset}/{got_crc}, expected {file_offset}/{expected})")
                if attempt == MAX_CHUNK_RETRIES:
                    raise DfuError(f"chunk {chunk_index} failed after {attempt} attempts")

            chunk_index += 1
            if obj_type == OBJ_DATA:
                pct = 100.0 * file_offset / total
                rate = file_offset / max(time.monotonic() - t0, 1e-6) / 1024
                print(f"\r  {pct:5.1f}%  {file_offset}/{total} bytes  "
                      f"{rate:.1f} kB/s", end="", flush=True)

        if obj_type == OBJ_DATA:
            print()
        log(f"{label}: complete")


def _is_frame_normal(d, adv) -> bool:
    """Frame running its application -- NOT the bootloader.

    The DFU bootloader advertises as "Frame Update", which a bare "frame"
    substring match would wrongly accept.
    """
    name = (adv.local_name or d.name or "").lower()
    if "update" in name:
        return False
    uuids = [u.lower() for u in (adv.service_uuids or [])]
    return FRAME_SERVICE in uuids or "frame" in name


async def find_dfu_device(timeout: float = 40.0):
    log("scanning for a device in DFU mode (service 0xFE59)...")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        dev = await BleakScanner.find_device_by_filter(
            lambda d, adv: DFU_SERVICE in [u.lower() for u in (adv.service_uuids or [])],
            timeout=6.0)
        if dev is not None:
            log(f"found DFU target: {dev.name!r} @ {dev.address}")
            return dev
    return None


async def enter_dfu() -> bool:
    """Ask a running Frame to reboot into its bootloader."""
    log("looking for Frame in normal mode...")
    dev = await BleakScanner.find_device_by_filter(_is_frame_normal, timeout=15.0)
    if dev is None:
        log("Frame not advertising in normal mode (may already be in DFU)")
        return False

    log(f"connecting to {dev.name!r} to trigger frame.update()...")
    async with BleakClient(dev, timeout=20.0) as c:
        await c.write_gatt_char(FRAME_TX, b"\x03", response=True)   # break
        await asyncio.sleep(0.5)
        try:
            await c.write_gatt_char(FRAME_TX, b"print(frame.battery_level())",
                                    response=True)
            await asyncio.sleep(1.0)
        except Exception:
            pass
        try:
            await c.write_gatt_char(
                FRAME_TX,
                b"frame.display.text('Updating firmware...',70,180);"
                b"frame.display.show()",
                response=True)
            await asyncio.sleep(0.4)
        except Exception:
            pass
        log("sending frame.update() -- device will reboot into the bootloader")
        try:
            await c.write_gatt_char(FRAME_TX, b"frame.update()", response=True)
        except Exception as exc:
            log(f"(write returned {type(exc).__name__}: {exc} -- expected on reboot)")
    return True


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--zip", type=Path,
                    default=Path("firmware/frame-firmware-v25.278.1113.zip"))
    ap.add_argument("--dry-run", action="store_true",
                    help="enter DFU and read device state, but write nothing")
    args = ap.parse_args()

    if not args.zip.exists():
        log(f"firmware not found: {args.zip}")
        return 2

    z = zipfile.ZipFile(args.zip)
    init_packet = z.read("application.dat")
    image = z.read("application.bin")
    log(f"firmware {args.zip.name}: init={len(init_packet)}B image={len(image)}B")

    dfu_dev = await BleakScanner.find_device_by_filter(
        lambda d, adv: DFU_SERVICE in [u.lower() for u in (adv.service_uuids or [])],
        timeout=6.0)
    if dfu_dev is None:
        await enter_dfu()
        log("waiting for the bootloader to come up...")
        await asyncio.sleep(3.0)
        dfu_dev = await find_dfu_device()

    if dfu_dev is None:
        log("ERROR: no DFU target found. The device did not enter the bootloader.")
        log("The glasses should still be running normally -- nothing was written.")
        return 1

    async with BleakClient(dfu_dev, timeout=25.0) as client:
        mtu = getattr(client, "mtu_size", 0) or 0
        log(f"connected to bootloader, MTU={mtu}")
        if mtu and mtu < PACKET_SIZE + 3:
            log(f"ERROR: MTU {mtu} cannot carry a {PACKET_SIZE}-byte packet "
                f"write (needs >= {PACKET_SIZE + 3}); the write would be "
                "rejected. Nothing written -- reconnect and retry.")
            return 1
        target = DfuTarget(client)
        await target.start()

        # Disable Packet Receipt Notifications. This is NOT optional: the
        # bootloader emits a PRN re-labelled as a CRC response
        # (p_res->request = NRF_DFU_OP_CRC_GET), so it is indistinguishable
        # from a real CALC_CRC answer and would be consumed as one. PRN
        # defaults to 0 but lives in transport RAM and survives a
        # disconnect/reconnect, so any earlier tool that set it would still
        # be in effect. Both pc-nrfutil and Nordic's Android library set it
        # unconditionally for exactly this reason.
        await target.control([OP_SET_PRN, 0x00, 0x00])
        log("packet receipt notifications disabled")

        cmd_max, cmd_off, cmd_crc = await target.select(OBJ_COMMAND)
        dat_max, dat_off, dat_crc = await target.select(OBJ_DATA)
        log(f"command object: max_size={cmd_max} offset={cmd_off} crc={cmd_crc}")
        log(f"data object:    max_size={dat_max} offset={dat_off} crc={dat_crc}")

        if args.dry_run:
            log("DRY RUN -- device is in DFU mode and responding correctly.")
            log("Nothing was written. Power-cycle (dock/undock) to leave DFU mode.")
            return 0

        log("--- flashing: do NOT dock the glasses ---")
        await target.transfer(init_packet, OBJ_COMMAND, "init packet")
        await target.transfer(image, OBJ_DATA, "firmware image")
        await asyncio.sleep(0.5)
        log("transfer complete -- device is rebooting into the new firmware")

    await asyncio.sleep(8.0)
    log("verifying...")
    for attempt in range(1, 9):
        dev = await BleakScanner.find_device_by_filter(_is_frame_normal, timeout=10.0)
        if dev is None:
            log(f"  attempt {attempt}/8: not advertising yet")
            continue
        try:
            async with BleakClient(dev, timeout=20.0) as c:
                got = {}

                def on_rx(_h, data: bytearray):
                    got["v"] = bytes(data).decode(errors="replace")

                await c.start_notify("7a230003-5475-a6a4-654c-8431f6ad49c4", on_rx)
                await c.write_gatt_char(FRAME_TX, b"\x03", response=True)
                await asyncio.sleep(0.5)
                await c.write_gatt_char(FRAME_TX, b"print(frame.FIRMWARE_VERSION)",
                                        response=True)
                await asyncio.sleep(2.0)
                log(f"FIRMWARE NOW: {got.get('v', '(no reply)')}")
                return 0
        except Exception as exc:
            log(f"  attempt {attempt}/8: {type(exc).__name__}: {exc}")
            await asyncio.sleep(2.0)

    log("Flash finished but could not read the version back.")
    log("If the laptop cannot reconnect, the bond may have been cleared:")
    log("  cradle-button reset, then BleakClient(addr).unpair(), then re-pair.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
