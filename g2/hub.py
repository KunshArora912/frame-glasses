"""The native dashboard hub: feeds the glasses' built-in watchface (time, weather, calendar,
news, quick-list tasks) from data on this laptop, owns the one native ``Glasses`` instance of
the process and keeps the link alive.  ROADMAP.md "Architecture".

    python g2/hub.py                     # standalone: all configured feeds, keep-alive, forever
    python g2/hub.py --once weather      # connect through the gateway, run ONE feed, print acks, exit
    python g2/hub.py --once all          # every configured feed once (hardware check)

Configuration: ``g2/config/hub.yaml`` (gitignored; copy ``g2/config.example.yaml``).  Everything
that touches the network is off by default.  Nothing talks to Even.

Choreography (native-protocol.md sections 1, 2, 8): the gateway has already authenticated and
run the prelude, so the hub only pushes.  Every push goes through the ``Glasses`` helper, which
starts the EvenHub heartbeat, writes, waits for the ack and stops the heartbeat again (the
native dashboard retakes the screen ~5 s later).  ``Hub.lock`` serialises pushes so two feeds
never interleave that start/push/stop sequence.  Keep-alive (60 s, left then right) runs in
``Glasses.keepalive_loop``; the hourly time sync is a feed here so it is acked and visible in
``hub.results``.

Feeds are small classes: ``collect()`` gathers the payload (network/disk, outside the lock),
``push(payload)`` hands it to the matching ``Glasses`` method (inside the lock).  Each feed runs
in its own asyncio task on ``interval_s`` plus jitter; an error is logged and retried on the
next interval.  ``await hub.refresh(name)`` forces one run.
"""
from __future__ import annotations

import argparse
import asyncio
import email.utils
import html
import logging
import math
import random
import re
import sys
import time as time_mod
import xml.etree.ElementTree as ET
from dataclasses import MISSING, dataclass, field, fields, is_dataclass
from datetime import datetime, date, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from g2.native import apps as native_apps, dashboard, even_ai, pb, session  # noqa: E402
from g2.native import menu as menu_mod  # noqa: E402
from g2.native.framing import ParsedFrame  # noqa: E402
from g2.native.transport import RIGHT, Glasses  # noqa: E402

log = logging.getLogger("g2.hub")

DEFAULT_CONFIG_PATH = HERE / "config" / "hub.yaml"
FEED_NAMES = ("time", "weather", "calendar", "news", "tasks")


# ═══════════════════════════════════════════════════════════════════════════════════════
# configuration
# ═══════════════════════════════════════════════════════════════════════════════════════


@dataclass
class KeepaliveConfig:
    enabled: bool = True
    retry_s: float = 60.0          # re-arm the keep-alive this long after two missed echoes


#: Even's built-in menu, the order the app shows: Even AI, Teleprompt, Translate, Navigate, Conversate,
#: Notifications, Silent mode, Dashboard.  Terminal (48) is a known id but left out on purpose.
FULL_BUILTIN_MENU = (menu_mod.EVEN_AI, menu_mod.TELEPROMPT, menu_mod.TRANSLATE, menu_mod.NAVIGATE, menu_mod.CONVERSATE,
                     menu_mod.NOTIFICATIONS, menu_mod.SILENT_MODE, menu_mod.DASHBOARD)
#: The gateway's own "Launch" entry: mentraos/g2/protocol/menu.py package_name_to_app_id("men-g2-ble-gateway.launch").
#: Reusing it keeps the gateway's 0xE0 cmd-17 launch handling (page rebuild + dashboard_launch reinit) working.
GATEWAY_LAUNCH_APP_ID = 10421


@dataclass
class MenuConfig:
    enabled: bool = True           # replace the glasses menu with Even's built-ins + one "Laptop" entry for our board
    builtin: list[int] = field(default_factory=lambda: list(FULL_BUILTIN_MENU))
    plugin_name: str = "Laptop"    # our board's entry (<= 31 chars); empty = no plugin entry
    plugin_app_id: int = GATEWAY_LAUNCH_APP_ID
    plugins: list[dict] = field(default_factory=list)   # extra entries [{name: "...", app_id: 100, icon: 1}]
    delay_s: float = 5.0           # push this long after start / the gateway reports ready (after ITS menu push)
    repush_delay_s: float = 3.0    # push again this long after a SID 0x03 reply we did not ask for is seen

    def plugin_items(self) -> list[menu_mod.PluginItem]:
        items = [menu_mod.PluginItem(self.plugin_name, int(self.plugin_app_id))] if self.plugin_name else []
        items += [menu_mod.PluginItem(str(p.get("name", "")), int(p["app_id"]), int(p.get("icon", menu_mod.PLUGIN_ICON)))
                  for p in self.plugins]
        return items


@dataclass
class TimeConfig:
    enabled: bool = True
    interval_s: float = session.TIME_SYNC_INTERVAL_S   # 3600


@dataclass
class WeatherConfig:
    enabled: bool = False          # network: Open-Meteo, lat/lon only (no key, no account)
    latitude: float | None = None
    longitude: float | None = None
    units: str = "celsius"         # celsius | fahrenheit (display only; the value sent is always Celsius)
    interval_s: float = 900.0
    timeout_s: float = 10.0
    endpoint: str = "https://api.open-meteo.com/v1/forecast"

    @property
    def configured(self) -> bool:
        return self.latitude is not None and self.longitude is not None


@dataclass
class CalendarConfig:
    enabled: bool = True           # local files only
    folder: str = "g2/data/calendar"
    interval_s: float = 900.0
    max_events: int = 5
    time_format: str = "12h"       # 12h | 24h


