# Frame glasses — project workspace

Development workspace for a pair of Brilliant Labs **Frame** AR glasses
(BLE address `<FRAME_ADDR>`, advertises as `Frame F5`).

- **`FRAME-NOTES.md`** — the reference. Hardware specs, BLE wire protocol, the
  complete on-device `frame.*` Lua API, SDK lifecycle, gotchas, and the pairing
  mechanics in section 8. Read this before writing code.
- **`diagnose.py`** — run this first, any time something doesn't work.

## Status as of 2026-10-13 — WORKING, 21/21 hardware checks pass

**The laptop can control the glasses.** Verified live, end to end:

| Checked | Result |
|---|---|
| Firmware | **v25.278.1113** (RCE patched 2026-10-13) |
| Battery | **92%** |
| BLE | advertises as `Frame F5`, bonded to this laptop, encrypted link |
| Display | text drawn from Python and shown |
| `selftest.py` | **PASS=21 FAIL=0 WARN=0 SKIP=1** on v25.278.1113 |
| Camera | **confirmed working** — real photos at 720x720, std dev 31 |
| Microphone | all 4 modes, real signal, no dropouts |

The bond took several reconnect attempts to land: Windows completed the Just
Works ceremony on its own after Frame's repeated `sd_ble_gap_authenticate()`
requests. **If a connection fails, retry a few times before concluding
anything** — it succeeded on the 4th attempt.

Nothing is wrong with this unit. Bought as a $99 return sold "not fully
functional," it turned out to be fully functional; it had simply never been
un-paired from its previous owner.

All three programs below ran green on hardware:

```
$ python hello.py
Connected to 'Frame F5'  (type=BrilliantDeviceType.FRAME, max_lua=244)
  frame.FIRMWARE_VERSION = v25.278.1113
  frame.HARDWARE_VERSION = Frame
  frame.battery_level() = 96.0
  drawing -> drawn

$ python app/plain_text.py
connected (type=BrilliantDeviceType.FRAME)
Frame app is running
-> 'HELLO FROM PYTHON' at (60,100)
rendered: HELLO FROM PYTHON
-> 'message layer works' at (60,160)
rendered: message layer works
```

**To pick up where this left off:** the glasses go into SYSTEM OFF when they
sleep and are then completely unreachable over Bluetooth. **Tap them, or dock
and undock, to wake them**, then run `diagnose.py`. Retrying a scan against a
sleeping Frame can never work — see "Things that will waste your time" below.

### The cradle button: erases the bond, but also fixes the filesystem

It runs `bluetooth_unpair()` and **erases the bond**, un-pairing this laptop.
That is the ONLY way to reformat LittleFS after a firmware update, so it is
sometimes necessary — just never accidental.
It also leaves Windows holding a key Frame no longer has, which breaks every
write permanently until Windows is told to forget the device too:

```python
import asyncio; from bleak import BleakClient
asyncio.run(BleakClient("<FRAME_ADDR>").unpair())
```

And when connecting: **expect ~3 `Insufficient Authentication` failures before
the bond lands on the 4th attempt.** That's normal and reproducible. Retry
rather than diagnosing. `FRAME-NOTES.md` section 10 has the details.

## Quick start

```bash
.venv/Scripts/python.exe diagnose.py
```

Exit code 0 = controllable, 1 = bond slot taken, 2 = not advertising.

## Programs

| Command | What it does | Verified on hardware |
|---|---|---|
| `python diagnose.py` | Why can't I talk to the glasses? Bonds, breaks a stuck app, reports firmware/battery/MTU, draws to the display | yes |
| `python selftest.py` | Full hardware sweep, PASS/FAIL table for every subsystem. `--tap` adds the interactive tap test | yes - 21 PASS / 0 FAIL |
| `python hello.py` | Smallest example: Lua REPL, draw text | yes |
| `python app/plain_text.py` | The two-program message-layer pattern | yes |
| `python app/live_camera.py` | Live camera stream to a window, with fps/throughput stats | yes - 1.4 fps at 512px |
| `python dfu_update.py` | Nordic Secure DFU firmware flasher (`--dry-run` first) | yes - flashed v25.278.1113 |
| `python app/live_audio.py` | Live microphone to your speakers + WAV | yes - 100% realtime |
| `python app/live_av.py` | Audio and camera together on one link | yes - 94% audio + 0.39 photo/s |
| `python assistant.py` | Tap, ask aloud, read the answer. `--camera` attaches a photo; "take a picture" saves one | yes |
| `python navigate.py --to "..."` | Walking directions on the display. `--dry-run` needs no glasses | yes - full 619 m route end to end, 2026-10-13 |
| `python test_nav.py` | Offline test suite for the whole navigation stack | yes - 48 pass |
| `python frame.py` | **The shell**: one launcher hosting every app, taps or Mudra band to navigate | logic yes (46 tests); **on-glasses run pending** |
| `python test_shell.py` | Offline tests for the shell, teleprompter, captions, agenda, streaming STT | yes - 46 pass |
| `python todo.py` | Tap, speak a thought, it is transcribed locally and saved. Page: see below | logic yes; **on-glasses run pending** |
| `python watch_battery.py` | Overnight: records the battery the moment the glasses reappear, then parks them | yes - light enough to survive |

