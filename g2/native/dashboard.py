"""The native dashboard: SID 0x01 (weather, calendar, news, stocks, display settings),
SID 0x0C quicklist/tasks, SID 0x0E health, SID 0x1F watchface.  Port of
``lib/protocol/dashboard.dart``, ``quicklist.dart``, ``stocks.dart``, ``health.dart`` and
``watchface.dart`` (open-g2-hub).

Every builder returns the protobuf **payload**; wrap it with ``framing.frame_pb(payload,
sid, FLAG_REQUEST, seq)``.  All dashboard pushes go to the RIGHT lens and are only acked
while the SID 0xE0 EvenHub heartbeat runs; stopping the heartbeat afterwards is what makes
the native dashboard show the new content (native-protocol.md section 1, "Why the heartbeat
matters").

Confidence per feature is copied from native-protocol.md section 7 into each docstring.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from . import pb
from .pb import Writer, message_field, varint_field

SID_DASHBOARD = 0x01
SID_QUICKLIST = 0x0C
SID_HEALTH = 0x0E
SID_WATCHFACE = 0x1F

# DashboardDataPackage command ids (dashboard.dart:15-29)
CMD_DASHBOARD_RESPOND, CMD_DASHBOARD_RECEIVE, CMD_APP_RESPOND, CMD_APP_RECEIVE = 1, 2, 3, 4
CMD_REQUEST_NEWS_INFO, CMD_RESPONSE_NEWS_INFO = 5, 6
CMD_APP_REQUEST_NEWS_INFO, CMD_DEV_RESPONSE_NEWS_INFO = 7, 8
CMD_APP_SEND_NEWS_DATA, CMD_DEV_RESPONSE_NEWS_DATA = 9, 10
CMD_DEV_REQUEST_NEWS_UPGRADE, CMD_DEV_NOTIFY_NEWS_EVENT = 11, 12
CMD_APP_REQUEST_CLEAR_ALL_DATA, CMD_DEV_RESPONSE_CLEAR_ALL_DATA = 13, 14

# widget types (dashboard.dart:31-36)
WIDGET_NEWS, WIDGET_STOCK, WIDGET_SCHEDULE, WIDGET_QUICKLIST, WIDGET_HEALTH = 1, 2, 3, 4, 5
TEMP_CELSIUS, TEMP_FAHRENHEIT = 1, 2
WEATHER_TYPES = ["Unknown", "Sunny", "Cloudy", "Drizzle", "Heavy drizzle", "Rain", "Heavy rain", "Thunderstorm",
                 "Thunder", "Snow", "Mist", "Fog", "Dust", "Squalls", "Tornado", "Freezing rain", "Clear night"]

ACK_SUCCESS, ACK_PARAMETER_ERROR, ACK_NEWS_VERSION_ERROR = 0, 1, 2
ACK_NAMES = {0: "SUCCESS", 1: "PARAMETER_ERROR", 2: "NEWS_VERSION_ERROR"}

#: hub-level formatting caps for calendar strings (hub_format.dart:395)
CAL_TITLE_MAX_BYTES, CAL_LOCATION_MAX_BYTES, CAL_TIME_MAX_BYTES, CAL_MAX_EVENTS = 175, 282, 62, 8

# news events the glasses send (frames.py:69-84)
NEWS_EVENT_NAMES = {0: "EXPAND_LIST", 1: "BACK_TO_WIDGET", 2: "SCROLL", 3: "OPEN_ARTICLE", 4: "BACK_TO_LIST"}


def _head(s: str, n: int) -> str:
    """Python ``s[:n]`` on code points (pytext.dart:36-39)."""
    return s if len(s) <= n else s[:n]


def utf8_fit(text: str, max_bytes: int) -> str:
    """Cut to ``max_bytes`` UTF-8 bytes on a character boundary (limits.dart:870-878)."""
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text
    cut = max(max_bytes, 0)
    while cut > 0 and (data[cut] & 0xC0) == 0x80:
        cut -= 1
    return data[:cut].decode("utf-8")


def limit_bytes(text: str, max_bytes: int) -> str:
    """calendar_events.limit_bytes: cut to max-3 bytes and append '...' (hub_format.dart:420-425)."""
    b = text.encode("utf-8")
    if len(b) <= max_bytes:
        return text
    return utf8_fit(text, max_bytes - 3) + "..."


# ── envelope (dashboard.dart:81-93) ─────────────────────────────────────────────────────


def display_setting(widget_order: Sequence[int], status_order: Sequence[int] = (), display_mode: int = 0,
                    half_day: int = 0, temperature_unit: int = 0) -> bytes:
    """DisplaySetting ``{1 displayMode, 2 n, 3 packed status, 4 n, 5 packed widgets, 6 halfDay, 7 tempUnit}``
    (dashboard.dart:48-57)."""
    w = Writer().varint(1, display_mode).varint(2, len(status_order))
    if status_order:
        w.packed_varints(3, list(status_order))
    w.varint(4, len(widget_order))
    if widget_order:
        w.packed_varints(5, list(widget_order))
    return w.varint(6, half_day).varint(7, temperature_unit).to_bytes()


def receive_from_app(package_id: int | None, content: bytes, setting: bytes | None = None) -> bytes:
    """DashboardReceiveFromApp ``{1 packageId?, 2 DisplaySetting?, 3 DashboardContent}`` (dashboard.dart:81-87).
    Field 3 is always written, even empty (the LIVE init does this; see :func:`dashboard_layout`)."""
    w = Writer()
    if package_id is not None:
        w.varint(1, package_id)
    if setting is not None:
        w.message(2, setting)
    return w.message(3, content).to_bytes()


def package(cmd: int, magic: int, receive: bytes | None = None) -> bytes:
    """DashboardDataPackage ``{1 cmd, 2 magic, 4 receive?}`` (dashboard.dart:89-93)."""
    w = Writer().varint(1, cmd).varint(2, magic)
    if receive is not None:
        w.message(4, receive)
    return w.to_bytes()


# ── display mode / widget order ─────────────────────────────────────────────────────────


def dashboard_init(magic: int, package_id: int = 1) -> bytes:
    """Base settings, LIVE (ack flag 0; effect on layout unverified): display_mode 4, status
    [1,2,3], widgets [NEWS, SCHEDULE, STOCK, STOCK], 12 h, Celsius, plus an empty field 3
    (dashboard.dart:199-209).  Example (magic 0x66):
    ``08021066221b08011215080410031a0301020320042a0401030202300138011a00``."""
    ds = display_setting([WIDGET_NEWS, WIDGET_SCHEDULE, WIDGET_STOCK, WIDGET_STOCK], [1, 2, 3], 4, 1, TEMP_CELSIUS)
    return package(CMD_DASHBOARD_RECEIVE, magic, receive_from_app(package_id, b"", ds))


def dashboard_layout(magic: int, widget_order: Sequence[int], display_mode: int = 4, half_day: int = 1,
                     temperature_unit: int = TEMP_CELSIUS, package_id: int = 1) -> bytes:
    """Custom widget order, EXPERIMENTAL (only the default order is live-confirmed).  Sends
    field 2 ONLY: fields 2 and 3 are one nanopb oneof in firmware, so a trailing empty field 3
    could overwrite the setting (dashboard.dart:353-361)."""
    ds = display_setting(list(widget_order), [1, 2, 3], display_mode, half_day, temperature_unit)
    return package(CMD_DASHBOARD_RECEIVE, magic, Writer().varint(1, package_id).message(2, ds).to_bytes())


def display_enable(magic: int, widget_types: Sequence[int], package_id: int = 1) -> bytes:
    """Enable widget slots without content (dashboard.dart:185-189).  Quicklist variant is LIVE:
    ``0802107a22130801120d0800100020012a0104300038001a00`` for ``[4]`` (magic 0x7a)."""
    return package(CMD_DASHBOARD_RECEIVE, magic, receive_from_app(package_id, b"", display_setting(list(widget_types))))


def quicklist_display_enable(magic: int, package_id: int = 1) -> bytes:
    """LIVE (features.dart:204-223)."""
    return display_enable(magic, [WIDGET_QUICKLIST], package_id)


# ── weather (status bar) -- LIVE ─────────────────────────────────────────────────────────


def weather_status(temperature_c: float, unit: int = TEMP_CELSIUS, weather_type: int = 0, update_time_ms: int = 0,
                   status: str = "", rainfall_icon: int = 0, rainfall_hint: str = "", sunset_select: int = 0,
                   sunset_hint: str = "") -> bytes:
    """WeatherStatus (dashboard.dart:117-133). ``temperature_c`` is a float32 and ALWAYS Celsius;
    ``unit`` only selects the display.  Strings are cut to 32 characters."""
    w = Writer().float32(1, float(temperature_c)).varint(2, unit).varint(3, weather_type).varint(4, update_time_ms)
    if status:
        w.string(5, _head(status, 32))
    w.varint(6, rainfall_icon)
    if rainfall_hint:
        w.string(7, _head(rainfall_hint, 32))
    w.varint(8, sunset_select)
    if sunset_hint:
        w.string(9, _head(sunset_hint, 32))
    return w.to_bytes()


def weather(magic: int, temperature_c: float, unit: int = TEMP_CELSIUS, weather_type: int = 0, update_time_ms: int = 0,
            status: str = "", rainfall_icon: int = 0, rainfall_hint: str = "", sunset_select: int = 0,
            sunset_hint: str = "", package_id: int | None = None) -> bytes:
    """Weather status-bar push, LIVE (dashboard.dart:232-253): ``{1:2, 2:m, 4:{3:{1:{1: WeatherStatus}}}}``
    with no packageId.  Wait for ack flag 0.  Example 21 C Sunny (magic 0x73):
    ``0802107322211a1f0a1d0a1b0d0000a8411001180120808cbbc995342a0553756e6e7930004000``.
    weather_type: see WEATHER_TYPES (1 Sunny, 2 Cloudy, 5 Rain, 9 Snow, 16 Clear night ...)."""
    ws = weather_status(temperature_c, unit, weather_type, update_time_ms, status, rainfall_icon, rainfall_hint, sunset_select, sunset_hint)
    content = message_field(1, message_field(1, ws))
    return package(CMD_DASHBOARD_RECEIVE, magic, receive_from_app(package_id, content))


# ── calendar -- LIVE ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Schedule:
    title: str
    location: str = ""
    time_str: str = ""  # "10:00 AM-10:30 AM", "Tmr 09:00-09:30", "All day"
    end_ts: int = 0  # unix seconds
    schedule_id: int | None = None  # only the multi-event form writes field 1


def _schedule_bytes(s: Schedule, with_id: bool) -> bytes:
    w = Writer()
    if with_id:
        w.varint(1, s.schedule_id or 0)
    if s.title:
        w.string(2, limit_bytes(s.title, CAL_TITLE_MAX_BYTES))
    if s.location:
        w.string(3, limit_bytes(s.location, CAL_LOCATION_MAX_BYTES))
    if s.time_str:
        w.string(4, limit_bytes(s.time_str, CAL_TIME_MAX_BYTES))
    return w.varint(5, s.end_ts).to_bytes()


def calendar_event(magic: int, schedule: Schedule | None, total: int, index: int, authority: int = 0,
                   package_id: int = 1) -> bytes:
    """ONE event per message as the official app sends it, LIVE (dashboard.dart:260-276):
    ``{1:2, 2:m, 4:{1:1, 3:{2:{3:{1 total, 2 index, 3 Schedule{2 title,3 location,4 time,5 end}}}}}}``.
    "No events" = ``calendar_event(m, None, 0, 0, authority=1)`` -> ``08021075220e08011a0a12081a06080010002001``.
    Example 1 event (magic 0x74): ``08021074223808011a3412321a30080110001a2a12075374616e6475701a06526f6f6d2031221131303a303020414d2d31303a333020414d28a0c2ddd606``."""
    w = Writer().varint(1, total).varint(2, index)
    if schedule is not None:
        w.message(3, _schedule_bytes(schedule, with_id=False))
    if total == 0:
        w.varint(4, authority)
    content = message_field(2, message_field(3, w.to_bytes()))
    return package(CMD_DASHBOARD_RECEIVE, magic, receive_from_app(package_id, content))


def calendar_none(magic: int, package_id: int = 1) -> bytes:
    """The app's "no events" push (LIVE)."""
    return calendar_event(magic, None, 0, 0, authority=1, package_id=package_id)


