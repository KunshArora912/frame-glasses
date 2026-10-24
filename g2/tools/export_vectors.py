"""Export golden test vectors from g2.native for the Kotlin port (android/g2core).

Run from C:/Users/Kunsh/Desktop/frame:

    .venv/Scripts/python.exe -m g2.tools.export_vectors

Writes android/g2core/src/test/resources/vectors.json: a list of
``{"name", "fn", "args", "out"}`` where ``fn`` names the Python builder, ``args`` are the exact
inputs (snake_case, bytes as hex strings) and ``out`` is the expected result: a hex string for a
builder that returns bytes, a list of hex strings for a frame list, or a small structure for the
few builders that return lists of tuples.  Every input is fixed (magics, seqs, clocks, time zones)
so the file is reproducible byte for byte.  No device identifiers or keys appear anywhere.
"""
from __future__ import annotations

import json
import os
import sys

from g2.native import apps, dashboard, even_ai, framing, menu, notifications, pb, session, settings
from g2.native.framing import FrameAssembler, MagicAllocator

OUT = os.path.join(os.path.dirname(__file__), "..", "..", "android", "g2core", "src", "test", "resources", "vectors.json")

V: list[dict] = []


def h(b: bytes) -> str:
    return bytes(b).hex()


def add(name: str, fn: str, args: dict, out) -> None:
    if isinstance(out, (bytes, bytearray)):
        out = h(out)
    elif isinstance(out, list) and out and isinstance(out[0], (bytes, bytearray)):
        out = [h(x) for x in out]
    V.append({"name": name, "fn": fn, "args": args, "out": out})


def pat(n: int, k: int = 7, c: int = 3) -> bytes:
    return bytes((i * k + c) & 0xFF for i in range(n))


def rx(frames: list[bytes]) -> list[bytes]:
    out = []
    for f in frames:
        b = bytearray(f)
        b[1] = 0x12
        out.append(bytes(b))
    return out


