# Brilliant Labs Frame — working reference

Compiled 2026-10-12 from docs.brilliant.xyz, the `brilliantlabsAR/brilliant_sdk`
monorepo (README + AGENTS.md + MIGRATION.md + example indexes), and press/review
coverage. Hardware/API numbers are from official docs; battery-life figures are
from reviews and are marked as such.

## 1. Status (read this first)

- **Frame is discontinued hardware.** Brilliant Labs' 2nd-gen device is **Halo**
  (released Aug 2026). No published end-of-support date for Frame.
- **Frame is still supported by the current SDK.** The `brilliant_sdk` monorepo
  targets *both* Halo and Frame, and device type is auto-detected after connect.
- So: build with the new packages, but expect new examples/features to be
  Halo-first. Several examples are explicitly "Halo-only" (speaker, click events,
  audio activity detection, ANCS, layout engine); a few are "Frame-only"
  (auto/manual exposure, metering).

## 2. Hardware specs

| Area | Spec |
|---|---|
| Display | 0.23" micro-OLED, **640x400**, **20 deg FOV**, 255 possible colors but **max 16 per frame**, 8-bit YCbCr |
| Camera | Omnivision **OV09734**, 1280x720 sensor, cropped to square up to **720x720**, YCbCr |
| Microphone | TDK **ICS-41351** MEMS, 4-20 kHz sample rate, 4-16 bit, -35.5 to 129.5 dB |
| IMU | MEMSIC **MC6470**, 6-axis (accel + e-compass), hardware tap interrupt |
| MCU | Nordic **nRF52840**, Cortex-M4F @64 MHz, 1 MB flash, 256 KB RAM, BT 5.3 |
| FPGA | Lattice **Crosslink-NX LIFCL-17**, 17k LCs, 432 KB EBR + 2.56 MB LRAM, over SPI — graphics accel + camera iface |
| Battery | 2x 105 mAh li-ion (~**210 mAh**), PMIC MAX77654 |
| Current draw | 45-100 mA active (typ. 80), **580 uA sleep**, 132 uA shutdown |
| Cradle | "Mister Power", 140 mAh cell, USB-C in, 400 mA max, also does factory reset |
| Weight | ~**39 g** |
| Temp | 0-45 C operating, -20-60 C storage |
| Lens | 6 mm thick; optional prescription lenses or separate Rx clip |

**Battery life (reviews, not official):** ~30 h standby, **~4-6 h real use**
(camera-heavy requests drain fastest). All-day claims assume topping up in the
cradle.

## 3. BLE wire protocol (if going SDK-less)

- Service `7A230001-5475-A6A4-654C-8431F6AD49C4`
- TX char `7A230002-...`, RX char `7A230003-...`
- **Lua strings**: sent as plain UTF-8 on TX, no prefix. Replies come back on RX
  when Frame calls `print()` or errors.
- **Raw data**: prefix one byte `0x01`; this routes to
  `frame.bluetooth.receive_callback()` instead of the REPL.
- Control bytes: **`0x03` = break** (kill running script/loop),
  **`0x04` = reset** (clear vars, run `main.lua`).
- MTU is host-chosen, commonly 27-251 bytes. Raw payload limit is **MTU - 4**.
  Query from Lua with `frame.bluetooth.max_length()`.
- Important: while Lua is executing, further *string* commands are ignored, but
  raw `0x01` messages still fire callbacks. That asymmetry is exactly why the
  real app pattern is a persistent Lua event loop + raw messages, not the REPL.

## 4. Python SDK — use the new names

| Legacy | Current | Version |
|---|---|---|
| `frame-ble` | **`brilliant-ble`** | 3.x |
| `frame-msg` | **`brilliant-msg`** | 7.x |
| — | `brilliant-sdk` (meta, installs both) | |
| — | `halo-emulator` (hardware-free Lua runtime) | experimental |

```bash
pip install brilliant-sdk        # or: uv add brilliant-sdk
```

Class renames: `FrameBle` -> **`BrilliantBle`**, `FrameMsg` -> **`BrilliantMsg`**.
Everything else (`TxSprite`, `RxPhoto`, `RxIMU`, `TxPlainText`, ...) kept
its name. `BrilliantDeviceType` is new, from `brilliant_ble`.

**Breaking change beyond the rename:** the device-side `data.lua` event loop was
rewritten. `data.parsers`, `data.app_data`, `data.app_data_block` are **gone**.
`data.process_raw_items()` now returns an ordered list of `(flag, raw_bytes)`
pairs and *you* dispatch/parse them. Every pre-2026 `*_frame_app.lua` found
online needs porting — see `python/MIGRATION.md` in the monorepo.