def calendar_events(magic: int, schedules: Sequence[Schedule], total: int | None = None, start_index: int = 0,
                    authority: int = 0, package_id: int = 1) -> bytes:
    """Multi-event in one message, LIVE (dashboard.dart:281-287): ScheduleWidget
    ``{1 total, 2 startIndex, 3 Schedule{1 id, 2..5}..., 4 authority}``."""
    w = Writer().varint(1, len(schedules) if total is None else total).varint(2, start_index)
    for i, s in enumerate(schedules):
        if s.schedule_id is None:
            s = Schedule(s.title, s.location, s.time_str, s.end_ts, i + 1)
        w.message(3, _schedule_bytes(s, with_id=True))
    w.varint(4, authority)
    content = message_field(2, message_field(3, w.to_bytes()))
    return package(CMD_DASHBOARD_RECEIVE, magic, receive_from_app(package_id, content))


def calendar_sequence(magic_fn, schedules: Sequence[Schedule]) -> list[bytes]:
    """The app's sequence: one message per event (or the "none" message), each to be awaited
    for ack flag 0 (features.dart:146-162).  ``magic_fn()`` returns the next magic."""
    if not schedules:
        return [calendar_none(magic_fn())]
    n = min(len(schedules), CAL_MAX_EVENTS)
    return [calendar_event(magic_fn(), schedules[i], n, i) for i in range(n)]