@dataclass
class NewsConfig:
    enabled: bool = False          # network: the RSS/Atom URLs you list, nothing else
    urls: list[str] = field(default_factory=list)
    interval_s: float = 900.0
    max_items: int = 5
    timeout_s: float = 10.0
    content_chars: int = 600


@dataclass
class TasksConfig:
    enabled: bool = True           # local: notes Store (thoughts.json)
    poll_s: float = 60.0           # how often the file mtime is checked
    repush_s: float = 900.0        # full re-push even when unchanged
    max_items: int = dashboard.QL_MAX_ITEMS
    path: str | None = None        # thoughts.json; null = the notes default


@dataclass
class HubConfig:
    gateway: str = "http://127.0.0.1:8765"
    jitter_s: float = 5.0          # random 0..jitter_s added to every interval
    stagger_s: float = 3.0         # first runs are spaced this far apart
    keepalive: KeepaliveConfig = field(default_factory=KeepaliveConfig)
    menu: MenuConfig = field(default_factory=MenuConfig)
    time: TimeConfig = field(default_factory=TimeConfig)
    weather: WeatherConfig = field(default_factory=WeatherConfig)
    calendar: CalendarConfig = field(default_factory=CalendarConfig)
    news: NewsConfig = field(default_factory=NewsConfig)
    tasks: TasksConfig = field(default_factory=TasksConfig)

    @classmethod
    def from_dict(cls, d: dict | None) -> "HubConfig":
        return _build(cls, d or {}, "")

    @classmethod
    def load(cls, path: str | Path | None = None) -> "HubConfig":
        """Read ``g2/config/hub.yaml`` (or ``path``); a missing file means defaults."""
        p = Path(path) if path else DEFAULT_CONFIG_PATH
        if not p.exists():
            log.info("no %s; using defaults (copy g2/config.example.yaml to enable feeds)", p)
            return cls()
        import yaml  # PyYAML
        with open(p, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"{p}: top level must be a mapping")
        return cls.from_dict(raw)

    def calendar_folder(self) -> Path:
        return _resolve(self.calendar.folder)

    def tasks_path(self) -> Path | None:
        return _resolve(self.tasks.path) if self.tasks.path else None


def _resolve(p: str) -> Path:
    path = Path(p).expanduser()
    return path if path.is_absolute() else ROOT / path


def _build(cls, d: dict, prefix: str):
    """Dataclass from a dict, nested dataclasses included; unknown keys are logged, not fatal."""
    kw = {}
    known = {f.name: f for f in fields(cls)}
    for k, v in d.items():
        f = known.get(k)
        if f is None:
            log.warning("hub config: unknown key %s%s ignored", prefix, k)
            continue
        default = f.default_factory() if f.default_factory is not MISSING else f.default  # type: ignore[misc]
        if is_dataclass(default) and isinstance(v, dict):
            kw[k] = _build(type(default), v, f"{prefix}{k}.")
        else:
            kw[k] = v
    return cls(**kw)


# ═══════════════════════════════════════════════════════════════════════════════════════
# weather: Open-Meteo WMO codes -> native weather_type
# ═══════════════════════════════════════════════════════════════════════════════════════

# dashboard.WEATHER_TYPES: 0 Unknown 1 Sunny 2 Cloudy 3 Drizzle 4 Heavy drizzle 5 Rain 6 Heavy rain
# 7 Thunderstorm 8 Thunder 9 Snow 10 Mist 11 Fog 12 Dust 13 Squalls 14 Tornado 15 Freezing rain 16 Clear night
W = {name: i for i, name in enumerate(dashboard.WEATHER_TYPES)}
WMO_TO_TYPE: dict[int, int] = {
    0: W["Sunny"], 1: W["Sunny"], 2: W["Cloudy"], 3: W["Cloudy"],
    45: W["Fog"], 48: W["Fog"],
    51: W["Drizzle"], 53: W["Drizzle"], 55: W["Heavy drizzle"],
    56: W["Freezing rain"], 57: W["Freezing rain"],
    61: W["Rain"], 63: W["Rain"], 65: W["Heavy rain"],
    66: W["Freezing rain"], 67: W["Freezing rain"],
    71: W["Snow"], 73: W["Snow"], 75: W["Snow"], 77: W["Snow"],
    80: W["Rain"], 81: W["Rain"], 82: W["Heavy rain"],
    85: W["Snow"], 86: W["Snow"],
    95: W["Thunderstorm"], 96: W["Thunderstorm"], 99: W["Thunderstorm"],
}
WMO_DESCRIPTIONS: dict[int, str] = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast", 45: "Fog", 48: "Rime fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Dense drizzle", 56: "Freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains", 80: "Showers", 81: "Showers",
    82: "Violent showers", 85: "Snow showers", 86: "Snow showers", 95: "Thunderstorm",
    96: "Thunderstorm, hail", 99: "Thunderstorm, hail",
}


def weather_type_for(wmo_code: int | None, is_day: bool = True) -> int:
    """Open-Meteo ``weather_code`` -> ``dashboard.WEATHER_TYPES`` index (clear at night -> 16)."""
    if wmo_code is None:
        return W["Unknown"]
    t = WMO_TO_TYPE.get(int(wmo_code), W["Unknown"])
    if t == W["Sunny"] and not is_day:
        return W["Clear night"]
    return t


def weather_status_for(wmo_code: int | None, is_day: bool = True) -> str:
    """Status-bar text (<= 32 chars) for a WMO code."""
    if wmo_code is None:
        return ""
    if int(wmo_code) in (0, 1) and not is_day:
        return "Clear night"
    return WMO_DESCRIPTIONS.get(int(wmo_code), dashboard.WEATHER_TYPES[weather_type_for(wmo_code, is_day)])


