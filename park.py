"""Leaving the glasses in a state that does not flatten them.

Every program in this project used to end with `stop_frame_app()` and
`disconnect()`. That is NOT a power-down. Reading the installed firmware
(v25.278.1113) makes the consequence explicit: after the app stops, `main()`
falls back into `run_lua()`, which draws the pairing screen and spins the
REPL. The CPU idles, but all four power rails, the FPGA, the OLED and BLE
advertising stay up -- 45-100 mA against a ~210 mAh cell, and there is no
idle timeout anywhere in the firmware. So every session ended with the
glasses lit, and roughly 2-4 hours later they were flat.

That matters far more than it sounds, because of what a flat Frame does:

    `shutdown()` is the only code in the firmware that arms a System OFF
    wake source. A battery that simply collapses never runs it. So a Frame
    that died of a flat battery has NO wake source armed, and tapping it
    can never work. Only docking and undocking can bring it back.

Hence this module. `park()` turns the display off before dropping the link,
which removes the largest load and the OLED burn-in risk together.

What it deliberately does NOT do is call `frame.sleep()`. In the firmware
the IMU interrupt is configured active-HIGH (imu.c) while the System OFF
wake is armed sense-LOW (main.c) -- opposite directions. If that inversion
is real, `frame.sleep()` parks the device in a state nothing can wake.
Until that is tested on hardware, docking is the only power-down path this
project treats as verified-reversible.
"""

from __future__ import annotations

from datetime import datetime

# Below this, a long undocked run is asking for the brownout described
# above. The gauge is voltage-based and clamped 0-100, so a reported "0%"
# is already past the point where a current burst collapses the rails.
SAFE_FLOOR_PCT = 20.0
START_FLOOR_PCT = 30.0


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


async def battery_level(frame) -> float | None:
    """Read the gauge, or None if it cannot be read."""
    try:
        reply = await getattr(frame, "ble", frame).send_lua("print(frame.battery_level())",
                                         await_print=True, timeout=6)
        return float(str(reply).strip())
    except Exception:
        return None


async def park(frame, *, report: bool = True) -> None:
    """Put the display to sleep and say where the charge ended up.

    Best effort throughout: this runs in a `finally`, and a failure here
    must never mask the error that actually ended the program.
    """
    level = await battery_level(frame)
    try:
        await getattr(frame, "ble", frame).send_lua("frame.display.power_save(true)", timeout=4)
    except Exception as exc:
        log(f"  could not power down the display: {type(exc).__name__}")

    if not report:
        return
    if level is None:
        log("battery unreadable -- dock the glasses when you are done")
    elif level <= SAFE_FLOOR_PCT:
        log(f"battery {level:.0f}% -- DOCK IT NOW. A Frame that dies flat "
            f"cannot be woken by tapping; only dock/undock brings it back")
    else:
        log(f"battery {level:.0f}% -- dock the glasses when you are done")


async def refuse_if_low(frame, floor: float = START_FLOOR_PCT) -> bool:
    """True if there is enough charge to start a long undocked run."""
    level = await battery_level(frame)
    if level is None:
        log("battery unreadable -- continuing, but dock it if the run is long")
        return True
    if level < floor:
        log(f"battery {level:.0f}% is below {floor:.0f}% -- charge first. "
            f"Running it to collapse is what makes it unwakeable.")
        return False
    log(f"battery {level:.0f}% at start")
    return True