# ── news (V3 FIFO) -- LIVE ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class NewsItem:
    title: str
    source: str = ""
    content: str = ""


def news_reset(magic: int) -> bytes:
    """Clear the device news FIFO ``{1: 13, 2: m, 15: {1: 1}}`` -> ``080d10767a020801`` (magic 0x76). LIVE."""
    return varint_field(1, CMD_APP_REQUEST_CLEAR_ALL_DATA) + varint_field(2, magic) + message_field(15, varint_field(1, 1))


def news_fifo_count_request(magic: int) -> bytes:
    """V3 FIFO count request ``{1: 7, 2: m, 9: {1: 1}}``; reply cmd 8 ``{10: {1: count}}`` (LIVE)."""
    return varint_field(1, CMD_APP_REQUEST_NEWS_INFO) + varint_field(2, magic) + message_field(9, varint_field(1, 1))


def news_push(magic: int, slot: int, item: NewsItem, report_time_local_ms: int, session_total: int = 1,
              session_id: int = 0, session_status: int = 0) -> bytes:
    """One article per slot, LIVE (dashboard.dart:162-229): ``{1: 9, 2: m, 11: AppSendNewsData{1 sessionId,
    2 sessionStatus, 3 sessionTotalCount, 4 sessionNewsIndex, 5 newsId, 6 title, 7 reportTime, 8 source, 9 content}}``.
    ``report_time_local_ms`` is LOCAL wall-clock milliseconds (UTC ms + UTC offset); newsId == slot.
    Reply cmd 10 ``{12: {5: newsStatus}}``."""
    w = Writer().varint(1, session_id).varint(2, session_status).varint(3, session_total).varint(4, slot).varint(5, slot)
    if item.title:
        w.string(6, item.title)
    w.varint(7, report_time_local_ms)
    if item.source:
        w.string(8, item.source)
    if item.content:
        w.string(9, item.content)
    return varint_field(1, CMD_APP_SEND_NEWS_DATA) + varint_field(2, magic) + message_field(11, w.to_bytes())