@dataclass(frozen=True)
class WeatherReading:
    temperature_c: float
    weather_type: int
    status: str
    code: int | None = None


def reading_from_open_meteo(data: dict) -> WeatherReading:
    """Map an Open-Meteo JSON answer (``current`` or the older ``current_weather``) to a reading."""
    cur = data.get("current")
    if isinstance(cur, dict) and "temperature_2m" in cur:
        temp, code, day = cur["temperature_2m"], cur.get("weather_code"), cur.get("is_day", 1)
    else:
        cur = data.get("current_weather") or {}
        if "temperature" not in cur:
            raise ValueError("Open-Meteo answer without current temperature")
        temp, code, day = cur["temperature"], cur.get("weathercode"), cur.get("is_day", 1)
    is_day = bool(int(day)) if day is not None else True
    code_i = int(code) if code is not None else None
    return WeatherReading(float(temp), weather_type_for(code_i, is_day), weather_status_for(code_i, is_day), code_i)


# ═══════════════════════════════════════════════════════════════════════════════════════
# calendar: minimal RFC 5545 reader
# ═══════════════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class IcsEvent:
    summary: str
    start: datetime          # naive, local time
    end: datetime            # naive, local time; all-day end is exclusive (next midnight)
    location: str = ""
    all_day: bool = False


def _unfold(text: str) -> list[str]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    for ln in lines:
        if ln[:1] in (" ", "\t") and out:
            out[-1] += ln[1:]
        elif ln:
            out.append(ln)
    return out


def _split_prop(line: str) -> tuple[str, dict[str, str], str] | None:
    """``NAME;PARAM=V;...:VALUE`` -> (NAME, {PARAM: V}, VALUE); the first ':' outside quotes splits."""
    inq = False
    for i, ch in enumerate(line):
        if ch == '"':
            inq = not inq
        elif ch == ":" and not inq:
            break
    else:
        return None
    head, value = line[:i], line[i + 1:]
    parts = head.split(";")
    params: dict[str, str] = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.strip().upper()] = v.strip().strip('"')
    return parts[0].strip().upper(), params, value


_ESC = re.compile(r"\\([\\;,nN])")


def _unescape(v: str) -> str:
    return _ESC.sub(lambda m: "\n" if m.group(1) in "nN" else m.group(1), v).strip()


def _to_local_naive(dt: datetime) -> datetime:
    return dt.astimezone().replace(tzinfo=None)


def _parse_dt(value: str, params: dict[str, str]) -> tuple[datetime, bool]:
    """DATE or DATE-TIME (floating, TZID, or UTC 'Z') -> (naive local datetime, all_day)."""
    v = value.strip()
    if params.get("VALUE", "").upper() == "DATE" or (len(v) == 8 and v.isdigit()):
        return datetime.strptime(v[:8], "%Y%m%d"), True
    utc = v.endswith("Z")
    v = v.rstrip("Zz")
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M"):
        try:
            dt = datetime.strptime(v, fmt)
            break
        except ValueError:
            continue
    else:
        raise ValueError(f"bad DATE-TIME {value!r}")
    if utc:
        return _to_local_naive(dt.replace(tzinfo=timezone.utc)), False
    tzid = params.get("TZID")
    if tzid and tzid.upper() in ("UTC", "GMT", "Z", "ETC/UTC", "ETC/GMT"):
        return _to_local_naive(dt.replace(tzinfo=timezone.utc)), False
    if tzid:  # needs the tzdata package on Windows; otherwise the time is taken as local floating
        try:
            from zoneinfo import ZoneInfo
            return _to_local_naive(dt.replace(tzinfo=ZoneInfo(tzid))), False
        except Exception:
            pass  # unknown zone: treat as local floating time
    return dt, False


def parse_ics(text: str) -> list[IcsEvent]:
    """VEVENT DTSTART/DTEND/SUMMARY/LOCATION (DURATION honoured when there is no DTEND); RRULE,
    EXDATE and nested VALARM blocks are ignored; an event without DTSTART is skipped."""
    events: list[IcsEvent] = []
    cur: dict[str, Any] | None = None
    depth = 0  # nesting inside a VEVENT (VALARM etc.)
    for line in _unfold(text):
        prop = _split_prop(line)
        if prop is None:
            continue
        name, params, value = prop
        if name == "BEGIN":
            if value.strip().upper() == "VEVENT" and cur is None:
                cur, depth = {}, 0
            elif cur is not None:
                depth += 1
            continue
        if name == "END":
            if cur is None:
                continue
            if depth:
                depth -= 1
                continue
            if value.strip().upper() == "VEVENT":
                ev = _finish_event(cur)
                if ev is not None:
                    events.append(ev)
                cur = None
            continue
        if cur is None or depth:
            continue
        try:
            if name == "DTSTART":
                cur["start"], cur["all_day"] = _parse_dt(value, params)
            elif name == "DTEND":
                cur["end"], cur["end_all_day"] = _parse_dt(value, params)
            elif name == "DURATION":
                cur["duration"] = _parse_duration(value)
            elif name == "SUMMARY":
                cur["summary"] = _unescape(value)
            elif name == "LOCATION":
                cur["location"] = _unescape(value)
        except ValueError as e:
            log.debug("ics: %s skipped (%s)", name, e)
    return events


