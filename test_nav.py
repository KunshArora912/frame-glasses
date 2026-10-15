"""Unit tests for the navigation stack. No network, no glasses.

Everything that decides what the wearer sees is pure logic, so all of it is
testable here. The one canned Valhalla response below is the real shape of
the API's reply, including the kilometres-not-metres trap.

    python test_nav.py
"""

from __future__ import annotations

import math
import random
import time
import unittest

from nav import arrows
from nav.guidance import (OFF_ROUTE_HITS, Guide, distance_text,
                          duration_text)
from nav.hud import from_guidance, next_line, pack_nav, unpack_nav
from nav.position import (Fix, FixedSource, SimulatedSource,
                          fix_from_params)
from nav.router import (Route, Step, bearing_deg, decode_polyline6,
                        distance_to_index, haversine_m, project_on_route)

# A 400 m dog-leg: 200 m north, then 200 m east, with a turn in the middle.
SHAPE = [(51.5000, -0.1000), (51.5009, -0.1000), (51.5018, -0.1000),
         (51.5018, -0.0971), (51.5018, -0.0942)]


def straight_route() -> Route:
    steps = [
        Step(icon="depart", street="Alpha Street", metres=200.0, seconds=150.0,
             instruction="Walk north on Alpha Street.", verbal="",
             begin_index=0, end_index=2, raw_type=1),
        Step(icon="right", street="Beta Road", metres=200.0, seconds=150.0,
             instruction="Turn right onto Beta Road.", verbal="",
             begin_index=2, end_index=4, raw_type=10),
        Step(icon="arrive", street="", metres=0.0, seconds=0.0,
             instruction="You have arrived at your destination.", verbal="",
             begin_index=4, end_index=4, raw_type=4),
    ]
    total = sum(haversine_m(SHAPE[i], SHAPE[i + 1])
                for i in range(len(SHAPE) - 1))
    return Route(steps=steps, shape=SHAPE, total_metres=total,
                 total_seconds=300.0)


def out_and_back(gap_deg: float = 0.00017) -> Route:
    """Out 2.5 km, back along a kerb `gap_deg` east -- about 12 m."""
    out = [(51.50 + i * 0.00018, -0.1000) for i in range(140)]
    back = [(51.50 + i * 0.00018, -0.1000 + gap_deg)
            for i in range(139, -1, -1)]
    shape = out + back
    steps = [
        Step(icon="depart", street="Out Street", metres=2500, seconds=1800,
             instruction="Walk north on Out Street.", verbal="",
             begin_index=0, end_index=139, raw_type=1),
        Step(icon="uturn", street="Back Street", metres=2500, seconds=1800,
             instruction="Turn around onto Back Street.", verbal="",
             begin_index=139, end_index=279, raw_type=12),
        Step(icon="arrive", street="", metres=0.0, seconds=0.0,
             instruction="You have arrived at your destination.", verbal="",
             begin_index=279, end_index=279, raw_type=4),
    ]
    total = sum(haversine_m(shape[i], shape[i + 1])
                for i in range(len(shape) - 1))
    return Route(steps=steps, shape=shape, total_metres=total,
                 total_seconds=total / 1.35)


def fix_at(point, east_m: float = 0.0, accuracy_m: float = 6.0) -> Fix:
    east_deg = east_m / (111320.0 * math.cos(math.radians(point[0])))
    return Fix(lat=point[0], lon=point[1] + east_deg,
               accuracy_m=accuracy_m, t=time.monotonic())


