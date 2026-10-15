"""Unit tests for the shell and its apps. No glasses, no microphone.

Everything that decides what gets drawn is pure logic, so it is all
testable here -- which matters because the shell has not yet run on
hardware, and the navigation app proved that simulation catches real bugs
before a hardware session does.

    python test_shell.py
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from ai.listener import new_tail
from shell.apps.agenda import Agenda, countdown, parse_when
from shell.apps.captions import VISIBLE_ROWS, Captions
from shell.apps.teleprompter import Teleprompter, match_line, split_script
from shell.apps.walk import StepCounter, Walk, pace_text
from shell.gestures import Gesture, gesture_for
from shell.mudra import band_present, intent_for
from shell.protocol import (MAX_ROWS, ROW_CHARS, pack_menu, pack_page,
                            unpack_menu, unpack_page)


def run(coro):
    return asyncio.run(coro)


class FakeScreen:
    """Records what would have been drawn."""

    def __init__(self) -> None:
        self.pages: list[list[str]] = []
        self.menus: list[tuple] = []

    async def page(self, lines, **kwargs) -> None:
        self.pages.append(list(lines))

    async def menu(self, labels, selected=0) -> None:
        self.menus.append((list(labels), selected))

    async def says(self, *lines, **kwargs) -> None:
        self.pages.append(list(lines))

    async def clear(self) -> None:
        self.pages.append([])

    def invalidate(self) -> None:
        pass

    @property
    def last(self) -> list[str]:
        return self.pages[-1] if self.pages else []


def attach(app) -> FakeScreen:
    screen = FakeScreen()
    app.screen = screen
    app.context = {}
    return screen


class Protocol(unittest.TestCase):
    def test_menu_round_trip(self):
        got = unpack_menu(pack_menu(["One", "Two", "Three"], 1))
        self.assertEqual(got["labels"], ["One", "Two", "Three"])
        self.assertEqual(got["selected"], 1)

    def test_menu_index_is_one_based_on_the_device(self):
        """The host counts from 0 and the device from 1; the conversion
        lives in exactly one place and this is it."""
        self.assertEqual(pack_menu(["a", "b", "c"], 0)[1], 1)
        self.assertEqual(pack_menu(["a", "b", "c"], 2)[1], 3)

    def test_menu_selection_is_clamped(self):
        self.assertEqual(unpack_menu(pack_menu(["a", "b"], 99))["selected"], 1)
        self.assertEqual(unpack_menu(pack_menu(["a", "b"], -5))["selected"], 0)

    def test_page_round_trip_with_flags(self):
        got = unpack_page(pack_page(["Head", "body"], heading=True))
        self.assertEqual(got["lines"], ["Head", "body"])
        self.assertTrue(got["heading"])
        self.assertFalse(got["emphasis"])

    def test_rows_beyond_the_panel_are_dropped_not_drawn_offscreen(self):
        """Text below y=352 vanishes SILENTLY, so it must never be sent."""
        got = unpack_page(pack_page([f"row {i}" for i in range(20)]))
        self.assertEqual(len(got["lines"]), MAX_ROWS)

    def test_fields_are_clipped_to_the_row_width(self):
        got = unpack_page(pack_page(["x" * 99]))
        self.assertLessEqual(len(got["lines"][0]), ROW_CHARS)

    def test_a_full_frame_fits_one_bluetooth_payload(self):
        self.assertLessEqual(len(pack_page(["x" * 99] * MAX_ROWS)), 244)
        self.assertLessEqual(len(pack_menu(["x" * 99] * MAX_ROWS, 0)), 244)

    def test_apostrophes_need_no_escaping(self):
        got = unpack_page(pack_page(["St Martin's Ln"]))
        self.assertEqual(got["lines"][0], "St Martin's Ln")

    def test_non_ascii_is_folded(self):
        got = unpack_page(pack_page(["Café — Nero"]))
        self.assertEqual(got["lines"][0], "Cafe - Nero")

    def test_empty_menu_does_not_crash(self):
        self.assertEqual(unpack_menu(pack_menu([], 0))["labels"], [])


class Gestures(unittest.TestCase):
    def test_tap_counts(self):
        self.assertIs(gesture_for(1), Gesture.NEXT)
        self.assertIs(gesture_for(2), Gesture.SELECT)
        self.assertIs(gesture_for(3), Gesture.BACK)

    def test_a_long_burst_quits_rather_than_doing_nothing(self):
        for n in (4, 5, 9):
            self.assertIs(gesture_for(n), Gesture.QUIT)

    def test_no_taps_is_no_gesture(self):
        self.assertIsNone(gesture_for(0))

    def test_mudra_maps_onto_the_same_intents(self):
        """An app must not be able to tell which input device it has."""
        self.assertIs(intent_for({"type": "gesture", "data": {"type": "tap"}}),
                      Gesture.SELECT)
        self.assertIs(intent_for({"type": "gesture",
                                  "data": {"type": "double_tap"}}),
                      Gesture.BACK)
        self.assertIs(intent_for({"type": "nav_direction",
                                  "data": {"direction": "Down"}}),
                      Gesture.NEXT)

    def test_mudra_ignores_what_it_does_not_understand(self):
        for frame in ({"type": "emg", "data": {"values": [[1]]}},
                      {"type": "gesture", "data": {"type": "wiggle"}},
                      {"type": "nav_direction", "data": {"direction": "Roll Left"}},
                      {}):
            self.assertIsNone(intent_for(frame), frame)

    def test_band_needs_both_firmware_and_serial(self):
        self.assertTrue(band_present({"device": {"firmware": "6.0", "serial_number": "A"}}))
        self.assertFalse(band_present({"device": {"firmware": "6.0", "serial_number": None}}))
        self.assertFalse(band_present({}))


class TeleprompterText(unittest.TestCase):
    SCRIPT = ("Hey everyone, welcome back. Today I'm showing you something "
              "wild. I bought a pair of AR glasses for ninety nine dollars. "
              "They were sold as broken.")

    def test_no_row_is_a_stranded_fragment(self):
        """One-word rows waste a third of the view AND match any sentence
        containing that word, which sent the follower to the wrong line."""
        for line in split_script(self.SCRIPT):
            self.assertGreaterEqual(len(line.split()), 2, line)

    def test_rows_fit_the_panel(self):
        for line in split_script(self.SCRIPT):
            self.assertLessEqual(len(line), ROW_CHARS)

    def test_following_advances_with_the_voice(self):
        lines = split_script(self.SCRIPT)
        index = 0
        for heard in ["hey everyone welcome back",
                      "today I'm showing you something wild",
                      "I bought a pair of AR glasses"]:
            moved = match_line(heard, lines, index)
            self.assertGreaterEqual(moved, index)
            index = moved
        self.assertGreater(index, 0)

    def test_an_ad_lib_does_not_move_the_script(self):
        lines = split_script(self.SCRIPT)
        self.assertEqual(
            match_line("completely unrelated tangent about lunch", lines, 2), 2)

    def test_it_never_goes_backwards(self):
        lines = split_script(self.SCRIPT)
        for start in range(len(lines)):
            self.assertGreaterEqual(
                match_line("hey everyone welcome back", lines, start), start)

    def test_the_current_line_lands_on_the_highlighted_row(self):
        """The device draws row 2 bright; padding above it would highlight
        the line already read."""
        app = Teleprompter(Path("nope.txt"))
        app.lines = split_script(self.SCRIPT)
        app.index = 2
        self.assertEqual(app.view()[1], app.lines[2])

    def test_a_missing_script_says_so_instead_of_crashing(self):
        app = Teleprompter(Path("definitely-not-here.txt"))
        app.load()
        self.assertEqual(app.lines, [])
        self.assertTrue(any("script" in row for row in app.view()))

    def test_tap_advances_and_stops_at_the_end(self):
        app = Teleprompter(Path("nope.txt"))
        attach(app)
        app.lines = split_script(self.SCRIPT)
        app.index = len(app.lines) - 1
        run(app.gesture(Gesture.NEXT))
        self.assertEqual(app.index, len(app.lines) - 1)


class CaptionText(unittest.TestCase):
    def setUp(self):
        self.app = Captions()
        self.screen = attach(self.app)
        run(self.app.start())
        self.app.started = True
        self.app.notice = ""

    def feed(self, sentence: str) -> None:
        for word in sentence.split():
            run(self.app.on_heard(word))

    def test_rows_never_exceed_the_panel_width(self):
        self.feed("so the thing about these glasses is that nobody expected "
                  "them to work at all given they were sold as broken")
        for row in self.app.rows + [self.app.pending]:
            self.assertLessEqual(len(row), ROW_CHARS, row)

    def test_a_very_long_word_breaks_instead_of_vanishing(self):
        self.feed("supercalifragilisticexpialidociousandthensome")
        joined = " ".join(self.app.rows + [self.app.pending])
        self.assertIn("supercalifragilistic", joined.replace("- ", ""))

    def test_the_view_always_has_a_full_set_of_rows(self):
        self.feed("just a few words")
        self.assertEqual(len(self.app.view()), VISIBLE_ROWS + 1)

    def test_text_grows_downward_from_a_stable_position(self):
        """Padding goes at the TOP so the first lines do not jump up the
        panel as more arrive."""
        self.feed("one")
        self.assertEqual(self.app.view()[0], "")

    def test_freezing_holds_the_view_but_not_the_transcript(self):
        self.feed("the first thing that was said here")
        run(self.app.gesture(Gesture.NEXT))
        frozen_view = list(self.app.view()[:-1])
        self.feed("and then some more words after that")
        self.assertEqual(self.app.view()[:-1], frozen_view)
        self.assertIn("more words", self.app.transcript)

    def test_resuming_catches_up(self):
        self.feed("first part of the sentence")
        run(self.app.gesture(Gesture.NEXT))
        self.feed("second part arrives while frozen")
        run(self.app.gesture(Gesture.NEXT))
        self.assertIn("frozen", " ".join(self.app.view()))

    def test_without_a_microphone_it_says_so(self):
        app = Captions()
        attach(app)
        run(app.start())
        self.assertTrue(app.notice)
        self.assertLessEqual(len(app.notice), ROW_CHARS)

    def test_the_status_row_reports_the_state(self):
        self.assertIn("listening", self.app.view()[-1])
        run(self.app.gesture(Gesture.NEXT))
        self.assertIn("FROZEN", self.app.view()[-1])


class Streaming(unittest.TestCase):
    def test_overlapping_windows_do_not_repeat_words(self):
        self.assertEqual(new_tail("hey everyone welcome",
                                  "hey everyone welcome back"), "back")

    def test_it_survives_the_model_changing_punctuation(self):
        self.assertEqual(new_tail("today I'm showing you",
                                  "Today I'm showing you something wild."),
                         "something wild.")

    def test_a_complete_change_of_mind_emits_everything(self):
        self.assertEqual(new_tail("one two three", "four five six"),
                         "four five six")

    def test_the_first_window_is_emitted_whole(self):
        self.assertEqual(new_tail("", "hello there"), "hello there")

    def test_an_unchanged_window_emits_nothing(self):
        self.assertEqual(new_tail("same words here", "same words here"), "")


class AgendaView(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "agenda.json"

    def tearDown(self):
        self.dir.cleanup()

    def write(self, events):
        self.path.write_text(json.dumps(events), encoding="utf-8")

    def test_countdown_units(self):
        self.assertEqual(countdown(timedelta(seconds=30)), "now")
        self.assertEqual(countdown(timedelta(minutes=12)), "in 12 min")
        self.assertTrue(countdown(timedelta(hours=3)).startswith("in 3h"))

    def test_something_already_started_says_so(self):
        self.assertIn("ago", countdown(timedelta(minutes=-5)))

    def test_next_event_leads_the_view(self):
        soon = datetime.now() + timedelta(minutes=8)
        self.write([{"start": soon.isoformat(), "title": "Standup",
                     "where": "Room 3"}])
        app = Agenda(self.path)
        attach(app)
        run(app.start())
        view = app.view(now=soon - timedelta(minutes=8))   # pin the clock
        self.assertIn("8 min", view[0])
        self.assertEqual(view[1], "Standup")

    def test_an_empty_day_is_not_an_error(self):
        self.write([])
        app = Agenda(self.path)
        attach(app)
        run(app.start())
        self.assertIn("nothing", " ".join(app.view()).lower())

    def test_a_missing_file_is_not_an_error(self):
        app = Agenda(Path("definitely-not-here.json"))
        app.load()
        self.assertEqual(app.events, [])

    def test_unparseable_rows_are_skipped_not_fatal(self):
        self.write([{"title": "no start time"},
                    {"start": "not a date", "title": "bad"},
                    {"start": (datetime.now() + timedelta(hours=1)).isoformat(),
                     "title": "Good one"}])
        app = Agenda(self.path)
        app.load()
        self.assertEqual([e["title"] for e in app.events], ["Good one"])

    def test_events_are_ordered_by_time(self):
        now = datetime.now()
        self.write([
            {"start": (now + timedelta(hours=3)).isoformat(), "title": "Late"},
            {"start": (now + timedelta(minutes=5)).isoformat(), "title": "Soon"},
        ])
        app = Agenda(self.path)
        app.load()
        self.assertEqual([e["title"] for e in app.events], ["Soon", "Late"])

    def test_rows_fit_the_panel(self):
        soon = datetime.now() + timedelta(minutes=8)
        self.write([{"start": soon.isoformat(),
                     "title": "A meeting with a very long name indeed",
                     "where": "Somewhere with a long name too"}])
        app = Agenda(self.path)
        app.load()
        for row in app.view():
            self.assertLessEqual(len(row), ROW_CHARS, row)

    def test_parse_when_handles_both_shapes(self):
        self.assertIsNotNone(parse_when("2026-10-14T09:30:00"))
        self.assertIsNotNone(parse_when("2026-10-14T09:30:00-05:00"))
        self.assertIsNone(parse_when("nonsense"))


class Steps(unittest.TestCase):
    """Synthetic head-mounted acceleration at the device's ~10 Hz."""

    @staticmethod
    def simulate(counter, seconds, cadence_hz, bump_g=0.18, tilt=0.0,
                 rate_hz=10.0):
        import math
        import random
        random.seed(1)
        for i in range(int(seconds * rate_hz)):
            t = i / rate_hz
            bump = bump_g * max(0.0, math.sin(2 * math.pi * cadence_hz * t))
            counter.feed(t, tilt + random.gauss(0, 0.02), 0.0,
                         1.0 + bump + random.gauss(0, 0.02))
        return counter.steps

    def test_a_normal_walk_counts_within_ten_percent(self):
        got = self.simulate(StepCounter(), 30, 1.8)
        self.assertAlmostEqual(got, 54, delta=6)

    def test_standing_still_counts_nothing(self):
        self.assertEqual(self.simulate(StepCounter(), 30, 0.0, bump_g=0.0), 0)

    def test_head_tilt_does_not_change_the_count(self):
        """A head-mounted sensor sees every glance at the pavement as a
        change in vertical; magnitude minus a moving baseline ignores it."""
        level = self.simulate(StepCounter(), 30, 1.8)
        tilted = self.simulate(StepCounter(), 30, 1.8, tilt=0.4)
        # Not bit-exact: tilt adds a sideways component, and magnitude is
        # a vector norm, so the same bump reads marginally smaller. What
        # must hold is that tilting does not wreck the count.
        self.assertAlmostEqual(tilted, level, delta=4)

    def test_one_footfall_cannot_count_twice(self):
        """A ragged peak must register once -- the minimum gap enforces it."""
        counter = StepCounter()
        t = 0.0
        # one bump with a jagged top, sampled finely
        for g in (1.0, 1.05, 1.12, 1.18, 1.16, 1.19, 1.17, 1.10, 1.02, 1.0):
            counter.feed(t, 0.0, 0.0, g)
            t += 0.03
        self.assertLessEqual(counter.steps, 1)

    def test_cadence_reads_zero_when_standing(self):
        self.assertEqual(StepCounter().cadence_spm, 0.0)

    def test_pace_labels_cover_the_range(self):
        self.assertEqual(pace_text(0), "standing")
        self.assertEqual(pace_text(100), "walking")
        self.assertEqual(pace_text(160), "running")

    def test_walk_view_fits_the_panel(self):
        app = Walk()
        attach(app)
        app.counter.steps = 12345
        for row in app.view():
            self.assertLessEqual(len(row), ROW_CHARS, row)

    def test_distance_is_labelled_as_an_estimate(self):
        app = Walk()
        attach(app)
        app.counter.steps = 100
        self.assertIn("~", app.view()[1])

    def test_reset_gesture_zeroes_the_count(self):
        app = Walk()
        attach(app)
        app.counter.steps = 40
        run(app.gesture(Gesture.SELECT))
        self.assertEqual(app.counter.steps, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