_DUR = re.compile(r"^([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


def _parse_duration(v: str) -> timedelta:
    m = _DUR.match(v.strip())
    if not m:
        raise ValueError(f"bad DURATION {v!r}")
    sign, w, d, h, mi, s = m.groups()
    td = timedelta(weeks=int(w or 0), days=int(d or 0), hours=int(h or 0), minutes=int(mi or 0), seconds=int(s or 0))
    return -td if sign == "-" else td


def _finish_event(cur: dict) -> IcsEvent | None:
    start = cur.get("start")
    if start is None:
        return None
    all_day = bool(cur.get("all_day"))
    end = cur.get("end")
    if end is None:
        if "duration" in cur:
            end = start + cur["duration"]
        else:
            end = start + timedelta(days=1) if all_day else start
    if end < start:
        end = start
    return IcsEvent(cur.get("summary", ""), start, end, cur.get("location", ""), all_day)


def load_ics_folder(folder: Path) -> list[IcsEvent]:
    """Every ``*.ics`` in ``folder`` (not recursive); unreadable files are logged and skipped."""
    out: list[IcsEvent] = []
    for p in sorted(Path(folder).glob("*.ics")):
        try:
            out.extend(parse_ics(p.read_text(encoding="utf-8", errors="replace")))
        except Exception as e:
            log.warning("calendar: %s skipped (%s)", p.name, e)
    return out


def _fmt_time(dt: datetime, time_format: str) -> str:
    return dt.strftime("%H:%M") if time_format == "24h" else dt.strftime("%I:%M %p")


def upcoming_schedules(events: Sequence[IcsEvent], now: datetime, limit: int = 5,
                       time_format: str = "12h") -> list[dashboard.Schedule]:
    """Today's and tomorrow's events that have not ended, soonest first, as the app formats them:
    ``hh:mm AM-hh:mm PM`` / ``HH:MM-HH:MM``, ``Tmr `` prefix for tomorrow, ``All day``."""
    today = now.date()
    tomorrow = today + timedelta(days=1)
    cutoff = datetime.combine(tomorrow + timedelta(days=1), dtime.min)
    keep: list[IcsEvent] = []
    for e in events:
        if e.start >= cutoff:
            continue
        if e.all_day:
            if e.end.date() <= today:
                continue
        elif e.end <= now:
            continue
        keep.append(e)
    keep.sort(key=lambda e: (e.start, e.summary))
    out = []
    for e in keep[:max(0, limit)]:
        starts_tomorrow = e.start.date() == tomorrow
        if e.all_day:
            ts = "Tmr All day" if starts_tomorrow else "All day"
        else:
            ts = ("Tmr " if starts_tomorrow else "") + f"{_fmt_time(e.start, time_format)}-{_fmt_time(e.end, time_format)}"
        out.append(dashboard.Schedule(e.summary or "(untitled)", e.location, ts, int(e.end.timestamp())))
    return out


# ═══════════════════════════════════════════════════════════════════════════════════════
# news: RSS 2.0 / RSS 1.0 / Atom with the stdlib parser
# ═══════════════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class NewsEntry:
    title: str
    source: str = ""
    content: str = ""
    published: datetime | None = None   # aware (UTC when the feed gave none)

    def item(self, content_chars: int = 600) -> dashboard.NewsItem:
        return dashboard.NewsItem(self.title, self.source, self.content[:content_chars])


_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def strip_html(s: str | None) -> str:
    if not s:
        return ""
    return _WS.sub(" ", html.unescape(_TAGS.sub(" ", s))).strip()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _child_text(el: ET.Element, *names: str) -> str:
    for name in names:
        for c in el:
            if _local(c.tag) == name:
                if c.text and c.text.strip():
                    return c.text.strip()
                # Atom <content type="xhtml"> wraps a div
                inner = "".join(ET.tostring(x, encoding="unicode") for x in c)
                if inner.strip():
                    return inner
    return ""


def parse_date(s: str | None) -> datetime | None:
    """RFC 822 (RSS) or ISO 8601 (Atom) -> aware datetime; None when absent or unreadable."""
    if not s:
        return None
    s = s.strip()
    try:
        dt = email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_feed(text: str, default_source: str = "") -> list[NewsEntry]:
    """Entries of an RSS 2.0, RSS 1.0 (RDF) or Atom document, document order.  Raises on bad XML."""
    root = ET.fromstring(text)
    kind = _local(root.tag)
    out: list[NewsEntry] = []
    if kind == "feed":  # Atom
        source = strip_html(_child_text(root, "title")) or default_source
        for e in root:
            if _local(e.tag) != "entry":
                continue
            out.append(NewsEntry(strip_html(_child_text(e, "title")), source,
                                 strip_html(_child_text(e, "summary", "content")),
                                 parse_date(_child_text(e, "published", "updated"))))
        return out
    # RSS 2.0: rss/channel/item ; RSS 1.0: RDF/channel + RDF/item
    channel = next((c for c in root.iter() if _local(c.tag) == "channel"), None)
    source = (strip_html(_child_text(channel, "title")) if channel is not None else "") or default_source
    for it in root.iter():
        if _local(it.tag) != "item":
            continue
        out.append(NewsEntry(strip_html(_child_text(it, "title")), source,
                             strip_html(_child_text(it, "description", "encoded", "summary")),
                             parse_date(_child_text(it, "pubDate", "date", "published", "updated"))))
    return out


def newest(entries: Sequence[NewsEntry], n: int) -> list[NewsEntry]:
    """Newest ``n`` by published date; undated entries keep document order after the dated ones."""
    indexed = list(enumerate(entries))
    indexed.sort(key=lambda p: (0, -p[1].published.timestamp()) if p[1].published else (1, p[0]))
    return [e for _, e in indexed[:max(0, n)]]


# ═══════════════════════════════════════════════════════════════════════════════════════
# feeds
# ═══════════════════════════════════════════════════════════════════════════════════════


class Feed:
    """``collect()`` -> payload (None = nothing to push); ``push(payload)`` -> result."""
    name = ""
    interval_s: float = 900.0

    def __init__(self, glasses: Glasses) -> None:
        self.glasses = glasses

    def changed(self) -> bool:
        """False lets the scheduler skip this interval without collecting (TasksFeed)."""
        return True

    async def collect(self):
        raise NotImplementedError

    async def push(self, payload):
        raise NotImplementedError

    @staticmethod
    def ok(result) -> bool:
        return result is not None

    @staticmethod
    def describe(result) -> str:
        return repr(result)


class TimeFeed(Feed):
    """Hourly ``0x80`` TIME_SYNC to the right lens, acked (native-protocol.md section 1 #8)."""
    name = "time"

    def __init__(self, glasses: Glasses, cfg: TimeConfig | None = None, clock: Callable[[], float] = time_mod.time) -> None:
        super().__init__(glasses)
        self.cfg = cfg or TimeConfig()
        self.interval_s = float(self.cfg.interval_s)
        self.clock = clock

    async def collect(self):
        return int(self.clock())

    async def push(self, payload):
        now = int(payload)
        pb_payload = session.time_sync(self.glasses.alloc.next_magic(), now, self.glasses.tz_units)
        return await self.glasses.send_pb(session.SID_DEVICE, pb_payload, RIGHT, "ack", 4.0)

    @staticmethod
    def describe(result) -> str:
        if result is None:
            return "no reply"
        m = session.parse_device(result.payload)
        return f"ack result={m.result}"


class WeatherFeed(Feed):
    """Open-Meteo current weather -> status bar.  Override :meth:`fetch` to inject JSON."""
    name = "weather"

    def __init__(self, glasses: Glasses, cfg: WeatherConfig, clock: Callable[[], float] = time_mod.time) -> None:
        super().__init__(glasses)
        self.cfg = cfg
        self.interval_s = float(cfg.interval_s)
        self.clock = clock
        self.last: WeatherReading | None = None

    async def fetch(self) -> dict:
        """One GET to Open-Meteo: lat/lon, current temperature/code/is_day.  No identifiers."""
        import aiohttp
        params = {"latitude": f"{float(self.cfg.latitude):.4f}", "longitude": f"{float(self.cfg.longitude):.4f}",
                  "current": "temperature_2m,weather_code,is_day", "timezone": "auto"}
        timeout = aiohttp.ClientTimeout(total=self.cfg.timeout_s)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(self.cfg.endpoint, params=params) as r:
                r.raise_for_status()
                return await r.json(content_type=None)

    async def collect(self) -> WeatherReading:
        if not self.cfg.configured:
            raise ValueError("weather needs latitude and longitude in g2/config/hub.yaml")
        self.last = reading_from_open_meteo(await self.fetch())
        return self.last

    async def push(self, payload: WeatherReading):
        return await self.glasses.set_weather(payload.temperature_c, payload.weather_type, payload.status,
                                              fahrenheit_display=self.cfg.units.lower().startswith("f"))

    @staticmethod
    def ok(result) -> bool:
        return result == 0

    @staticmethod
    def describe(result) -> str:
        return "no reply" if result is None else f"ack flag {result} ({dashboard.ACK_NAMES.get(result, result)})"


class CalendarFeed(Feed):
    """Local ``*.ics`` files -> next events of today/tomorrow -> schedule widget."""
    name = "calendar"

    def __init__(self, glasses: Glasses, cfg: CalendarConfig, now: Callable[[], datetime] = datetime.now,
                 folder: Path | None = None) -> None:
        super().__init__(glasses)
        self.cfg = cfg
        self.interval_s = float(cfg.interval_s)
        self.now = now
        self.folder = Path(folder) if folder is not None else _resolve(cfg.folder)
        self._warned = False

    async def collect(self) -> list[dashboard.Schedule] | None:
        if not self.folder.is_dir():
            if not self._warned:
                log.warning("calendar: folder %s does not exist; nothing pushed", self.folder)
                self._warned = True
            return None
        events = await asyncio.to_thread(load_ics_folder, self.folder)
        return upcoming_schedules(events, self.now(), self.cfg.max_events, self.cfg.time_format)

    async def push(self, payload: list[dashboard.Schedule]):
        return await self.glasses.set_calendar(payload)

    @staticmethod
    def ok(result) -> bool:
        return bool(result) and all(f == 0 for f in result)

    @staticmethod
    def describe(result) -> str:
        return "no messages" if not result else "ack flags " + ",".join("-" if f is None else str(f) for f in result)


class NewsFeed(Feed):
    """RSS/Atom URLs -> newest N -> news V3 FIFO.  Override :meth:`fetch` to inject XML."""
    name = "news"

    def __init__(self, glasses: Glasses, cfg: NewsConfig) -> None:
        super().__init__(glasses)
        self.cfg = cfg
        self.interval_s = float(cfg.interval_s)

    async def fetch(self, url: str) -> str:
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=self.cfg.timeout_s)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(url) as r:
                r.raise_for_status()
                return await r.text()

    async def collect(self) -> list[dashboard.NewsItem] | None:
        entries: list[NewsEntry] = []
        for url in self.cfg.urls:
            try:
                text = await self.fetch(url)
                entries.extend(parse_feed(text, urlsplit(url).hostname or url))
            except Exception as e:
                log.warning("news: %s failed (%s)", url, e)
        if not entries:
            return None  # keep what the glasses already show
        return [e.item(self.cfg.content_chars) for e in newest(entries, self.cfg.max_items)]

    async def push(self, payload: list[dashboard.NewsItem]):
        return await self.glasses.push_news(payload)

    @staticmethod
    def ok(result) -> bool:
        return result is not None and all(s == 0 for s in result)

    @staticmethod
    def describe(result) -> str:
        return "nothing pushed" if not result else "newsStatus per slot " + ",".join("-" if s is None else str(s) for s in result)