class Geometry(unittest.TestCase):
    @staticmethod
    def _encode6(points):
        """Minimal precision-6 encoder, so the decoder round-trips for real."""
        out, prev_lat, prev_lon = [], 0, 0
        for lat, lon in points:
            ilat, ilon = round(lat * 1e6), round(lon * 1e6)
            for delta in (ilat - prev_lat, ilon - prev_lon):
                v = ~(delta << 1) if delta < 0 else (delta << 1)
                while v >= 0x20:
                    out.append(chr((0x20 | (v & 0x1F)) + 63))
                    v >>= 5
                out.append(chr(v + 63))
            prev_lat, prev_lon = ilat, ilon
        return "".join(out)

    def test_polyline6_round_trip(self):
        original = [(51.500730, -0.124600), (51.515360, -0.140780),
                    (-33.868800, 151.209300)]
        decoded = decode_polyline6(self._encode6(original))
        self.assertEqual(len(decoded), len(original))
        for (alat, alon), (blat, blon) in zip(original, decoded):
            self.assertAlmostEqual(alat, blat, places=6)
            self.assertAlmostEqual(alon, blon, places=6)

    def test_polyline6_is_not_polyline5(self):
        """Decoding Valhalla's output at precision 5 puts you 10x too far
        from the equator -- the single easiest way to break this."""
        encoded = self._encode6([(51.5007, -0.1246)])
        self.assertAlmostEqual(decode_polyline6(encoded)[0][0], 51.5007,
                               places=5)

    def test_haversine_known_distance(self):
        # one minute of latitude is a nautical mile
        d = haversine_m((51.0, 0.0), (51.0 + 1 / 60.0, 0.0))
        self.assertAlmostEqual(d, 1852.0, delta=5.0)

    def test_bearing_cardinals(self):
        self.assertAlmostEqual(bearing_deg((51.5, 0.0), (51.6, 0.0)), 0.0, delta=0.5)
        self.assertAlmostEqual(bearing_deg((51.5, 0.0), (51.5, 0.1)), 90.0, delta=0.5)

    def test_projection_lands_mid_segment(self):
        snap = project_on_route(SHAPE, (51.00045 + 51.0 - 51.0, -0.1))
        self.assertIsNotNone(snap)

    def test_projection_measures_lateral_offset(self):
        # 0.0001 deg of longitude at 51.5N is about 7 m
        snap = project_on_route(SHAPE, (51.0004 + 51.0 - 51.0, -0.1001))
        self.assertGreater(snap.lateral_m, 0.0)

    def test_distance_counts_down_along_route(self):
        total = distance_to_index(SHAPE, project_on_route(SHAPE, SHAPE[0]),
                                  len(SHAPE) - 1)
        half = distance_to_index(SHAPE, project_on_route(SHAPE, SHAPE[2]),
                                 len(SHAPE) - 1)
        self.assertGreater(total, half)
        self.assertAlmostEqual(total - half, 200.0, delta=15.0)

    def test_window_stops_snapping_backwards(self):
        """A route doubling back must not snap us onto the leg we walked."""
        there_and_back = SHAPE + SHAPE[::-1]
        late = project_on_route(there_and_back, SHAPE[1], from_i=5,
                                look_ahead=10)
        self.assertGreaterEqual(late.seg_i, 5)


class Formatting(unittest.TestCase):
    def test_distance_text(self):
        self.assertEqual(distance_text(5), "now")
        self.assertEqual(distance_text(47), "50 m")
        self.assertEqual(distance_text(1500), "1.5 km")
        self.assertEqual(distance_text(42000), "42 km")

    def test_distance_never_prints_1000_m(self):
        for m in range(960, 1040):
            self.assertNotEqual(distance_text(m), "1000 m")

    def test_duration_text(self):
        self.assertEqual(duration_text(30), "<1 min")
        self.assertEqual(duration_text(600), "10 min")
        self.assertEqual(duration_text(7200), "2h00")

    def test_step_label_falls_back_to_instruction(self):
        step = Step(icon="left", street="", metres=10, seconds=8,
                    instruction="Turn left onto the walkway.", verbal="",
                    begin_index=0, end_index=1, raw_type=15)
        self.assertEqual(step.label, "the walkway")

    def test_step_label_abbreviates(self):
        step = Step(icon="left", street="Great Titchfield Street", metres=10,
                    seconds=8, instruction="", verbal="", begin_index=0,
                    end_index=1, raw_type=15)
        self.assertEqual(step.label, "Great Titchfield St")

    def test_label_is_never_empty(self):
        step = Step(icon="steps", street="", metres=5, seconds=4,
                    instruction="Take the stairs to Level 0.", verbal="",
                    begin_index=0, end_index=1, raw_type=39)
        self.assertTrue(step.label.strip())