def build() -> None:
    # ── pb ──────────────────────────────────────────────────────────────────────────────
    for v in (0, 1, 127, 128, 300, 156, 16383, 16384, 65365, 0xFFFFFFFF, 1792497600000, -1, -16, -24, -128, -(1 << 31)):
        add(f"pb.varint {v}", "pb.encode_varint", {"value": v}, pb.encode_varint(v))
    w = (pb.Writer().varint(1, 300).int32(2, -5).bool(3, True).string(4, "héllo €").bytes(5, b"\x00\xff").float32(6, 1.5)
         .double(7, 2.25).message(8, pb.Writer().varint(1, 7)).packed_varints(9, [1, 3, 2, 2, 300]).packed_floats(11, [1.0, -2.5]))
    add("pb.writer mixed", "pb.writer_mixed", {}, w.to_bytes())
    for f in (21.0, -3.5, 0.1, 190.5, 1e10, -0.0):
        add(f"pb.float32 {f}", "pb.float_field", {"field": 1, "value": f}, pb.float_field(1, f))

    # ── framing ─────────────────────────────────────────────────────────────────────────
    for s in (b"", b"123456789", session.PRELUDE_PAYLOAD, pat(300)):
        add(f"framing.crc16 len{len(s)}", "framing.crc16", {"data": h(s)}, f"{framing.crc16(s):04x}")
    for n in (0, 2, 100, 229, 230, 231, 232, 233, 463, 464, 465, 700, 1000):
        p = pat(n)
        add(f"framing.frame_pb n{n}", "framing.frame_pb", {"pb": h(p), "sid": 0x01, "flag": 0x20, "seq": 0x2A, "max_write": 240},
            framing.frame_pb(p, 0x01, 0x20, 0x2A))
    add("framing.frame_pb max_write 60", "framing.frame_pb", {"pb": h(pat(100)), "sid": 0x09, "flag": 0x00, "seq": 255, "max_write": 60},
        framing.frame_pb(pat(100), 0x09, 0x00, 255, 60))
    for m, s in ((1, None), (2, None), (0x0C, 1), (127, None), (128, None), (200, 9), (255, None)):
        add(f"framing.auth_frame m{m} s{s}", "framing.auth_frame", {"magic": m, "seq": s}, framing.auth_frame(m, s))
    pk = bytes.fromhex("aa21010c010180000804100c1a0408011004")
    add("framing.add_crc", "framing.add_crc", {"packet": h(pk)}, framing.add_crc(pk))

    for n, sid, seq in ((5, 0x09, 3), (231, 0x01, 1), (300, 0x01, 7), (520, 0x07, 200), (1200, 0xC4, 0)):
        pkts = rx(framing.frame_pb(pat(n, 13, 1), sid, 0x00, seq))
        asm = FrameAssembler()
        outs = [asm.feed(p) for p in pkts]
        assert all(o is None for o in outs[:-1])
        add(f"framing.assemble n{n}", "framing.assemble", {"packets": [h(p) for p in pkts]},
            {"assembled": h(outs[-1]), "payload": h(framing.parse_frame(outs[-1]).payload), "crc_errors": asm.crc_errors})
    pkts = rx(framing.frame_pb(pat(300), 0x01, 0x00, 7))
    order = (pkts[1], pkts[0], pkts[1])
    asm = FrameAssembler()
    outs = [asm.feed(p) for p in order]
    add("framing.assemble out-of-order", "framing.assemble_seq", {"packets": [h(p) for p in order]},
        {"outs": [None if o is None else h(o) for o in outs], "crc_errors": asm.crc_errors})
    bad = bytearray(pkts[1])
    bad[-1] ^= 0xFF
    asm = FrameAssembler()
    outs = [asm.feed(p) for p in (pkts[0], bytes(bad))]
    add("framing.assemble bad crc", "framing.assemble_seq", {"packets": [h(pkts[0]), h(bad)]},
        {"outs": [None if o is None else h(o) for o in outs], "crc_errors": asm.crc_errors})
    for p in ("080210661a0408011000", "109c01", "1a00", "0880011068", "08", ""):
        add(f"framing.read_ack_magic '{p}'", "framing.read_ack_magic", {"pb": p}, framing.read_ack_magic(bytes.fromhex(p)))

    # ── session ─────────────────────────────────────────────────────────────────────────
    add("session.prelude_payload", "session.prelude_payload", {}, session.PRELUDE_PAYLOAD)
    add("session.prelude_frame", "session.prelude_frame", {}, session.PRELUDE_FRAME)
    for m in (1, 0x0C, 200):
        add(f"session.auth_payload {m}", "session.auth_payload", {"magic": m}, session.auth_payload(m))
    for m, r in ((0x69, 1), (0x0E, 2), (5, 0)):
        add(f"session.role_change {m} {r}", "session.role_change", {"magic": m, "role": r}, session.role_change(m, r))
    for m, t, tz in ((0x68, 1792497600, -16), (0x0F, 1700000000, -24), (1, 0, 0), (200, 0xFFFFFFFF, 127), (7, 1792497600, -128),
                     (9, 1792497600, 22)):
        add(f"session.time_sync {m} {t} {tz}", "session.time_sync", {"magic": m, "unix_s": t, "tz_units": tz}, session.time_sync(m, t, tz))
    for off in (-4 * 3600, 5 * 3600 + 1800, -14 * 3600 * 10, 0, -1800 - 1, 13 * 3600 + 2700):
        add(f"session.tz_units {off}", "session.tz_units_from_offset", {"offset_seconds": off}, session.tz_units_from_offset(off))
    for m in (0x6B, 1, 255):
        add(f"session.keepalive {m}", "session.keepalive", {"magic": m}, session.keepalive(m))
    add("session.keepalive_frame", "session.keepalive_frame", {"magic": 0x6B, "seq": 0x0E}, session.keepalive_frame(0x6B, 0x0E))
    for m in (0x65, 1, 127, 128, 255):
        add(f"session.evenhub_heartbeat {m}", "session.evenhub_heartbeat", {"magic": m}, session.evenhub_heartbeat(m))
    add("session.evenhub_heartbeat frame", "session.evenhub_heartbeat_frame", {"magic": 0x65, "seq": 11},
        framing.frame_pb(session.evenhub_heartbeat(0x65), 0xE0, 0x20, 11))
    add("session.app_sync_subscribe", "session.app_sync_subscribe", {"magic": 0x72}, session.app_sync_subscribe(0x72))
    add("session.onboarding_finish", "session.onboarding_finish", {"magic": 0x20}, session.onboarding_finish(0x20))
    add("session.wear_query", "session.wear_query", {"magic": 0x21}, session.wear_query(0x21))
    add("session.case_query", "session.case_query", {"magic": 0x22}, session.case_query(0x22))
    for full in (False, True):
        seq = session.connect_sequence(MagicAllocator().next_magic, now=1792497600, tz_units=-16, full=full)
        add(f"session.connect_sequence full={full}", "session.connect_sequence", {"now": 1792497600, "tz_units": -16, "full": full},
            [[s, h(p), side] for s, p, side in seq])
    plan = session.MinimalDashboardPlan(MagicAllocator(), now=1792497600, tz_units=-16, temperature_c=21, weather_type=1,
                                        weather_status="Sunny")
    add("session.minimal_dashboard_plan", "session.minimal_dashboard_plan",
        {"now": 1792497600, "tz_units": -16, "temperature_c": 21.0, "weather_type": 1, "weather_status": "Sunny"},
        [{"number": s.number, "label": s.label, "action": s.action, "side": s.side, "sid": s.sid, "magic": s.magic,
          "frames": [h(f) for f in s.frames], "wait_ms": s.wait_ms, "expect": s.expect, "timeout_ms": s.timeout_ms} for s in plan.steps()])

    # ── settings ────────────────────────────────────────────────────────────────────────
    for t in (0, 1):
        add(f"settings.read_settings {t}", "settings.read_settings", {"magic": 0x6A, "setting_type": t}, settings.read_settings(0x6A, t))
    for lv in (1, 20, 50):
        add(f"settings.brightness {lv}", "settings.brightness", {"magic": 0x7D, "level": lv}, settings.brightness(0x7D, lv))
    for on in (True, False):
        add(f"settings.auto_brightness {on}", "settings.auto_brightness", {"magic": 3, "on": on}, settings.auto_brightness(3, on))
    add("settings.brightness_calibration", "settings.brightness_calibration", {"magic": 4, "left": 10, "right": 255},
        settings.brightness_calibration(4, 10, 255))
    for lv in (0, 10, 20):
        add(f"settings.display_height {lv}", "settings.display_height", {"magic": 0x7E, "level": lv}, settings.display_height(0x7E, lv))
        add(f"settings.display_distance {lv}", "settings.display_distance", {"magic": 0x7F, "level": lv}, settings.display_distance(0x7F, lv))
    add("settings.head_up on 30", "settings.head_up", {"magic": 1, "enabled": True, "angle": 30}, settings.head_up(1, True, 30))
    add("settings.head_up off", "settings.head_up", {"magic": 1, "enabled": False, "angle": None}, settings.head_up(1, False))
    add("settings.wear_detection", "settings.wear_detection", {"magic": 2, "on": True}, settings.wear_detection(2, True))
    add("settings.silent_mode", "settings.silent_mode", {"magic": 2, "on": False}, settings.silent_mode(2, False))
    add("settings.units default", "settings.units", {"magic": 0x6B, "unit_format": 0, "distance_unit": 0, "time_format": 1, "date_format": 0,
                                                     "temperature_unit": 1}, settings.units(0x6B))
    add("settings.units custom", "settings.units", {"magic": 1, "unit_format": 0, "distance_unit": 1, "time_format": 0, "date_format": 2,
                                                    "temperature_unit": 2}, settings.units(1, 0, 1, 0, 2, 2))
    add("settings.gestures default", "settings.gestures", {"magic": 0x6E, "entries": [list(e) for e in settings.GESTURES_APP_DEFAULT]},
        settings.gestures(0x6E))
    add("settings.gestures custom", "settings.gestures", {"magic": 5, "entries": [[1, 15, 1], [0, 3, 0]]},
        settings.gestures(5, [(1, 15, 1), (0, 3, 0)]))
    add("settings.dominant_hand", "settings.dominant_hand", {"magic": 1, "hand": 1, "ring_mac": "010203040506"},
        settings.dominant_hand(1, 1, bytes.fromhex("010203040506")))
    add("settings.dominant_hand nomac", "settings.dominant_hand", {"magic": 1, "hand": 0, "ring_mac": None}, settings.dominant_hand(1, 0))
    add("settings.app_ack", "settings.app_ack", {"magic": 9, "package_id": 3, "flag": 1}, settings.app_ack(9, 3, 1))
    for s in (3, 5, 10, 15, 30, None):
        add(f"settings.display_timeout {s}", "settings.display_timeout", {"magic": 2, "seconds": s}, settings.display_timeout(2, s))
    add("settings.read_display_timeout", "settings.read_display_timeout", {"magic": 128}, settings.read_display_timeout(128))
    for i in (0, 8):
        add(f"settings.language {i}", "settings.language", {"magic": 3, "index": i}, settings.language(3, i))
    add("settings.get_device_info", "settings.get_device_info", {"magic": 4, "item": 5}, settings.get_device_info(4, 5))
    add("settings.quick_restart", "settings.quick_restart", {"magic": 5}, settings.quick_restart(5))
    add("settings.factory_reset", "settings.factory_reset", {"magic": 6}, settings.factory_reset(6))
    add("settings.unpair", "settings.unpair", {"magic": 7}, settings.unpair(7))

    # ── dashboard ───────────────────────────────────────────────────────────────────────
    add("dashboard.dashboard_init", "dashboard.dashboard_init", {"magic": 0x66}, dashboard.dashboard_init(0x66))
    add("dashboard.dashboard_init 200", "dashboard.dashboard_init", {"magic": 200}, dashboard.dashboard_init(200))
    add("dashboard.dashboard_layout", "dashboard.dashboard_layout", {"magic": 0x67, "widget_order": [3, 1]},
        dashboard.dashboard_layout(0x67, [3, 1]))
    add("dashboard.display_enable", "dashboard.display_enable", {"magic": 1, "widget_types": [1, 3, 4]}, dashboard.display_enable(1, [1, 3, 4]))
    add("dashboard.quicklist_display_enable", "dashboard.quicklist_display_enable", {"magic": 0x7A}, dashboard.quicklist_display_enable(0x7A))
    wx = [
        dict(magic=0x73, temperature_c=21.0, unit=1, weather_type=1, update_time_ms=1792497600000, status="Sunny"),
        dict(magic=1, temperature_c=-3.5, unit=2, weather_type=9, update_time_ms=0, status="x" * 40, rainfall_icon=2, rainfall_hint="rain",
             sunset_select=1, sunset_hint="sun"),
        dict(magic=200, temperature_c=0.1, unit=1, weather_type=16, update_time_ms=1, status="Ciel dégagé ☀"),
        dict(magic=2, temperature_c=37.25, unit=1, weather_type=0, update_time_ms=0, status=""),
        dict(magic=3, temperature_c=21.0, unit=1, weather_type=5, update_time_ms=5, status="Rain", package_id=1),
        dict(magic=4, temperature_c=20.0, unit=1, weather_type=1, update_time_ms=0, status="😀" * 40),
    ]
    for i, a in enumerate(wx):
        add(f"dashboard.weather {i}", "dashboard.weather", a, dashboard.weather(**a))
    sched = [dict(title="Standup", location="Room 1", time_str="10:00 AM-10:30 AM", end_ts=1792500000),
             dict(title="B", location="", time_str="", end_ts=5),
             dict(title="é" * 200, location="Ort €" * 70, time_str="Tmr 09:00-09:30", end_ts=1792500000, schedule_id=42)]

    def sch(d):
        return dashboard.Schedule(**d)

    add("dashboard.calendar_event 1", "dashboard.calendar_event",
        {"magic": 0x74, "schedule": sched[0], "total": 1, "index": 0, "authority": 0}, dashboard.calendar_event(0x74, sch(sched[0]), 1, 0))
    add("dashboard.calendar_event long", "dashboard.calendar_event",
        {"magic": 9, "schedule": sched[2], "total": 3, "index": 2, "authority": 0}, dashboard.calendar_event(9, sch(sched[2]), 3, 2))
    add("dashboard.calendar_none", "dashboard.calendar_event", {"magic": 0x75, "schedule": None, "total": 0, "index": 0, "authority": 1},
        dashboard.calendar_none(0x75))
    add("dashboard.calendar_events multi", "dashboard.calendar_events", {"magic": 1, "schedules": sched[:2]},
        dashboard.calendar_events(1, [sch(s) for s in sched[:2]]))
    add("dashboard.calendar_events ids", "dashboard.calendar_events", {"magic": 2, "schedules": sched},
        dashboard.calendar_events(2, [sch(s) for s in sched]))
    ten = [sched[0], sched[1]] * 5
    add("dashboard.calendar_sequence 10", "dashboard.calendar_sequence", {"schedules": ten},
        dashboard.calendar_sequence(MagicAllocator().next_magic, [sch(s) for s in ten]))
    add("dashboard.calendar_sequence empty", "dashboard.calendar_sequence", {"schedules": []},
        dashboard.calendar_sequence(MagicAllocator().next_magic, []))
    add("dashboard.news_reset", "dashboard.news_reset", {"magic": 0x76}, dashboard.news_reset(0x76))
    add("dashboard.news_fifo_count_request", "dashboard.news_fifo_count_request", {"magic": 0x77}, dashboard.news_fifo_count_request(0x77))
    add("dashboard.news_push", "dashboard.news_push", {"magic": 0x78, "slot": 2, "title": "Title", "source": "Src", "content": "Body",
                                                       "report_time_local_ms": 1792497600000, "session_total": 5},
        dashboard.news_push(0x78, 2, dashboard.NewsItem("Title", "Src", "Body"), 1792497600000, session_total=5))
    tasks = [dict(uid=7, title="Milk"), dict(uid=8, title="Eggs", completed=True), dict(uid=300, title="Brot €", timestamp=1792497600, ts_type=0),
             dict(uid=9, title="é" * 150)]

    def tk(d):
        return dashboard.Task(**d)

    add("dashboard.quicklist_full_update 2", "dashboard.quicklist_full_update", {"magic": 0x20, "tasks": tasks[:2]},
        dashboard.quicklist_full_update(0x20, [tk(t) for t in tasks[:2]]))
    add("dashboard.quicklist_full_update 4", "dashboard.quicklist_full_update", {"magic": 0x21, "tasks": tasks},
        dashboard.quicklist_full_update(0x21, [tk(t) for t in tasks]))
    many = [dict(uid=i, title=f"Task {i}") for i in range(25)]
    add("dashboard.quicklist_add_page", "dashboard.quicklist_add_page", {"magic": 1, "tasks": many, "first_index": 20},
        dashboard.quicklist_add_page(1, [tk(t) for t in many], 20))
    add("dashboard.quicklist_empty", "dashboard.quicklist_empty", {"magic": 5}, dashboard.quicklist_empty(5))
    add("dashboard.tasks_sequence", "dashboard.tasks_sequence", {"tasks": tasks[:2]},
        [[s, h(p), e] for s, p, e in dashboard.tasks_sequence(MagicAllocator().next_magic, [tk(t) for t in tasks[:2]])])
    stock = {"code": "AAPL", "market_suffix": "US", "market_cap": 3.1e12, "change_percent": -1.25, "price": 190.5, "name": "Apple Inc.",
             "day_high": 191.0, "day_low": 188.25, "open": 189.0, "volume": 51234567, "market_value": 2.5e9, "pe_ratio": 29.5, "trend": 2,
             "dark_price": 190.75, "points": [1.0, 2.0, 1.5]}
    add("dashboard.stock_push full", "dashboard.stock_push", {"magic": 1, "stock": stock, "total": 2, "index": 1},
        dashboard.stock_push(1, stock, 2, 1))
    add("dashboard.stock_push min", "dashboard.stock_push", {"magic": 2, "stock": {"code": "X" * 80, "price": 1.0}, "total": 1, "index": 0},
        dashboard.stock_push(2, {"code": "X" * 80, "price": 1.0}))
    add("dashboard.stock_display_enable", "dashboard.stock_display_enable", {"magic": 3}, dashboard.stock_display_enable(3))
    add("dashboard.health_snapshot", "dashboard.health_snapshot",
        {"magic": 1, "steps": 5000.0, "step_goal": 8000, "heart_rate": 70.0, "heart_rate_avg": 65.5, "sleep_minutes": 420},
        dashboard.health_snapshot(1, steps=5000, step_goal=8000, heart_rate=70, heart_rate_avg=65.5, sleep_minutes=420))
    add("dashboard.watchface_info_request", "dashboard.watchface_info_request", {"magic": 0x7C}, dashboard.watchface_info_request(0x7C))

    # ── menu ────────────────────────────────────────────────────────────────────────────
    add("menu.menu_push default", "menu.menu_push", {"magic": 0x7B, "builtin_ids": list(menu.DEFAULT_MENU), "plugins": []},
        menu.menu_push(0x7B))
    add("menu.menu_push all", "menu.menu_push", {"magic": 3, "builtin_ids": [1, 4, 5, 6, 7, 8, 11, 48, 266], "plugins": []},
        menu.menu_push(3, [1, 4, 5, 6, 7, 8, 11, 48, 266]))
    plug = [{"name": "My App", "app_id": 10000, "icon": 1}, {"name": "é" * 40, "app_id": 2, "icon": 3},
            {"name": "  \x01 ", "app_id": 3, "icon": 1}]
    add("menu.menu_push plugins", "menu.menu_push", {"magic": 1, "builtin_ids": [1, 4], "plugins": plug},
        menu.menu_push(1, [1, 4], [menu.PluginItem(**p) for p in plug]))

    # ── notifications ───────────────────────────────────────────────────────────────────
    for s in (b"", b"123456789", pat(1000), "{\"a\":\"é\"}".encode()):
        add(f"notifications.crc32_efs len{len(s)}", "notifications.crc32_efs", {"data": h(s)}, f"{notifications.crc32_efs(s):08x}")
        add(f"notifications.crc32c len{len(s)}", "notifications.crc32c", {"data": h(s)}, f"{notifications.crc32c(s):08x}")
    add("notifications.notification_ctrl", "notifications.notification_ctrl",
        {"magic": 0x10, "enable": True, "auto_display": True, "disp_time": 5, "dnd": False}, notifications.notification_ctrl(0x10))
    add("notifications.notification_ctrl dnd", "notifications.notification_ctrl",
        {"magic": 0x10, "enable": True, "auto_display": True, "disp_time": 7, "dnd": True}, notifications.notification_ctrl(0x10, dnd=True, disp_time=7))
    add("notifications.notification_ctrl off", "notifications.notification_ctrl",
        {"magic": 1, "enable": False, "auto_display": False, "disp_time": 0, "dnd": False}, notifications.notification_ctrl(1, False, False, 0))
    add("notifications.whitelist_disable", "notifications.whitelist_disable", {"magic": 0x11, "disable": True},
        notifications.whitelist_disable(0x11))
    add("notifications.ios_register", "notifications.ios_register", {"magic": 1, "app_id": "com.example.app" * 3, "display_name": "X"},
        notifications.ios_register(1, "com.example.app" * 3, "X"))
    nj = [
        dict(title="Title", message="Body", subtitle="Sub", now=1700000000, utc_offset_s=3600, msg_id=42),
        dict(title="é" * 100, message="x" * 600, display_name="n" * 50, app_identifier="a" * 80, now=0, utc_offset_s=0),
        dict(title='q"x\\y', message="line1\nline2\ttab\r\x01\x1f\x7f\x08\x0c", subtitle="", now=1792497600, utc_offset_s=-4 * 3600, msg_id=7),
        dict(title="Python", message="Hello!", subtitle="Test", app_identifier="com.google.android.gm", display_name="Gmail", now=1700000000,
             utc_offset_s=0, msg_id=12345),
        dict(title="Emoji 😀", message="中文 текст", now=1792497600, utc_offset_s=19800),
    ]
    for i, a in enumerate(nj):
        full = dict(subtitle="", display_name="Even Realities", app_identifier="com.even.sg", msg_id=None)
        full.update(a)
        add(f"notifications.notification_json {i}", "notifications.notification_json", full, notifications.notification_json(**a))
    data = notifications.notification_json(**nj[0])
    add("notifications.efs_start", "notifications.efs_start", {"seq": 0x10, "data": h(data)}, notifications.efs_start(0x10, data))
    add("notifications.efs_data_prefix", "notifications.efs_data_prefix", {"seq": 0x49}, notifications.efs_data_prefix(0x49))
    add("notifications.efs_result_check", "notifications.efs_result_check", {"seq": 0xDA}, notifications.efs_result_check(0xDA))
    for n in (0, 100, 231, 232, 233, 464, 4096):
        add(f"notifications.efs_data_packets {n}", "notifications.efs_data_packets", {"seq": 0x49, "block": h(pat(n))},
            notifications.efs_data_packets(0x49, pat(n)))
    big = pat(5000, 3, 9)
    for nm, blob in (("5000", big), ("json", data)):
        add(f"notifications.hub_notification_steps {nm}", "notifications.hub_notification_steps", {"data": h(blob)},
            [{"name": s.name, "frames": [h(f) for f in s.frames], "expect_cid": s.expect_cid, "offset": s.offset}
             for s in notifications.hub_notification_steps(MagicAllocator().next_seq, blob)])
    cdata = notifications.notification_json(**nj[3])
    add("notifications.community_notification_frames", "notifications.community_notification_frames", {"data": h(cdata)},
        notifications.community_notification_frames(cdata))
    add("notifications.community_data 300", "notifications.community_data", {"data": h(pat(300))}, notifications.community_data(pat(300)))
    add("notifications.community_file_check", "notifications.community_file_check", {"data": h(data), "seq": 0x10},
        notifications.community_file_check(data, 0x10))

    # ── even ai ─────────────────────────────────────────────────────────────────────────
    add("even_ai.enter 129", "even_ai.enter", {"magic": 129}, even_ai.enter(129))
    add("even_ai.enter 100", "even_ai.enter", {"magic": 100}, even_ai.enter(100))
    add("even_ai.exit", "even_ai.exit_", {"magic": 5}, even_ai.exit_(5))
    for m, c in ((6, 3), (200, 1000)):
        add(f"even_ai.heartbeat {m} {c}", "even_ai.heartbeat", {"magic": m, "count": c}, even_ai.heartbeat(m, c))
    add("even_ai.config", "even_ai.config", {"magic": 1, "voice_switch": 0, "stream_speed": 80}, even_ai.config(1))
    add("even_ai.config_captured", "even_ai.config_captured", {"magic": 1}, even_ai.config_captured(1))
    for text, com in (("Hi", False), ("What is 2 + 2?", True), ("Wie spät ist es? ⏰", False)):
        add(f"even_ai.ask {text} {com}", "even_ai.ask", {"magic": 7, "text": text, "community": com}, even_ai.ask(7, text, com))
    add("even_ai.analyse", "even_ai.analyse", {"magic": 8}, even_ai.analyse(8))
    for text, fin, com in (("ok", True, False), ("ok", False, False), ("The answer is 4!", True, True), ("é" * 256, True, False)):
        add(f"even_ai.reply {len(text)} {fin} {com}", "even_ai.reply", {"magic": 9, "chunk": text, "final": fin, "community": com},
            even_ai.reply(9, text, fin, com))
    for text, lim in (("é" * 600, 512), ("", 512), ("abc😀def" * 10, 16), ("x" * 1100, 512)):
        add(f"even_ai.reply_chunks {len(text)} {lim}", "even_ai.reply_chunks", {"text": text, "limit": lim},
            {"chunks": [h(c) for c in even_ai.reply_chunks(text, lim)]})
    add("even_ai.reply_sequence", "even_ai.reply_sequence", {"text": "A" * 600 + "é" * 300},
        even_ai.reply_sequence(MagicAllocator().next_magic, "A" * 600 + "é" * 300))
    add("even_ai.vad_info", "even_ai.vad_info", {"magic": 1, "status": 2}, even_ai.vad_info(1, 2))
    add("even_ai.prompt", "even_ai.prompt", {"magic": 1, "code": 9}, even_ai.prompt(1, 9))
    add("even_ai.event", "even_ai.event", {"magic": 1, "event_id": 2}, even_ai.event(1, 2))
    for en in (True, False):
        add(f"even_ai.evenhub_audio_control {en}", "even_ai.evenhub_audio_control", {"magic": 2, "enable": en},
            even_ai.evenhub_audio_control(2, en))

    # ── apps ────────────────────────────────────────────────────────────────────────────
    add("apps.tp_start", "apps.tp_start", {"magic": 1, "total_pages": 3, "total_lines": 25, "mode": 1, "start_page": 0, "start_line": 0,
                                           "scroll_interval_ms": 0, "countdown_s": 3}, apps.tp_start(1, 3, 25, countdown_s=3))
    add("apps.tp_page", "apps.tp_page", {"magic": 2, "page_id": 1, "lines": ["a", "b"]}, apps.tp_page(2, 1, ["a", "b"]))
    add("apps.tp_page empty", "apps.tp_page", {"magic": 2, "page_id": 0, "lines": []}, apps.tp_page(2, 0, []))
    add("apps.tp_heartbeat", "apps.tp_heartbeat", {"magic": 3, "page_id": 2, "line_id": 5}, apps.tp_heartbeat(3, 2, 5))
    add("apps.tp_close", "apps.tp_close", {"magic": 4}, apps.tp_close(4))
    files = [["id1", "Speech"], ["id2", "A very long file name that exceeds"]]
    add("apps.tp_file_list", "apps.tp_file_list", {"magic": 1, "files": files, "menu_mode": 1},
        apps.tp_file_list(1, [tuple(f) for f in files], 1))
    scripts = ["word " * 100,
               "Hello world.\n\nThis is   a teleprompter test with some longer words like internationalization.\r\nEnd",
               "supercalifragilisticexpialidocious and more words without spaces here " * 3,
               "\n\n  x  \n\n",
               "Tabs\tand\ttabs here plus a fairly long line of normal words that wraps more than once ok " * 6]
    for i, text in enumerate(scripts):
        add(f"apps.tp_paginate {i}", "apps.tp_paginate", {"script": text}, {"pages": apps.tp_paginate(text)})
    tpt = "Hello world, this is the community teleprompter flow. " * 8
    add("apps.community_tp_frames", "apps.community_tp_frames", {"text": tpt}, apps.community_tp_frames(tpt))
    add("apps.tr_open", "apps.tr_open", {"magic": 1, "src_lang": "en", "dst_lang": "de", "lang_list": [["EN", "English"], ["DE", "Deutsch"]]},
        apps.tr_open(1, "en", "de", lang_list=[("EN", "English"), ("DE", "Deutsch")]))
    add("apps.tr_text", "apps.tr_text", {"magic": 2, "src_text": " Hallo ", "dst_text": "Hello", "final": True, "speaker": "A"},
        apps.tr_text(2, " Hallo ", "Hello", True, "A"))
    add("apps.tr_text space", "apps.tr_text", {"magic": 2, "src_text": "a", "dst_text": " ", "final": False, "speaker": None},
        apps.tr_text(2, "a", " ", False))
    add("apps.tr_heartbeat", "apps.tr_heartbeat", {"magic": 3}, apps.tr_heartbeat(3))
    add("apps.tr_close", "apps.tr_close", {"magic": 4}, apps.tr_close(4))
    add("apps.nav_basic_info", "apps.nav_basic_info",
        {"magic": 1, "direction": "turn right", "distance": "200 m", "road": "Main St", "eta": "12:30", "method": 2, "counter": 3},
        apps.nav_basic_info(1, "turn right", "200 m", "Main St", eta="12:30", method=2, counter=3))
    add("apps.nav_start", "apps.nav_start", {"magic": 2}, apps.nav_start(2))
    add("apps.nav_location_list", "apps.nav_location_list", {"magic": 4, "names": ["Home", ""], "icons": [1, 2]},
        apps.nav_location_list(4, ["Home", ""], [1, 2]))
    add("apps.nav_error_info", "apps.nav_error_info", {"magic": 5, "message": "No route", "index": 2}, apps.nav_error_info(5, "No route", 2))


def main() -> None:
    build()
    path = os.path.abspath(OUT)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(V, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print(f"wrote {len(V)} vectors to {path}", file=sys.stderr)


if __name__ == "__main__":
    main()