class TasksFeed(Feed):
    """Pending thoughts (notes Store) -> quick list.  Re-pushes when thoughts.json changes on disk."""
    name = "tasks"

    def __init__(self, glasses: Glasses, cfg: TasksConfig, store, clock: Callable[[], float] = time_mod.time) -> None:
        super().__init__(glasses)
        self.cfg = cfg
        self.store = store
        self.clock = clock
        self.interval_s = float(cfg.poll_s)
        self.pushed_mtime: float | None = None
        self.pushed_at: float | None = None

    def mtime(self) -> float | None:
        try:
            return Path(self.store.path).stat().st_mtime
        except OSError:
            return None

    def changed(self) -> bool:
        if self.pushed_at is None or self.mtime() != self.pushed_mtime:
            return True
        return self.clock() - self.pushed_at >= self.cfg.repush_s

    async def collect(self) -> list[dashboard.Task]:
        self._mtime_seen = self.mtime()
        self.store.load()
        tasks = []
        for i, t in enumerate(self.store.pending()[:self.cfg.max_items]):
            tasks.append(dashboard.Task(i + 1, t.text, completed=bool(t.done)))
        return tasks

    async def push(self, payload: list[dashboard.Task]):
        r = await self.glasses.set_tasks(payload)
        if r:
            self.pushed_mtime, self.pushed_at = getattr(self, "_mtime_seen", self.mtime()), self.clock()
        return r

    @staticmethod
    def ok(result) -> bool:
        return bool(result)

    @staticmethod
    def describe(result) -> str:
        return "init ack + FULL_UPDATE echo + display-enable ack" if result else "a step got no reply / bad flag"