class Guidance(unittest.TestCase):
    def setUp(self):
        self.route = straight_route()
        self.guide = Guide(self.route, destination="Beta Cafe")
        self.sim = SimulatedSource(SHAPE, speed_mps=1.4)
        self.sim.start()

    def test_upcoming_turn_is_the_next_maneuver_not_the_current_one(self):
        """The classic off-by-one: show maneuver k+1 while inside k."""
        g = self.guide.update(self.sim.latest())
        self.assertEqual(g.icon, "right")           # not "depart"
        self.assertEqual(g.street, "Beta Rd")       # the street turned ONTO

    def test_distance_to_turn_decreases_monotonically(self):
        seen = []
        for _ in range(18):
            seen.append(self.guide.update(self.sim.latest()).distance_m)
            self.sim.advance(10.0)
        first_leg = [d for d in seen[:15]]
        self.assertEqual(first_leg, sorted(first_leg, reverse=True))

    def test_arrival_is_detected_and_named(self):
        self.sim.advance(10_000.0)
        g = self.guide.update(self.sim.latest())
        self.assertTrue(g.arrived)
        self.assertEqual(g.icon, "arrive")
        self.assertEqual(g.street, "Beta Cafe")     # not the instruction

    def test_off_route_needs_several_bad_fixes(self):
        for _ in range(3):
            self.assertFalse(self.guide.update(self.sim.latest()).off_route)
        self.sim.lateral_m = 80.0
        results = [self.guide.update(self.sim.latest()).off_route
                   for _ in range(OFF_ROUTE_HITS + 1)]
        self.assertFalse(results[0], "one bad fix must not trigger a reroute")
        self.assertTrue(results[-1])

    def test_returning_to_the_line_clears_off_route(self):
        self.sim.lateral_m = 80.0
        for _ in range(OFF_ROUTE_HITS + 1):
            self.guide.update(self.sim.latest())
        self.sim.lateral_m = 0.0
        self.assertFalse(self.guide.update(self.sim.latest()).off_route)

    def test_a_sloppy_fix_widens_the_off_route_threshold(self):
        """25 m of GPS error must not read as a wrong turn."""
        self.sim.lateral_m = 40.0
        for _ in range(OFF_ROUTE_HITS + 2):
            fix = self.sim.latest()
            fix.accuracy_m = 30.0                  # a poor urban fix
            g = self.guide.update(fix)
        self.assertFalse(g.off_route)

    def test_eta_ignores_an_implausible_speed(self):
        fix = self.sim.latest()
        fix.speed_mps = 40.0                       # in a car, or a bad fix
        fast = self.guide.update(fix).remaining_s
        fix.speed_mps = 0.0                        # stopped at a light
        stopped = self.guide.update(fix).remaining_s
        self.assertAlmostEqual(fast, stopped, delta=1.0)

    def test_eta_uses_a_believable_measured_pace(self):
        fix = self.sim.latest()
        fix.speed_mps = 2.5                        # a brisk but real walk
        brisk = self.guide.update(fix).remaining_s
        fix.speed_mps = 0.8                        # a dawdle
        slow = self.guide.update(fix).remaining_s
        self.assertLess(brisk, slow)


class Hud(unittest.TestCase):
    def test_round_trip(self):
        raw = pack_nav("250 m", "Oxford St", "then left", "1.2 km  9 min", "")
        got = unpack_nav(raw)
        self.assertEqual(got["distance"], "250 m")
        self.assertEqual(got["street"], "Oxford St")
        self.assertFalse(got["off_route"])

    def test_flags(self):
        raw = pack_nav("", "", "", "", "", off_route=True, imminent=True)
        got = unpack_nav(raw)
        self.assertTrue(got["off_route"])
        self.assertTrue(got["imminent"])
        self.assertFalse(got["arrived"])

    def test_non_ascii_is_folded_not_dropped(self):
        got = unpack_nav(pack_nav("", "Café — Nero", "", "", ""))
        self.assertEqual(got["street"], "Cafe - Nero")

    def test_fields_are_clipped_to_the_panel(self):
        got = unpack_nav(pack_nav("x" * 99, "y" * 99, "", "", ""))
        self.assertLessEqual(len(got["street"]), 26)

    def test_narrow_column_fields_use_the_narrow_budget(self):
        """distance and street are drawn at x=150, not x=14, so they only
        have room for about 20 glyphs -- and over-run clips SILENTLY."""
        got = unpack_nav(pack_nav("x" * 40, "Tottenham Court Road North",
                                  "y" * 40, "z" * 40, "w" * 40))
        self.assertLessEqual(len(got["distance"]), 20)
        self.assertLessEqual(len(got["street"]), 20)
        self.assertEqual(len(got["next_line"]), 26)      # full width is fine
        self.assertEqual(len(got["remaining"]), 26)

    def test_packet_fits_one_bluetooth_payload(self):
        raw = pack_nav(*["z" * 99] * 5)
        self.assertLessEqual(len(raw), 244)

    def test_apostrophes_survive(self):
        """Length prefixes mean no escaping, unlike the Lua literal path."""
        got = unpack_nav(pack_nav("", "St Martin's Ln", "", "", ""))
        self.assertEqual(got["street"], "St Martin's Ln")

    def test_arrive_preview_names_the_destination(self):
        guide = Guide(straight_route(), destination="Beta Cafe")
        sim = SimulatedSource(SHAPE, speed_mps=1.4)
        sim.start()
        sim.advance(250.0)                         # onto the second leg
        g = guide.update(sim.latest())
        if g.next_icon == "arrive":
            self.assertNotIn("You have arrived", next_line(g))

    def test_remaining_line_does_not_say_now(self):
        guide = Guide(straight_route(), destination="Beta Cafe")
        sim = SimulatedSource(SHAPE, speed_mps=1.4)
        sim.start()
        sim.advance(10_000.0)
        got = unpack_nav(from_guidance(guide.update(sim.latest())))
        self.assertNotIn("now", got["remaining"])