> Note: most community Frame code and most model priors predate this. Assume any
> `frame_ble` / `FrameBle` / `data.app_data` snippet is stale.

### Quick REPL-style control (`brilliant-ble` alone)

```python
import asyncio
from brilliant_ble import BrilliantBle

async def main():
    frame = BrilliantBle()
    try:
        name = await frame.connect()
        print(f"Connected to {name}")
        await frame.send_lua(
            "frame.display.text('Hello, World!', 1, 1);"
            "frame.display.show();print(nil)",
            await_print=True,
        )
    finally:
        await frame.disconnect()

asyncio.run(main())
```

`BrilliantBle`: `connect()`, `disconnect()`, `send_lua()`, `send_message()`,
`max_lua_payload()`, `.type` (device detection).

### The real app pattern (`brilliant-msg`)

Every non-trivial app is **two programs** — a host program (Python) and a
device-side Lua app — coupled by single-byte message codes that must match on
both sides. Canonical lifecycle:

```
connect() -> upload_stdlua_libs([...]) -> upload_frame_app(...) ->
start_frame_app() -> send_message(code, packed) / attach rx handlers ->
stop_frame_app() -> disconnect()
```

```python
from brilliant_msg import BrilliantMsg, TxSprite

frame = BrilliantMsg()
await frame.connect()
await frame.print_short_text('Loading...')
await frame.upload_stdlua_libs(lib_names=['data', 'sprite'])
await frame.upload_frame_app(local_filename="lua/sprite_frame_app.lua")
frame.attach_print_response_handler()
await frame.start_frame_app()

sprite = TxSprite.from_indexed_png_bytes(Path("images/logo_1bit.png").read_bytes())
await frame.send_message(0x20, sprite.pack())
# ...
await frame.stop_frame_app()
await frame.disconnect()
```

Convention: `<name>.py` pairs with `lua/<name>_frame_app.lua`.

**Message types.** Tx (host->device): `TxCode`, `TxPlainText`, `TxSprite`,
`TxSpriteCoords`, `TxImageSpriteBlock`, `TxTextSpriteBlock`, `TxCaptureSettings`,
`TxAutoExpSettings`, `TxManualExpSettings`. Rx (device->host): `RxPhoto`,
`RxAudio`, `RxIMU`, `RxTap`, `RxAutoExpResult`, `RxMeteringData`.
Python lacks `RxClick` and `TxTextPage` (Flutter/WebBluetooth only); Python
uniquely has `brilliant_msg.calibration` / `.heading` and the emulator.

### Develop without the glasses

```bash
pip install halo-emulator
halo-emulator ./my_app/     # interactive REPL + display window
```

Firmware-faithful **Halo** emulator (Lua 5.4, real fonts/palette, IMU/tap/mic
event injection). It's Halo, not Frame — but device-side Lua is shared, so it's
still the fastest write->run->observe loop for the Lua half.

## 5. On-device Lua API (`frame.*`, Lua 5.4)

`io` and `os` from the Lua stdlib are **absent**.

**`frame.display`** — `text(str, x, y, {color='WHITE', spacing=4})` (1-indexed,
x 1..640, y 1..400, variable-width font, **no word wrap**);
`bitmap(x, y, width, color_format, palette_offset, data)` (color_format 2/4/16);
`show()`; `assign_color(color, r, g, b)` / `assign_color_ycbcr(...)`;
`set_brightness(-2..2)`; `power_save(bool)`; `write_register(reg, val)`.
`vector()` is documented but **not implemented**.
**Double-buffered: nothing appears until `show()`.** Palette names: VOID, WHITE,
GREY, RED, PINK, DARKBROWN, BROWN, ORANGE, YELLOW, DARKGREEN, GREEN, LIGHTGREEN,
NIGHTBLUE, SEABLUE, SKYBLUE, CLOUDBLUE.

**`frame.camera`** — `capture{resolution=512, quality='VERY_HIGH', pan=0}`
(resolution even, 100..720; pan -140..140; quality VERY_LOW..VERY_HIGH);
`image_ready()`; `read(n)`; `read_raw(n)` (skips the 623-byte JPEG header);
`auto{metering=..., exposure=..., ...}` (**call every ~100 ms**, returns stats);
`set_shutter(4..16383)`; `set_gain(1..248)`; `set_white_balance(r,g,b)` (0..1023
each); `read_register` / `write_register`; `power_save(bool)`.

**`frame.microphone`** — `start{sample_rate=8000|16000, bit_depth=8|16}`
(16-bit is signed little-endian); `stop()`; `read(n)`.
Bitrate = rate x depth / 8 B/s. 32 KB buffer; overflow = audio gaps.

