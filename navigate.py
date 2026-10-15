"""Walking directions on the Frame display.

    python navigate.py --to "Oxford Circus" --from 51.5033,-0.1276 --simulate
    python navigate.py --to coffee --gps          # fixes pushed from a phone
    python navigate.py --to "Big Ben" --from here --dry-run   # terminal only

Destinations can be a name ("the library", "coffee", "Big Ben") or a bare
`lat,lon`. With --gps the laptop listens for position from a phone; with
--simulate it walks the route by itself, which is how the whole thing was
developed without going outside.

The HUD shows one arrow, how far to it, and the street it puts you on,
with the rest of the trip underneath. Tap the glasses to read the full
instruction when a street name is too long for the panel; three taps
to stop.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path

from brilliant_msg import BrilliantMsg, RxTap, TxCode

from nav import arrows, geocode
from nav.guidance import Guide, distance_text, duration_text
from nav.hud import from_guidance, pack_nav, unpack_nav
from nav.position import FixedSource, HttpSource, SimulatedSource
from nav.router import route as plan_route
from park import park

NAV_MSG = 0x50
SPRITE_MSG = 0x20
CLEAR_MSG = 0x43
TAP_CTRL_MSG = 0x41

LUA_APP = Path(__file__).parent / "app" / "lua" / "nav_frame_app.lua"
STD_LIBS = ["data", "sprite", "code", "tap"]

UPDATE_HZ = 1.0
BATTERY_EVERY = 60.0        # one Lua round trip a minute, no more
REROUTE_COOLDOWN = 20.0     # never hammer the router from a bad fix
REROUTE_TIMEOUT = 12.0      # the HUD is frozen while this runs
# Stopping takes THREE taps, not two.
#
# Measured on hardware: two separate runs ended themselves within seconds
# of starting, both reporting a double tap. A 25 s diagnostic with the
# glasses untouched recorded ZERO taps, so the IMU is not storming -- the
# taps were real ones from waking the device. The rule was simply too easy
# to trip: the loop polls once a second, so ANY two taps landing in the
# same second read as a gesture, and one physical tap is documented to
# fire several IMU interrupts.
#
# Three taps inside a short window is something a pocket, a wake-up or a
# bounce does not produce by accident.
STOP_TAPS = 3
GESTURE_WINDOW = 1.6

# The taps that WAKE the glasses arrive while the app is still uploading,
# so the grace period has to outlast the startup, not just the attach.
TAP_GRACE = 6.0
FIX_WAIT = 120.0


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


class Navigator:
    def __init__(self, frame: BrilliantMsg | None, source, guide: Guide,
                 destination: str, dest_coord: tuple[float, float],
                 dry_run: bool = False) -> None:
        self.frame = frame
        self.source = source
        self.guide = guide
        self.destination = destination
        # The geocoded destination, kept verbatim. Re-deriving it from
        # route.shape[-1] makes every reroute chain off the previous
        # route's snapped endpoint rather than the place you asked for.
        self.dest_coord = dest_coord
        self.dry_run = dry_run
        self.rx_tap = RxTap()
        self.tap_q = None
        self._icon: str | None = None
        self._battery: int | None = None
        self._battery_at = 0.0
        self._reroute_at = 0.0
        self._rerouting = False
        self._last_drawn: bytes | None = None
        self._send_fails = 0
        self._taps_live_at = 0.0

    # ---------------------------------------------------------- the glasses

    async def send_sprite(self, icon: str) -> None:
        """Only ever called when the arrow actually changes."""
        if self.dry_run or self.frame is None:
            self._icon = icon
            return
        if await self._send(SPRITE_MSG, arrows.sprite(icon).pack()):
            self._icon = icon

    async def _send(self, code: int, payload: bytes) -> bool:
        """One write, surviving the link hiccups this hardware actually has.

        A dropped packet mid-walk used to propagate out of the run loop and
        end navigation in the middle of a street. One frame of HUD is not
        worth that; only a link that stays down is.
        """
        try:
            await self.frame.send_message(code, payload)
            self._send_fails = 0
            return True
        except Exception as exc:
            self._send_fails += 1
            log(f"  send failed ({self._send_fails}): "
                f"{type(exc).__name__}: {exc}")
            return False

    async def send_hud(self, packet: bytes) -> None:
        if packet == self._last_drawn:
            return                       # nothing changed; leave the panel be
        if self.dry_run or self.frame is None:
            self._last_drawn = packet
            got = unpack_nav(packet)
            bar = "!" if got["off_route"] else (">" if got["arrived"] else " ")
            print("  %s %-8s %-24s | %-24s | %-20s %s" % (
                bar, got["distance"], got["street"], got["next_line"],
                got["remaining"], got["status"]), flush=True)
            return
        # Only remember what was drawn if it actually got there -- a failed
        # write would otherwise leave the panel permanently stale.
        if await self._send(NAV_MSG, packet):
            self._last_drawn = packet

    async def battery(self) -> int | None:
        if self.dry_run or self.frame is None:
            return None
        if time.monotonic() - self._battery_at < BATTERY_EVERY:
            return self._battery
        self._battery_at = time.monotonic()
        try:
            reply = await self.frame.ble.send_lua(
                "print(frame.battery_level())", await_print=True, timeout=6)
            fresh = int(float(str(reply).strip()))
            if fresh != self._battery:
                log(f"battery {fresh}%")
            self._battery = fresh
        except Exception:
            pass                          # a missed reading is not an error
        return self._battery

    # -------------------------------------------------------------- taps

    async def drain_taps(self) -> int:
        n = 0
        while self.tap_q is not None:
            try:
                self.tap_q.get_nowait()
                n += 1
            except asyncio.QueueEmpty:
                break
        return n

    async def check_taps(self, guidance) -> bool:
        """Returns True if the wearer asked to stop."""
        if self.tap_q is None or self.tap_q.empty():
            return False

        # Anything arriving during startup is the wake-up gesture echoing,
        # not an instruction.
        if time.monotonic() < self._taps_live_at:
            dropped = await self.drain_taps()
            if dropped:
                log(f"  ignored {dropped} tap(s) during startup")
            return False

        # Count the whole burst, then decide. Counting is done against a
        # window rather than against the poll interval, so how many taps a
        # gesture appears to contain no longer depends on where the 1 Hz
        # poll happened to fall.
        taps = await self.drain_taps()
        burst_until = time.monotonic() + GESTURE_WINDOW
        while time.monotonic() < burst_until:
            try:
                await asyncio.wait_for(
                    self.tap_q.get(),
                    timeout=max(0.05, burst_until - time.monotonic()))
                taps += 1
            except asyncio.TimeoutError:
                break
        taps += await self.drain_taps()

        if taps >= STOP_TAPS:
            log(f"  {taps} taps -- stopping")
            return True

        # Fewer than the stop gesture: show the instruction in full, since
        # the column beside
        # the arrow cuts a real street name off at about 20 characters.
        # The expanded text goes in the three FULL-WIDTH rows -- putting it
        # back in the narrow column would clip the very thing being
        # expanded.
        text = guidance.instruction or guidance.street
        lines, line = [], ""
        for word in text.split():
            if len(line) + len(word) + 1 <= 26:
                line = f"{line} {word}".strip()
            else:
                lines.append(line)
                line = word
        if line:
            lines.append(line)
        lines += [""] * 2
        # Two rows of instruction, and the hint always keeps the third. A
        # long instruction used to overwrite the only clue about how to
        # stop, and anything past the fourth row vanished silently.
        await self.send_hud(pack_nav("", "", lines[0], lines[1],
                                     "3 taps to stop"))
        await asyncio.sleep(3.0)
        await self.drain_taps()        # taps during the overlay are not new
        self._last_drawn = None        # force a full HUD redraw
        return False

    # ----------------------------------------------------------- rerouting

    async def reroute(self, pos) -> bool:
        if time.monotonic() - self._reroute_at < REROUTE_COOLDOWN:
            return False
        self._reroute_at = time.monotonic()
        self._rerouting = True
        log(f"off route -- recalculating from {pos[0]:.5f},{pos[1]:.5f}")
        # Tell the wearer NOW. Setting the flag and clearing it either side
        # of the await meant the next loop iteration always read it as
        # False, so "rerouting..." never once reached the panel -- it kept
        # showing the stale pre-reroute frame for the whole call.
        self._last_drawn = None
        await self.send_hud(pack_nav("", "", "", "", "rerouting..."))
        try:
            fresh = await asyncio.to_thread(plan_route, pos, self.dest_coord,
                                            REROUTE_TIMEOUT)
        except Exception as exc:
            log(f"  reroute failed: {type(exc).__name__}: {exc}")
            self._rerouting = False
            return False
        self.guide = Guide(fresh, destination=self.destination)
        if isinstance(self.source, SimulatedSource):
            # keep the simulated walker on the NEW line, or it would stay
            # off-route for ever and reroute on every cooldown
            self.source.__init__(fresh.shape, speed_mps=self.source.speed)
            self.source.start()
        self._rerouting = False
        self._icon = None                         # force the arrow to resend
        self._last_drawn = None
        log(f"  new route: {fresh.total_metres:.0f} m, "
            f"{duration_text(fresh.total_seconds)}")
        return True

    # ---------------------------------------------------------------- loop

    async def run(self) -> int:
        if not self.dry_run and self.frame is not None:
            self.tap_q = await self.rx_tap.attach(self.frame)
            await self.frame.send_message(TAP_CTRL_MSG, TxCode(value=1).pack())
            await asyncio.sleep(0.4)
            dropped = await self.drain_taps()
            if dropped:
                log(f"  ignored {dropped} wake-up tap(s)")
            self._taps_live_at = time.monotonic() + TAP_GRACE

        log(f"navigating to {self.destination} "
            f"({self.guide.route.total_metres:.0f} m, "
            f"{duration_text(self.guide.route.total_seconds)})")

        period = 1.0 / UPDATE_HZ
        waited_for_fix = 0.0
        while True:
            fix = self.source.latest()
            if fix is not None:
                waited_for_fix = 0.0      # separate gaps must not accumulate
            else:
                if waited_for_fix == 0.0:
                    log("waiting for the first GPS fix...")
                waited_for_fix += period
                if waited_for_fix > FIX_WAIT:
                    log("no GPS fix arrived -- giving up")
                    return 2
                await self.send_hud(pack_nav("", "", "", "",
                                             "waiting for GPS"))
                await asyncio.sleep(period)
                continue

            guidance = self.guide.update(fix)
            if guidance is None:
                await asyncio.sleep(period)
                continue

            if guidance.icon != self._icon:
                await self.send_sprite(guidance.icon)
                # The arrow alone does not repaint the panel, so make sure
                # the HUD that follows is actually sent even if every text
                # field happens to be unchanged.
                self._last_drawn = None

            battery = await self.battery()
            await self.send_hud(from_guidance(guidance, battery=battery,
                                              rerouting=self._rerouting))

            if await self.check_taps(guidance):
                log("stopped by a double tap")
                return 0

            if self._send_fails >= 10:
                log("the glasses stopped responding -- giving up")
                return 2

            if guidance.arrived:
                log(f"arrived at {self.destination}")
                await asyncio.sleep(6.0)
                return 0

            if guidance.off_route:
                await self.reroute(fix.coord)

            await asyncio.sleep(period)


# ------------------------------------------------------------------ set-up

def resolve_start(args, http: HttpSource | None):
    """Where the walk begins, which may mean waiting for the phone."""
    if args.start and args.start.lower() not in ("here", "gps"):
        coord = geocode.parse_coord(args.start)
        if coord:
            return coord
        return geocode.resolve(args.start).coord

    if http is None:
        raise SystemExit("--from here needs --gps (or give a lat,lon)")

    log(f"waiting for a fix from the phone -- point GPSLogger at:\n    {http.url}")
    deadline = time.monotonic() + FIX_WAIT
    while time.monotonic() < deadline:
        fix = http.latest()
        if fix is not None:
            log(f"  got {fix.lat:.5f},{fix.lon:.5f} (+/-{fix.accuracy_m:.0f} m)")
            return fix.coord
        time.sleep(1.0)
    raise SystemExit("no GPS fix arrived from the phone")


async def report_battery(frame, when: str) -> float | None:
    """Read and LOG the battery. Every connection to these glasses should
    leave a number behind -- not knowing the charge at the moment something
    went wrong has already cost two misdiagnoses on this unit."""
    try:
        reply = await frame.ble.send_lua("print(frame.battery_level())",
                                         await_print=True, timeout=6)
        level = float(str(reply).strip())
        log(f"battery {level:.0f}% ({when})")
        return level
    except Exception as exc:
        log(f"battery unreadable ({when}): {type(exc).__name__}: {exc}")
        return None


async def connect(frame: BrilliantMsg, attempts: int = 20) -> bool:
    for i in range(1, attempts + 1):
        try:
            await frame.connect()
            return True
        except Exception as exc:
            log(f"  connect {i}/{attempts}: {exc}")
            await asyncio.sleep(3)
    return False


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--to", required=True, help="destination: a name or lat,lon")
    ap.add_argument("--from", dest="start", default="here",
                    help="start: a name, lat,lon, or 'here' for GPS")
    ap.add_argument("--gps", action="store_true",
                    help="take position from a phone over the LAN")
    ap.add_argument("--port", type=int, default=8900)
    ap.add_argument("--simulate", action="store_true",
                    help="walk the route automatically, for testing")
    ap.add_argument("--speed", type=float, default=1.4,
                    help="simulated walking speed, m/s (default 1.4)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the HUD to the terminal, skip the glasses")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)

    if not args.simulate and not args.gps:
        args.simulate = True
        log("no position source given -- simulating the walk")

    http = None
    if args.gps:
        http = HttpSource(port=args.port)
        http.start()
        log(f"GPS endpoint listening on {http.url}")

    try:
        start = resolve_start(args, http)
        dest = geocode.resolve(args.to, near=start)
        log(f"destination: {dest.short_name} "
            f"({dest.lat:.5f},{dest.lon:.5f})")

        r = await asyncio.to_thread(plan_route, start, dest.coord)
        log(f"route: {r.total_metres:.0f} m, {duration_text(r.total_seconds)}, "
            f"{len(r.steps)} maneuvers")

        if args.simulate:
            source = SimulatedSource(r.shape, speed_mps=args.speed)
        else:
            source = http
        source.start()

        guide = Guide(r, destination=dest.short_name)

        frame = None
        if not args.dry_run:
            frame = BrilliantMsg()
            log("connecting to the glasses...")
            if not await connect(frame):
                log("could not connect -- tap the glasses or dock/undock them")
                return 2
            log(f"connected (payload {frame.ble.max_lua_payload()})")
            await report_battery(frame, "on connect")
            await frame.ble.send_break_signal()
            await asyncio.sleep(0.5)
            await frame.ble.drain_print_channel()
            await frame.upload_stdlua_libs(lib_names=STD_LIBS)
            await frame.upload_frame_app(local_filename=str(LUA_APP))
            await frame.start_frame_app()

        nav = Navigator(frame, source, guide, dest.short_name, dest.coord,
                        dry_run=args.dry_run)
        try:
            return await nav.run()
        finally:
            if frame is not None:
                try:
                    await frame.send_message(CLEAR_MSG, TxCode(value=1).pack())
                    await frame.stop_frame_app()
                except Exception:
                    pass
                await park(frame)
                await frame.disconnect()
                log("disconnected")
    except KeyboardInterrupt:
        log("stopped")
        return 0
    except Exception as exc:
        log(f"error: {type(exc).__name__}: {exc}")
        return 1
    finally:
        if http is not None:
            http.stop()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