Useful flags: `--no-window` / `--no-play` for headless, `--save DIR` to keep
frames, `--seconds 0` to run until Ctrl-C, `--resolution` / `--quality` for
the camera, `--rate` / `--depth` for audio.

## Power: read this before anything else

Three things learned by reading the installed firmware (v25.278.1113) that
every program here now respects:

**A Frame that dies flat cannot be tapped awake.** `shutdown()` is the only
code that arms a System OFF wake source, and a collapsing battery never runs
it. There is no low-battery handling anywhere in the firmware. Dock, wait,
**undock** -- that is the one wake transition the firmware guarantees.

**Docked = dark.** With valid input power the device boots and immediately
returns to System OFF. A dark screen on the cradle is correct, not a fault.

**Never press the cradle pin to wake it.** It runs `bluetooth_unpair()` and
erases the single bond slot every time, leaving Windows with a key Frame no
longer has. Another owner filed exactly this as upstream issue #305.

And the self-inflicted one: `stop_frame_app()` + `disconnect()` leaves the
glasses **lit at 45-100 mA with no timeout**, so every session used to end
with them ~2-4 h from flat. Every program now calls `park(frame)` first,
which powers the display down and shouts if the charge is under 20%.

## The shell

```bash
python frame.py            # connect once, upload once, then switch apps
python frame.py --dry-run  # no glasses: prints what it would draw
```

| Taps | Menu | In an app |
|---|---|---|
| 1 | move down | the app's "next" |
| 2 | open | the app's "do it" |
| 3 | — | back to the menu |
| 4+ | quit | quit |

Apps are ~60-line classes that get a `Screen` and handle gestures. They
never touch Bluetooth, never upload Lua, and never know which input device
produced a gesture -- so a Mudra band (via its Companion app's localhost
WebSocket) drops in without any app changing.

| App | What it does | Better than Meta's because |
|---|---|---|
| **Teleprompter** | Your script on the HUD while you film. **Follows your voice** -- hears what you say, finds that line, advances itself | Meta's needs a finger-pinch per card |
| **Captions** | What people near you are saying, as they say it. Tap to freeze on "wait, what?" | Local Whisper, nothing leaves the laptop |
| **Day ahead** | Next event and the countdown, from your calendar | Meta's widget shows; this one can act |

**Audio OUT works.** Frame has no speaker, but your earphones pair to
Windows, and the laptop runs every app -- so it can speak through them while
the glasses show things. `shell/voice.py`: offline Windows speech, no
install, 1.5 s for a short prompt.

## Navigation

```bash
# no glasses, no phone, no walking -- prints the HUD to the terminal
python navigate.py --to "Oxford Circus" --from 51.5033,-0.1276 --simulate --dry-run

# on the glasses, position pushed from a phone
python navigate.py --to coffee --gps
```

Destinations can be a name ("coffee", "the library", "Big Ben") or a bare
`lat,lon`. The HUD is one arrow, the distance to it, and the street it puts
you on, with the rest of the trip underneath.

**Position comes from the phone, not the glasses** — Frame has Bluetooth and
an IMU, nothing that knows latitude. With `--gps` the laptop opens an endpoint
on the LAN; point GPSLogger (Android) at it under *Logging details → Log to
custom URL*:

```
http://<laptop-ip>:8900/gps?lat=%LAT&lon=%LON&acc=%ACC&dir=%DIR&spd=%SPD
```

Browser geolocation is deliberately not used: Chrome refuses it on a plain
`http://` LAN origin, so it would need TLS to work at all.

### Verified on hardware, 2026-10-13