def news_v2_push(magic: int, items: Sequence[tuple[int, str, int, str, str]], package_id: int = 1,
                 set_display_order: bool = True) -> bytes:
    """V2 WidgetComponent.news push -- LIVE-NEGATIVE: always answers PARAMETER_ERROR on current
    firmware (dashboard.dart:141-147).  Kept for parity only."""
    nw = Writer().varint(1, len(items)).varint(2, 0)
    for news_id, title, report_time, source, content in items:
        n = Writer().varint(1, news_id)
        if title:
            n.string(2, title)
        n.varint(3, report_time)
        if source:
            n.string(4, source)
        if content:
            n.string(5, content)
        nw.message(3, n.to_bytes())
    nw.varint(4, 0)
    content_b = message_field(2, message_field(1, nw.to_bytes()))
    ds = display_setting([WIDGET_NEWS]) if set_display_order else None
    return package(CMD_DASHBOARD_RECEIVE, magic, receive_from_app(package_id, content_b, ds))


def news_fifo_count(reply: bytes | None, default: int = 5) -> int:
    """Slot count from the cmd 8 reply (field 10 -> 1); ``default`` when missing/0 (dashboard.dart:305-311)."""
    if reply is None:
        return default
    resp = pb.read_bytes(reply, 10)
    if resp is None:
        return default
    v = pb.read_varint(resp, 1, default)
    return default if v == 0 else v


def news_status(reply: bytes | None) -> int | None:
    """newsStatus of the cmd 10 reply (field 12 -> 5); None when no reply."""
    if reply is None:
        return None
    resp = pb.read_bytes(reply, 12)
    return pb.read_varint(resp, 5, 0) if resp is not None else 0