# ═══════════════════════════════════════════════════════════════════════════════════════
# hub
# ═══════════════════════════════════════════════════════════════════════════════════════


@dataclass
class FeedResult:
    name: str
    at: float
    ok: bool
    detail: str = ""
    error: str | None = None
    skipped: bool = False


@dataclass(frozen=True)
class NativeAppEvent:
    """The glasses opened (or foregrounded) something from THEIR menu.

    ``kind``: 'foreground' (SID 0x0D app-sync push, ``sid`` = foreground SID), 'startup' (the native
    app's first message: Navigate 0x08 cmd 1, Teleprompt 0x06 cmd 162, Translate 0x05 cmd 164,
    Even AI 0x07 CTRL WAKE_UP) or 'plugin' (0xE0 cmd 17 MENU_STARTUP for a plugin entry, ``app_id`` set;
    the gateway answers that one itself)."""
    kind: str
    sid: int
    name: str
    app_id: int | None = None
    payload: bytes = b""


class Hub:
    """Owns the process's native ``Glasses`` (``hub.glasses``; apps see it as ``ctx.native``),
    schedules the feeds, replaces the glasses menu and runs the keep-alive.  ``sleep``/``rand`` are
    injectable for tests.

    Menu ordering: the gateway pushes its own 2-entry menu ("Notifications", "Launch") during its connect
    and reconnect sequence, fire-and-forget, right before it creates its blank startup page.  Ours must
    land after it, so the hub pushes (a) ``menu.delay_s`` after ``start()``, (b) ``menu.delay_s`` after
    ``on_connection(True)`` (the launcher forwards the gateway's ready event) and (c) ``menu.repush_delay_s``
    after any inbound SID 0x03 reply it did not ask for (that reply is the glasses acking the gateway's
    push; the raw-event stream shows every notification).  Pending pushes coalesce, so one push happens.
    Our plugin entry reuses the gateway's Launch app id, so picking it still rebuilds the board."""

    def __init__(self, glasses: Glasses, config: HubConfig | None = None, store=None,
                 clock: Callable[[], float] = time_mod.time, now: Callable[[], datetime] = datetime.now, *,
                 sleep: Callable[[float], Any] = asyncio.sleep, rand: Callable[[], float] = random.random) -> None:
        self.glasses = glasses
        self.config = config or HubConfig()
        self.store = store
        self.clock = clock
        self.now = now
        self.sleep = sleep
        self.rand = rand
        self.lock = asyncio.Lock()
        self.feeds: dict[str, Feed] = {}
        self.results: dict[str, FeedResult] = {}
        self.runs: dict[str, int] = {}
        self.menu_pushed: bool | None = None
        self._tasks: dict[str, asyncio.Task] = {}
        self._ka_retry: asyncio.Task | None = None
        self._menu_task: asyncio.Task | None = None
        self._menu_in_flight = False
        self._native_app_callbacks: list[Callable[[NativeAppEvent], Any]] = []
        self.native_events: list[NativeAppEvent] = []
        self._started = False
        for name in FEED_NAMES:
            if self._enabled(name):
                try:
                    self.feeds[name] = self.make_feed(name)
                except Exception as e:
                    log.warning("feed %s not started: %s", name, e)

    # -- feeds --------------------------------------------------------------------------
    def _enabled(self, name: str) -> bool:
        c = self.config
        if name == "weather":
            return c.weather.enabled and c.weather.configured
        if name == "news":
            return c.news.enabled and bool(c.news.urls)
        return bool(getattr(c, name).enabled)

    def make_feed(self, name: str) -> Feed:
        """Build a feed whether or not the config enables it (``--once`` uses this)."""
        c = self.config
        if name == "time":
            return TimeFeed(self.glasses, c.time, self.clock)
        if name == "weather":
            if not c.weather.configured:
                raise ValueError("weather.latitude / weather.longitude are not set in g2/config/hub.yaml")
            return WeatherFeed(self.glasses, c.weather, self.clock)
        if name == "calendar":
            return CalendarFeed(self.glasses, c.calendar, self.now, c.calendar_folder())
        if name == "news":
            if not c.news.urls:
                raise ValueError("news.urls is empty in g2/config/hub.yaml")
            return NewsFeed(self.glasses, c.news)
        if name == "tasks":
            if self.store is None:
                from notes.store import Store
                self.store = Store(c.tasks_path())
            return TasksFeed(self.glasses, c.tasks, self.store, self.clock)
        raise KeyError(f"unknown feed {name!r}; one of {', '.join(FEED_NAMES)}")

    async def run_feed(self, feed: Feed, force: bool = False) -> FeedResult:
        """collect() outside the lock, push() inside it; never raises."""
        at = self.clock()
        if not force and not feed.changed():
            return self.results.get(feed.name) or FeedResult(feed.name, at, True, "unchanged", skipped=True)
        self.runs[feed.name] = self.runs.get(feed.name, 0) + 1
        try:
            payload = await feed.collect()
            if payload is None:
                res = FeedResult(feed.name, at, True, "nothing to push", skipped=True)
            else:
                async with self.lock:
                    result = await feed.push(payload)
                res = FeedResult(feed.name, at, feed.ok(result), feed.describe(result))
                (log.info if res.ok else log.warning)("%s: %s", feed.name, res.detail)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("%s failed", feed.name)
            res = FeedResult(feed.name, at, False, error=f"{type(e).__name__}: {e}")
        self.results[feed.name] = res
        return res

    async def refresh(self, name: str | None = None) -> dict[str, FeedResult]:
        """Force one run of ``name`` (built on the fly when the config has it off) or of every feed."""
        names = [name] if name else list(self.feeds)
        out: dict[str, FeedResult] = {}
        for n in names:
            feed = self.feeds.get(n)
            if feed is None:
                try:
                    feed = self.make_feed(n)
                except Exception as e:
                    out[n] = self.results[n] = FeedResult(n, self.clock(), False, error=str(e))
                    continue
            out[n] = await self.run_feed(feed, force=True)
        return out

    async def _loop(self, feed: Feed, first_delay: float) -> None:
        delay = first_delay
        while True:
            try:
                await self.sleep(delay + self.rand() * self.config.jitter_s)
                await self.run_feed(feed)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - run_feed already catches; this guards sleep/rand
                log.exception("%s loop error", feed.name)
            delay = feed.interval_s

    # -- lifecycle ----------------------------------------------------------------------
    async def wait_connected(self, timeout: float = 3.0) -> bool:
        """Give the gateway WebSocket reader a moment so the first ack is not missed."""
        t0 = self.clock()
        while not getattr(self.glasses.t, "connected", True):
            if self.clock() - t0 >= timeout:
                return False
            await asyncio.sleep(0.05)
        return True

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        await self.glasses.start()
        self.glasses.on(self._on_inbound)
        await self.wait_connected()
        if self.config.menu.enabled:
            self._schedule_menu_push(self.config.menu.delay_s, "start")
        if self.config.keepalive.enabled:
            self.glasses.on_link_lost = self._link_lost
            kw = {"time_sync_every": math.inf} if "time" in self.feeds else {}
            self.glasses.start_keepalive(**kw)
        for i, feed in enumerate(self.feeds.values()):
            self._tasks[feed.name] = asyncio.create_task(self._loop(feed, i * self.config.stagger_s), name=f"g2-hub-{feed.name}")
        log.info("hub started: feeds %s%s", ", ".join(self.feeds) or "none",
                 "; keep-alive on" if self.config.keepalive.enabled else "")

    async def stop(self) -> None:
        tasks = list(self._tasks.values()) + [t for t in (self._ka_retry, self._menu_task) if t is not None]
        self._tasks.clear()
        self._ka_retry = self._menu_task = None
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        await self.glasses.stop()
        self._started = False

    # -- menu ---------------------------------------------------------------------------
    def menu_items(self) -> tuple[list[int], list[menu_mod.PluginItem]]:
        return list(self.config.menu.builtin), self.config.menu.plugin_items()

    async def push_menu(self) -> bool:
        """Replace the whole glasses menu now (built-ins + our plugin entry); True when acked."""
        builtin, plugins = self.menu_items()
        async with self.lock:
            self._menu_in_flight = True
            try:
                self.menu_pushed = await self.glasses.set_menu(builtin_ids=builtin, plugins=plugins)
            except Exception as e:
                log.warning("menu push failed: %s", e)
                self.menu_pushed = False
            finally:
                self._menu_in_flight = False
        (log.info if self.menu_pushed else log.warning)("menu %s (%d built-in + %s)", "replaced" if self.menu_pushed else "NOT acked",
                                                        len(builtin), ", ".join(p.name for p in plugins) or "no plugin")
        return bool(self.menu_pushed)

    def _schedule_menu_push(self, delay: float, why: str) -> None:
        """(Re)arm one menu push ``delay`` s from now; a pending one is replaced so pushes coalesce."""
        if not self.config.menu.enabled:
            return
        if self._menu_task is not None and not self._menu_task.done():
            self._menu_task.cancel()
        log.debug("menu push in %.0f s (%s)", delay, why)
        self._menu_task = asyncio.create_task(self._menu_later(delay), name="g2-hub-menu")

    async def _menu_later(self, delay: float) -> None:
        await self.sleep(delay)
        await self.push_menu()

    def on_connection(self, ready: bool) -> None:
        """The launcher forwards the gateway's connection(ready) event here."""
        if ready:
            self._schedule_menu_push(self.config.menu.delay_s, "gateway ready")
        elif self._menu_task is not None and not self._menu_task.done():
            self._menu_task.cancel()

    # -- native app events --------------------------------------------------------------
    def on_native_app(self, callback: Callable[[NativeAppEvent], Any]) -> None:
        """Register ``callback(NativeAppEvent)`` (sync or async) for selections of the glasses' own apps."""
        self._native_app_callbacks.append(callback)

    def _emit_native(self, ev: NativeAppEvent) -> None:
        self.native_events.append(ev)
        del self.native_events[:-64]
        log.info("native app %s: %s (sid 0x%02x%s)", ev.kind, ev.name, ev.sid, f" app_id {ev.app_id}" if ev.app_id else "")
        for fn in list(self._native_app_callbacks):
            try:
                r = fn(ev)
                if asyncio.iscoroutine(r):
                    asyncio.ensure_future(r)
            except Exception:
                log.exception("on_native_app callback failed")

    def _on_inbound(self, side: str, p: ParsedFrame) -> None:
        """Glasses listener: gateway menu replies (re-push ours) and native app selections."""
        try:
            sid = p.sid
            if sid == menu_mod.SID_MENU:
                if not self._menu_in_flight:
                    self._schedule_menu_push(self.config.menu.repush_delay_s, "foreign SID 0x03 reply seen")
                return
            cmd = dashboard.read_cmd(p.payload)
            if sid == session.SID_APP_SYNC:
                a = session.parse_app_sync(p.payload)
                if a.foreground:
                    self._emit_native(NativeAppEvent("foreground", a.foreground, menu_mod.BUILTIN.get(a.foreground, f"sid {a.foreground}"), payload=p.payload))
            elif (sid, cmd) in ((native_apps.SID_NAVIGATION, native_apps.NAV_CMD_MENU_STARTUP),
                                (native_apps.SID_TELEPROMPTER, native_apps.TP_CMD_FILE_LIST_REQUEST),
                                (native_apps.SID_TRANSLATE, native_apps.TR_CMD_LANG_SWITCH_REQ)):
                self._emit_native(NativeAppEvent("startup", sid, menu_mod.BUILTIN.get(sid, f"sid {sid}"), payload=p.payload))
            elif sid == even_ai.SID_EVEN_AI and even_ai.parse(p.payload).is_wake_up:
                self._emit_native(NativeAppEvent("startup", sid, menu_mod.BUILTIN[menu_mod.EVEN_AI], payload=p.payload))
            elif sid == session.SID_EVENHUB and cmd == 17:
                sub = pb.read_bytes(p.payload, 20)
                app_id = pb.read_varint(sub, 1, 0) if sub is not None else None
                self._emit_native(NativeAppEvent("plugin", sid, "plugin", app_id, p.payload))
        except Exception as e:  # a decode error must never kill the reader
            log.debug("inbound sid=0x%02x not understood: %r", p.sid, e)

    def _link_lost(self, reason: str) -> None:
        log.warning("keep-alive: %s; re-arming in %.0f s", reason, self.config.keepalive.retry_s)
        if self._ka_retry is None or self._ka_retry.done():
            self._ka_retry = asyncio.create_task(self._rearm_keepalive(), name="g2-hub-keepalive-retry")

    async def _rearm_keepalive(self) -> None:
        await self.sleep(self.config.keepalive.retry_s)
        kw = {"time_sync_every": math.inf} if "time" in self.feeds else {}
        self.glasses.start_keepalive(**kw)