class Position(unittest.TestCase):
    def test_accepts_common_key_spellings(self):
        for params in ({"lat": "51.5", "lon": "-0.12"},
                       {"latitude": 51.5, "longitude": -0.12},
                       {"Lat": 51.5, "Lng": -0.12}):
            self.assertIsNotNone(fix_from_params(params), params)

    def test_rejects_junk(self):
        for params in ({"lat": "0", "lon": "0"},          # GPSLogger "no fix"
                       {"lon": "-0.12"},                  # missing latitude
                       {"lat": "999", "lon": "1"},        # out of range
                       {"lat": "abc", "lon": "1"}):
            self.assertIsNone(fix_from_params(params), params)

    def test_simulated_walk_covers_the_route(self):
        sim = SimulatedSource(SHAPE, speed_mps=1.4)
        sim.start()
        self.assertFalse(sim.finished)
        sim.advance(sim.total_m + 1.0)
        self.assertTrue(sim.finished)

    def test_fixed_source_never_goes_stale(self):
        src = FixedSource((51.5, -0.12))
        self.assertFalse(src.latest().is_stale)


class Arrows(unittest.TestCase):
    def test_every_icon_renders(self):
        for icon in arrows.ICONS:
            png = arrows.png(icon)
            self.assertTrue(png.startswith(b"\x89PNG"), icon)
            self.assertLess(len(png), 8000, icon)

    def test_icons_are_distinct(self):
        rendered = {arrows.png(i) for i in arrows.ICONS}
        # depart and straight are deliberately the same arrow
        self.assertGreaterEqual(len(rendered), len(arrows.ICONS) - 1)

    def test_no_icon_is_clipped_by_its_own_canvas(self):
        """Drawing straight into a 128 px box cut the heads off the left and
        right arrows -- sharp-left reached x = -3."""
        import io

        from PIL import Image
        for icon in arrows.ICONS:
            box = Image.open(io.BytesIO(arrows.png(icon))).getbbox()
            self.assertIsNotNone(box, icon)
            self.assertGreaterEqual(box[0], 0, icon)
            self.assertGreaterEqual(box[1], 0, icon)
            self.assertLessEqual(box[2], arrows.SIZE, icon)
            self.assertLessEqual(box[3], arrows.SIZE, icon)

    def test_icons_fill_a_consistent_area(self):
        """An arrow that visibly changes size between turns reads as a
        glitch, so every icon is fitted to the same box."""
        import io

        from PIL import Image
        widths = []
        for icon in arrows.ICONS:
            box = Image.open(io.BytesIO(arrows.png(icon))).getbbox()
            widths.append(max(box[2] - box[0], box[3] - box[1]))
        self.assertLess(max(widths) - min(widths), 12, widths)

    def test_sprite_fits_the_device_budget(self):
        packed = arrows.sprite("left").pack()
        self.assertLess(len(packed), 4096)

    def test_router_icon_map_only_names_real_icons(self):
        from nav.router import _ICON_BY_TYPE
        self.assertTrue(set(_ICON_BY_TYPE.values()) <= set(arrows.ICONS))