@dataclass(frozen=True)
class NewsEvent:
    cmd: int  # 11 = scroll-down-to-refresh, 12 = DEV_NOTIFY_NEWS_EVENT
    event_id: int | None = None  # 0 expand list, 1 back to widget, 2 scroll, 3 open article, 4 back to list
    news_id: int | None = None

    @property
    def refresh_requested(self) -> bool:
        return self.cmd == CMD_DEV_REQUEST_NEWS_UPGRADE

    @property
    def event_name(self) -> str | None:
        return None if self.event_id is None else NEWS_EVENT_NAMES.get(self.event_id, str(self.event_id))


def parse_news_event(payload: bytes) -> NewsEvent | None:
    """Glasses -> app news events on SID 0x01: cmd 11 (refresh) and cmd 12 ``{14: {1 eventID, 2 newsId}}``
    (frames.py:69-84, features.dart:583-592)."""
    cmd = pb.read_varint(payload, 1, 0)
    if cmd == CMD_DEV_REQUEST_NEWS_UPGRADE:
        return NewsEvent(cmd)
    if cmd == CMD_DEV_NOTIFY_NEWS_EVENT:
        ev = pb.read_bytes(payload, 14)
        if ev is None:
            return NewsEvent(cmd)
        return NewsEvent(cmd, pb.read_varint(ev, 1, 0), pb.read_varint(ev, 2, 0))
    return None


# ── acks (dashboard.dart:289-302) ───────────────────────────────────────────────────────


def ack_flag(reply: bytes | None) -> int | None:
    """Result flag of a Dashboard_Receive ack ``{3: {1 packageId, 2 flag}}``; None when no reply or
    no field 3.  NOTE: an EMPTY field 3 also means success (P/glasses.py:16-24)."""
    if reply is None:
        return None
    respond = pb.read_bytes(reply, 3)
    if respond is None:
        return None
    return pb.read_varint(respond, 2, 0)


def read_cmd(payload: bytes) -> int:
    return pb.read_varint(payload, 1, 0)


def read_magic(payload: bytes) -> int:
    return pb.read_varint(payload, 2, -1)


# ── quicklist / tasks (SID 0x0C) -- LIVE (quicklist.dart) ───────────────────────────────

QL_CMD_ITEM, QL_CMD_MULT_ITEMS, QL_CMD_EVENT = 1, 2, 3
QL_FULL_UPDATE, QL_ADD, QL_MODIFY, QL_DELETE = 1, 2, 3, 4
QL_TS_DATETIME, QL_TS_DATE, QL_TS_TIME = 0, 1, 2
QL_TITLE_MAX_BYTES = 200
QL_MAX_ITEMS = 20
QL_EVENT_UP, QL_EVENT_DOWN = 1, 2


@dataclass(frozen=True)
class Task:
    uid: int
    title: str
    completed: bool = False
    timestamp: int = 0  # unix s, 0 = none
    ts_type: int = QL_TS_TIME  # the app uses 2


def _title_bytes(title: str) -> bytes:
    """UTF-8 title cut on a code point boundary at <=200 bytes, plus NUL (quicklist.dart:811-815)."""
    return utf8_fit(title, QL_TITLE_MAX_BYTES).encode("utf-8") + b"\x00"


def quicklist_item(t: Task, index: int) -> bytes:
    """Item ``{1 uid, 2 index, 3 isCompleted, 4 timestamp, 5 bytes title+NUL, 7 tsType}``."""
    w = Writer().varint(1, t.uid).varint(2, index).varint(3, 1 if t.completed else 0).varint(4, t.timestamp)
    if t.title:
        w.bytes(5, _title_bytes(t.title))
    return w.varint(7, t.ts_type).to_bytes()


def _mult_items(items: Sequence[Task], data_type: int, total: int | None = None, first_index: int = 0) -> bytes:
    w = Writer().varint(1, data_type).varint(2, len(items) if total is None else total)
    for i, t in enumerate(items):
        w.message(3, quicklist_item(t, first_index + i))
    return w.to_bytes()


def quicklist_full_update(magic: int, tasks: Sequence[Task]) -> bytes:
    """``{1: 2, 2: m, 4: {1: 1 FULL_UPDATE, 2: n, 3: Item...}}`` LIVE; <=20 items per packet (raises above)."""
    if len(tasks) > QL_MAX_ITEMS:
        raise ValueError(f"at most {QL_MAX_ITEMS} items per packet, got {len(tasks)}")
    return varint_field(1, QL_CMD_MULT_ITEMS) + varint_field(2, magic) + message_field(4, _mult_items(tasks, QL_FULL_UPDATE))