**`frame.imu`** — `direction()` -> `{roll, pitch, heading}` degrees;
`tap_callback(fn)` (nil to clear; a tap **always** wakes from sleep);
`raw()` -> `{accelerometer={x,y,z}, compass={x,y,z}}`.

**`frame.bluetooth`** — `address()`, `receive_callback(fn)`, `max_length()`,
`send(data)` (<= max_length, null bytes OK).

**`frame.file`** — `open(name, 'read'|'write'|'append')` (**full mode words, not
'r'/'w'/'a'**), `remove`, `rename`, `listdir`, `mkdir`; file objects do
`f:read([n])`, `f:write(data)`, `f:close()`.

**`frame.compression`** — `decompress(data, block_size)`,
`process_function(fn)`. Compress host-side with `lz4 -9 -B1024 in out.lz4`.

**`frame.time`** — `utc([ts])`, `zone(['-7:00'])`, `date([ts])`.

**System** — `frame.HARDWARE_VERSION`, `FIRMWARE_VERSION` (e.g. `v24.046.1546`),
`GIT_TAG`, `battery_level()` (returns a float, e.g. 96.0), `sleep([secs])` (no arg = sleep until
tap), `update()` (reboot to bootloader for OTA), `stay_awake(bool)`,
`fpga_read(addr, n)` / `fpga_write(addr, data)`.

## 6. Gotchas worth remembering

1. **Nothing renders without `frame.display.show()`.** Frame is double-buffered;
   Halo draws immediately — don't copy Halo Lua verbatim.
2. **16 colors max on screen at once** out of a 255-color space.
3. **Bluetooth is the bottleneck: ~25 kB/s measured** (docs claim ~40). The 32 KB on-device buffer absorbs
   blocking tasks; exceed it and you get dropped audio / corrupt frames. This is
   the single biggest design constraint for camera or audio streaming.
4. Call `image_ready()` before `camera.read()` or the image is corrupt.
5. Auto-exposure needs ~100 ms polling to behave.
6. `f:read(n)` **bug**: may return up to 512 bytes regardless of `n`.
7. Always `f:close()` after writing or the file corrupts.
8. No "file exists" check — `open()` for read and catch the failure.
9. `set_brightness` high and `stay_awake(true)` both raise **OLED burn-in** risk.
   `stay_awake_true.py` is handy during dev; remember to undo it.
10. 1 MB flash / 256 KB RAM on the MCU — keep Lua apps and sprite payloads small;
    that's why `TxImageSpriteBlock` exists (slice-by-slice, low peak memory).
11. Sending a plain Lua string while a Lua app is running does nothing. Send
    `0x03` (break) first, or use raw messages.

## 7. Links

- Docs home: https://docs.brilliant.xyz/
- Frame hardware: https://docs.brilliant.xyz/frame/hardware/
- Frame Lua API: https://docs.brilliant.xyz/frame/frame-sdk-lua/
- Frame BLE specs: https://docs.brilliant.xyz/frame/frame-sdk-bluetooth-specs/
- Frame Python SDK: https://docs.brilliant.xyz/frame/frame-sdk-python/
- **SDK monorepo: https://github.com/brilliantlabsAR/brilliant_sdk**
  - `AGENTS.md` at the root is a genuinely good agent-oriented map — read it
    before starting an app. It also ships Claude Code skills in
    `.claude/skills/`: **`glasses-app`** (start here for a new app) and `release`.
  - `python/MIGRATION.md` — legacy -> current port guide
  - `python/packages/brilliant_msg/examples/EXAMPLES.md` — annotated example index
  - `python/packages/brilliant_ble/examples/EXAMPLES.md` — low-level/utility index
- Raw docs markdown (token-cheap):
  `https://raw.githubusercontent.com/brilliantlabsAR/docs/main/frame/<page>.md`
- Frame firmware: https://github.com/brilliantlabsAR/frame-codebase
- API refs: https://brilliant-ble.readthedocs.io/ and https://brilliant-msg.readthedocs.io/
- AR Studio (VSCode extension) — write/run Lua on Frame interactively
- Noa — the official companion AI app

## 8. Pairing & bonding (learned the hard way, 2026-10-12)

**Frame stores exactly ONE bond** (`bluetooth.c`: one `ble_gap_enc_info_t` at
`__bond_storage_start`). It accepts a new bond only when that page is blank
(all `0xFF`); otherwise it replies `AUTH_REQ` and disconnects immediately.
=> Pair to the **phone (Noa) OR the laptop, never both.** Switching needs a reset.

Occupied-slot symptom: connects fine at MTU 247, then every write returns ATT
`Insufficient Authentication`, and pairing fails + drops the link instantly.

**Factory reset / un-pair** (`main.c`, evaluated once at boot):
`if (charging && case_detect == LOW) { bluetooth_unpair(); stay_awake = true; }`

