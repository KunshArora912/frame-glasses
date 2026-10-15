"""Turning a route plus a position into the one thing to show right now.

This is the whole brain of the navigator, and it is deliberately pure: in a
Route and a Fix, out a Guidance. No Bluetooth, no network, no clock beyond
what the Fix carries. That is what makes a full walk testable in a quarter
of a second.

Two pieces of Valhalla semantics drive the design, and getting them wrong
produces a HUD that is confidently off by one turn:

  * a maneuver's `type` is the action you take AT its `begin_shape_index`,
    and its span runs from there to `end_shape_index`. So while you are
    walking inside maneuver k, the turn you are approaching belongs to
    maneuver k+1, not k.
  * `street_names` on maneuver k+1 is the street you turn ONTO. That is the
    name to show next to the arrow -- the same thing Google shows, and not
    the street you are currently on.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass

from nav.position import Fix
from nav.router import (Route, Snap, bearing_deg, distance_to_index,
                        haversine_m, project_on_route, project_segment)

# How far off the line counts as off-route. GPS in a street canyon is
# routinely 15-20 m out, so a tight threshold reroutes constantly while
# walking a perfectly correct line.
OFF_ROUTE_M = 30.0
OFF_ROUTE_HITS = 3          # consecutive bad fixes before we believe it
ARRIVE_M = 15.0

# A human walking pace, used to sanity-check the speed a fix reports.
WALK_MIN_MPS = 0.6          # slower than this and we are standing still
WALK_MAX_MPS = 3.0          # faster and the fix is jumping, not walking
IMMINENT_M = 20.0           # "now" rather than a distance

# Search window, in METRES along the route, around where we last were.
#
# This used to be a window in shape VERTICES scored purely on lateral
# distance, and that was wrong in a way that only shows up on real
# geometry: an out-and-back route puts the return leg ~12 m from the
# outbound one (opposite kerbs of the same street), which is well inside
# GPS noise. A single 8 m wobble would snap onto the return leg, and
# because nothing scored along-route continuity, it STAYED there -- the
# remaining distance collapsed by a kilometre and then counted UP as the
# wearer kept walking forward. On a loop route the same wobble on the
# first fix reported arrival before a step was taken.
#
# So candidates are now scored on lateral distance PLUS a penalty for
# jumping along the route, and the window is measured in metres rather
# than vertices (vertex spacing varies by an order of magnitude).
WINDOW_BACK_M = 40.0
WINDOW_FORWARD_M = 300.0
SNAP_FREE_M = 40.0          # ordinary progress, plus room for a GPS gap
SNAP_JUMP_PENALTY = 0.5     # metres of "lateral" charged per metre beyond


@dataclass
class Guidance:
    icon: str                   # arrow to draw for the upcoming action
    distance_m: float           # metres to that action
    street: str                 # street the action puts you on
    instruction: str
    next_icon: str | None       # the action after that, as a preview
    next_street: str
    remaining_m: float
    remaining_s: float
    step_index: int
    lateral_m: float
    off_route: bool = False
    arrived: bool = False
    stale: bool = False

    @property
    def imminent(self) -> bool:
        return self.distance_m <= IMMINENT_M


def distance_text(metres: float) -> str:
    """Short, glanceable, and never more precise than the fix deserves."""
    if metres < IMMINENT_M:
        return "now"
    if metres < 975:                       # 996 m must not print "1000 m"
        return f"{int(round(metres / 10.0) * 10)} m"
    if metres < 10000:
        return f"{metres / 1000.0:.1f} km"
    return f"{int(round(metres / 1000.0))} km"


def duration_text(seconds: float) -> str:
    if seconds < 60:
        return "<1 min"
    if seconds < 3600:
        return f"{int(round(seconds / 60.0))} min"
    hours, mins = divmod(int(round(seconds / 60.0)), 60)
    return f"{hours}h{mins:02d}"


class Guide:
    def __init__(self, route: Route, off_route_m: float = OFF_ROUTE_M,
                 arrive_m: float = ARRIVE_M,
                 destination: str = "") -> None:
        self.route = route
        self.destination = destination
        self.off_route_m = off_route_m
        self.arrive_m = arrive_m
        self._last_i = 0
        self._last_step = 0
        self._off_hits = 0
        # Cumulative distance to each shape vertex, so a position can be
        # expressed as one number along the route.
        shape = route.shape
        self._cum = [0.0]
        for i in range(len(shape) - 1):
            self._cum.append(self._cum[-1] + haversine_m(shape[i], shape[i + 1]))
        self._along = 0.0
        self._pace = (route.total_metres / route.total_seconds
                      if route.total_seconds > 0 else 1.35)

    # ------------------------------------------------------------ snapping

    def _snap(self, pos: tuple[float, float], tolerance: float) -> Snap:
        """Best point on the route for this position.

        Scores every candidate segment in the window on how far off the
        line it is AND how far it would move us along the route, so a
        parallel leg a few metres away cannot steal the snap from the leg
        we are actually walking.
        """
        shape = self.route.shape
        if len(shape) < 2:
            return Snap(0, 0.0, shape[0] if shape else pos, 0.0, 0.0)

        lo = max(0, bisect.bisect_left(self._cum,
                                       self._along - WINDOW_BACK_M) - 1)
        hi = min(len(shape) - 1,
                 bisect.bisect_right(self._cum,
                                     self._along + WINDOW_FORWARD_M))

        best: Snap | None = None
        best_score = float("inf")
        for i in range(lo, max(hi, lo + 1)):
            frac, point, lateral = project_segment(shape[i], shape[i + 1], pos)
            along = self._cum[i] + frac * (self._cum[i + 1] - self._cum[i])
            jump = max(0.0, abs(along - self._along) - SNAP_FREE_M)
            score = lateral + SNAP_JUMP_PENALTY * jump
            if score < best_score:
                best_score = score
                best = Snap(i, frac, point, lateral,
                            bearing_deg(shape[i], shape[i + 1]))

        # Genuinely lost -- a long GPS outage, or a lift across town. Only
        # then is searching the whole route the lesser risk.
        if best is None or best.lateral_m > tolerance:
            wide = project_on_route(shape, pos)
            if best is None or wide.lateral_m < best.lateral_m:
                best = wide
        return best

    def _along_of(self, snap: Snap) -> float:
        i = min(snap.seg_i, len(self._cum) - 2)
        return self._cum[i] + snap.t * (self._cum[i + 1] - self._cum[i])

    # -------------------------------------------------------------- steps

    def _step_at(self, seg_i: int) -> int:
        """Index of the maneuver whose span contains this shape segment."""
        steps = self.route.steps
        cur = 0
        for k, step in enumerate(steps):
            if step.begin_index <= seg_i:
                cur = k
            else:
                break
        # A maneuver whose span we have already walked past hands over to
        # the next one; the last maneuver (arrive) keeps the baton.
        while (cur < len(steps) - 1
               and seg_i >= steps[cur].end_index
               and steps[cur].end_index > steps[cur].begin_index):
            cur += 1
        return cur

    # ------------------------------------------------------------- update

    def update(self, fix: Fix | None) -> Guidance | None:
        if fix is None:
            return None

        shape, steps = self.route.shape, self.route.steps
        # A sloppy fix should not be read as a wrong turn.
        tolerance = max(self.off_route_m, 2.5 * (fix.accuracy_m or 0.0))

        snap = self._snap(fix.coord, tolerance)
        self._last_i = snap.seg_i
        self._along = self._along_of(snap)

        if snap.lateral_m > tolerance:
            self._off_hits += 1
        else:
            self._off_hits = 0
        off_route = self._off_hits >= OFF_ROUTE_HITS

        # Maneuvers never go backwards. Recomputing `cur` from scratch each
        # fix let ordinary GPS noise flip the drawn arrow to the turn just
        # made -- measured on a real 30-maneuver route: 13 reversals in 199
        # fixes, three of which reversed the arrow's direction outright.
        cur = max(self._step_at(snap.seg_i), self._last_step)
        self._last_step = cur
        nxt = min(cur + 1, len(steps) - 1)
        after = min(cur + 2, len(steps) - 1)

        turn_at = steps[cur].end_index if cur < len(steps) - 1 else len(shape) - 1
        to_turn = distance_to_index(shape, snap, turn_at)
        remaining = distance_to_index(shape, snap, len(shape) - 1)

        # Prefer the wearer's measured pace, but only while it is a pace a
        # person could actually be walking at. A stalled fix reads near
        # zero and a jumpy one reads like a car; both produce a nonsense
        # arrival time, so anything outside human range falls back to the
        # router's own estimate for this route.
        measured = fix.speed_mps or 0.0
        speed = measured if WALK_MIN_MPS <= measured <= WALK_MAX_MPS else self._pace
        remaining_s = remaining / max(speed, 0.3)

        arrived = remaining <= self.arrive_m

        upcoming = steps[nxt]
        if upcoming.icon == "arrive":
            # The arrive maneuver has no street name, so its label falls
            # back to its instruction -- which spent the whole final leg
            # showing "You have arrived at" while the wearer was still
            # several hundred metres away.
            street = self.destination or "destination"
        else:
            street = upcoming.label or steps[cur].label
        if arrived:
            # The arrive maneuver has no street, and its instruction
            # ("You have arrived at your destination.") just truncates into
            # noise on a 26-character panel. Name the place instead.
            street = self.destination or "destination"
        next_step = steps[after] if after != nxt else None
        # The arrive maneuver labels itself with its own instruction, which
        # reads as "then arrive: You have arri" once the panel clips it.
        next_label = ""
        if next_step is not None:
            next_label = (self.destination or "destination"
                          if next_step.icon == "arrive" else next_step.label)

        return Guidance(
            icon="arrive" if arrived else upcoming.icon,
            distance_m=0.0 if arrived else to_turn,
            street=street,
            instruction=upcoming.instruction or steps[cur].instruction,
            next_icon=next_step.icon if next_step else None,
            next_street=next_label,
            remaining_m=remaining,
            remaining_s=remaining_s,
            step_index=cur,
            lateral_m=snap.lateral_m,
            off_route=off_route and not arrived,
            arrived=arrived,
            stale=fix.is_stale,
        )