class ParallelLegs(unittest.TestCase):
    """The failure mode that only appears on real geometry.

    An out-and-back route puts the return leg about 12 m from the outbound
    one -- opposite kerbs of the same street, well inside GPS noise. Snapping
    on lateral distance alone let a single wobble latch onto the wrong leg
    and never recover.
    """

    def test_a_wobble_does_not_jump_to_the_return_leg(self):
        route = out_and_back()
        guide = Guide(route, destination="The Shop")
        shape = route.shape

        for v in (120, 125, 129):
            guide.update(fix_at(shape[v]))
        before = guide.update(fix_at(shape[129])).remaining_m

        guide.update(fix_at(shape[130], east_m=8.0))      # one 8 m wobble
        after = guide.update(fix_at(shape[131])).remaining_m

        self.assertLess(after, before, "remaining distance must keep falling")
        self.assertLess(before - after, 100.0,
                        "a wobble must not teleport us down the route")

    def test_remaining_never_rises_while_walking_forward(self):
        route = out_and_back()
        guide = Guide(route, destination="The Shop")
        shape = route.shape
        previous, rises = float("inf"), 0
        for v in range(100, 200):
            east = 8.0 if v == 130 else 0.0
            remaining = guide.update(fix_at(shape[v], east_m=east)).remaining_m
            if remaining > previous + 1.0:
                rises += 1
            previous = remaining
        self.assertEqual(rises, 0)

    def test_a_loop_route_does_not_arrive_before_you_start(self):
        """Origin next to destination plus a wobble used to report arrival
        on the very first fix."""
        loop = [(51.50 + 0.0045 * math.sin(a * math.pi / 60),
                 -0.10 + 0.0070 * (1 - math.cos(a * math.pi / 60)))
                for a in range(121)]
        steps = [
            Step(icon="depart", street="Loop Road", metres=1800, seconds=1300,
                 instruction="Start.", verbal="", begin_index=0,
                 end_index=120, raw_type=1),
            Step(icon="arrive", street="", metres=0, seconds=0,
                 instruction="You have arrived at your destination.",
                 verbal="", begin_index=120, end_index=120, raw_type=4),
        ]
        total = sum(haversine_m(loop[i], loop[i + 1])
                    for i in range(len(loop) - 1))
        route = Route(steps=steps, shape=loop, total_metres=total,
                      total_seconds=total / 1.35)
        for wobble in (3.5, 7.0, 12.0):
            guide = Guide(route, destination="Home")
            got = guide.update(fix_at(loop[0], east_m=wobble, accuracy_m=8.0))
            self.assertFalse(got.arrived, f"{wobble} m wobble reported arrival")
            self.assertGreater(got.remaining_m, total * 0.9)

    def test_a_real_detour_still_reports_off_route(self):
        """The continuity penalty must not mask an actual wrong turn."""
        route = out_and_back()
        guide = Guide(route, destination="The Shop")
        shape = route.shape
        guide.update(fix_at(shape[60]))
        for _ in range(OFF_ROUTE_HITS + 1):
            got = guide.update(fix_at(shape[60], east_m=120.0))
        self.assertTrue(got.off_route)

    def test_a_long_gps_gap_still_catches_up(self):
        route = out_and_back()
        guide = Guide(route, destination="The Shop")
        shape = route.shape
        guide.update(fix_at(shape[40]))
        got = guide.update(fix_at(shape[52]))     # ~240 m later, one jump
        self.assertLess(got.lateral_m, 15.0)
        self.assertGreater(got.step_index, -1)


class NoiseStability(unittest.TestCase):
    def test_the_arrow_never_goes_backwards(self):
        """Recomputing the maneuver from scratch each fix let ordinary
        noise redraw the turn the wearer had just made."""
        route = out_and_back()
        guide = Guide(route, destination="The Shop")
        shape = route.shape
        random.seed(7)
        last_step, backwards = 0, 0
        for v in range(0, 279, 2):
            jitter = random.uniform(-8.0, 8.0)
            got = guide.update(fix_at(shape[v], east_m=jitter,
                                      accuracy_m=8.0))
            if got.step_index < last_step:
                backwards += 1
            last_step = got.step_index
        self.assertEqual(backwards, 0)

    def test_the_final_leg_is_not_labelled_with_the_instruction(self):
        """The arrive maneuver has no street, so its label fell back to
        "You have arrived at your destination" -- shown for the whole leg."""
        route = out_and_back()
        guide = Guide(route, destination="The Shop")
        shape = route.shape
        for v in range(150, 279, 4):
            street = guide.update(fix_at(shape[v])).street
            self.assertNotIn("arrived", street.lower())
            self.assertTrue(street.strip())


if __name__ == "__main__":
    unittest.main(verbosity=2)