1. Dock Frame, cradle **plugged into power** (LED on) — otherwise `charging` is
   false and the branch never runs.
2. SIM tool, press and **hold** the button.
3. **Still holding, undock. Then release.** (`shutdown()` returns early while
   `stay_awake` is set, so it stays alive.)

Releasing while still docked sends case-detect LOW->HIGH, which is the
`LOTOHI` trigger for `case_detect_pin_interrupt_handler()` -> `shutdown()`.

**Pair from the OS, not from code.** Frame calls `sd_ble_gap_authenticate()`
itself on connect (`bond=1, mitm=0, io_caps=NONE` = Just Works), so the
*peripheral* drives the ceremony. `bleak`'s `client.pair()` always fails with
`Could not pair with device: FAILED`. Use **Windows Settings -> Bluetooth &
devices -> Add device -> "Frame F5"**, then bleak works over the encrypted link.

Only one host holds the connection at a time — a phone with Noa open will steal
it and make laptop scans time out. Turn phone Bluetooth off while developing.

This unit: `<FRAME_ADDR>`, advertises as `Frame F5`, MTU 247.

No J-Link needed: the bond is clearable via the cradle button, `pinout.h` shows
no debug header on Frame, and production nRF52840s are usually APPROTECT-locked
(SWD access would mean a mass erase + full reflash).

## 9. Verified firmware behaviour (read from frame-codebase, 2026-10-12)

Confirmed live on this unit: firmware **v25.080.0838**, battery **96%**,
text drawn to the display from this laptop. So everything below is tested,
not theoretical.

### Sleep is SYSTEM OFF, not a doze

`shutdown()` (`main.c:114`) calls `sd_softdevice_disable()` then sets
`NRF_POWER->SYSTEMOFF = 1`. A sleeping Frame is **not advertising and not
connectable** — no BLE command of any kind can reach it. There are three
sleep entries:

| Path | Wakes on tap? |
|---|---|
| `frame.sleep()` with **no args** -> `shutdown(true)` | **yes** |
| Docking (case-detect LOTOHI) -> `shutdown(false)` | no |
| Boot while charging + case-detect high -> `shutdown(false)` | no |

Wake sources are GPIO sense only: **undocking always wakes it**; a tap wakes it
only if it slept via `frame.sleep()`. **Bluetooth can never wake it.** There is
no idle auto-sleep timer in the firmware.

`frame.sleep(n)` **with** a number does NOT sleep the device — it spins on
`sd_app_evt_wait()` until the time elapses. That's what the REPL idle loop
uses, and it's the right call inside an app loop.

### A looping main.lua blocks the REPL

`luaport.c:153` runs `frame.stay_awake(false); require('main')`. If a companion
app (Noa) left a long-running `main.lua`, that call **never returns**, so the
splash draw and the REPL loop are both unreachable — Lua strings you write sit
in the buffer unexecuted and the device looks dead.

**Fix: send a single byte `0x03` (break signal) first.** It aborts `main.lua`,
redraws "Frame is Paired", and enters the REPL. `0x04` is the heavier reset.
`diagnose.py` sends the break automatically.

Also: with no `main.lua` at all the display does **not** stay blank — it falls
through to `show_pairing_screen()`. And **unpaired boots reformat the LittleFS
filesystem** (`lua_open_file_library(L, !is_paired)`), destroying any
`main.lua`; paired boots preserve it.

### Display gotchas

- `show()` is one SPI opcode (`0x14`) that swaps buffers, and the FPGA
  **auto-clears the new write buffer**. So every frame must be fully redrawn —
  there is no accumulation and no idle repaint.
- **Keep `y <= 352`.** Coordinates are range-checked 1..640 / 1..400 and raise
  a Lua error outside that, but the per-glyph fit check (`x + w <= 640 &&
  y + h <= 400`) fails **silently** — and normal glyph height is **48 px**. Text
  placed too low just disappears with no error.
- After a normal boot the panel is already powered on (boot config ends with
  `{0x00, 0x93}`), so `power_save(false)` is defensive, not required.
- Default text color is WHITE. `'VOID'` is unreachable — the name lookup starts
  at index 1.

### Device naming and the auth requirement

Device name is `"Frame %02X"` of the last address byte — hence `Frame F5` for
`<FRAME_ADDR>`. Both characteristics are declared `ENC_NO_MITM`, which is
why an unbonded host gets `Insufficient Authentication` on every write.
Write to `7a230002-...`; notifications come from `7a230003-...`.
First-byte protocol: `0x01` raw data, `0x03` break, `0x04` reset, anything
else is Lua source — so **never let a Lua payload start with 0x01/0x03/0x04**.