def quicklist_add_page(magic: int, tasks: Sequence[Task], first_index: int = 0) -> bytes:
    """Answer to a paging EVENT: MULT_ITEMS ADD with <=20 items (cut here) (quicklist.dart:841-848)."""
    page = list(tasks)[:QL_MAX_ITEMS]
    return varint_field(1, QL_CMD_MULT_ITEMS) + varint_field(2, magic) + message_field(4, _mult_items(page, QL_ADD, first_index=first_index))


def quicklist_empty(magic: int) -> bytes:
    """FULL_UPDATE with no items (the captured connect sequence's ``0x0C cmd 2 {4:{1:1,2:0}}``)."""
    return varint_field(1, QL_CMD_MULT_ITEMS) + varint_field(2, magic) + message_field(4, _mult_items([], QL_FULL_UPDATE, total=0))


def tasks_sequence(magic_fn, tasks: Sequence[Task]) -> list[tuple[int, bytes, str]]:
    """The LIVE push order (features.dart:204-223): dashboard init (0x01) -> 300 ms -> FULL_UPDATE
    (0x0C, await the echo of cmd+magic) -> quicklist display-enable (0x01, flag 0).  Returns
    ``[(sid, payload, expect)]`` with expect 'ack' / 'echo'.  Heartbeat must be running."""
    return [
        (SID_DASHBOARD, dashboard_init(magic_fn()), "ack"),
        (SID_QUICKLIST, quicklist_full_update(magic_fn(), list(tasks)[:QL_MAX_ITEMS]), "echo"),
        (SID_DASHBOARD, quicklist_display_enable(magic_fn()), "ack"),
    ]


@dataclass(frozen=True)
class TaskTick:
    uid: int
    index: int
    completed: bool
    title: str | None


def parse_quicklist_ticks(payload: bytes) -> list[TaskTick]:
    """Completion changes made on the glasses: cmd 1 (field 3) / cmd 2 (field 4) (quicklist.dart:875-891).
    The echo of your own FULL_UPDATE has the same shape: filter by magic."""
    cmd = pb.read_varint(payload, 1, -1)
    raws: list[bytes] = []
    if cmd == QL_CMD_ITEM:
        r = pb.read_bytes(payload, 3)
        raws = [r] if r is not None else []
    elif cmd == QL_CMD_MULT_ITEMS:
        mult = pb.read_bytes(payload, 4)
        raws = pb.read_messages(mult, 3) if mult is not None else []
    out = []
    for raw in raws:
        t = pb.read_bytes(raw, 5)
        title = None if t is None else t.rstrip(b"\x00").decode("utf-8", "replace")
        out.append(TaskTick(pb.read_varint(raw, 1, 0), pb.read_varint(raw, 2, 0), pb.read_varint(raw, 3, 0) != 0, title))
    return out


def parse_quicklist_event(payload: bytes) -> tuple[int, int] | None:
    """cmd 3 ``{5: {1 event 1 UP / 2 DOWN, 2 pivotUid}}`` -> (event, pivot_uid) or None."""
    if pb.read_varint(payload, 1, -1) != QL_CMD_EVENT:
        return None
    ev = pb.read_bytes(payload, 5)
    if ev is None:
        return None
    return pb.read_varint(ev, 1, 0), pb.read_varint(ev, 2, 0)


def page_around(tasks: Sequence[Task], event: int, pivot_uid: int) -> list[Task]:
    """Up to 20 tasks before (UP) or after (DOWN) the pivot uid; [] for an unknown pivot."""
    idx = next((i for i, t in enumerate(tasks) if t.uid == pivot_uid), -1)
    if idx < 0:
        return []
    if event == QL_EVENT_UP:
        return list(tasks[max(0, idx - QL_MAX_ITEMS):idx])
    return list(tasks[idx + 1:idx + 1 + QL_MAX_ITEMS])


# ── stocks -- DECODED, never seen on glasses (stocks.dart) ──────────────────────────────

STOCK_TREND_FLAT, STOCK_TREND_UP, STOCK_TREND_DOWN = 0, 1, 2
_STOCK_FIELDS = [("code", 1, "str"), ("market_suffix", 2, "str"), ("market_cap", 3, "double"), ("change_percent", 4, "float"),
                 ("price", 5, "float"), ("name", 6, "str"), ("day_high", 7, "float"), ("day_low", 8, "float"),
                 ("open", 9, "float"), ("volume", 10, "varint"), ("market_value", 11, "double"), ("pe_ratio", 12, "float"),
                 ("trend", 13, "varint"), ("point_total", 14, "varint"), ("dark_price", 15, "float")]
