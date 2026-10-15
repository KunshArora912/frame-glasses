# Frame undock-wake investigation and fix

## Symptom
Lifting the glasses off the charging cradle never woke them. Only the cradle's
pairing pin (which also wipes the Bluetooth bond) could boot them.

## Root cause (measured 2026-10-14, firmware v25.278.1113)
Stock firmware sleeps on dock by entering nRF52840 System OFF with one wake
source: GPIO P0.17 (`CASE_DETECT_PIN`) going low. That line is a 620k/300k
divider off the 5 V charging pad. On this unit it **stays high after undocking**
(logged with `frame.case_detect()` while the PMIC's CHGIN monitor showed the
charger voltage collapsing), so the wake can never fire. The pairing pin shorts
the same line to ground, which is why it always works. This is a board fault
(most likely the 300k pull-down open); the firmware has no other wake path.

Evidence: `frame.boot_diag()` retained boot log showed no boot attempt and no
reset of any kind between the dock sleep and the next pin press. A battery-
powered DFU reboot came up fine, ruling out brown-out. Sag under load was ~1%.

## Fix (firmware, `apply_diag_patch.py` then `apply_wake_fix.py` on tag v25.278.1113)
- When sleeping while docked, route the MAX77654 AMUX to the CHGIN voltage
  monitor and arm the nRF LPCOMP on AIN3 (ref 5/16 VDD, falling edge, hysteresis)
  so charger removal wakes the chip via ANADETECT. Skip PMIC main-bias LPM in
  that case so the AMUX keeps working.
- Stop the nrfx GPIOTE driver before arming wake senses: its PORT handler was
  inverting the sense polarity when a sense was armed on a pin already at the
  sensed level (found by source audit).
- Arm case-detect to wake on a change from its current level; arm the IMU INTA
  as sense-high (it is push-pull active-high).
- Re-read the charger status 300 ms later at boot when it first reads
  "present", so an undock wake is not misread as "still docked".
- Boot log retained across resets and System OFF, readable as
  `frame.boot_diag()`; probes `frame.case_detect()` and `frame.amux(sel)`.
- Diagnostic builds default to `stay_awake = true` (stay on while charging).
  Call `frame.stay_awake(false)` before `frame.sleep()` to actually sleep.

Verified: docked sleep -> lift -> woke in ~20 s, boot log reason 0x20000 (LPCOMP).

## Build / flash
- Toolchain: Arm GNU 13.3 in WSL (`~/tools/...`), build with
  `make application BUILD_VERSION=<ver>` in a worktree of frame-codebase.
- Package: `make_dfu.py <repo> <out.zip>` (pc-nrfutil 6.1.7 in a Python 3.10
  venv, signed with the repo's `dfu_private_key.pem`).
- Flash: `python dfu_update.py --zip <zip>` (glasses awake, nothing else connected).
- DFU keeps the bond but wipes LittleFS; pin resets wipe the bond.

## Idle sleep (fix2, `apply_idle_sleep.py` on top of the wake fix)
Off the cradle, with no Bluetooth connection and no tap for `idle_sleep_seconds`
(default 300; `frame.idle_sleep(n)` sets it, 0 disables), the glasses sleep with
tap-wake armed. Docked glasses never idle-sleep ("docked" = the PMIC sees charger
voltage, not the stuck detect pin). Poll point is the Lua watchdog hook, so any
running app or REPL is covered; a tap resets the timer.

Verified 2026-10-14 23:24: with a 20 s timeout and no connection it slept on
schedule. (It was on the cradle at the time but `frame.amux(1)` read 0.125 V,
i.e. no charger contact, so the "docked" guard correctly did not apply.)

Open item: the flashed fix2 was built before two prototypes were added; the
committed build is identical in behaviour but warning-free. Re-flash
`firmware/frame-fix2.zip` next time the glasses are awake and reachable.

## Quick checks
- Is it really charging? `frame.amux(1)` > 0.5 V means charger voltage present;
  ~0.1 V means no contact even if it is sitting in the cradle.
- Wake a sleeping Frame: three firm taps (tap-sleep or idle-sleep), or lift it
  off a powered cradle (docked sleep), or the cradle pin (wipes the bond).

## fix3 (2026-10-15)

`frame-fix3.zip` changes only diagnostics, no wake logic:

- `diag_format()` is bounds-safe (the old `len - n` could wrap once `n`
  passed `len`), skips implausible entries, and records the RAM[8] power /
  retention register seen at boot plus whether the retained log survived.
- `frame.boot_diag_raw()` returns the retained struct as hex.

Pitfall found while testing: a single Lua `print()` whose output exceeds
`frame.bluetooth.max_length()` (243 bytes here) is dropped silently, so a
full `frame.boot_diag()` (500+ bytes) reads back as an empty string. Print
it in chunks: `local s=frame.boot_diag() for i=1,#s,200 do print(s:sub(i,i+199)) end`.
`wake/read_diag.py`, `wake/wake_watch.py` and `wake/lua_exec.py` do this.

Verified on hardware 2026-10-15 (watcher log, `wake/wake_watch.py`):
docked sleep -> lifted off cradle -> advertising 2 s after boot;
idle sleep (30 s timeout, off cradle) -> silent 47 s -> three taps ->
advertising 1.4 s after boot, screen on.