## 10. Three traps that cost hours (all confirmed on hardware)

### 10.1 The factory-reset button ERASES the bond

`bluetooth_unpair()` wipes Frame's stored key. **Do not press the cradle button
once you are paired** — it un-pairs the laptop too.

Worse, it leaves a **one-sided bond**: Windows keeps its copy of the LTK while
Frame has thrown its away, so every write fails with `Insufficient
Authentication` forever and re-pairing silently does nothing. Windows must be
told to forget it as well:

```python
import asyncio
from bleak import BleakClient
asyncio.run(BleakClient("<FRAME_ADDR>").unpair())
```

(or Settings -> Bluetooth & devices -> Frame F5 -> Remove device). No elevation
needed. Verify it worked -- the PnP entry should disappear:

```powershell
Get-PnpDevice | Where-Object { $_.InstanceId -match '<frameaddr-nocolons>' }
```

### 10.2 Windows bonds only after ~3 rejected attempts

Reproduced twice, identically: connects 1-3 give `Insufficient Authentication`
at MTU 247, then **connect 4 succeeds** at MTU 23 with the encrypted link up.
Frame calls `sd_ble_gap_authenticate()` itself on every connect, and Windows
takes a few of those before it initiates the bond.

**So retry. Never conclude anything from a single failed connect.** Do NOT call
`client.pair()` to hurry it along -- that makes Frame hang up (see section 8).

### 10.3 A frame app MUST print() at startup

`start_frame_app(await_print=True)` (the default) blocks until the device
prints. If your `*_frame_app.lua` never calls `print()`, the host dies with
`Exception: device didn't respond` **even though the app uploaded and is
running perfectly** -- the display will be showing your app's own startup
screen while the host claims the device is unresponsive.

Every upstream example opens its loop with a startup print. Do the same:

```lua
function app_loop()
    print('Frame app is running')   -- the handshake start_frame_app waits for
    while true do ... end
end
```

Note `send_message()` has its own 5s timeout waiting for the ACK that
`data.lua` sends back (`'\x01\x00\x00'`), and it raises the *same*
`device didn't respond` message -- so check which stage actually failed before
assuming it's the link. Printing progress before each send makes that obvious.

## 11. Measured hardware status (2026-10-12, this unit)

`selftest.py` result: **PASS=20 FAIL=0 WARN=1 SKIP=1**. Firmware v25.080.0838,
git 29604c5, battery 96%, BLE `XX:XX:XX:XX:XX:XX`, max_length 243,
host payload 244, round-trip latency **33 ms**, Lua heap 26.8 KB.

| Subsystem | Status | Evidence |
|---|---|---|
| nRF52840 / Lua 5.4 VM | **PASS** | file upload + require, 26.8 KB heap |
| PMIC (MAX77654) | **PASS** | battery_level 96% |
| Bluetooth 5.3 | **PASS** | MTU 247, 33 ms RTT |
| FPGA (Crosslink-NX) | **PASS** | `fpga_read(0x25,6)` returns 6 bytes |
| Display (micro-OLED) | **PASS** | text, named + custom palette colours, brightness, 1bpp bitmap |
| Microphone (ICS-41351) | **PASS** | all 4 modes; sustained 8640 / 16768 / 32000 B/s vs 8000 / 16000 / 32000 expected; real signal at -25.6 dBFS, longest flat run 5 (no dropouts) |
| IMU accelerometer | **PASS** | `(-45,-4197,270)`, orientation roll/pitch/heading |
| IMU magnetometer | **PASS** | `(-426,3643,8191)` -- note z sits at the 8191 rail |
| LittleFS filesystem | **PASS** | mkdir/write/read/listdir/remove round trip |
| Clock | **PASS** | utc set/read, date decomposition |
| LZ4 compression | **PASS** | API bound |
| **Camera pipeline** | **PASS** | valid 256x256 JPEGs captured, transferred, decoded |
| **Camera light** | **WARN** | see below |
| IMU tap | SKIP | needs a physical tap: `selftest.py --tap` |

### RESOLVED: the camera works -- the room was dark

Auto-exposure drives **shutter to its max 16383 and analog gain to its ceiling
16.0**, and scene brightness still reads **0.0016** (spot and matrix metering
0.0001, FPGA metering registers all zero). Every captured frame is uniform
near-black: luma 14-16 of 255, stddev under 2.

What this is NOT: the pipeline is fine -- captures complete, JPEGs are valid
and decode correctly, and transfer works. Only the photons are missing.

Two candidates remain, and they need physical access to separate:
1. **A dark scene.** The IMU read pitch = -86 degrees during testing, i.e. the
   glasses were lying nearly vertical, plausibly with the lens against a
   surface. Scene brightness reads 0.0016 rather than exactly 0, and a truly
   dead sensor would read a rigid 0 -- so this looks like a live sensor in the
   dark.