STOCK_CODE_MAX_BYTES, STOCK_NAME_MAX_BYTES = 63, 127


def stock_message(stock: dict) -> bytes:
    """One Stock message from a dict with keys of ``_STOCK_FIELDS`` plus optional ``points`` (packed floats, field 16)."""
    s = dict(stock)
    points = s.get("points")
    if points and s.get("point_total") is None:
        s["point_total"] = len(points)
    w = Writer()
    for key, fld, kind in _STOCK_FIELDS:
        v = s.get(key)
        if v is None:
            continue
        if kind == "str":
            limit, chars = (STOCK_NAME_MAX_BYTES, 126) if key == "name" else (STOCK_CODE_MAX_BYTES, 62)
            w.string(fld, utf8_fit(_head(str(v), chars), limit))
        elif kind == "float":
            w.float32(fld, float(v))
        elif kind == "double":
            w.double(fld, float(v))
        else:
            w.varint(fld, int(v))
    if points:
        w.packed_floats(16, [float(p) for p in points])
    return w.to_bytes()


def stock_push(magic: int, stock: dict, total: int = 1, index: int = 0, package_id: int | None = None) -> bytes:
    """DECODED (never seen on glasses): ``{1:2, 2:m, 4:{3:{2:{2: StockWidget{1 total, 2 num, 3 Stock}}}}}``
    (stocks.dart:72-80).  The card only shows if the widget order contains 2."""
    widget = varint_field(1, total) + varint_field(2, index) + message_field(3, stock_message(stock))
    content = message_field(2, message_field(2, widget))
    return package(CMD_DASHBOARD_RECEIVE, magic, receive_from_app(package_id, content))


def stock_display_enable(magic: int, widget_order: Sequence[int] = (WIDGET_NEWS, WIDGET_SCHEDULE, WIDGET_STOCK, WIDGET_STOCK),
                         package_id: int = 1, display_mode: int = 4) -> bytes:
    """Base settings that make the stock card visible (stocks.dart:83-91). DECODED."""
    if WIDGET_STOCK not in widget_order:
        raise ValueError("widget_order must contain WIDGET_STOCK (2) or the card never shows")
    ds = display_setting(list(widget_order), [1, 2, 3], display_mode, 1, TEMP_CELSIUS)
    return package(CMD_DASHBOARD_RECEIVE, magic, receive_from_app(package_id, b"", ds))


# ── health (SID 0x0E) -- DECODED (health.dart) ──────────────────────────────────────────

HEALTH_CMD_SINGLE, HEALTH_CMD_MULT = 1, 2
HEALTH_ALL, HEALTH_STEPS, HEALTH_CALORIES, HEALTH_SLEEP, HEALTH_HEART_RATE = 1, 2, 3, 4, 5
HEALTH_SPO2, HEALTH_TEMPERATURE, HEALTH_HRV, HEALTH_PRODUCTIVITY = 6, 7, 8, 9
HEALTH_MAX_ITEMS = 8


def health_single(data_type: int, value: float | None = None, goal: int | None = None, avg: float | None = None,
                  duration: int | None = None, trend: int | None = None, error: int | None = None) -> bytes:
    """HealthSingleData ``{1 type, 2 goal, 3 float value, 4 float avg, 5 duration, 6 err, 7 trend}``;
    zero/None fields omitted (health.dart:119-128)."""
    w = Writer().varint(1, data_type)
    if goal:
        w.varint(2, int(goal))
    if value is not None:
        w.float32(3, float(value))
    if avg is not None:
        w.float32(4, float(avg))
    if duration:
        w.varint(5, int(duration))
    if error:
        w.varint(6, error)
    if trend:
        w.varint(7, trend)
    return w.to_bytes()


def health_push(magic: int, items: Sequence[bytes], data_type: int = HEALTH_ALL) -> bytes:
    """``{1: 2 MULT_DATA, 2: m, 4: {1: dataType, 2: Single...(<=8)}}`` DECODED (health.dart:130-139).
    No display-enable works for the health card (features.dart:315)."""
    w = Writer().varint(1, data_type)
    for it in list(items)[:HEALTH_MAX_ITEMS]:
        w.message(2, it)
    return varint_field(1, HEALTH_CMD_MULT) + varint_field(2, magic) + message_field(4, w.to_bytes())


