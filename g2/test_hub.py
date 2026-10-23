"""Hub tests: ics / RSS / Atom / Open-Meteo parsing, scheduling with a fake clock, the push lock,
config defaults, launcher integration and a FakeTransport end-to-end run.  No hardware, no network,
no real sleeps.

    python -m unittest g2.test_hub
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import os
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from g2 import hub as hubmod  # noqa: E402
from g2.hub import (CalendarConfig, CalendarFeed, Feed, Hub, HubConfig, NewsConfig, NewsFeed, TasksConfig,  # noqa: E402
                    TasksFeed, TimeFeed, WeatherConfig, WeatherFeed, newest, parse_feed, parse_ics,
                    reading_from_open_meteo, upcoming_schedules, weather_type_for)
from g2.native import dashboard, pb, session, transport  # noqa: E402
from g2.native.framing import MagicAllocator, parse_frame  # noqa: E402
from g2.native.pb import message_field, varint_field  # noqa: E402
from notes.store import Store  # noqa: E402

NOW = datetime(2026, 10, 23, 9, 0, 0)


def local_naive(y, mo, d, h, mi) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc).astimezone().replace(tzinfo=None)


ICS = textwrap.dedent("""\
    BEGIN:VCALENDAR
    VERSION:2.0
    BEGIN:VEVENT
    UID:1
    DTSTART:20261023T100000
    DTEND:20261023T103000
    SUMMARY:Standup\\, daily
      (folded)
    LOCATION:Room 1
    BEGIN:VALARM
    TRIGGER:-PT10M
    SUMMARY:ALARM SHOULD NOT LEAK
    END:VALARM
    END:VEVENT
    BEGIN:VEVENT
    UID:2
    DTSTART:20261023T070000
    DTEND:20261023T080000
    SUMMARY:Past
    END:VEVENT
    BEGIN:VEVENT
    UID:3
    DTSTART:20261024T090000
    DTEND:20261024T093000
    SUMMARY:Tomorrow
    END:VEVENT
    BEGIN:VEVENT
    UID:4
    DTSTART;VALUE=DATE:20261023
    DTEND;VALUE=DATE:20261024
    SUMMARY:Holiday
    END:VEVENT
    BEGIN:VEVENT
    UID:5
    DTSTART:20261025T090000
    DTEND:20261025T100000
    SUMMARY:Later
    END:VEVENT
    BEGIN:VEVENT
    UID:6
    DTSTART:20261020T090000
    DTEND:20261020T100000
    RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR
    SUMMARY:Recurring (ignored)
    END:VEVENT
    BEGIN:VEVENT
    UID:7
    DTSTART:20261023T200000Z
    DTEND:20261023T210000Z
    SUMMARY:UTC call
    END:VEVENT
    BEGIN:VEVENT
    UID:8
    DTSTART:20261023T150000
    SUMMARY:No end
    END:VEVENT
    BEGIN:VEVENT
    UID:9
    SUMMARY:No start (skipped)
    END:VEVENT
    END:VCALENDAR
