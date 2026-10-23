# g2.native — Even Realities G2 native firmware protocol (Python, stdlib only)

Builders and parsers for the glasses' **built-in** UI: dashboard (clock/weather/calendar/news/tasks),
settings, menu, notifications, Even AI and the native apps (teleprompter, translate, navigation).
Transport-agnostic: every builder returns bytes, every parser takes bytes. Nothing here opens a
BLE or network connection by itself; a transport object does the I/O.

Primary source: [`../docs/native-protocol.md`](../docs/native-protocol.md) and the Dart port it cites
(`../vendor/open-g2-hub/lib/protocol/*.dart`, `lib/hub/*.dart`). Secondary: `docs/positioning-and-surfaces.md`
§1 and the `even-g2-protocol` community branches `feature/even-ai` / `feature/notification-simple`.
Where the doc and the Dart disagreed, the Dart won (see "Deviations" below).

Tests: `C:\Users\Kunsh\Desktop\frame\.venv\Scripts\python.exe -m unittest g2.test_native` from
`C:\Users\Kunsh\Desktop\frame` (59 tests, byte-exact against every hex vector in the doc and the
community files; no hardware).

## Module map

| Module | What | Key names |
|---|---|---|
| `pb.py` | Hand-rolled protobuf wire codec (= `pb.dart`): varint (negatives as 10-byte two's complement), length-delimited, fixed32/64, packed, nested | `Writer`, `Reader`, `iter_fields`, `read_varint/string/bytes/float/messages`, `encode_varint`, `unhex` |
| `framing.py` | `AA 21 seq len total num sid flag payload crc16` envelope, 232-byte fragmentation, `AA 12` parsing, inbound reassembly, counters | `frame_pb`, `auth_frame`, `parse_frame`, `parse_rx`, `read_ack_magic`, `FrameAssembler`, `MagicAllocator`, `crc16` |
| `session.py` | Prelude, keep-alive, time sync, role, onboarding/case/app-sync, captured connect sequence, the 18-step plan, parsers for 0x80/0x10/0x81/0x0D | `PRELUDE_FRAME`, `evenhub_heartbeat`, `time_sync`, `keepalive`, `connect_sequence`, `MinimalDashboardPlan`, `Step`, `parse_auth_reply`, `parse_device`, `parse_case`, `parse_wear_reply`, `parse_app_sync` |
| `settings.py` | SID 0x09 SET/READ + snapshot, SID 0x20 module config, SID 0x80 GET_DEVICE_INFO / restart / reset | `brightness`, `display_height`, `display_distance`, `head_up`, `units`, `gestures`, `read_settings`, `Snapshot`, `parse`, `display_timeout`, `language`, `get_device_info` |
| `dashboard.py` | SID 0x01 (init/layout, weather, calendar, news V3, stocks), 0x0C quicklist, 0x0E health, 0x1F watchface read | `dashboard_init`, `weather`, `calendar_event(s)`, `news_reset/fifo_count_request/push`, `Task`, `quicklist_full_update`, `tasks_sequence`, `stock_push`, `health_snapshot`, `watchface_info_request`, `ack_flag` |
| `menu.py` | SID 0x03 whole-menu replace (built-ins + plugin items), selection-event mapping | `menu_push`, `BUILTIN`, `PluginItem`, `validate`, `parse_selection_event` |
| `notifications.py` | SID 0x04 CTRL/whitelist, notification JSON, EvenFile transfer (hub form and community form), CRC-32 variants | `notification_ctrl`, `whitelist_disable`, `notification_json`, `efs_start/data_prefix/data_packets/result_check`, `hub_notification_steps`, `community_notification_frames`, `crc32_efs`, `crc32c` |
| `even_ai.py` | SID 0x07 session (ENTER, heartbeat, ASK/ANALYSE/REPLY, EXIT, community form) and the 0x6402 LC3 mic packet layout | `enter`, `heartbeat`, `ask`, `analyse`, `reply`, `reply_chunks`, `exit_`, `parse`, `parse_mic_packet`, `evenhub_audio_control` |
| `apps.py` | Teleprompter (hub flow + community 0x0E-blob flow), Translate, Navigation | `tp_start/page/heartbeat/parse`, `community_tp_frames`, `tr_open/text/close/parse`, `nav_start/basic_info/parse` |
| `transport.py` | `Transport` protocol, `GatewayRawTransport` (HTTP `POST /api/raw` + `/ws` events), `FakeTransport`, `Glasses` high-level helper | `Glasses.send/run_plan/read_settings/set_weather/set_calendar/push_news/set_tasks/set_menu/notify/keepalive_loop` |

## Confidence per feature (from native-protocol.md §7)

| Feature | Module | Confidence |
|---|---|---|
| Auth frame, role, time sync, keep-alive, flag rule | `framing`, `session` | **LIVE** (captured from the app, acked) |
| Prelude (SID 0x01 magic 156) + EvenHub heartbeat gating | `session` | **LIVE** |
| Dashboard init (display_mode 4) | `dashboard.dashboard_init` | **LIVE** (flag 0; effect on layout unverified) |
| Custom widget order | `dashboard.dashboard_layout` | EXPERIMENTAL (only the default order confirmed) |
| Weather status bar | `dashboard.weather` | **LIVE** |
| Calendar (1/msg and multi) | `dashboard.calendar_event(s)` | **LIVE** |
| News V3 FIFO | `dashboard.news_*` | **LIVE** (every step acked 2026-09-29) |
| News V2 widget push | `dashboard.news_v2_push` | LIVE-NEGATIVE (always PARAMETER_ERROR) |
| Quicklist/tasks + ticks + paging | `dashboard.quicklist_*` | **LIVE** |
| Menu replacement (built-ins) | `menu.menu_push` | **LIVE** |
| Menu with plugin items | `menu.PluginItem` | DECODED |
| Settings SET brightness/auto/silent/wear/head-up/units/gestures | `settings` | **LIVE** |
| Settings SET display height/distance, dominant hand | `settings.display_height/_distance/dominant_hand` | DECODED (never sent by either hub) |
| Settings READ + readback fields | `settings.read_settings`, `Snapshot` | **LIVE** |
| Module config (timeout, language) | `settings.display_timeout/language` | **LIVE** |
| GET_DEVICE_INFO / SET_DEVICE_INFO | `settings.get_device_info` (no SET builder) | DECODED |
| Quick restart / factory reset / unpair | `settings.quick_restart/factory_reset/unpair` | DECODED (destructive) |
| Stocks card | `dashboard.stock_push` | DECODED (never seen on glasses) |
| Health card | `dashboard.health_snapshot` | DECODED |
| Watchface layout read | `dashboard.watchface_info_request` | DECODED, risky — **no upload builder on purpose** |
| Even AI full session | `even_ai` | **LIVE** |
| Even AI minimal Q&A (no heartbeat) | `even_ai.ask/reply(community=True)` | COMMUNITY |
| Mic stream 0x6402 layout | `even_ai.parse_mic_packet` | **LIVE** (386 captured packets) |
| EvenHub audio control (cmd 15) | `even_ai.evenhub_audio_control` | **LIVE** |
| Translate open/text/close | `apps.tr_*` | DECODED (live COMM_RESP seen, incl. FAIL) |
| Teleprompter (hub flow) | `apps.tp_*` | DECODED |
| Teleprompter (community 0x0E-blob flow) | `apps.community_tp_*` | COMMUNITY |
| Navigation | `apps.nav_*` | DECODED |
| Notification CTRL / whitelist-off | `notifications.notification_ctrl/whitelist_disable` | **LIVE** |
| Notification JSON over EvenFile (hub form) | `notifications.hub_notification_steps` | DECODED/UNTESTED |
| Notification JSON over EvenFile (community form) | `notifications.community_notification_frames` | COMMUNITY |
| App-sync foreground pushes | `session.parse_app_sync` | **LIVE** |
| Case / wear / onboarding queries | `session.case_query/wear_query/onboarding_finish` | **LIVE** |

## Deviations from the doc (Dart wins)

1. **`len` byte of a frame** = bytes after the header in *this* packet, i.e. payload chunk **including** the
   CRC bytes that land in it (`framing.dart:38 chunk.length`); a single-packet message has `len = len(pb) + 2`.
   The doc's prose ("len") is ambiguous; all its hex vectors agree with the Dart.
2. **EvenFile checksum is not CRC-32C.** `crc.dart:411-431` is poly 0x1EDC6F41, MSB-first, init 0, no XOR
   (check `0xC052A8C8`); standard CRC-32C (reflected, check `0xE3069283`) is a different function. Both are
   implemented (`crc32_efs` goes on the wire; `crc32c` is for reference/tests only).
3. **Hub DATA packets for a 231-byte block** come out as `[231, 2]` (231 data bytes, then a CRC-only packet), not
   `[232, 1]`: `file_service.dart:53-58` chunks the block first and appends the empty chunk when `len % 232` is 231.
4. **Auth magic ≥ 128**: `framing.dart:84` writes the magic as one raw byte, which is only a valid varint for 1..127.
   `auth_frame()` encodes 128..255 as a proper 2-byte varint (and adjusts `len`); 1..127 is byte-identical to the Dart.
5. **Truncated varints** stop `iter_fields` instead of raising (the Dart's `iterFields` throws `RangeError` and every
   hub decode is wrapped in a try). Builders are unaffected.
6. **Gesture entries** are sub-messages (`0a 06 …`) under field 10 (`settings.dart:544 p.message(1, item)`); the doc's
   `{10: {1:{…}, 1:{…}}}` notation is consistent with this, the bytes just are not spelled out there.
7. **Community keep-alive frame** `aa210e0601018020080e106b6a00e174` is internally inconsistent (len 6, 8 bytes follow, CRC
   does not match any reading) — not reproduced; `session.keepalive()` is used after the community notification instead
   (as the doc recommends).
8. The community's **SID names** for 0x0E etc. are wrong (`sids.dart`); `apps.community_tp_display_config` keeps their bytes
   but documents that 0x0E is the health channel.

## Frames whose bytes could NOT be cross-checked against any source

Verify these on hardware first (they are built from field layouts only, no capture/vector exists):

- `settings.display_height`, `settings.display_distance` — bytes match the doc's examples, but the doc marks them DECODED (never sent by either hub); the firmware's acceptance is what is unverified.
- `settings.dominant_hand`, `settings.brightness_calibration`, `settings.app_ack`, `settings.unpair`, `settings.get_device_info`, `settings.quick_restart`, `settings.factory_reset`.
- `dashboard.dashboard_layout` (field 2 only) and `dashboard.display_enable` for anything but `[4]`.
- `dashboard.calendar_events` (multi-event with scheduleId) — LIVE per the doc but no hex vector exists.
- `dashboard.news_fifo_count_request`, `dashboard.news_push` — LIVE per the doc but no hex vector exists (shape taken from `dashboard.dart:162-182,219-229`).
- `dashboard.quicklist_full_update/_add_page/_empty` — no hex vector; layout from `quicklist.dart`.
- `dashboard.stock_push`, `dashboard.stock_display_enable`, `dashboard.health_snapshot` — DECODED, never seen on glasses.
- `notifications.efs_*` / `hub_notification_steps` — the hub transfer is UNTESTED in the hub itself; only the 93-byte START header could be cross-checked (identical to the community FILE_CHECK).
- `notifications.ios_register`.
- `even_ai.heartbeat`, `even_ai.analyse`, `even_ai.reply(... final=True)` (the `6: 1` end flag), `even_ai.config`, `even_ai.prompt` — hub-LIVE but no vector; only ENTER/ASK/REPLY(community form) have captured bytes.
- `apps.tp_*` (hub teleprompter), `apps.tr_*` (translate), `apps.nav_*` (navigation) — DECODED, no vectors.
- `apps.community_tp_*` — bytes reproduce the community script exactly, but the script itself was the only source (no btsnoop hex in the docs).
- `session.connect_sequence(full=True)` extras (`0x07` Even AI config, `0x0C` empty quicklist, `0x1F` info, the captured `0x04` ctrl) — from `device.dart:184-199` only.

Cross-checked byte-exact (in `test_native.py`): prelude frame, EvenHub heartbeat, auth frame (hub + community), dashboard init,
0x0D subscribe, role change (RIGHT + community LEFT), time sync (doc UTC-4 and community tz -24), settings READ, keep-alive,
weather example, calendar 1-event and none, news reset, quicklist display-enable, menu default, brightness/height/distance,
module timeout READ, watchface info request, notification CTRL + whitelist off, Even AI ENTER/ASK/REPLY (three community frames
with CRC) and the community REPLY from the glasses, CRC-16 and both CRC-32 check values, START header = community FILE_CHECK.

## Hardware verification checklist (native-protocol.md §8, "things to verify live")

Run with `Glasses.run_plan(MinimalDashboardPlan(...))` and watch the log:

1. Step 1 auth (R) gets a reply ending `1a 00` within 5 s; step 6 dashboard init acks **flag 0**; step 17 weather acks flag 0.
2. After step 18 (heartbeat stopped) the native watchface appears in ~5 s with **clock, date, battery, weather**.
3. Whether step 6 alone changes the widget order (oneof caveat §2) — compare `dashboard_init` vs `dashboard_layout` (field 2 only).
4. What status-bar slots 2 and 3 are (status order `[1,2,3]`; only 1 = weather is identified).
5. Whether the glasses keep the dashboard **without** steps 7 and 12-15 (0x0D subscribe, onboarding FINISH, wear, case, gestures).
6. The display-height / display-distance SET fields (`settings.display_height/_distance`, never sent by either hub): does the
   settings ack come back, and does the readback snapshot (fields 3/4) change?
7. Keep-alive: the right lens echoes `0x80 cmd 14` (watch `Glasses.last_keepalive_echo`); two misses ⇒ reconnect.
8. Notifications: hub form (`notify(form="hub")`) vs community form; whether `app_identifier="com.even.sg"` displays with the
   whitelist disabled, and whether >234-byte JSON displays in the hub form.
9. Even AI: screen lifetime without the 2 s heartbeat (community path) vs with it.

## Usage

```python
import asyncio
from g2.native import dashboard, session, settings, transport

async def main():
    # Another process owns Bluetooth; the gateway relays raw frames.
    t = transport.GatewayRawTransport("http://127.0.0.1:8765")   # POST /api/raw, events on /ws
    async with transport.Glasses(t) as g:                         # starts the /ws reader
        plan = session.MinimalDashboardPlan(g.alloc, temperature_c=21, weather_type=1, weather_status="Sunny")
        results = await g.run_plan(plan)                          # steps 1-18, acks matched on (sid, magic)
        for step, reply in results:
            print(step.number, step.label, "ok" if reply is not None or not step.expect else "NO REPLY")
        print("battery", g.snapshot.battery, "height", g.snapshot.display_height)

        g.start_keepalive()                                       # 60 s L->R keep-alive, hourly time sync
        await g.set_calendar([dashboard.Schedule("Standup", "Room 1", "10:00 AM-10:30 AM", end_ts=1792500000)])
        await g.push_news([dashboard.NewsItem("Title", "Source", "Body")])
        await g.set_tasks([dashboard.Task(1, "Milk"), dashboard.Task(2, "Eggs", completed=True)])
        await g.set_menu([1, 4, 5, 6, 7])
        await g.apply_setting(settings.brightness(g.alloc.next_magic(), 20))
        await g.notify("Hello", "from the laptop", form="community")
        await asyncio.sleep(3600)

asyncio.run(main())
```

Lower level, without `Glasses`:

```python
from g2.native import framing, session, dashboard
alloc = framing.MagicAllocator()
m = alloc.next_magic()
frames = framing.frame_pb(dashboard.weather(m, 21.0, weather_type=1, status="Sunny"), 0x01, framing.FLAG_REQUEST, alloc.next_seq())
# write frames[*] to 0x5401 of the RIGHT lens (while the 0xE0 heartbeat runs), then wait for a 0x01 reply whose
# read_ack_magic(payload) == m and dashboard.ack_flag(payload) == 0
```

Gateway contract used by `GatewayRawTransport` (the endpoint is being added to the gateway):

- `POST /api/raw` body `{"side": "left"|"right"|"both", "hex": "aa21...", "response": false}` or `{"side": ..., "frames": ["hex", ...]}`;
  `write_file` adds `"char": "7401"` for EvenFile frames.
- `GET /ws` events `{"kind": "glasses.raw_packet", "data": {"source": "L"|"R", "hex": "aa12..."}}`.

Timing constants (`session.*_S`) are the hub's: heartbeat 4 s, prelude drain 1.5 s, connect-sequence gap 150 ms, keep-alive 60 s
(left first, right 2 s later, 5 s echo wait), time sync hourly, dashboard retake ~5 s after the heartbeat stops.
`Glasses.time_scale` scales every wait (tests use 0.01).
