"""End-to-end hardware self-test for Brilliant Labs Frame.

Exercises every subsystem on the board over Bluetooth and prints a PASS /
FAIL / WARN / SKIP table. Uploads selftest.lua to the device as st.lua,
drives it from the REPL, then removes it.

    .venv/Scripts/python.exe selftest.py
    .venv/Scripts/python.exe selftest.py --tap     # include interactive tap test

Subsystems covered: MCU/firmware, PMIC battery, Bluetooth link, FPGA SPI,
display (text/colour/palette/brightness/bitmap), camera (pipeline + exposure
telemetry), microphone (both sample rates and bit depths), IMU (orientation,
accelerometer, magnetometer, tap), LittleFS filesystem, compression binding,
real-time clock, and Lua heap.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

from brilliant_ble import BrilliantBle
from park import park

LUA_SRC = Path(__file__).parent / "selftest.lua"

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, str]] = []

    def add(self, subsystem: str, check: str, status: str, detail: str = "") -> None:
        self.rows.append((subsystem, check, status, detail))
        mark = {PASS: "[PASS]", FAIL: "[FAIL]", WARN: "[WARN]", SKIP: "[SKIP]"}[status]
        print(f"  {mark} {subsystem:<12} {check:<22} {detail}")

    def summary(self) -> str:
        counts = {s: 0 for s in (PASS, FAIL, WARN, SKIP)}
        for *_, status, _ in ((r[0], r[1], r[2], r[3]) for r in self.rows):
            counts[status] += 1
        return " ".join(f"{k}={v}" for k, v in counts.items())


async def connect_with_retry(frame: BrilliantBle, attempts: int = 10) -> str | None:
    """Frame sleeps into SYSTEM OFF (unreachable), and the Windows bond
    typically only lands after ~3 rejected attempts. So retry."""
    for attempt in range(1, attempts + 1):
        try:
            return await frame.connect(timeout=15)
        except Exception as exc:
            print(f"  connect {attempt}/{attempts}: {exc}")
            if attempt < attempts:
                await asyncio.sleep(2.0)
    return None


async def call(frame: BrilliantBle, lua: str, timeout: float = 30.0) -> str | None:
    """Run Lua, return the printed line, or None on error/timeout."""
    try:
        out = await frame.send_lua(lua, await_print=True, timeout=timeout)
        if out is None:
            return None
        out = out.strip()
        if "attempt to call a nil value" in out or out.startswith("[string"):
            return None
        return out
    except Exception:
        return None


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tap", action="store_true",
                        help="include the interactive tap test (needs you to tap the glasses)")
    parser.add_argument("--cam-steps", type=int, default=30,
                        help="auto-exposure convergence steps (default 30)")
    args = parser.parse_args()

    rep = Report()
    frame = BrilliantBle()

    print("Frame hardware self-test\n")
    name = await connect_with_retry(frame)
    if name is None:
        print("\nCould not connect. The glasses sleep into SYSTEM OFF and are then")
        print("unreachable over Bluetooth -- tap them, or dock and undock, then retry.")
        return 2

    try:
        print(f"connected to {name!r}\n")

        # --- break any running app so the REPL is reachable -------------
        await frame.send_break_signal()
        await asyncio.sleep(0.5)

        # --- upload the device-side helpers -----------------------------
        src = LUA_SRC.read_text(encoding="utf-8")
        await frame.upload_file_from_string(src, frame_file_path="st.lua")
        if await call(frame, "st=require('st');print('loaded')") is None:
            rep.add("Lua", "upload+require", FAIL, "could not load st.lua")
            await call(frame, "frame.file.remove('st.lua');print('rm')")
            return 1
        rep.add("Lua", "upload+require", PASS, f"{len(src)} bytes uploaded")

        # --- MCU / firmware ---------------------------------------------
        out = await call(frame, "st.sys()")
        if out and out.count("|") == 3:
            hw, fw, git, batt = out.split("|")
            rep.add("System", "versions", PASS, f"{hw} {fw} git={git}")
            try:
                pct = float(batt)
                rep.add("PMIC", "battery_level", PASS if pct > 0 else FAIL, f"{pct:.0f}%")
            except ValueError:
                rep.add("PMIC", "battery_level", FAIL, batt)
        else:
            rep.add("System", "versions", FAIL, str(out))

        # --- Bluetooth ---------------------------------------------------
        out = await call(frame, "st.bt()")
        if out and "|" in out:
            addr, maxlen = out.split("|")
            rep.add("Bluetooth", "address/max_length", PASS,
                    f"{addr} max_len={maxlen} host_payload={frame.max_lua_payload()}")
        else:
            rep.add("Bluetooth", "address/max_length", FAIL, str(out))

        # throughput: time a batch of round trips
        t0 = time.monotonic()
        n_rt = 10
        for _ in range(n_rt):
            await call(frame, "print('x')", timeout=10)
        rtt = (time.monotonic() - t0) / n_rt * 1000
        rep.add("Bluetooth", "round-trip latency", PASS if rtt < 500 else WARN,
                f"{rtt:.0f} ms/call")

        # --- Lua heap ----------------------------------------------------
        out = await call(frame, "st.mem()")
        rep.add("Lua", "heap", PASS if out else FAIL, f"{out} KB in use" if out else "")

        # --- FPGA --------------------------------------------------------
        out = await call(frame, "st.fpga()")
        if out and "|" in out:
            nbytes, first = out.split("|")
            rep.add("FPGA", "SPI read 0x25", PASS, f"{nbytes} bytes, first={first}")
        else:
            rep.add("FPGA", "SPI read 0x25", FAIL, str(out))

        # --- Display -----------------------------------------------------
        out = await call(frame, "st.disp()")
        rep.add("Display", "text/colour/bright", PASS if out == "ok" else FAIL,
                "drew 3 lines incl. custom RED palette entry" if out == "ok" else str(out))
        await asyncio.sleep(1.0)

        out = await call(frame, "st.bitmap()")
        rep.add("Display", "bitmap 1bpp", PASS if out == "ok" else FAIL,
                "8x8 pattern" if out == "ok" else str(out))
        await asyncio.sleep(1.0)

        # --- Microphone --------------------------------------------------
        for rate, depth in ((8000, 8), (8000, 16), (16000, 8), (16000, 16)):
            out = await call(frame, f"st.mic({rate},{depth},1.0)", timeout=30)
            label = f"{rate}Hz/{depth}bit"
            if not out or "|" not in out:
                rep.add("Microphone", label, FAIL, str(out))
                continue
            nbytes, mn, mx = out.split("|")
            if nbytes == "nil":
                rep.add("Microphone", label, FAIL, "read() returned nil")
                continue
            spread = int(mx) - int(mn)
            # signed-sample spread; floor scales with bit depth
            floor = 64 if depth == 16 else 2
            status = PASS if int(nbytes) > 0 and spread > floor else WARN
            rep.add("Microphone", label, status,
                    f"{nbytes} bytes, samples {mn}..{mx} (spread {spread})")

        # --- IMU ---------------------------------------------------------
        out = await call(frame, "st.imu()")
        if out and out.count("|") == 4:
            roll, pitch, heading, accel, compass = out.split("|")
            rep.add("IMU", "orientation", PASS,
                    f"roll={roll} pitch={pitch} heading={heading}")
            ax = [float(v) for v in accel.split(",")]
            rep.add("IMU", "accelerometer", PASS if any(ax) else WARN, f"({accel})")
            mg = [float(v) for v in compass.split(",")]
            rep.add("IMU", "magnetometer", PASS if any(mg) else WARN, f"({compass})")
        else:
            rep.add("IMU", "orientation", FAIL, str(out))

        # --- Filesystem --------------------------------------------------
        out = await call(frame, "st.fs()")
        if out and "|" in out:
            ok, nbytes, entries = out.split("|")
            rep.add("Filesystem", "mkdir/write/read", PASS if ok == "ok" else FAIL,
                    f"round-tripped {nbytes} bytes, listdir saw {entries}")
        else:
            rep.add("Filesystem", "mkdir/write/read", FAIL, str(out))

        # --- Clock -------------------------------------------------------
        out = await call(frame, "st.tim()")
        if out and "|" in out:
            utc, date = out.split("|")
            rep.add("Clock", "utc/date", PASS, f"utc={utc} -> {date}")
        else:
            rep.add("Clock", "utc/date", FAIL, str(out))

        # --- Compression: real LZ4 round trip ----------------------------
        try:
            import lz4.frame

            payload = (b"frame lz4 selftest payload. " * 12)[:324]
            # MUST be lz4 FRAME format -- raw block format is rejected by the
            # firmware with "decompression failed".
            comp = lz4.frame.compress(payload, compression_level=9,
                                      block_size=lz4.frame.BLOCKSIZE_MAX64KB)
            if len(comp) > frame.max_data_payload():
                rep.add("Compression", "lz4 round trip", SKIP,
                        f"{len(comp)}B exceeds one packet")
            elif await call(frame, "st.lz4_arm(1024)") != "armed":
                rep.add("Compression", "lz4 round trip", FAIL, "could not arm")
            else:
                await frame.send_data(bytearray(comp))
                await asyncio.sleep(2.0)
                out = await call(frame, "st.lz4_result()")
                await call(frame, "st.lz4_clear()")
                if out and "|" in out:
                    n, head = out.split("|", 1)
                    ok = n.isdigit() and int(n) == len(payload)
                    rep.add("Compression", "lz4 round trip",
                            PASS if ok else FAIL,
                            f"{len(payload)}B -> {len(comp)}B -> {n}B decompressed"
                            + ("" if ok else f" ({head})"))
                else:
                    rep.add("Compression", "lz4 round trip", FAIL, str(out))
        except ImportError:
            rep.add("Compression", "lz4 round trip", SKIP, "host lz4 package missing")

        # --- Camera ------------------------------------------------------
        print(f"\n  (converging auto-exposure, {args.cam_steps} steps...)")
        out = await call(frame, f"st.cam({args.cam_steps})", timeout=60)
        if out and out.count("|") == 3:
            shutter, gain, scene, spot = (float(v) for v in out.split("|"))
            detail = (f"shutter={shutter:.0f}/16383 gain={gain:.1f} "
                      f"scene={scene:.5f} spot={spot:.5f}")
            if scene < 0.01 and shutter >= 16000:
                rep.add("Camera", "exposure telemetry", WARN,
                        detail + "  <- maxed out, ~no light on sensor")
            else:
                rep.add("Camera", "exposure telemetry", PASS, detail)
        else:
            rep.add("Camera", "exposure telemetry", FAIL, str(out))

        out = await call(frame, "st.cam_manual()")
        rep.add("Camera", "manual shutter/gain/WB", PASS if out else FAIL,
                f"metering after max={out}" if out else "set_shutter/set_gain failed")

        # --- Tap (interactive) -------------------------------------------
        if args.tap:
            await call(frame, "st.tap_arm()")
            print("\n  >>> TAP THE GLASSES NOW -- 12 seconds <<<\n")
            await asyncio.sleep(12.0)
            out = await call(frame, "st.tap_count()")
            await call(frame, "st.tap_disarm()")
            try:
                taps = int(float(out))
            except (TypeError, ValueError):
                taps = -1
            rep.add("IMU", "tap interrupt", PASS if taps > 0 else FAIL,
                    f"{taps} taps detected")
        else:
            rep.add("IMU", "tap interrupt", SKIP, "needs a physical tap (--tap)")

        # --- cleanup ------------------------------------------------------
        await call(frame, "frame.file.remove('st.lua');print('rm')")

        print(f"\n{'-' * 72}\n{rep.summary()}\n")
        return 1 if any(r[2] == FAIL for r in rep.rows) else 0
    finally:
        try:
            await park(frame)
            await frame.disconnect()
        except Exception:
            pass
        print("disconnected")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