""")

RSS = """<?xml version="1.0"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><title>Example News</title><link>http://example.org</link>
<item><title>Old</title><description>&lt;p&gt;old &amp; cold&lt;/p&gt;</description><pubDate>Mon, 20 Oct 2026 08:00:00 GMT</pubDate></item>
<item><title>Newest</title><description><![CDATA[<b>bold</b> body]]></description><pubDate>Thu, 22 Oct 2026 18:30:00 +0000</pubDate></item>
<item><title>Middle</title><content:encoded><![CDATA[encoded body]]></content:encoded><pubDate>Wed, 21 Oct 2026 09:00:00 GMT</pubDate></item>
<item><title>Undated</title><description>no date</description></item>
</channel></rss>"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>Atom Source</title>
<entry><title>A1</title><summary>first</summary><published>2026-10-21T10:00:00Z</published></entry>
<entry><title>A2</title><content type="html">&lt;i&gt;second&lt;/i&gt;</content><updated>2026-10-23T07:00:00+02:00</updated></entry>
</feed>"""


class Clock:
    def __init__(self, t=1_792_497_600.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeSleeper:
    """Records requested delays, advances the clock, parks forever after ``stop_after`` calls."""

    def __init__(self, clock: Clock, stop_after: int, advance: bool = True):
        self.clock, self.stop_after, self.calls, self.advance = clock, stop_after, [], advance
        self.done = asyncio.Event()

    async def __call__(self, s):
        self.calls.append(s)
        if self.advance:
            self.clock.t += s
        if len(self.calls) >= self.stop_after:
            self.done.set()
            await asyncio.Event().wait()
        await asyncio.sleep(0)


class GatedSleeper:
    """Each sleep blocks on its own future until the test releases it."""

    def __init__(self):
        self.calls: list[float] = []
        self.gates: list[asyncio.Future] = []

    async def __call__(self, s):
        fut = asyncio.get_running_loop().create_future()
        self.calls.append(s)
        self.gates.append(fut)
        await fut

    def release(self, i=-1):
        if not self.gates[i].done():
            self.gates[i].set_result(None)


class StubGlasses:
    """Just the Glasses surface the hub touches; records every push."""

    def __init__(self, yields: int = 0):
        self.calls: list[tuple] = []
        self.events: list[str] = []
        self.started = False
        self.keepalive_kwargs = None
        self.on_link_lost = None
        self.alloc = MagicAllocator()
        self.tz_units = -16
        self.t = SimpleNamespace(connected=True)
        self.yields = yields

    async def start(self):
        self.started = True

    async def stop(self):
        self.started = False

    def start_keepalive(self, **kw):
        self.keepalive_kwargs = kw

    def on(self, fn):
        pass

    async def _push(self, name, *args):
        self.events.append(f"enter {name}")
        for _ in range(self.yields):
            await asyncio.sleep(0)
        self.calls.append((name, *args))
        self.events.append(f"exit {name}")

    async def set_weather(self, temperature_c, weather_type=0, status="", fahrenheit_display=False, show=True, **extra):
        await self._push("weather", temperature_c, weather_type, status, fahrenheit_display)
        return 0

    async def set_calendar(self, schedules, show=True):
        await self._push("calendar", list(schedules))
        return [0] * max(1, len(schedules))

    async def push_news(self, items, show=True):
        await self._push("news", list(items))
        return [0] * 5 if items else []

    async def set_tasks(self, tasks, show=True):
        await self._push("tasks", list(tasks))
        return True

    async def set_menu(self, builtin_ids=(1, 4, 5, 6, 7), plugins=(), timeout=4.0):
        await self._push("menu", list(builtin_ids), list(plugins))
        return True

    async def send_pb(self, sid, payload, side="right", expect=None, timeout=3.0, flag=None):
        await self._push("pb", sid, payload, side, expect)
        magic = pb.read_varint(payload, 2, 0)
        return SimpleNamespace(payload=varint_field(1, 128) + varint_field(2, magic) + message_field(128, varint_field(3, 0)))


# ═══════════════════════════════════════════════════════════════════════════════════════
# parsing
# ═══════════════════════════════════════════════════════════════════════════════════════


class TestIcs(unittest.TestCase):
    def test_parse_fields_folding_escapes_and_nested_alarm(self):
        ev = {e.summary.split(" ")[0]: e for e in parse_ics(ICS)}
        self.assertEqual(len(ev), 8)  # UID 9 has no DTSTART
        st = ev["Standup,"]
        self.assertEqual(st.summary, "Standup, daily (folded)")
        self.assertEqual((st.location, st.all_day), ("Room 1", False))
        self.assertEqual((st.start, st.end), (datetime(2026, 10, 23, 10, 0), datetime(2026, 10, 23, 10, 30)))
        self.assertNotIn("ALARM", " ".join(e.summary for e in ev.values()))

    def test_all_day_utc_and_missing_end(self):
        ev = {e.summary: e for e in parse_ics(ICS)}
        self.assertTrue(ev["Holiday"].all_day)
        self.assertEqual(ev["Holiday"].end, datetime(2026, 10, 24))
        self.assertEqual(ev["UTC call"].start, local_naive(2026, 10, 23, 20, 0))
        self.assertEqual(ev["No end"].end, ev["No end"].start)
        one = parse_ics("BEGIN:VEVENT\nDTSTART;VALUE=DATE:20261101\nSUMMARY:X\nEND:VEVENT")[0]
        self.assertEqual(one.end, datetime(2026, 11, 2))  # all-day without DTEND = one day
        dur = parse_ics("BEGIN:VEVENT\nDTSTART:20261101T100000\nDURATION:PT1H30M\nSUMMARY:X\nEND:VEVENT")[0]
        self.assertEqual(dur.end, datetime(2026, 11, 1, 11, 30))

    def test_upcoming_window_order_prefixes_and_limit(self):
        sch = upcoming_schedules(parse_ics(ICS), NOW, limit=5)
        titles = [s.title for s in sch]
        self.assertNotIn("Past", titles)
        self.assertNotIn("Later", titles)
        self.assertNotIn("Recurring (ignored)", titles)
        self.assertEqual(titles[0], "Holiday")  # all-day sorts at midnight
        self.assertEqual(sch[0].time_str, "All day")
        st = next(s for s in sch if s.title.startswith("Standup"))
        self.assertEqual(st.time_str, "10:00 AM-10:30 AM")
        self.assertEqual(st.location, "Room 1")
        self.assertEqual(st.end_ts, int(datetime(2026, 10, 23, 10, 30).timestamp()))
        self.assertEqual(len(sch), 5)  # Holiday, Standup, No end, UTC call, Tomorrow
        self.assertEqual(len(upcoming_schedules(parse_ics(ICS), NOW, limit=3)), 3)
        self.assertEqual(sch[-1].title, "Tomorrow")
        self.assertEqual(sch[-1].time_str, "Tmr 09:00 AM-09:30 AM")
        self.assertEqual(upcoming_schedules(parse_ics(ICS), NOW, limit=5, time_format="24h")[-1].time_str, "Tmr 09:00-09:30")
        self.assertEqual(upcoming_schedules([], NOW), [])

    def test_tzid_and_bad_values(self):
        txt = "BEGIN:VEVENT\nDTSTART;TZID=UTC:20261023T120000\nDTEND;TZID=Nowhere/Zone:20261023T130000\nSUMMARY:Z\nEND:VEVENT"
        e = parse_ics(txt)[0]
        self.assertEqual(e.start, local_naive(2026, 10, 23, 12, 0))
        self.assertEqual(e.end, max(datetime(2026, 10, 23, 13, 0), e.start))  # unknown zone = floating
        self.assertEqual(parse_ics("BEGIN:VEVENT\nDTSTART:garbage\nSUMMARY:X\nEND:VEVENT"), [])
        self.assertEqual(parse_ics(""), [])

    def test_folder_skips_unreadable_files(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "a.ics").write_text(ICS, encoding="utf-8")
            (Path(d) / "b.ics").write_bytes(b"\xff\xfe not a calendar")
            (Path(d) / "c.txt").write_text("BEGIN:VEVENT\nDTSTART:20261023T100000\nSUMMARY:ignored ext\nEND:VEVENT")
            self.assertEqual(len(hubmod.load_ics_folder(Path(d))), 8)


class TestNewsParsing(unittest.TestCase):
    def test_rss2_items_html_stripped_and_newest_first(self):
        entries = parse_feed(RSS, "fallback")
        self.assertEqual([e.title for e in entries], ["Old", "Newest", "Middle", "Undated"])
        self.assertTrue(all(e.source == "Example News" for e in entries))
        self.assertEqual(entries[0].content, "old & cold")
        self.assertEqual(entries[1].content, "bold body")
        self.assertEqual(entries[2].content, "encoded body")
        self.assertIsNone(entries[3].published)
        top = newest(entries, 3)
        self.assertEqual([e.title for e in top], ["Newest", "Middle", "Old"])
        self.assertEqual([e.title for e in newest(entries, 10)][-1], "Undated")
        item = top[0].item(3)
        self.assertEqual((item.title, item.source, item.content), ("Newest", "Example News", "bol"))

    def test_atom_entries(self):
        entries = parse_feed(ATOM)
        self.assertEqual([(e.title, e.source, e.content) for e in entries], [("A1", "Atom Source", "first"), ("A2", "Atom Source", "second")])
        self.assertEqual(entries[1].published.astimezone(timezone.utc).hour, 5)
        self.assertEqual(newest(entries, 1)[0].title, "A2")
        with self.assertRaises(Exception):
            parse_feed("<rss><channel><item>", "x")

    def test_news_feed_merges_urls_and_caps(self):
        g = StubGlasses()
        feed = NewsFeed(g, NewsConfig(enabled=True, urls=["http://a.test/rss", "http://b.test/atom", "http://c.test/broken"], max_items=4))
        pages = {"http://a.test/rss": RSS, "http://b.test/atom": ATOM}

        async def fetch(url):
            if url not in pages:
                raise ConnectionError("down")
            return pages[url]
        feed.fetch = fetch
        items = asyncio.run(feed.collect())
        self.assertEqual([i.title for i in items], ["A2", "Newest", "A1", "Middle"])
        self.assertIsInstance(items[0], dashboard.NewsItem)
        feed.fetch = lambda url: fetch("http://c.test/broken")
        self.assertIsNone(asyncio.run(feed.collect()))  # all sources down: keep what the glasses show


class TestOpenMeteo(unittest.TestCase):
    def test_code_mapping(self):
        T = {n: i for i, n in enumerate(dashboard.WEATHER_TYPES)}
        self.assertEqual(weather_type_for(0, True), T["Sunny"])
        self.assertEqual(weather_type_for(1, False), T["Clear night"])
        self.assertEqual(weather_type_for(2), T["Cloudy"])
        self.assertEqual(weather_type_for(3, False), T["Cloudy"])
        self.assertEqual(weather_type_for(45), T["Fog"])
        self.assertEqual(weather_type_for(51), T["Drizzle"])
        self.assertEqual(weather_type_for(55), T["Heavy drizzle"])
        self.assertEqual(weather_type_for(56), T["Freezing rain"])
        self.assertEqual(weather_type_for(61), T["Rain"])
        self.assertEqual(weather_type_for(65), T["Heavy rain"])
        self.assertEqual(weather_type_for(73), T["Snow"])
        self.assertEqual(weather_type_for(82), T["Heavy rain"])
        self.assertEqual(weather_type_for(95), T["Thunderstorm"])
        self.assertEqual(weather_type_for(99), T["Thunderstorm"])
        self.assertEqual(weather_type_for(123), T["Unknown"])
        self.assertEqual(weather_type_for(None), T["Unknown"])
        self.assertEqual(hubmod.weather_status_for(0, False), "Clear night")
        self.assertEqual(hubmod.weather_status_for(2), "Partly cloudy")
        self.assertTrue(all(len(s) <= 32 for s in hubmod.WMO_DESCRIPTIONS.values()))

    def test_reading_from_json_both_shapes(self):
        r = reading_from_open_meteo({"current": {"temperature_2m": 12.3, "weather_code": 61, "is_day": 1}})
        self.assertEqual((r.temperature_c, r.weather_type, r.status, r.code), (12.3, 5, "Light rain", 61))
        r = reading_from_open_meteo({"current_weather": {"temperature": -1, "weathercode": 0, "is_day": 0}})
        self.assertEqual((r.temperature_c, r.weather_type, r.status), (-1.0, 16, "Clear night"))
        with self.assertRaises(ValueError):
            reading_from_open_meteo({"hourly": {}})

    def test_weather_feed_collect_and_push(self):
        g = StubGlasses()
        feed = WeatherFeed(g, WeatherConfig(enabled=True, latitude=51.5, longitude=-0.1, units="fahrenheit"))

        async def fetch():
            return {"current": {"temperature_2m": 21.0, "weather_code": 1, "is_day": 1}}
        feed.fetch = fetch

        async def go():
            payload = await feed.collect()
            return await feed.push(payload)
        self.assertEqual(asyncio.run(go()), 0)
        self.assertEqual(g.calls, [("weather", 21.0, 1, "Mainly clear", True)])
        self.assertEqual(WeatherFeed.describe(0), "ack flag 0 (SUCCESS)")
        self.assertTrue(WeatherFeed.ok(0) and not WeatherFeed.ok(1) and not WeatherFeed.ok(None))
        bare = WeatherFeed(g, WeatherConfig(enabled=True))
        with self.assertRaises(ValueError):
            asyncio.run(bare.collect())


# ═══════════════════════════════════════════════════════════════════════════════════════
# config
# ═══════════════════════════════════════════════════════════════════════════════════════


class TestConfig(unittest.TestCase):
    def test_defaults_are_offline(self):
        c = HubConfig()
        self.assertFalse(c.weather.enabled)
        self.assertFalse(c.news.enabled)
        self.assertTrue(c.menu.enabled)
        self.assertEqual((c.menu.plugin_name, c.menu.plugin_app_id, c.menu.delay_s, c.menu.repush_delay_s), ("Laptop", 10421, 5.0, 3.0))
        self.assertEqual(c.menu.plugin_items(), [hubmod.menu_mod.PluginItem("Laptop", 10421, 1)])
        self.assertIsNone(hubmod.menu_mod.validate(c.menu.builtin, c.menu.plugin_items()))
        self.assertEqual(c.news.urls, [])
        self.assertIsNone(c.weather.latitude)
        self.assertTrue(c.time.enabled and c.calendar.enabled and c.tasks.enabled and c.keepalive.enabled)
        self.assertEqual((c.time.interval_s, c.weather.interval_s, c.calendar.interval_s, c.news.interval_s), (3600, 900, 900, 900))
        self.assertEqual((c.tasks.poll_s, c.tasks.repush_s, c.tasks.max_items), (60, 900, 20))
        self.assertEqual(c.calendar.folder, "g2/data/calendar")
        self.assertEqual(c.calendar_folder(), ROOT / "g2" / "data" / "calendar")
        self.assertEqual(c.menu.builtin, [7, 6, 5, 8, 11, 4, 266, 1])
        self.assertEqual(HubConfig.from_dict({}), c)
        self.assertEqual(HubConfig.from_dict(None), c)

    def test_example_file_documents_the_defaults(self):
        self.assertEqual(HubConfig.load(ROOT / "g2" / "config.example.yaml"), HubConfig())

    def test_load_yaml_overrides_and_missing_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "hub.yaml"
            p.write_text(textwrap.dedent("""\
                gateway: http://127.0.0.1:9999
                jitter_s: 0
                weather: {enabled: true, latitude: 51.5, longitude: -0.1, units: fahrenheit}
                news:
                  enabled: true
                  urls: [http://a.test/rss]
                calendar: {folder: cal, time_format: 24h}
                menu: {enabled: true, plugin_name: Board, plugin_app_id: 10421, plugins: [{name: Extra, app_id: 100}]}
                bogus: 1
                tasks: {path: t.json, unknown_key: 2}
            """), encoding="utf-8")
            c = HubConfig.load(p)
            self.assertEqual(c.gateway, "http://127.0.0.1:9999")
            self.assertEqual((c.weather.enabled, c.weather.latitude, c.weather.longitude, c.weather.units), (True, 51.5, -0.1, "fahrenheit"))
            self.assertTrue(c.weather.configured)
            self.assertEqual(c.news.urls, ["http://a.test/rss"])
            self.assertEqual(c.calendar_folder(), ROOT / "cal")
            self.assertEqual(c.calendar.time_format, "24h")
            self.assertEqual(c.calendar.interval_s, 900)  # untouched sibling keeps its default
            self.assertEqual(c.menu.plugin_items(), [hubmod.menu_mod.PluginItem("Board", 10421, 1), hubmod.menu_mod.PluginItem("Extra", 100, 1)])
            self.assertEqual(c.tasks_path(), ROOT / "t.json")
            self.assertEqual(HubConfig.load(Path(d) / "absent.yaml"), HubConfig())

    def test_hub_builds_only_enabled_and_configured_feeds(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "thoughts.json")
            h = Hub(StubGlasses(), HubConfig(), store=store)
            self.assertEqual(set(h.feeds), {"time", "calendar", "tasks"})
            c = HubConfig.from_dict({"weather": {"enabled": True}, "news": {"enabled": True}, "time": {"enabled": False}})
            h = Hub(StubGlasses(), c, store=store)
            self.assertEqual(set(h.feeds), {"calendar", "tasks"})  # weather without lat/lon, news without urls
            c.weather.latitude, c.weather.longitude, c.news.urls = 1.0, 2.0, ["http://x.test/f"]
            h = Hub(StubGlasses(), c, store=store)
            self.assertEqual(set(h.feeds), {"calendar", "tasks", "weather", "news"})
            self.assertEqual(h.feeds["weather"].interval_s, 900)
            with self.assertRaises(KeyError):
                h.make_feed("stocks")


# ═══════════════════════════════════════════════════════════════════════════════════════
# scheduling, lock, tasks, menu, launcher
# ═══════════════════════════════════════════════════════════════════════════════════════


class CountingFeed(Feed):
    name = "counting"

    def __init__(self, glasses, interval_s=10.0, fail_first=False):
        super().__init__(glasses)
        self.interval_s = interval_s
        self.fail_first = fail_first
        self.collected = 0
        self.pushed = []

    async def collect(self):
        self.collected += 1
        if self.fail_first and self.collected == 1:
            raise RuntimeError("boom")
        return self.collected

    async def push(self, payload):
        self.pushed.append(payload)
        return payload


class TestScheduling(unittest.IsolatedAsyncioTestCase):
    def _hub(self, stop_after, jitter=0.0, rand=lambda: 0.0, stagger=0.0):
        clock = Clock()
        sleeper = FakeSleeper(clock, stop_after)
        cfg = HubConfig.from_dict({"jitter_s": jitter, "stagger_s": stagger, "time": {"enabled": False},
                                   "calendar": {"enabled": False}, "tasks": {"enabled": False}, "menu": {"enabled": False}})
        h = Hub(StubGlasses(), cfg, clock=clock, sleep=sleeper, rand=rand)
        return h, sleeper, clock

    async def test_feed_runs_on_interval_with_fake_clock(self):
        h, sleeper, clock = self._hub(stop_after=4)
        feed = CountingFeed(h.glasses, interval_s=10)
        h.feeds[feed.name] = feed
        await h.start()
        await asyncio.wait_for(sleeper.done.wait(), 2)
        await h.stop()
        self.assertEqual(sleeper.calls, [0.0, 10.0, 10.0, 10.0])
        self.assertEqual(feed.pushed, [1, 2, 3])
        self.assertEqual(h.runs["counting"], 3)
        self.assertTrue(h.results["counting"].ok)
        self.assertEqual(clock.t, 1_792_497_630.0)

    async def test_jitter_stagger_and_error_retry(self):
        h, sleeper, _ = self._hub(stop_after=3, jitter=4.0, rand=lambda: 0.5, stagger=3.0)
        bad = CountingFeed(h.glasses, interval_s=20, fail_first=True)
        h.feeds["a"] = CountingFeed(h.glasses, interval_s=20)
        h.feeds[bad.name] = bad
        await h.start()
        await asyncio.wait_for(sleeper.done.wait(), 2)
        first = sorted(sleeper.calls[:2])
        self.assertEqual(first, [2.0, 5.0])  # stagger 0 / 3 s, plus 0.5 * 4 s jitter
        # let the failing feed reach its second run
        for _ in range(20):
            await asyncio.sleep(0)
            if bad.collected >= 2:
                break
        await h.stop()
        self.assertGreaterEqual(bad.collected, 1)
        res = h.results["counting"]
        if bad.collected == 1:
            self.assertFalse(res.ok)
            self.assertIn("RuntimeError: boom", res.error)
        else:
            self.assertTrue(res.ok)  # retried on the next interval and recovered
            self.assertEqual(bad.pushed, [2])

    async def test_refresh_forces_and_builds_disabled_feed(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "x.ics").write_text(ICS, encoding="utf-8")
            cfg = HubConfig.from_dict({"calendar": {"enabled": False, "folder": d}, "time": {"enabled": False},
                                       "tasks": {"enabled": False}})
            h = Hub(StubGlasses(), cfg, now=lambda: NOW)
            self.assertEqual(h.feeds, {})
            out = await h.refresh("calendar")
            self.assertTrue(out["calendar"].ok)
            self.assertEqual(out["calendar"].detail, "ack flags 0,0,0,0,0")
            self.assertEqual(len(h.glasses.calls[0][1]), 5)
            self.assertFalse((await h.refresh("weather"))["weather"].ok)  # no lat/lon: error, not an exception
            self.assertIn("latitude", h.results["weather"].error)

    async def test_pushes_never_interleave_under_the_lock(self):
        g = StubGlasses(yields=3)
        cfg = HubConfig.from_dict({"weather": {"enabled": True, "latitude": 1, "longitude": 2}, "time": {"enabled": False},
                                   "tasks": {"enabled": False}, "calendar": {"enabled": False}})
        h = Hub(g, cfg, now=lambda: NOW)
        w = h.feeds["weather"]

        async def fetch():
            return {"current": {"temperature_2m": 5, "weather_code": 3, "is_day": 1}}
        w.fetch = fetch
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "x.ics").write_text(ICS, encoding="utf-8")
            c = CalendarFeed(g, CalendarConfig(), now=lambda: NOW, folder=Path(d))
            t = TimeFeed(g, clock=Clock())
            await asyncio.gather(h.run_feed(w, True), h.run_feed(c, True), h.run_feed(t, True))
        ev = g.events
        self.assertEqual(len(ev), 6)
        for i in range(0, 6, 2):
            self.assertTrue(ev[i].startswith("enter ") and ev[i + 1] == ev[i].replace("enter", "exit"), ev)
        self.assertTrue(all(r.ok for r in h.results.values()), h.results)
        # without the lock the same pushes do interleave (proves the test bites)
        g2 = StubGlasses(yields=3)
        await asyncio.gather(g2.set_weather(1), g2.set_calendar([]))
        self.assertEqual(g2.events[:2], ["enter weather", "enter calendar"])

    async def test_time_feed_sends_time_sync_and_keepalive_skips_its_own(self):
        g = StubGlasses()
        clock = Clock(1_792_497_600.0)
        cfg = HubConfig.from_dict({"calendar": {"enabled": False}, "tasks": {"enabled": False}, "stagger_s": 0, "menu": {"enabled": False}})
        sleeper = FakeSleeper(clock, 1)
        h = Hub(g, cfg, clock=clock, sleep=sleeper)
        await h.start()
        self.assertEqual(h.feeds["time"].interval_s, 3600)
        self.assertEqual(g.keepalive_kwargs, {"time_sync_every": float("inf")})
        res = await h.refresh("time")
        await h.stop()
        self.assertTrue(res["time"].ok)
        self.assertEqual(res["time"].detail, "ack result=0")
        kind, sid, payload, side, expect = g.calls[-1]
        self.assertEqual((kind, sid, side, expect), ("pb", session.SID_DEVICE, "right", "ack"))
        m = session.parse_device(payload)
        self.assertEqual((m.cmd, m.timestamp, m.timezone), (session.CMD_TIME_SYNC, 1_792_497_600, -16))
        self.assertFalse(g.started)

    async def test_menu_push_ordering_after_gateway_and_coalescing(self):
        g = StubGlasses()
        cfg = HubConfig.from_dict({"time": {"enabled": False}, "calendar": {"enabled": False}, "tasks": {"enabled": False},
                                   "keepalive": {"enabled": False}})
        sleeper = GatedSleeper()
        h = Hub(g, cfg, sleep=sleeper)
        await h.start()
        await asyncio.sleep(0)
        self.assertEqual(sleeper.calls, [5.0])            # armed at start, nothing pushed yet
        self.assertEqual(g.calls, [])
        h.on_connection(True)                               # the launcher forwards the gateway's ready event
        await asyncio.sleep(0)
        self.assertEqual(sleeper.calls, [5.0, 5.0])
        self.assertTrue(sleeper.gates[0].cancelled())       # the earlier arm was replaced
        # the glasses ack the gateway's own menu push: a SID 0x03 reply we did not ask for
        reply = parse_frame(transport.rx_frame(0x03, varint_field(1, 1) + varint_field(2, 77)))
        h._on_inbound("right", reply)
        await asyncio.sleep(0)
        self.assertEqual(sleeper.calls, [5.0, 5.0, 3.0])
        self.assertEqual(g.calls, [])
        sleeper.release(2)
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertEqual(g.calls, [("menu", [7, 6, 5, 8, 11, 4, 266, 1], [hubmod.menu_mod.PluginItem("Laptop", 10421, 1)])])
        self.assertTrue(h.menu_pushed)
        # our own reply while the push is in flight must not re-arm; a later foreign one does
        h._menu_in_flight = True
        h._on_inbound("right", reply)
        await asyncio.sleep(0)
        self.assertEqual(len(sleeper.calls), 3)
        h._menu_in_flight = False
        h._on_inbound("right", reply)
        await asyncio.sleep(0)
        self.assertEqual(sleeper.calls[-1], 3.0)
        h.on_connection(False)                              # link lost: pending push dropped
        await asyncio.sleep(0)
        self.assertTrue(sleeper.gates[-1].cancelled())
        await h.stop()
        self.assertEqual(len(g.calls), 1)

    async def test_menu_disabled_pushes_nothing(self):
        g = StubGlasses()
        cfg = HubConfig.from_dict({"time": {"enabled": False}, "calendar": {"enabled": False}, "tasks": {"enabled": False},
                                   "keepalive": {"enabled": False}, "menu": {"enabled": False}})
        h = Hub(g, cfg, sleep=GatedSleeper())
        await h.start()
        h.on_connection(True)
        await asyncio.sleep(0)
        await h.stop()
        self.assertEqual(g.calls, [])
        self.assertIsNone(h.menu_pushed)
        self.assertIsNone(g.keepalive_kwargs)

    def test_plugin_app_id_matches_the_gateway_launch_entry(self):
        gw_dir = ROOT / "g2" / "vendor" / "men-g2-ble-gateway"
        if not (gw_dir / "mentraos" / "g2" / "protocol" / "menu.py").exists():
            self.skipTest("gateway vendor checkout not present")
        sys.path.insert(0, str(gw_dir))
        try:
            from mentraos.g2.protocol import menu as gw_menu
        finally:
            sys.path.remove(str(gw_dir))
        self.assertEqual(gw_menu.package_name_to_app_id("men-g2-ble-gateway.launch"), hubmod.GATEWAY_LAUNCH_APP_ID)

    async def test_native_app_events_reach_the_hook(self):
        from g2.native import apps as napps, even_ai
        g = StubGlasses()
        h = Hub(g, HubConfig.from_dict({"time": {"enabled": False}, "calendar": {"enabled": False}, "tasks": {"enabled": False}}))
        seen = []
        h.on_native_app(seen.append)

        async def async_cb(ev):
            seen.append(("async", ev.kind))
        h.on_native_app(async_cb)

        def rx(sid, payload):
            return parse_frame(transport.rx_frame(sid, payload))
        h._on_inbound("right", rx(0x0D, varint_field(1, 1) + varint_field(2, 5) + message_field(3, varint_field(1, 1) + varint_field(2, 8))))
        h._on_inbound("right", rx(napps.SID_NAVIGATION, varint_field(1, napps.NAV_CMD_MENU_STARTUP) + varint_field(2, 9)))
        h._on_inbound("right", rx(napps.SID_TELEPROMPTER, varint_field(1, napps.TP_CMD_FILE_LIST_REQUEST) + varint_field(2, 9)))
        h._on_inbound("right", rx(napps.SID_TRANSLATE, varint_field(1, napps.TR_CMD_LANG_SWITCH_REQ) + varint_field(2, 9)))
        h._on_inbound("right", rx(even_ai.SID_EVEN_AI, even_ai.ctrl(9, even_ai.CTRL_WAKE_UP)))
        h._on_inbound("right", rx(even_ai.SID_EVEN_AI, even_ai.ctrl(9, even_ai.CTRL_EXIT)))          # not a start
        h._on_inbound("right", rx(0xE0, varint_field(1, 17) + varint_field(2, 9) + message_field(20, varint_field(1, 10421))))
        h._on_inbound("right", rx(0x01, b"\x08\x02\x10\x05"))                                       # unrelated ack
        h._on_inbound("right", rx(0x0D, b"\xff"))                                                      # garbage: no crash
        await asyncio.sleep(0)
        sync = [e for e in seen if not isinstance(e, tuple)]
        self.assertEqual([(e.kind, e.sid, e.name) for e in sync],
                         [("foreground", 8, "Navigate"), ("startup", 8, "Navigate"), ("startup", 6, "Teleprompt"),
                          ("startup", 5, "Translate"), ("startup", 7, "Even AI"), ("plugin", 0xE0, "plugin")])
        self.assertEqual(sync[-1].app_id, 10421)
        self.assertEqual(len([e for e in seen if isinstance(e, tuple)]), 6)
        self.assertEqual(len(h.native_events), 6)

    async def test_tasks_feed_repushes_on_mtime_change_and_repush_interval(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "thoughts.json"
            store = Store(path)
            store.add("remind me to buy milk")
            g = StubGlasses()
            clock = Clock()
            feed = TasksFeed(g, TasksConfig(repush_s=900), store, clock)
            h = Hub(g, HubConfig.from_dict({"time": {"enabled": False}, "calendar": {"enabled": False}}), store=store, clock=clock)
            self.assertTrue(feed.changed())
            r = await h.run_feed(feed)
            self.assertTrue(r.ok and not r.skipped)
            self.assertEqual([t.title for t in g.calls[-1][1]], ["Buy milk"])
            self.assertEqual(g.calls[-1][1][0], dashboard.Task(1, "Buy milk"))
            self.assertFalse(feed.changed())
            self.assertTrue((await h.run_feed(feed)).skipped or len(g.calls) == 1)
            self.assertEqual(len(g.calls), 1)
            # another process appends a thought (mtime bumped explicitly: Windows mtime is coarse)
            Store(path).add("call the landlord")
            os.utime(path, (clock.t + 100, clock.t + 100))
            self.assertTrue(feed.changed())
            await h.run_feed(feed)
            self.assertEqual([t.title for t in g.calls[-1][1]], ["Buy milk", "Call the landlord"])
            self.assertFalse(feed.changed())
            clock.t += 901
            self.assertTrue(feed.changed())  # periodic full re-push
            self.assertEqual(h.feeds["tasks"].interval_s, 60)

    async def test_launcher_exposes_ctx_native_and_starts_stops_hub(self):
        from g2.fakes import FakeGateway
        from g2.launcher import Launcher

        class HubStub:
            def __init__(self):
                self.glasses = object()
                self.log = []

            async def start(self):
                self.log.append("start")

            async def stop(self):
                self.log.append("stop")

        gw = FakeGateway()
        la = Launcher(gw, None, None, None, apps=[], clock=Clock())
        self.assertIsNone(la.ctx.hub)
        self.assertIsNone(la.ctx.native)
        hs = HubStub()
        la = Launcher(gw, None, None, None, apps=[], clock=Clock(), hub=hs)
        self.assertIs(la.ctx.hub, hs)
        self.assertIs(la.ctx.native, hs.glasses)
        await la.start()
        await la.stop()
        self.assertEqual(hs.log, ["start", "stop"])

    async def test_start_parked_shows_native_dashboard_until_launched(self):
        from g2.fakes import FakeGateway
        from g2.launcher import Launcher

        class HubStub:
            glasses = object()

            def __init__(self):
                self.conn = []

            async def start(self):
                pass

            async def stop(self):
                pass

            def on_connection(self, ready):
                self.conn.append(ready)

        gw = FakeGateway()
        clock = Clock()
        hs = HubStub()
        la = Launcher(gw, None, None, None, apps=[], clock=clock, hub=hs, start_parked=True)
        self.assertEqual(la.screen, "parked")
        await la.start()
        self.assertEqual(gw.shown, [])                                   # nothing drawn on the board
        self.assertEqual(gw.pages, [{"shutdown": True, "exit_mode": 1}])
        await gw.emit("connection", True, "ready")                       # first snapshot: no redraw, no second POST within 5 s
        self.assertEqual(gw.shown, [])
        self.assertEqual(len(gw.pages), 1)
        self.assertEqual(hs.conn, [True])
        await gw.emit("reinit", "page_parked")                           # the gateway's answer to our shutdown
        await gw.emit("touch", "single_tap", 1)                          # taps while parked do nothing
        await gw.emit("battery", 50, False)
        await la.tick()
        self.assertEqual(gw.shown, [])
        self.assertEqual(la.screen, "parked")
        await gw.emit("reinit", "dashboard_launch")                      # user picked Laptop from the glasses menu
        self.assertEqual(la.screen, "home")
        self.assertEqual(len(gw.shown), 1)
        await gw.emit("reinit", "page_parked")                           # double tap out: parked again
        self.assertEqual(la.screen, "parked")
        clock.t += 10
        await gw.emit("connection", False, "away")
        await gw.emit("connection", True, "ready")                       # reconnect while parked: park again
        self.assertEqual(len(gw.pages), 2)
        self.assertEqual(len(gw.shown), 1)
        self.assertEqual(hs.conn, [True, False, True])
        await gw.emit("reinit", "page_create")                           # POST /api/page create -> board
        self.assertEqual(la.screen, "home")
        await la.stop()
        # --board (start_parked=False) keeps the old behaviour
        gw2 = FakeGateway()
        la2 = Launcher(gw2, None, None, None, apps=[], clock=Clock(), start_parked=False)
        await la2.start()
        self.assertEqual((la2.screen, gw2.pages, len(gw2.shown)), ("home", [], 1))
        await la2.stop()

    async def test_run_once_prints_acks(self):
        g = StubGlasses()
        cfg = HubConfig.from_dict({"time": {"enabled": False}, "calendar": {"enabled": False}, "tasks": {"enabled": False},
                                   "weather": {"enabled": True, "latitude": 1, "longitude": 2}})
        h = Hub(g, cfg)

        async def fetch():
            return {"current": {"temperature_2m": 9, "weather_code": 71, "is_day": 1}}
        h.feeds["weather"].fetch = fetch
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = await hubmod.run_once(h, "all")
        self.assertEqual(rc, 0)
        self.assertIn("OK  ack flag 0 (SUCCESS)", buf.getvalue())
        self.assertFalse(g.started)


# ═══════════════════════════════════════════════════════════════════════════════════════
# end to end on FakeTransport
# ═══════════════════════════════════════════════════════════════════════════════════════


def responder_with_time_sync_ack():
    """fake_glasses_responder plus an ack for 0x80 cmd 128 (it only answers cmd 4 and 14)."""
    base = transport.fake_glasses_responder()

    def respond(side, p, raw):
        out = base(side, p, raw)
        if out is None and p.sid == session.SID_DEVICE and dashboard.read_cmd(p.payload) == session.CMD_TIME_SYNC and side == transport.RIGHT:
            magic = pb.read_varint(p.payload, 2, 0)
            head = varint_field(1, session.CMD_TIME_SYNC) + varint_field(2, magic)
            return [(side, transport.rx_frame(session.SID_DEVICE, head + message_field(128, varint_field(3, 0))))]
        return out
    return respond


class TestEndToEnd(unittest.IsolatedAsyncioTestCase):
    async def test_weather_calendar_tasks_time_pushed_and_acked(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "cal").mkdir()
            (Path(d) / "cal" / "work.ics").write_text(ICS, encoding="utf-8")
            store = Store(Path(d) / "thoughts.json")
            store.add("Buy milk")
            store.add("Eggs")
            clock = Clock(1_792_497_600.0)
            t = transport.FakeTransport(responder_with_time_sync_ack())
            g = transport.Glasses(t, MagicAllocator(), clock=clock, tz_units=-16)
            g.time_scale = 0.01
            cfg = HubConfig.from_dict({"weather": {"enabled": True, "latitude": 51.5, "longitude": -0.1},
                                       "calendar": {"folder": str(Path(d) / "cal"), "max_events": 3},
                                       "menu": {"enabled": True}, "jitter_s": 0})
            sleeper = FakeSleeper(clock, 1, advance=False)   # park every loop (feeds, menu arm) at its first sleep
            h = Hub(g, cfg, store=store, clock=clock, now=lambda: NOW, sleep=sleeper)

            async def fetch():
                return {"current": {"temperature_2m": 21.0, "weather_code": 0, "is_day": 1}}
            h.feeds["weather"].fetch = fetch
            await h.start()
            self.assertIsNone(h.menu_pushed)   # armed for menu.delay_s, not pushed yet (sleeper parks)
            self.assertTrue(await h.push_menu())
            self.assertIsNotNone(g._ka)
            results = await h.refresh()
            await h.stop()

        self.assertEqual(set(results), {"time", "weather", "calendar", "tasks"})
        for name, r in results.items():
            self.assertTrue(r.ok, f"{name}: {r}")
        self.assertEqual(results["weather"].detail, "ack flag 0 (SUCCESS)")
        self.assertEqual(results["calendar"].detail, "ack flags 0,0,0")
        self.assertFalse(g.heartbeat_running)
        self.assertGreaterEqual(g.hb_count, 1)
        dash = t.payloads("right", 0x01)
        weather = [p for p in dash if pb.read_bytes(p, 4) and pb.read_bytes(pb.read_bytes(p, 4), 3) and pb.read_bytes(pb.read_bytes(pb.read_bytes(p, 4), 3), 1)]
        self.assertEqual(len(weather), 1)
        ws = pb.read_bytes(pb.read_bytes(pb.read_bytes(pb.read_bytes(weather[0], 4), 3), 1), 1)
        self.assertEqual(pb.read_string(ws, 5), "Clear sky")
        self.assertEqual(pb.read_varint(ws, 3, -1), 1)  # Sunny
        cal = [p for p in dash if pb.read_bytes(p, 4) and pb.read_bytes(pb.read_bytes(p, 4), 3) and pb.read_bytes(pb.read_bytes(pb.read_bytes(p, 4), 3), 2)]
        self.assertEqual(len(cal), 3)
        titles = [pb.read_string(pb.read_bytes(pb.read_bytes(pb.read_bytes(pb.read_bytes(pb.read_bytes(p, 4), 3), 2), 3), 3), 2) for p in cal]
        self.assertEqual(titles[0], "Holiday")
        ql = t.payloads("right", 0x0C)
        self.assertEqual(len(ql), 1)
        ticks = dashboard.parse_quicklist_ticks(ql[0])
        self.assertEqual([x.title for x in ticks], ["Buy milk", "Eggs"])
        menus = t.payloads("right", 0x03)
        self.assertEqual(len(menus), 1)
        items = pb.read_messages(pb.read_bytes(menus[0], 3), 2)
        self.assertEqual(len(items), 9)
        self.assertEqual([pb.read_varint(i, 4) for i in items][-1], 10421)
        self.assertEqual(pb.read_bytes(items[-1], 3), b"Laptop")
        ts = [p for p in t.payloads("right", 0x80) if dashboard.read_cmd(p) == session.CMD_TIME_SYNC]
        self.assertEqual(len(ts), 1)
        self.assertEqual(session.parse_device(ts[0]).timestamp, 1_792_497_600)
        magics = [pb.read_varint(p, 2, -1) for p in dash + ql]
        self.assertEqual(len(magics), len(set(magics)))


if __name__ == "__main__":
    unittest.main()