A full 619 m / 9-maneuver route ran end to end on the glasses, untouched:
connect at the full 244-byte payload, app upload, arrow sprites, HUD
updates at 1 Hz, arrival. 93 s at a simulated 6 m/s, no dropped writes.

**Stopping takes THREE taps.** Two earlier hardware runs ended themselves
within seconds, both reporting a double tap. A 25 s diagnostic with the
glasses untouched recorded ZERO taps, so the IMU was not storming -- those
were real taps from waking the device. The rule was just too easy to trip:
the loop polls once a second, so any two taps in the same second read as a
gesture, and one physical tap fires several IMU interrupts. Three taps in a
1.6 s window is not something a wake-up or a pocket produces by accident,
and taps are ignored entirely for the first 6 s while the app uploads.

### What was measured building it

| Finding | Consequence |
|---|---|
| Valhalla reports lengths in **kilometres** even when asked for `units: meters` | everything converts to metres at the boundary, or every distance is 1000x out |
| Valhalla maneuver *k*'s type is the action taken **at** its `begin_shape_index` | the HUD must show maneuver *k+1* while you walk inside *k*, or it is a whole turn early |
| Nominatim's `bounded=1` returns **zero** hits for anything outside the box | a local pass first, falling through to a global one, or "Heathrow Airport" finds nothing |
| Re-sorting Nominatim hits by distance destroys the name match | "Trafalgar Square" resolved to a pub 100 m nearer. Relevance leads; distance only breaks ties |
| A bare category word matches a *shop named after it* | "bathroom" found Bathroom Merchant Services. Known category words query `[toilets]` instead |
| Routing from indoors sends you through building corridors | Valhalla happily routes through the Palace of Westminster's Commons Corridor |
| ~1/3 of real maneuvers carry **no** `street_names` | crosswalks and walkways need a label derived from the instruction, or the panel shows a blank |
| An out-and-back route puts the return leg **~12 m** from the outbound one | snapping on lateral distance alone latches onto the wrong kerb after one GPS wobble and never recovers — candidates are scored on route continuity too |
| The *arrive* maneuver has no street name either | its label fell back to its instruction, so the whole final leg read "You have arrived at" while still 300 m away |
| Recomputing the current maneuver from each fix | ±8 m of noise reversed the drawn arrow 3 times in 199 fixes; the maneuver index is now latched forward |

Guidance is pure logic — a Route and a Fix in, one Guidance out — so a whole
2 km walk replays in about 50 ms and all 48 tests run with no network and no
glasses. Seven of those tests are regressions for the snapping bugs above:
they walk an out-and-back route through a deliberate GPS wobble and assert the
remaining distance never rises.

## SECURITY: firmware is PATCHED