2. **A sensor or optics fault.**

**Resolution (same day): the sensor is confirmed functional.** Across runs
minutes apart the readings moved:

| Reading | Dark run | Later run |
|---|---|---|
| `brightness.scene` | 0.00159 | **0.12466** (78x) |
| `brightness.spot.average` | 0.0001 | 0.01176 |
| FPGA metering reg 0x25 byte 0 | 0 | 3, then 17 |
| manual-exposure metering | `0,0,0` | `4,3,5` / `3,3,3` |

The metering **tracks environmental light**, which a dead sensor cannot do. So
the camera, its FPGA interface, the JPEG encoder and the transfer path are all
good, and `selftest.py` now reports Camera **PASS**.

What is still unconfirmed is **image quality in good light**. Even at the
higher reading, 0.1247 is roughly 3 stops underexposed *at max shutter AND max
gain*, so every frame captured so far is luma ~16/255. That is a dim room, not
a fault.

**To confirm image quality:** point the glasses at a window or lamp and run
`python app/live_camera.py --frames 5 --warmup 5`. Luma above ~40 with
stddev above ~20 means a properly exposed picture.

### Docs error found

`frame.camera.set_exposure()` **does not exist** in v25.080.0838, despite
being documented. The real name is **`frame.camera.set_shutter()`**, which is
what the SDK's own `camera.lua` calls. `set_gain` and `set_white_balance` are
correct as documented.

## 12. Measured link throughput -- the real numbers

The docs say "~40 kB/s under good signal conditions". **Measured sustained
throughput is ~25 kB/s.**

| Load | Demanded | Achieved | Verdict |
|---|---|---|---|
| audio 8 kHz / 16 bit | 16 kB/s | 16.0 kB/s | **100% of realtime** |
| audio 16 kHz / 8 bit | 16 kB/s | 16.1 kB/s | **100% of realtime** |
| audio 16 kHz / 16 bit | 32 kB/s | ~25 kB/s | **78% -- drops samples** (6.6 s wall time produced 4.8 s of audio) |
| audio 8/16 + photo every 2 s | ~17 kB/s | 15.9 kB/s | 94% audio, 0.39 photos/s |
| camera alone, 256px MEDIUM | -- | 2.8 kB/s | **1.5 fps -- latency-bound, not bandwidth-bound** |

Two distinct limits, worth keeping separate:
- **Bandwidth** caps sustained streaming at ~25 kB/s.
- **Per-frame latency** (~0.65 s for a 2 kB JPEG) is what caps the camera at
  ~1.5 fps. At 2 kB/frame that is only 2.8 kB/s, nowhere near the ceiling --
  so a bigger resolution costs far less fps than you would expect, and
  shrinking the image buys almost nothing.

### The governing constraint for audio: a 2.048 s FIFO

From `lua_libraries/microphone.c`: the PDM always runs at 16 kHz into a
`int16_t buffer[32768]`, so the device-side buffer is **always 2.048 seconds
of audio regardless of the sample_rate/bit_depth you choose** (only the bytes
on the wire change). That is the longest the Lua loop may go without calling
`read()`.

The ISR wraps `head` with **no check against `tail`** -- there is no overflow
flag, no drop counter, and once it laps you get up to 2 s of stale audio
followed by a discontinuity, and **it never recovers to realtime**. Diagnose
by comparing bytes/byte_rate against wall clock: growing drift means you are
behind.

This is why photos and audio coexist: a 2 kB JPEG transfers in well under
2.048 s, so the FIFO absorbs the pause. Do not combine 16 kHz/16 bit audio
with large JPEGs -- a 10 kB 640px frame takes ~1.25 s, which is 61% of the
budget in one capture.

Also from the firmware: **8 kHz is naive decimation with no anti-alias
filter** (the source says so), so content above 4 kHz folds back. At an equal
16 kB/s, **16000/8 is cleaner than 8000/16**. And 8-bit is simply the top byte
of the int16 -- signed, no dither, ~48 dB.

### Audio gotchas that cost real time

- **`RxAudio.to_wav_bytes` already converts signed to unsigned for 8-bit.**
  Offsetting the samples yourself double-converts them. Verified: a correct
  8-bit WAV of a quiet room centres at ~128.
- **`sd.RawOutputStream.write()` blocks**, which stalls the asyncio loop and
  starves bleak's notification handler -- i.e. it costs you the audio you were
  trying to play. Use callback mode with a jitter buffer (`app/audio_out.py`).