# ═══════════════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════════════


async def run_once(hub: Hub, which: str) -> int:
    """Connect, run one feed (or all configured) with acks printed, disconnect."""
    names = list(hub.feeds) if which == "all" else [which]
    if which == "menu":
        await hub.glasses.start()
        await hub.wait_connected(5.0)
        try:
            ok = await hub.push_menu()
            b, pl = hub.menu_items()
            print("menu:", "OK acked" if ok else "NOT acked", "built-ins", b, "plugins", [(p.name, p.app_id) for p in pl])
        finally:
            await hub.glasses.stop()
        return 0 if ok else 1
    if which == "all" and not names:
        print("no feeds enabled in config; use --once <feed> to force one")
        return 1
    await hub.glasses.start()
    if not await hub.wait_connected(5.0):
        print("warning: gateway /ws not connected yet; acks may be missed")
    seen: list[tuple[str, int]] = []
    hub.glasses.on(lambda side, p: seen.append((side, p.sid)))
    rc = 0
    try:
        for n in names:
            print(f"{n}: running ...")
            res = (await hub.refresh(n))[n]
            if res.error:
                print(f"  ERROR {res.error}")
            elif res.skipped:
                print(f"  skipped: {res.detail}")
            else:
                print(f"  {'OK ' if res.ok else 'BAD'} {res.detail}")
            rc = rc or (0 if res.ok else 1)
    finally:
        await hub.glasses.stop()
    print("inbound frames seen:", len(seen), "sids:", sorted({hex(s) for _, s in seen}))
    return rc


async def run_forever(hub: Hub) -> int:
    await hub.start()
    log.info("hub running; Ctrl-C to stop")
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await hub.stop()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--once", choices=[*FEED_NAMES, "menu", "all"], help="run one feed (or the menu push) through the gateway, print acks, exit")
    ap.add_argument("--config", help=f"hub.yaml (default {DEFAULT_CONFIG_PATH})")
    ap.add_argument("--gateway", help="overrides config.gateway")
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.v else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    from g2.native.transport import GatewayRawTransport
    cfg = HubConfig.load(a.config)
    base = a.gateway or cfg.gateway
    glasses = Glasses(GatewayRawTransport(base), log=logging.getLogger("g2.native").info)
    hub = Hub(glasses, cfg)
    if a.once:
        return asyncio.run(run_once(hub, a.once))
    return asyncio.run(run_forever(hub))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        pass