def health_snapshot(magic: int, steps: float | None = None, step_goal: int | None = None, calories: float | None = None,
                    calorie_goal: int | None = None, sleep: float | None = None, sleep_minutes: int | None = None,
                    heart_rate: float | None = None, heart_rate_avg: float | None = None, spo2: float | None = None,
                    spo2_avg: float | None = None, productivity: float | None = None,
                    productivity_trend: int | None = None) -> bytes:
    """The hub's dashboard snapshot (health.dart:143-166). DECODED."""
    items = []
    if steps is not None:
        items.append(health_single(HEALTH_STEPS, steps, step_goal))
    if calories is not None:
        items.append(health_single(HEALTH_CALORIES, calories, calorie_goal))
    if sleep is not None or sleep_minutes:
        items.append(health_single(HEALTH_SLEEP, 0 if not sleep else sleep, duration=sleep_minutes))
    if heart_rate is not None:
        items.append(health_single(HEALTH_HEART_RATE, heart_rate, avg=heart_rate_avg))
    if spo2 is not None:
        items.append(health_single(HEALTH_SPO2, spo2, avg=spo2_avg))
    if productivity is not None:
        items.append(health_single(HEALTH_PRODUCTIVITY, productivity, trend=productivity_trend))
    return health_push(magic, items)


# ── watchface (SID 0x1F) -- DECODED, untested, RISKY ────────────────────────────────────

WF_CMD_REQUEST_INFO, WF_CMD_INFO_RESPONSE, WF_CMD_REQUEST_UPGRADE, WF_CMD_UPGRADE_RESPONSE = 0, 1, 2, 3
WF_CMD_TRANSMIT_START, WF_CMD_FILE_DATA, WF_CMD_FILE_DATA_ACK, WF_CMD_UPDATE_SUCCESS = 4, 5, 6, 7
WF_CMD_NAMES = {0: "REQUEST_INFO", 1: "INFO_RESPONSE", 2: "REQUEST_UPGRADE", 3: "UPGRADE_RESPONSE", 4: "TRANSMIT_START",
                5: "FILE_DATA", 6: "FILE_DATA_ACK", 7: "UPDATE_SUCCESS"}
WF_COMP_NAMES = {0: "none", 1: "temperature", 2: "sunset", 3: "next_rain", 4: "notification", 5: "heart_rate", 6: "steps",
                 7: "calories", 8: "blood_oxygen", 9: "sleep"}


def watchface_info_request(magic: int) -> bytes:
    """READ ONLY: ``{1: 0, 2: m, 3: {1: 1}}`` -> ``0800107c1a020801`` (magic 0x7c).  DECODED, RISKY
    area: keep the reported version/hash; this library deliberately has NO upload builder because a
    bad layout file may blank the dashboard until a valid one is uploaded (watchface.dart:2-3)."""
    return varint_field(1, WF_CMD_REQUEST_INFO) + varint_field(2, magic) + message_field(3, varint_field(1, 1))


@dataclass
class WatchfaceMessage:
    cmd: int
    magic: int
    exists: int | None = None
    version: str | None = None
    hash: str | None = None
    status: int | None = None
    value: int | None = None

    @property
    def name(self) -> str:
        return WF_CMD_NAMES.get(self.cmd, f"cmd={self.cmd}")


def parse_watchface(payload: bytes) -> WatchfaceMessage:
    """Decode a DashboardExtPackage ``{1 cmd, 2 m, (cmd+3): body}`` (watchface.dart:348-374)."""
    m = WatchfaceMessage(pb.read_varint(payload, 1, 0), pb.read_varint(payload, 2, -1))
    sub = pb.read_bytes(payload, m.cmd + 3) if m.cmd in WF_CMD_NAMES else None
    if sub is None:
        return m
    if m.cmd == WF_CMD_INFO_RESPONSE:
        m.exists, m.version, m.hash = pb.read_varint(sub, 1, 0), pb.read_string(sub, 2), pb.read_string(sub, 3)
    elif m.cmd == WF_CMD_UPGRADE_RESPONSE:
        m.status = pb.read_varint(sub, 1, 0)
    elif m.cmd == WF_CMD_FILE_DATA_ACK:
        m.status = pb.read_varint(sub, 6, 0)
    else:
        m.value = pb.read_varint(sub, 1, 0)
    return m