- **WASAPI shared mode refuses 8/16 kHz** (the endpoint runs at 48 kHz). Pass
  `sd.WasapiSettings(auto_convert=True)`: measured **26 ms** latency versus
  91-120 ms on MME/DirectSound.
- **`audio.lua`'s send-retry loop has no sleep and no bail-out**, so if the
  link drops mid-stream it spins at 100% CPU until the watchdog fires. Always
  send the stop command before disconnecting -- the apps here do it in
  `finally`.
- Only `nil` from `read_and_send_audio()` means end-of-stream; `0` just means
  the buffer is momentarily empty. Expect empty reads right after start.
- The `None` queue sentinel arrives **only** after you send stop. A bare
  disconnect never produces it, so never `await queue.get()` unguarded.

### Camera: small images buy you almost nothing

Measured back-to-back on this unit, 5 frames each:

| Setting | Bytes/frame | fps | Sustained |
|---|---|---|---|
| 256px MEDIUM | 2.0 kB | 1.68 | 3.4 kB/s |
| 512px VERY_HIGH | 6.8 kB | 1.44 | 10.0 kB/s |

A **3.4x larger image costs only 14% of the frame rate.** At 10 kB/s it is
nowhere near the ~25 kB/s the link sustains, so bandwidth is not what limits
frame rate -- **per-frame latency is** (capture + device loop pass + round
trip, ~0.65 s).

Consequences:
- **Default to a big image.** 512px VERY_HIGH is nearly free next to 256px
  MEDIUM. `app/live_camera.py` now defaults to it.
- **To gain fps, cut latency, not pixels** -- `LOOP_SLEEP` in the device-side
  Lua app, and the auto-exposure cadence.
- Only around 720px VERY_HIGH (~50-100 kB) does bandwidth start to dominate.

### Display: exhaustive check (all green)

| Check | Result |
|---|---|
| All 15 reachable palette names | **15/15 render** |
| `set_brightness(-2..2)` | **5/5 accepted** |
| `bitmap()` color_format 2 / 4 / 16 | **all three OK** |
| `assign_color` / `assign_color_ycbcr` | both OK |
| `power_save(true)` / `(false)` | both OK |
| 1bpp bitmap pattern | OK |

`'VOID'` is the 16th name but is **unreachable** -- the firmware's name lookup
starts at index 1, so it can never be selected. That is why only 15 are
testable.

## 13. Firmware update to v25.278.1113 (done 2026-10-13)

Flashed with `dfu_update.py` in this project. `selftest.py` on the new build:
**PASS=21 FAIL=0 WARN=0 SKIP=1** -- the `frame.*` API is unchanged between
v25.080.0838 and v25.278.1113, so sections 5 and 9 above still apply.

The SDK cannot do this flash: `BrilliantBle.ota_flash_firmware()` is
**Halo-only** (BLE SMP / MCUmgr). Frame is nRF5 SDK and needs **Nordic Secure
DFU** over service `0xFE59`. `dfu_update.py` is a port of AR Studio's
`src/nordicdfu.ts`, hardened against the nRF5 bootloader source.

### Things that cost real time, in order

**1. The DFU wipes LittleFS.** Straight after the flash, every `frame.*`
namespace worked but `mkdir` / `listdir` / `open` all failed. The filesystem
region is erased and a *paired* boot does not reformat it. Recovery is an
**unpaired boot**: `lua_open_file_library(L, !is_paired)` ->
`if (reformat || mount_error) lfs_format(...)`. So a cradle-button factory
reset fixes it -- at the cost of the bond. This matters because
`upload_frame_app()` and `upload_stdlua_libs()` both need the filesystem, so
the entire message layer is dead until it is restored.

**2. The bond survives the DFU, but not the reset that fixes the
filesystem.** Reconnected first try after flashing; it was the subsequent
factory reset that required re-pairing (Windows unpair, then Add device).

**3. Charge it first.** The flash reported 92% -> 44%, and the device later
would not boot off-dock at all until charged. Budget power before starting.

**4. The cradle LED is ambiguous and it misled us.** "Off" means EITHER
charging complete OR no power at all. A dark LED was read as "fully charged"
when the cradle simply was not charging; the device then would not wake for
~40 minutes. **Always confirm the LED comes ON when docking** -- that is the
only unambiguous reading.

**5. Tapping cannot wake a Frame that slept through the dock path.** The IMU
wake source is only configured by `shutdown(true)`, which only `frame.sleep()`
(no args) calls. Docking calls `shutdown(false)`. Undocking is the sole
unconditional wake.

### DFU client details that are load-bearing

Verified against the nRF5 bootloader source; do not "simplify" these away:

- **The final EXECUTE may never answer** -- the MCU resets as it activates the
  image. Tolerate a *timeout* there, but NEVER a non-OK *result code*: that
  EXECUTE is where ECDSA signature and SHA-256 validation run, so a rejection
  means the firmware was refused. Conflating the two reports a rejected image
  as a successful flash.
- **Send `SET_PRN(0)` explicitly.** A Packet Receipt Notification is
  re-labelled by the bootloader as a CRC response
  (`p_res->request = NRF_DFU_OP_CRC_GET`), so it is indistinguishable from a
  real answer and cannot be filtered by opcode.
- **CRC is cumulative over the whole file**, never per object.
- `CREATE(DATA)` queues an **async ~85 ms page erase** and answers before it
  completes -- sleep after it or the 17-buffer pool overruns.
- The **10 ms inter-packet delay is the only flow control** (no PRN, no MTU
  backpressure). Do not shorten it.
- AR Studio's own retry path is broken: `chk--` without rewinding
  `fileOffset` silently drops 4 KB and hangs on the last chunk.

**You cannot brick a Frame through a DFU protocol error.** An
application-only Secure DFU never writes the MBR, SoftDevice, bootloader or
settings page, and the image is signature- and hash-validated before
activation. Worst case is a device sitting in the bootloader advertising
`0xFE59` as "Frame Update", re-flashable indefinitely. The one real danger is
losing power during activation -- a power problem, not a protocol one.

### 13.1 Charge from a WALL adapter, not a laptop port

The cradle draws up to **400 mA** and the manual says to use "any USB Type-C
**power supply**". A laptop USB port is not reliably that: ports get
power-managed down, throttled, or cut when the machine sleeps or suspends the
hub.

Charging this unit from a laptop port for a whole session produced a
consistent and very misleading picture:

- the cradle LED stayed lit for 2+ hours without ever reaching "complete"
- `battery_level()` read **92-96%** right after "charging"
- yet the device died within minutes off-dock, and at one point would not
  boot off-dock at all
- the firmware flash reported **92% -> 44%** in two minutes

Every one of those symptoms reads like a **degraded cell**, and that was the
hypothesis -- wrongly. It is equally what a cell that never actually filled
looks like, because the gauge is **voltage-based**: surface charge reads as a
healthy percentage and then collapses under load.

Two things follow:

1. **Never judge the battery from `battery_level()` alone.** It reports
   voltage, not capacity. Judge it from a discharge curve (`battery_test.py`).
2. **The cradle has its own 140 mAh cell** and will charge the glasses from it
   while unplugged -- so "docked" does not mean "charging". When that cell is
   flat you get a dark LED and no charge at all.

**The LED is the only unambiguous signal, and it is ambiguous in one
direction**: lit = charging; dark = EITHER complete OR not powered. Always
confirm it comes ON when docking before trusting that anything is happening.

### 13.2 "Dies out of the dock" was stay_awake, not the battery

A long and wrong diagnosis, recorded so nobody repeats it.

**Symptoms:** after the firmware update the glasses ran fine on cradle power
but appeared to die the instant they were undocked. The display went dark,
they stopped advertising, and `battery_level()` read 92-96% right up until
they vanished. That looks exactly like a cell that holds voltage but no
charge, and that is what it was diagnosed as -- twice, wrongly.

**Actual cause: `stay_awake`.** `run_lua()` clears the flag on every boot
(`luaport.c:153` runs `frame.stay_awake(false); require('main')`), including
the `true` that the cradle-button reset branch had just set. The device then
powers itself down, and "powered down" is indistinguishable from "flat" from
the host side -- both are simply absent from a BLE scan.

**Fix:** set it over Bluetooth after boot, where it sticks --
`frame.stay_awake(true)`, or `python stay_awake.py` in this project. The
device then runs docked AND undocked.

**Proof it was never the battery.** With `stay_awake(true)` set, `workout.py`
ran the full subsystem suite entirely on battery:

| Stage | Result |
|---|---|
| Display | palette + brightness sweep |
| IMU | orientation + accelerometer |
| Camera | 6 frames at 720px VERY_HIGH, 11-17 kB, luma 30-36 stddev 8-15 |
| Microphone | 313.6 kB in 20.0 s = 16.0 kB/s, **100% of realtime** |
| Message layer | TxPlainText rendered |

Battery across the whole run: **94% -> 94%.**

**Lessons:**
1. **An absent device is not a flat device.** A Frame in SYSTEM OFF and a
   Frame with a dead cell look identical from the host. Distinguish them by
   whether it runs *at all* once awake, not by how it disappears.
2. **`battery_level()` is a voltage reading**, so it is useless for capacity
   either way -- it neither proves nor disproves a bad cell.
3. The earlier evidence that it DID run handheld on battery (the 720px photos)
   should have outweighed the gauge readings. It was dismissed too readily.