**Updated to v25.278.1113 on 2026-10-13 — the Bluetooth RCE is closed.**
The previous build, v25.080.0838, carried a buffer overflow in
`lua_write_to_repl()` allowing remote code execution over Bluetooth
(frame-codebase PR #301). `selftest.py` passes 21/21 on the new build and the
`frame.*` API is unchanged between the two.

Flashed with **`dfu_update.py`** in this project. The SDK cannot do it:
`BrilliantBle.ota_flash_firmware()` is **Halo-only** (BLE SMP / MCUmgr).
Frame is nRF5 SDK and needs **Nordic Secure DFU** over service `0xFE59`, so
`dfu_update.py` is a port of AR Studio's `nordicdfu.ts`, hardened against the
bootloader source. Run `--dry-run` first; it enters DFU, reads device state
and writes nothing.

If you ever flash again, read `FRAME-NOTES.md` section 13 first. The short
version:

1. **Charge to full.** The flash reported 92% -> 44%, and the glasses then
   would not boot off-dock until recharged.
2. **Undocked throughout.** The documented brick report is caused by the
   device being docked when the update completes.
3. **Expect the filesystem to be wiped.** A DFU erases LittleFS and only an
   *unpaired boot* reformats it — so budget a cradle-button reset, which also
   costs the bond.

**Do not open Noa afterwards** — see below.

## Don't bother with the Noa app

`noa-flutter` hard-pins `_firmwareVersion = "v25.080.0838"` and compares by
exact string equality; any mismatch triggers a DFU of its bundled copy. **Noa
will actively downgrade a patched Frame back to the vulnerable build.**

Beyond that: Noa v1.6.3 was released **2025-03-21** and the repo has had no
code pushed since May 2025; it scores 2.3 stars on Play; `brilliant.xyz/
products/frame` now 404s (only Halo is sold); and the separate "Brilliant Noa"
app for Halo is Halo-only. It also needs a mandatory cloud account with credit
caps.

Two useful facts if you ever do go back to it:
- **"Un-pair Frame first" is a catch-all error screen**, not an account check.
  It renders for `State.requiresRepair`, which is reached from any connect
  exception, any error stopping the Lua app, or a 1-second firmware-probe
  timeout. A stale bond and an Android-15 bonding bug produce the same message.
- **There is no account binding.** Noa's API has no device-registration
  endpoint and stores the paired device only in local `SharedPreferences`, so a
  previous owner cannot lock you out. All a second-hand Frame carries is their
  BLE bond, which the cradle pinhole reset clears.

Better alternatives, both Frame-compatible: `brilliant-sdk` (what this project
uses, pushed to Oct 2026) and `brilliantlabsAR/frame_realtime_gemini_voicevision`
for a Noa-like assistant you control.

## Handing the bond to this laptop

Frame stores **exactly one** Bluetooth bond — one `ble_gap_enc_info_t` in a
single flash page. It can be paired to the phone **or** this laptop, never
both. Switching requires a factory reset:

1. **Bluetooth OFF on the phone** (or "forget" Frame there). Frame accepts one
   connection at a time, so a phone with Noa open will also steal the link.
2. Dock Frame on the cradle, cradle **plugged into power** (white LED lit).
3. SIM tool on the cradle button — press and **HOLD**.
4. **Still holding, lift Frame out of the cradle. THEN release.**
5. Pair via **Windows Settings → Bluetooth & devices → Add device → Bluetooth
   → "Frame F5"**.

Why each step matters (all verified in the firmware source):

- **Step 2** — the reset branch is `if (charging && case_detect == LOW)`,
  evaluated once at boot. Off-charger, `charging` is false and the reset can
  never run, no matter how long you hold the button.
- **Step 4 order** — releasing while still docked drives case-detect low→high,
  which is the `LOTOHI` trigger for `case_detect_pin_interrupt_handler()` →
  `shutdown()`. Undocking first avoids that edge. `stay_awake = true` is set by
  the reset, and `shutdown()` returns early while it's set, so it stays alive.
- **Step 5** — Frame calls `sd_ble_gap_authenticate()` *itself* on connect
  (`bond=1, mitm=0, io_caps=NONE`, i.e. Just Works), so the **peripheral**
  drives the ceremony. `bleak`'s `client.pair()` consistently fails with
  `Could not pair with device: FAILED` and Frame hangs up. Let the OS do it.

## Things that will waste your time otherwise

1. **Docked Frame is powered OFF by default** — tapping it while docked does
   nothing, because no firmware is running to take the IMU interrupt.
   **Exception:** the cradle-button reset sets `stay_awake`, and `shutdown()`
   returns early while that is set — so a Frame reset with the button stays
   awake and fully reachable *while docked and charging*. Normal state, not a
   fault. It is also the one state never to run a firmware DFU in: the
   documented brick (noa-flutter #163) is updating while docked, because the
   device powers off when the update completes.
2. **A blank display is the default**, not a fault. The display is
   double-buffered; nothing appears until `frame.display.show()`.
3. **The display is a 20° FOV window** — easy to miss entirely if the nose
   bridge height is wrong. Try the 3mm / 2mm / no adaptor options.
4. **Bluetooth sustains ~25 kB/s measured** (docs claim ~40) with a 32 KB device-side buffer. This,
   not CPU, is the binding constraint on any camera or audio streaming design.
5. **Use the current SDK names** — `brilliant-ble` / `brilliant-msg`
   (`BrilliantBle` / `BrilliantMsg`). The legacy `frame-ble` / `FrameBle`
   packages and essentially every pre-2026 snippet online are stale, and the
   device-side `data.lua` event loop was rewritten (`data.app_data` and
   `data.parsers` no longer exist). See `python/MIGRATION.md` in the monorepo.
6. **Frame is discontinued** (superseded by Halo, Aug 2026) but still supported
   by the current SDK, which auto-detects device type on connect.

## References

- Reference notes: `FRAME-NOTES.md`
- SDK monorepo: https://github.com/brilliantlabsAR/brilliant_sdk
  (its root `AGENTS.md` is a good map; ships a `glasses-app` Claude Code skill)
- Device docs: https://docs.brilliant.xyz/frame/
- Firmware (authoritative for device behavior):
  https://github.com/brilliantlabsAR/frame-codebase
