# Even Realities G2 — native UI protocol (dashboard, menu, native apps, notifications)

Goal: drive the firmware's built-in dashboard/widgets/apps from a laptop over BLE without Even's phone app.
Everything below is taken from the sources on disk; each claim carries `file:line`. Written 2026-10-23.

Path aliases used in citations
- `D/` = `C:\Users\Kunsh\Desktop\frame\g2\vendor\open-g2-hub\lib\protocol\` (Dart port of the `g2` Python library, RE'd from Even app 2.3.1)
- `H/` = `C:\Users\Kunsh\Desktop\frame\g2\vendor\open-g2-hub\lib\hub\`
- `P/` = `C:\Users\Kunsh\Desktop\frame\g2\vendor\open-g2-hub\hub\` (Python hub; imports a `g2` package that is NOT on disk)
- `E/` = `C:\Users\Kunsh\Desktop\frame\g2\vendor\even-g2-protocol\` (community RE, main branch)
- `S/` = scratchpad clone of `ithinkthisiscool/even-g2-protocol` (`...\scratchpad\g2-research\even-g2-protocol`), branches `origin/feature/even-ai`, `origin/feature/notification-simple`

Confidence vocabulary
- **LIVE** — the source marks it live-confirmed / captured from the official app and acked on real glasses.
- **DECODED** — field layout recovered from the app binary; never (or not reported) tested on glasses.
- **COMMUNITY** — claimed working by the community branch; not independently verified here.
- **INFERRED** — my reading of the code; no explicit confirmation.

Transport recap (given, not re-derived): two peripherals (L/R), service `...5450`, write `...5401`, notify `...5402`, mic notify `...6402` on service `...6450`, EvenFile write `...7401` / notify `...7402` on service `...7450` (`D/../ble/lens_ad.dart:10-26`). Frame = `AA 21 seq len total num SID flag payload crc16(CCITT-FALSE, LE, payload only)`; glasses reply with `AA 12 ...`. Max 232 payload bytes per packet; multi-packet messages share `seq`, carry `total/num`, and the CRC covers the whole payload once (`D/framing.dart:19-43`). Replies to a request echo `{1: cmd, 2: magic}`; the hub matches acks on `(sid, magic)` (`H/glasses_session.dart:343-372`, `D/framing.dart:130-142`). Message magic runs 1..255, never 0 (`H/glasses_session.dart:98-100`).

Scratchpad clone check (asked): **No** `g2` Python package there. File list of `S/` (main): `.gitignore, CONTRIBUTING.md, README.md, captures/{auth-sequence,fresh-pairing,scripted-session,teleprompter-session}.log (binary btsnoop), docs/{ble-uuids,packet-structure,services,teleprompter}.md, examples/teleprompter/{README.md,requirements.txt,teleprompter.py}, proto/g2_protocol.proto`. Branch `feature/even-ai` adds `docs/even-ai.md, docs/R1_ANALYSIS.md, docs/notification.md, examples/even-ai/{README.md,even_ai.py,requirements.txt}, examples/notif/{README.md,notification.py,notification_trunc.py}, requirements.txt`; `feature/notification-simple` adds the `docs/notification.md` + `examples/notif/*` subset. No `session.py`, `evenhub.py`, `dashboard.py` anywhere. (The only other `g2/` dirs on disk, `men-g2-ble-gateway/mentraos/g2` and `hermes-g2/app/g2`, are different packages: `client.py/transport.py/...` and TypeScript respectively.)

---

## 1. Session prelude required before native widgets work

Order and timing are the hub's (`H/glasses_session.dart:158-243`, mirroring `P/connection.py:120-158,46-57`). Everything to the **right** lens unless noted.

| # | What | SID / flag | Payload (protobuf) | Lens | Source | Conf. |
|---|------|-----------|--------------------|------|--------|-------|
| 0 | Connect both lenses, OS-level bond (LE Secure Connections; unbonded lenses accept writes but never advance), MTU 247+, subscribe `5402` on both | — | — | both | `D/../ble/lens_link.dart:104-119` | LIVE |
| 1 | **Auth** ("config array") | `0x80` flag **0x00** | `08 04 10 <magic> 1A 04 08 01 10 04` = `{1: 4 AUTHENTICATION, 2: magic, 3: {1: 1, 2: 4 (phone=Android)}}`; frame `seq` == magic. Right first (wait ≤5 s for a reply ending `1A 00`), then left fire-and-forget, 300 ms settle | both | `D/framing.dart:74-87`, `D/device.dart:105-106`, `H/glasses_session.dart:176-191` | LIVE |
| 2 | **Prelude** | `0x01` flag 0x20 | `08 02 10 9C 01 22 0A 1A 08 12 06 12 04 08 00 10 00` = `{1: 2 DASHBOARD_RECEIVE, 2: 156, 4: {3: {2: {2: {1: 0, 2: 0}}}}}` (an empty schedule widget, fixed magic 156); frame seq fixed 0x92, then 1.5 s drain. Whole frame: `aa219213010101200802109c01220a1a081206120408001000a142` | right | `D/evenhub.dart:16,144-153`, `H/glasses_session.dart:195-197`, `P/connection.py:122-135` | LIVE |
| 3 | **EvenHub heartbeat** start | `0xE0` flag 0x20 | `{1: 12, 2: magic, 14: {1: 0}}` e.g. `080c106572020800`, every **4 s**, fire-and-forget | right | `D/evenhub.dart:155-157`, `H/glasses_session.dart:89,290-299` | LIVE |
| 4 | Prelude again (hub re-sends it) + 1.5 s | `0x01` | as #2 | right | `H/glasses_session.dart:213-216` | LIVE |
| 5 | **Dashboard init** (base settings) | `0x01` | `{1: 2, 2: m, 4: {1: 1 packageId, 2: DisplaySetting{1: 4 displayMode, 2: 3, 3: packed[1,2,3] statusOrder, 4: 4, 5: packed[1,3,2,2] widgetOrder, 6: 1 halfDay(12 h), 7: 1 Celsius}, 3: {} }}` e.g. `08021066221b08011215080410031a0301020320042a0401030202300138011a00`; wait for ack flag 0 (≤4 s) | right | `D/dashboard.dart:48-57,197-209`, `H/glasses_session.dart:217-220` | LIVE (ack flag 0) |
| 6 | App-sync subscribe | `0x0D` | `{1: 0, 2: m}` (`08001072`); glasses then push `{3: {1 backgroundSID, 2 foregroundSID}}` whenever the foreground app changes | right | `D/app_sync.dart:11-43`, `P/frames.py:93-99` | LIVE |
| 7 | Role change | `0x80` flag 0x20 | `{1: 5, 2: m, 4: {1: 1 RIGHT}}` (`0805106922020801`) | right | `D/device.dart:108-111,182` | LIVE (captured) |
| 8 | **Time sync** | `0x80` flag 0x20 | `{1: 128, 2: m, 128: {1: unix_s (uint32), 2: tz int32 in 15-min units, negative = 10-byte varint}}` e.g. UTC-4 → `08 80 01 10 68 82 08 11 08 c0afddd606 10 f0ffffffffffffffff01`; re-sent hourly | right | `D/device.dart:113-132`, `H/glasses_session.dart:273-277` | LIVE (captured) |
| 9 | Settings READ basic | `0x09` | `{1: 2, 2: m, 4: {1: 1}}` (`0802106a22020801`) → reply carries battery, charging, firmware, calibration (see §3) | right | `D/settings.dart:113-116` | LIVE |
| 10 | Units | `0x09` | `{1: 1, 2: m, 3: {9: {1: 0 unitFormat, 2: 0 distance, 3: 1 timeFormat(12 h), 4: 0 dateFormat, 5: 1 Celsius}}}` | right | `D/settings.dart:154-163` | LIVE (captured) |
| 11 | Onboarding FINISH | `0x10` | `{1: 1, 2: m, 3: {1: 4}}` | right | `D/device.dart:148-153` | LIVE (captured) |
| 12 | Wear-status query | `0x10` | `{1: 3, 2: m, 5: {1: 1}}`; reply `5: {1: 1, 2: worn(0/1)}` | right | `D/device.dart:155-159,304-312` | LIVE |
| 13 | Case info query | `0x81` | `{1: 1, 2: m, 3: {}}`; reply `3: {1 battery, 2 charging, 3 lid, 4 in_case, 5 err}` | right | `D/device.dart:161-162,321-337` | LIVE |
| 14 | Gestures | `0x09` | `{1: 1, 2: m, 3: {10: {1:{1:0,2:0,3:0}, 1:{1:0,2:1,3:0}, 1:{1:0,2:2,3:0}}}}` | right | `D/settings.dart:41-46,165-180` | LIVE (captured) |
| 15 | Notification CTRL + whitelist off | `0x04` | `{1: 1, 2: m, 3: {1: 1 enable, 2: 1 autoDisplay, 3: 5 dispTime[, 5: 1 dnd]}}` then `{1: 3, 2: m, 6: {1: 1}}`; glasses forget notifEnable on reboot | right | `D/file_service.dart:132-144`, `H/glasses_session.dart:232-235` | LIVE |
| 16 | **Keep-alive** | `0x80` flag **0x00** | `{1: 14, 2: m, 13: {}}` = `08 0e 10 <m> 6a 00`; every **60 s**, **left lens first, then right 2 s later**, each with its own magic; the right lens echoes it — two missed echoes ⇒ reconnect | both | `D/device.dart:81-82,134-135,205-208`, `H/glasses_session.dart:245-272` | LIVE (captured) |

Steps 7-15 are sent 150 ms apart (`H/glasses_session.dart:91,236-239`). The `full=true` variant of the captured app sequence also inserts `0x07 cmd 10 {13:{1:0,2:80,4:0}}` (Even AI config), `0x0C cmd 2 {4:{1:1,2:0}}` (empty quicklist), `0x0D cmd 0`, `0x1F cmd 0 {3:{1:1}}` (watchface info request) and the `0x04` ctrl at their captured positions (`D/device.dart:184-199`).

Frame flag rule: `0x00` only for `0x80` cmd 4 (auth) and cmd 14 (keep-alive); `0x20` for everything else (`D/device.dart:93-100`).

The community "7-packet auth" (`S/examples/teleprompter/teleprompter.py:70-114`, `even_ai.py:56-96`, sent identically to each lens) is the same three messages repeated: auth `{1:4,2:m,3:{1:1,2:4}}`, role change `{1:5,2:m,4:{1:2 LEFT}}` / `{...4:{1:1 RIGHT}}`, and time sync `{1:128,2:m,128:{1:ts,2:-24}}` — their "transaction id" `E8 FF FF FF FF FF FF FF FF 01` is just tz = -24 (UTC-6) as a negative varint. COMMUNITY-live for teleprompter/Even AI; the hub shows the single auth frame suffices.

Why the heartbeat matters: SID `0x01` writes are only acked while the `0xE0` heartbeat runs; **stopping** the heartbeat is what lets the native dashboard retake the screen (~5 s) and show freshly pushed widgets. Pattern per push: `startHeartbeat()` + 2 s → pushes → `stopHeartbeat()` (`D/dashboard.dart:1-5`, `H/features.dart:98-117`, `P/routes/news.py:47-55,130-132`, `P/glasses.py:41-57`). The open-g2-hub README's claim that halting the heartbeat triggers a connection-lost error is contradicted by the code, which stops it deliberately after every push.

---

## 2. The native dashboard (SID 0x01 and friends)

Envelope for everything on SID 0x01: `DashboardDataPackage {1: commandId, 2: magic, 4: DashboardReceiveFromApp{1: packageId?, 2: DisplaySetting?, 3: DashboardContent}}` (`D/dashboard.dart:81-93`). Command ids: 1 DASHBOARD_RESPOND, 2 DASHBOARD_RECEIVE, 3/4 APP_RESPOND/RECEIVE, 5/6 news info (V2), 7 APP_REQUEST_NEWS_INFO, 8 DEV_RESPONSE_NEWS_INFO, 9 APP_SEND_NEWS_DATA, 10 DEV_RESPONSE_NEWS_DATA, 11 DEV_REQUEST_NEWS_UPGRADE, 12 DEV_NOTIFY_NEWS_EVENT, 13 APP_REQUEST_CLEAR_ALL_DATA, 14 DEV_RESPONSE_CLEAR_ALL_DATA (`D/dashboard.dart:15-29`).
`DashboardContent {1: StatusComponent{1: WeatherStatus}, 2: WidgetComponent{1: NewsWidget, 2: StockWidget, 3: ScheduleWidget}}` (`D/dashboard.dart:78-79,113,135-136`, `D/stocks.dart:78`).
Widget type enum (DisplaySetting field 5 / widget order): 1 NEWS, 2 STOCK, 3 SCHEDULE, 4 QUICKLIST, 5 HEALTH (`D/dashboard.dart:31-36`).

**Ack**: reply `{1: cmd, 2: magic, 3: Respond{1: packageId, 2: flag}}`; flag 0 SUCCESS, 1 PARAMETER_ERROR, 2 NEWS_VERSION_ERROR; an empty field 3 also means success (`D/dashboard.dart:289-302`, `P/glasses.py:16-24`). All dashboard pushes are sent to the **right** lens and waited on (≤4 s).

Note on DisplaySetting vs content: fields 2 and 3 of `DashboardReceiveFromApp` are one nanopb oneof in firmware (desc 0x7d262c), so a trailing empty field 3 can overwrite field 2. `buildDashboardLayout` therefore sends field 2 only (`D/dashboard.dart:357-360`), while the LIVE `buildDashboardInit` still sends `2` then an empty `3` and gets flag 0 (`D/dashboard.dart:199-209`). Unresolved: whether init's setting actually takes effect or the ack is merely polite. If you change widget order, send field 2 alone.

### 2.1 Time / date
No dashboard message carries the clock: it comes from `0x80` TimeSync (§1 #8, LIVE), hourly re-sync. 12/24 h: DisplaySetting field 6 `halfDayFormat` (1 = 12 h in the live init) and settings units field 3 `time_format` (hub sends 0 for 24 h, 1 for 12 h; `H/features.dart:258-265`). Date format: units field 4, enum 0..15, values undocumented (`D/settings.dart:154-163`).

### 2.2 Weather (status bar) — LIVE
`{1: 2, 2: m, 4: {3: {1: {1: WeatherStatus}}}}` with no packageId. `WeatherStatus {1: float32 temperature (ALWAYS Celsius), 2: unit 1 C / 2 F (display only), 3: weatherType, 4: updateTime epoch ms, 5: statusString ≤32 chars, 6: rainfallIcon, 7: rainfallHint ≤32, 8: sunsetSelect, 9: sunsetHint ≤32}` (`D/dashboard.dart:115-133,232-253`). weatherType: 0 Unknown, 1 Sunny, 2 Cloudy, 3 Drizzle, 4 Heavy drizzle, 5 Rain, 6 Heavy rain, 7 Thunderstorm, 8 Thunder, 9 Snow, 10 Mist, 11 Fog, 12 Dust, 13 Squalls, 14 Tornado, 15 Freezing rain, 16 Clear night (`D/dashboard.dart:43-46`). Example 21 °C Sunny: `0802107322211a1f0a1d0a1b0d0000a8411001180120808cbbc995342a0553756e6e7930004000`. The hub refreshes every 15 min (`P/routes/weather.py:113-121`). Status order `[1,2,3]` in the init: component 1 is weather; 2 and 3 are not identified in any source (INFERRED: the status-bar slots).

### 2.3 Battery
Not pushed; the glasses own it. Read it with settings READ basic (§3) → fields 12 battery %, 13 charging; the glasses also push unsolicited `0x09` frames with the same fields (`D/evenhub.dart:86-99`, `H/glasses_session.dart:389-402`). Case battery via `0x81` (§1 #13). LIVE.

### 2.4 Notifications count
Settings read-back field 19 `unread_count` (`D/settings.dart:67`) is the only count seen; the watchface layout has a `COMP_NOTIFICATION = 4` clock-side component (`D/watchface.dart:23`). Nothing pushes a count; the glasses derive it from notifications delivered via EvenFile (§6). DECODED.

### 2.5 Calendar — LIVE
`ScheduleWidget {1: scheduleTotal, 2: startIndex/scheduleNum, 3: Schedule (repeated), 4: scheduleAuthority}`; `Schedule {1: scheduleId, 2: title, 3: location, 4: timeStr, 5: endTimestamp s}` (`D/dashboard.dart:59-76`). The official app sends **one event per message** with `{1: total, 2: index}` and no scheduleId; "no events" = `{1: 0, 2: 0, 4: 1}` (`D/dashboard.dart:260-276`, `P/routes/calendar.py:58-75`). Multi-event in one message is also LIVE (`D/dashboard.dart:281-287`). Example (1 event): `08021074223808011a3412321a30080110001a2a12075374616e6475701a06526f6f6d2031221131303a303020414d2d31303a333020414d28a0c2ddd606`; none: `08021075220e08011a0a12081a06080010002001`. Formatting the app uses: max 8 events, today + tomorrow (prefix `Tmr `), `hh:mm AM-hh:mm PM` or 24 h, `All day`; byte caps title 175 / location 282 / time 62 (`D/hub_format.dart:20-104`). Re-pushed every 15 min. Each message is awaited for flag 0.

### 2.6 News — LIVE (V3 FIFO path only)
The V2 `WidgetComponent.news` push always answers PARAMETER_ERROR (`D/dashboard.dart:141`, `P/routes/news.py:38-39`). Working sequence (`H/features.dart:169-201`, `P/routes/news.py:28-138`):
1. heartbeat running (+2 s if just started);
2. reset FIFO `{1: 13, 2: m, 15: {1: 1}}` (`080d10767a020801`), wait ack, 500 ms;
3. FIFO count `{1: 7, 2: m, 9: {1: 1}}` → reply cmd 8 `{10: {1: count}}` (always 5 so far), 200 ms;
4. per slot i in 0..count-1: `{1: 9, 2: m, 11: AppSendNewsData{1: sessionId 0, 2: sessionStatus 0, 3: sessionTotalCount, 4: sessionNewsIndex i, 5: newsId i, 6: title, 7: reportTime **local wall-clock ms** (UTC ms + offset), 8: source, 9: content}}`; reply cmd 10 `{12: {5: newsStatus}}`, 150 ms gap; (`D/dashboard.dart:162-229`)
5. stop heartbeat; rotation ~10 s per item.
Glasses → app: cmd 11 = "scroll down to refresh" (hub re-fetches, 30 s cooldown); cmd 12 `{14: {1 eventID (0 expand list, 1 back to widget, 2 scroll, 3 open article, 4 back to list), 2 newsId}}` (`P/frames.py:69-84`, `H/features.dart:583-592`).

### 2.7 Tasks / quick notes (Quicklist, SID 0x0C) — LIVE
`{1: 2 MULT_ITEMS, 2: m, 4: {1: dataType (1 FULL_UPDATE, 2 ADD, 3 MODIFY, 4 DELETE), 2: totalCount, 3: Item...}}`; `Item {1: uid, 2: index, 3: isCompleted, 4: timestamp s (0 = none), 5: bytes title ≤200 B + NUL, 7: tsType (0 datetime, 1 date, 2 time; app uses 2)}`; ≤20 items per packet (`D/quicklist.dart:13-51`). Sequence (`H/features.dart:204-223`, `P/routes/tasks.py:87-205`): heartbeat → dashboard init (again) → 300 ms → quicklist FULL_UPDATE (await echo of cmd+magic on 0x0C) → `0x01` display-enable `{1:2,2:m,4:{1:1,2:{1:0,2:0,4:1,5:[4],6:0,7:0},3:{}}}` (`0802107a22130801120d0800100020012a0104300038001a00`, flag 0) → stop heartbeat. Glasses → app: cmd 1 (f3) / cmd 2 (f4) = completion ticks by uid; cmd 3 `{5: {1: event 1 UP / 2 DOWN, 2: pivotUid}}` = paging request, answered with MULT_ITEMS ADD (≤20) (`D/quicklist.dart:88-125`, `P/routes/tasks.py:218-261`). The glasses cannot add/delete tasks.

### 2.8 Stocks — DECODED, never seen on glasses
One quote per message: `{1:2, 2:m, 4:{3:{2:{2: StockWidget{1: stockTotal, 2: stockNum, 3: Stock}}}}}`; `Stock {1 code str ≤63 B, 2 market_suffix, 3 double market_cap, 4 float change_percent, 5 float price, 6 name ≤127 B, 7 float day_high, 8 float day_low, 9 float open, 10 volume, 11 double market_value, 12 float pe_ratio, 13 trend (0 flat 1 up 2 down), 14 point_total, 15 float dark_price, 16 packed float points}` (`D/stocks.dart:15-80`). The card only shows if widget order contains 2 (`D/stocks.dart:82-91`).

### 2.9 Health card (SID 0x0E) — DECODED
`{1: 2 MULT_DATA, 2: m, 4: {1: 1 ALL, 2: Single...(≤8)}}`; `Single {1 type (2 steps 3 calories 4 sleep 5 HR 6 SpO2 7 temp 8 HRV 9 productivity), 2 goal, 3 float value, 4 float avg, 5 duration, 6 err, 7 trend}` (`D/health.dart:10-48`). No display-enable works for it (`H/features.dart:315`).

### 2.10 Watchface / what sits next to the clock (SID 0x1F) — DECODED, untested, risky
Package `{1: cmd, 2: m, (cmd+3): body}`; cmds 0 REQUEST_INFO, 1 INFO_RESPONSE `{1 exists, 2 version, 3 hash}`, 2 REQUEST_UPGRADE, 3 UPGRADE_RESPONSE `{1 status}`, 4 TRANSMIT_START (glasses), 5 FILE_DATA `{1 session, 2 totalSize, 3 compress 0, 4 fragIdx, 5 len, 6 bytes}` ≤2047 B/fragment, file ≤30000 B, 6 FILE_DATA_ACK `{...6 status}`, 7 UPDATE_SUCCESS (`D/watchface.dart:12-27,37-76,140-166`). **Read first**: `{1:0, 2:m, 3:{1:1}}` (`0800107c1a020801`) and keep the reported version/hash. Layout file `{1 version ≤14 B, 2 hash ≤30 B (hub: crc16 hex of the body), 3 pos 0 L/1 C/2 R, 4 n, 6 packed widgetSeq (0 news 1 stock 2 schedule 3 health 4 quicklist 5 user), 7: {layoutNumber: Layout}}`; Layout1 `{1 clockType, 2 leftComp, 3 rightComp}`, Layout3 `{1 align, 2 dateEn, 3 tempEn}`; components 0 none 1 temperature 2 sunset 3 next_rain 4 notification 5 HR 6 steps 7 calories 8 SpO2 9 sleep (`D/watchface.dart:21-24,78-94`). Upload flow: REQUEST_UPGRADE → (UPGRADE_RESPONSE status 0) → TRANSMIT_START → fragments each acked → UPDATE_SUCCESS (`H/features.dart:416-441`). A bad file may blank the dashboard until a valid one is uploaded (`D/watchface.dart:2-3`).

Restore order the hub uses after every connect: settings, layout, weather, menu, tasks, calendar, news, stocks, health (`P/sync.py:30-41`).

---

## 3. Layout / position / behaviour settings (SID 0x09, 0x20, 0x80) — with READ commands

### 3.1 SID 0x09 `G2SettingPackage` (`D/settings.dart`)
Cmds: 1 SET, 2 READ, 3 DEVICE_EVENT, 4 DEVICE_ACK, 5 APP_ACK (`:14-15`). SET = `{1: 1, 2: m, 3: {field: sub}}` (`:104-108`). All LIVE for the values the hub exposes (brightness, auto-brightness, silent, wear, head-up, units, gestures; `H/features.dart:244-278`); height/distance builders are DECODED only.

| field | name | SET sub-message | notes |
|---|---|---|---|
| 1 | brightness | `{1: auto 0/1}` or `{2: level 1..50}` or `{3: leftCal 0..255, 4: rightCal}` | `:118-128` |
| 2 | display_height (`fY`) | `{1: level 0..20}` | "lens y" — `:140-141` |
| 3 | display_distance (`fX`) | `{1: level 0..20}` | "lens x" — `:143-144` |
| 4 | head_up | `{1: enabled, 2: angle 0..60}`; decode also shows `3 calibration_switch, 4 calibration` | `:134-138,255-261` |
| 5 | wear_detection | `{1: on}` | `:132` |
| 6 | silent_mode | `{1: on}` | `:130` |
| 7 | app_page | unknown sub | `:28` |
| 8 | kill_all_features | unknown | `:29` |
| 9 | units | `{1 unit_format, 2 distance_unit(0 metric 1 imperial), 3 time_format(0 24h 1 12h), 4 date_format, 5 temperature_unit(1 C 2 F)}` each 0..15 | `:154-163` |
| 10 | gestures | repeated `{1: {1 screen_on, 2 operation_type 0..15, 3 app_type 0/1}}` | `:165-180` |
| 11 | dominant_hand | `{1: hand 0/1, 2: ring_mac 6 B}` | `:182-189` |
| 12 | turn_on_device | unknown | `:33` |

No field literally named "lens coord x/y offset" exists in these sources; fields 2/3 (named `fY`/`fX` in the port) are the closest. Example bytes: brightness 20 `0801107d1a040a021014`; height 10 `0801107e1a041202080a`; distance 10 `0801107f1a041a02080a`.

**READ** = `{1: 2, 2: m, 4: {1: setting_type}}`, type 0 = brightness, 1 = basic (`:17,113-116`). Reply/push carries field 4 with: 1 setting_type, 2 brightness_level, 3 **display_height**, 4 **display_distance**, 5 left_version (str), 6 right_version (str), 7 head_up, 8 head_up_angle, 9 head_up_calibration, 10 wear_detection, 11 running_status, 12 battery, 13 charging, 14 silent_mode, 15 left_calibration, 16 right_calibration, 17 head_up_recalibration_ok, 18 brightness_auto, 19 unread_count (`:48-68`). A frame with field 4 but no `setting_type` is an unsolicited push (`:294-301`). Field 5 of an incoming package = device event `{1 recalibration_status, 2 silent_mode}`; fields 6/7 = acks `{1 package_id, 2 flag}` (`:309-316`). Do this READ before changing anything and cache fields 2-4, 7-9, 15-16, 18.

### 3.2 SID 0x20 module config (`D/settings.dart:71-75,199-215,322-343`)
- Screen-off time: SET `{1: 2, 2: m, 4: {1: seconds}}`, choices 3/5/10/15/30 or 65365 = never; READ `{1: 1, 2: m, 4: {}}` (`08011080012200`) → `{4: {1: t}}` (t > 20 ⇒ never).
- UI language: `{1: 0, 2: m, 3: {1: idx}}` 0 EN 1 DE 2 FR 3 ES 4 IT 5 JA 6 ZH-TW 7 ZH-CN 8 KO; reply `{3: {1: idx}}`.

### 3.3 SID 0x80 GET_DEVICE_INFO (DECODED; `D/device.dart:63-73,143-146,210-247`)
`{1: 12, 2: m, 11: {1: {1: item}}}`, item 0 ALL, 1 BRIGHTNESS, 2 ANTI_SHAKE, 3 DISPLAY_MODE, 4 WORK_MODE, 5 WAKEUP_ANGLE, 6 GLASSES_SN, 7 DEVICE_SN, 8 BLE_MAC. Reply field 11 → repeated `{1: {1 item, 2 {1 als_brightness, 2 als_auto}, 3 anti_shake, 4 {1 display_mode}, 5 {1 wake_mode}, 6 {1 wakeup_angle, 2 wakeup_offset}, 7 {1 glasses_sn}, 8 {1 device_sn}, 9 {1 mac}}}`. SET_DEVICE_INFO is cmd 11 (field 10) with the same shape (not built anywhere). Also: cmd 15 QUICK_RESTART (reboot), cmd 13 FACTORY_RESET (destructive), cmd 9 UNPAIR (`D/device.dart:17-31,137-141`).

Hub "refresh status" burst: settings READ, wear query, case query, timeout READ, 150 ms apart (`H/features.dart:282-291`, `P/routes/settings.py:128-137`).

---

## 4. Menu / app launcher (SID 0x03) — LIVE

`{1: 0 APP_SEND_MENU_INFO, 2: m, 3: {1: count, 2: Item...}}`; built-in `Item {1: 0 itemType, 4: appId}`; plugin `Item {1: 1, 2: iconNum 1, 3: name ≤31 B, 4: appId}` (`D/menu.dart:10-66`, `D/evenhub_pages.dart:240-259`). The push **replaces the whole menu**; app rule 5..10 items, no duplicates (`D/menu.dart:26,69-77`). Reply cmd 1 echoes magic (hub waits ≤4 s). Built-in ids (LIVE): 1 Dashboard, 4 Notifications, 5 Translate, 6 Teleprompt, 7 Even AI, 8 Navigate, 11 Conversate, 48 Terminal, 266 Silent mode — note 1/4/5/6/7/8/11/48 are the SIDs of those features (`D/menu.dart:15-25`). Default `[1,4,5,6,7]` (`D/evenhub_pages.dart:227`). Example: `0800107b1a200805120408002001120408002004120408002005120408002006120408002007`.

Selection events: there is no SID 0x03 "selected" message. What arrives instead:
- `0x0D` app-sync push `{3: {1 bg, 2 fg}}` with the foreground SID (LIVE, `D/app_sync.dart:31-40`);
- the chosen native app opens its own channel: Navigate → `0x08` cmd 1 MENU_STARTUP; Teleprompt → `0x06` cmd 162 FILE_LIST_REQUEST; Translate → `0x05` cmd 164 LANG_SWITCH_REQ / notify; Even AI → `0x07` CTRL WAKE_UP (`H/features.dart:610-629,659-662`);
- a plugin item → `0xE0` cmd 17 MENU_STARTUP `{20: {1: widget_id}}`, which must be answered with a cmd 0 page or cmd 18 `{21: {1 err, 2 str}}` (`D/evenhub.dart:23-25,178-180`, `D/evenhub_pages.dart:134-135`). EvenHub page clicks come as `{13: {1 list{1 id,2 name,3 item,4 idx,5 ev} | 2 text{1,2,3 ev} | 3 sys{1 ev,2 src,3 imu,4 exit,5 widget,6 session}}}`, events 0 CLICK 1/2 SCROLL 3 DOUBLE 4/5 FG enter/exit 6/7 exits 8 IMU 9/10 long press (`D/evenhub.dart:27-43,175-230`).

---

## 5. Full-screen native apps

Common shape: open → periodic heartbeat → content → glasses send their own exit → close (`P/native_apps.py:1-12`). Package = `{1: cmd, 2: m, fieldForCmd: sub}`. Only one native app at a time.

### 5.1 Teleprompter (SID 0x06) — DECODED in the hub; COMMUNITY-live via the older sequence
Cmds→field: 1 CONTROL→3, 2 FILE_LIST→4, 3 PAGE_DATA→5, 4 PAGE_AI_SYNC→6, 161 STATUS_NOTIFY→7, 162 FILE_LIST_REQUEST→8, 163 FILE_SELECT→9, 164 PAGE_DATA_REQUEST→10, 165 PAGE_SCROLL_SYNC→11, 166 COMM_RESP→12, 255 HEARTBEAT→13, 167 PAGE_LOAD_DONE→14, 168/169 MODE_SWITCH req/rsp→15/16, 170 END_SESSION_REQUEST→17 (`D/teleprompter.dart:13-24`).
- START: `{1:1, 2:m, 3:{1: 1 START, 2: Settings{1 mode (0 AI 1 MANUAL 2 AUTO), 2 startPage, 3 startLine, 4 totalPages, 5 totalLines, 6 displayWidth 567, 7 scrollIntervalMs, 8 countdown s, 9 displayLines 9}}}` (zero fields omitted); retried once on COMM_RESP FAIL (`:99-127`, `H/features.dart:509-515`).
- Glasses ask `PAGE_DATA_REQUEST {1: pageId}` → answer `PAGE_DATA {1 pageId, 2 lineCount, 3 bytes text}` = lines joined by `\n` + ` \n`, ≤1024 B; ≤10 lines of ≤28 chars (`:32-38,134-143`).
- HEARTBEAT `{255, m, 13: {1 pageId, 2 lineId}}` every 6 s; glasses' heartbeat reports position in fields 3/4 (`:37,145-146,188-191`).
- PAUSE/RESUME/CLOSE = CONTROL `{1: 2/3/4}`; glasses exit = cmd 170 or STATUS_NOTIFY value 4 (`:129-132,223-224`). Menu launch: FILE_LIST_REQUEST → `FILE_LIST {1: {1 fileId bytes, 2 name ≤30}..., 2: {1 menuMode}}` → FILE_SELECT `{1: fileId}` (`:155-163`).
- Community variant (`S/docs/teleprompter.md`, `S/examples/teleprompter/teleprompter.py:121-178`, COMMUNITY-live): an `0x0E` cmd 2 "display config" blob of 106 bytes, then init `{1:1,2:m,3:{1: scriptIndex, 2:{1:1,2:0,3:0,4:267,5:contentHeight,6:230,7:1294,8:5,9:mode}}}`, 10 pages pushed unsolicited, a cmd-255 marker `{13:{1:0,2:6}}`, a `0x80` cmd 14 keep-alive, remaining pages; text starts with `\n`, ends ` \n`, ≥14 pages. The `0x0E` blob is the health channel in the hub's map (`D/sids.dart:16`) — the community's SID names for 0x04/0x07/0x0D/0x0E/0x81 (`E/docs/services.md:27-37`) are wrong; trust `D/sids.dart`.

### 5.2 Translate (SID 0x05) — DECODED; hub reports live COMM_RESP replies
Cmds→field: 1 CTRL→3, 2 RESULT→4, 161 NOTIFY→5, 163 MODE_SWITCH→6, 162 COMM_RESP→7, 255 HEARTBEAT→8, 164 LANG_SWITCH_REQ→9, 165 LANG_SWITCH_RSP→10 (`D/translate.dart:13-19`).
- OPEN: `{1:1, 2:m, 3:{1: 1 OPEN, 2: "SRC>DST", 5: {1 key ≤8 B, 2 text}..., 6: srcKey, 7: dstKey}}`; expect COMM_RESP `{7: {1: err}}` (0 OK, 1 FAIL, 2 NETWORK, 3 NOT_SUPPORT, 4 FALLBACK_FAIL, 66 MAGIC_DUPLICATE) (`:21-43`).
- Text: RESULT `{4: {1 srcText, 2 dstText (≤1024 B each), 4: 1 endFlag, 5: speaker bytes}}` (`:52-62`); HEARTBEAT `{255, m, 8: {}}` every 4 s; CLOSE = CTRL `{1: 2}`; glasses exit = NOTIFY `{5: {1: 2 CLOSE}}`; answer LANG_SWITCH_REQ with cmd 165 `{10: {}}` (`:64-67`, `H/features.dart:610-614`). A rejected OPEN (COMM_RESP FAIL) means no session: do not keep heart-beating (`P/native_apps.py:131-145`).

### 5.3 Navigation (SID 0x08) — DECODED
Cmds: 0 HEARTBEAT, 1 MENU_STARTUP (glasses), 2 LOCATION_LIST, 3 LOCATION_NONE, 4 LOCATION_SELECTED (glasses), 5 START, 6 ERROR_INFO, 7 BASIC_INFO, 8/9 mini/max map, 10 RECALCULATING, 11 ARRIVE, 12 STOP, 13 NOTIFY_EXIT, 14 VIEW_CHANGED, 15 COMPASS_CHANGED, 16/17 compass calib, 18/19 SWITCH_TRANSPORT req/rsp, 20 END_SESSION_REQ (`D/navigation.dart:20-33`). Simple cmds carry an empty field 3. START (5) → BASIC_INFO (7) per update → HEARTBEAT (0) every 5 s → ARRIVE/STOP; `BASIC_INFO {5: {1 directionSignIndex 1..35, 2 distance, 3 road, 4 spendTime, 5 remainDistance, 6 eta ("ETA: " prefixed), 7 speed (each ≤60 B, blank → "--"), 8 method 1 walk 2 cycle, 10 counter}}` (`:4-7,62-73,79-82,120-139`). Menu launch: MENU_STARTUP → `LOCATION_LIST {4: {1 n, 2 names..., 3 packed icons}}` (≤20) or LOCATION_NONE → `LOCATION_SELECTED {8: {1 name, 3 index, 4 method}}` (`:142-153,195-199`). Exit = cmd 13 or 20.

### 5.4 Even AI (SID 0x07) — LIVE in the hub; COMMUNITY-live for the minimal Q&A path
Cmds→field: 1 CTRL→3, 2 VAD_INFO→4, 3 ASK→5, 4 ANALYSE→6, 5 REPLY→7, 6 SKILL→8, 7 PROMPT→9, 8 EVENT→10, 9 HEARTBEAT→11, 161 COMM_RSP→12, 10 CONFIG→13 (`D/even_ai.dart:23-36`). CTRL status 1 WAKE_UP, 2 ENTER, 3 EXIT; VAD 1 START 2 END 3 TIMEOUT; EVENT 1 SCROLL 2 STREAM_COMPLETE; PROMPT 1 NETWORK_ERR .. 9 COMMAND_EXE_FAIL (`:38-50`).
Session (`:4-14`, `P/routes/even_ai.py:185-304`, `H/even_ai.dart:1-6`):
1. glasses → `CTRL {3: {1: 1 WAKE_UP}}` (user said "Hey Even" / opened Even AI from the menu);
2. app → `CTRL ENTER` `{1:1, 2:m, 3:{1: 2}}` (`08011081011a020802`) within a few seconds, then `HEARTBEAT {9, m, 11: {1: count}}` every **2 s**;
3. **audio**: subscribe notify `0x6402` (service `0x6450`) on both lenses; packets are raw, no AA frame: bytes 0..199 = five 40-byte LC3 frames (10 ms, 16 kHz mono, 32 kbps), 200..201 int16 LE signal ratio, 202..203 int16 LE direction of arrival, 204 counter (`D/mic_audio.dart:6-12`). Left lens is cleaner. The glasses never send VAD_END in practice; detect silence yourself (`D/even_ai.dart:8`, `P/routes/even_ai.py:116-143`). Alternative mic enable without an Even AI session: `0xE0` cmd 15 `{18: {1: 1}}` audio control (`D/evenhub.dart:159-160`, `P/routes/even_ai.py:534-538`);
4. app → `ASK {5: {4: bytes text}}`, 300 ms, `ANALYSE {6: {}}` ("thinking"), then `REPLY {7: {4: chunk, [6: 1 on the last]}}` chunks ≤512 UTF-8 bytes, 150 ms apart (`:52-80`); glasses → `EVENT {10: {1: 2 STREAM_COMPLETE}}`;
5. either side → `CTRL {3: {1: 3 EXIT}}`; stop the heartbeat. Optional `CONFIG {13: {1 voiceSwitch, 2 streamSpeed 80}}` (`:65-66`; the captured connect sequence sends it with speed 80, `D/device.dart:185`).
Community minimal path (`S:feature/even-ai docs/even-ai.md:25-36,278-299`, `examples/even-ai/even_ai.py:103-194`): after auth, `CTRL ENTER` → 0.3 s → `ASK {5: {1:0, 2:0, 3:0, 4: text}}` → 1 s → `REPLY {7: {1:0, 2:0, 3:0, 4: text}}` with no heartbeat, no ANALYSE, no end flag; verified packets `aa21080a01010720080110641a0208025ca9` (ENTER, CRC checks), `aa21091e01010720080310652a16080010001800220e576861742069732032202b20323fc72b` (ASK), `aa210a2001010720080510663a18080010001800221054686520616e737765722069732034218432` (REPLY). Their field names `cmdCnt/streamEnable/textMode` for 1/2/3 of Ask/ReplyInfo come from an aegray proto dump; the hub omits those zero fields (proto3 default). COMMUNITY: "✅ custom Q&A confirmed" (Jan 2026 firmware). Expect the glasses to drop the screen after a few seconds without the heartbeat (`D/even_ai.dart:5-7`).

---

## 6. Phone-style notification cards — DECODED/UNTESTED in the hub, COMMUNITY-live

Two parts: (a) SID 0x04 control on `5401` (§1 #15; also cmd 1 field 4 iOS app register `{1 appId ≤32 B, 2 displayName ≤32 B}`, whitelist check reply field 7 `{1 crc, 2 status 0 MATCH 1 MISMATCH}`; `D/notification.dart:14-26,60-121`); (b) the card itself as a **JSON file over the EvenFile service** (`7401`/`7402`, SIDs `0xC4` cmd / `0xC5` data, flag 0x00, right lens).

JSON body (compact, key order as the app): `{"android_notification":{"msg_id":<int>,"action":0,"app_identifier":"com.even.sg","title":"…","subtitle":"…","message":"…","time_s":<unix s>,"date":"YYYYMMDDTHHMMSS" (local),"display_name":"Even Realities"}}`. Firmware strncpy caps (bytes): app_identifier 63, title 63, subtitle 63, message 511, display_name 31 (`D/file_service.dart:102-129`, `D/limits.dart:35-40`). `action` 0 = show.

Hub transfer (`D/file_service.dart:28-67,147`, `H/file_transfer.dart:52-78`): not AA-framed protobuf but raw command bytes inside the AA header:
1. START: payload `00` + u32 LE fileType (0 whitelist, 1 notification) + u32 LE length + u32 LE **crc32 poly 0x1EDC6F41, MSB-first, init 0, no xor** (check value `0xC052A8C8` for "123456789") + 80-byte zero-padded `user/notify_whitelist.json`; one frame `AA 21 seq 5F 01 01 C4 00 … crc16`; wait for reply `[cid 0, rsp]`.
2. Per 4096-byte block: DATA prefix frame `AA 21 m 03 01 01 C4 00 01 crc16`, then the block as `C5` frames of 232 bytes (`total/num` set, CRC16 of the block only on the last packet, extra empty packet when `len % 232` is 0 or 231); wait `[1, rsp]`; retry ≤3 on rsp 2 CRC / 4 TIMEOUT / 5 NO_RESOURCES (5 s pause).
3. RESULT_CHECK `AA 21 seq 03 01 01 C4 00 02 crc16`; wait `[2, 0]`. Replies also show up on `5402`.

Community variant (`S:feature/notification-simple docs/notification.md:38-66,124-161,230-246`, `examples/notif/notification.py:189-231`, COMMUNITY-live): same three steps, but their 93-byte START header is `u32 0x100, u32 len*256, u32 (crc<<8), u8 crc>>24, 80-byte name` — byte-for-byte identical to the hub's `00 | fileType=1 | len | crc32` layout (the shift is the leading `00` byte). Same CRC-32C table (poly 0x1EDC6F41, non-reflected). They send the whole JSON in one `C5` frame (≤234 B; multi-packet "fails silently"), use `app_identifier "com.google.android.gm"` / `display_name "Gmail"`, wait 0.3 s between steps, ignore replies, and finish with a `0x80` keep-alive to the left lens (their frame `aa210e0601018020080e106b6a00e174` is internally inconsistent — len 6 but 8 bytes follow and no CRC reading matches; use the §1 keep-alive form). Same checksum ⇒ cached, no re-display: vary `msg_id`/text. The hub's whitelist-disable (§1 #15) is presumably why any `app_identifier` works there; the community had only Gmail confirmed.

---

## 7. Feature → source → confidence

| Feature | Implemented in | Confidence |
|---|---|---|
| Auth frame, role, time sync, keep-alive, flags | `D/device.dart`, `D/framing.dart`, `H/glasses_session.dart` | LIVE (captured from app, acked) |
| Prelude (SID 0x01 magic 156) + EvenHub heartbeat gating | `D/evenhub.dart:144-157`, `H/features.dart:98-117` | LIVE |
| Dashboard init (display_mode 4) | `D/dashboard.dart:199-209` | LIVE (flag 0; effect on layout unverified) |
| Custom widget order (`buildDashboardLayout`) | `D/dashboard.dart:353-361` | EXPERIMENTAL (only default order confirmed) |
| Weather status bar | `D/dashboard.dart:232-253`, `P/routes/weather.py` | LIVE |
| Calendar (1/msg and multi) | `D/dashboard.dart:260-287`, `P/routes/calendar.py` | LIVE |
| News V3 FIFO | `D/dashboard.dart:178-229`, `P/routes/news.py` | LIVE (every step acked 2026-09-29) |
| News V2 widget push | `D/dashboard.dart:142-147` | LIVE-NEGATIVE (PARAMETER_ERROR) |
| Quicklist/tasks + ticks + paging | `D/quicklist.dart`, `P/routes/tasks.py` | LIVE |
| Menu replacement (built-ins) | `D/menu.dart`, `P/routes/menu.py` | LIVE |
| Menu with plugin items | `D/evenhub_pages.dart:240-259` | DECODED |
| Settings SET brightness/auto/silent/wear/head-up/units/gestures | `D/settings.dart` | LIVE |
| Settings SET display height/distance, hand | `D/settings.dart:140-152,182-189` | DECODED |
| Settings READ + readback fields | `D/settings.dart:113-116,48-68` | LIVE |
| Module config (timeout, language) | `D/settings.dart:199-215` | LIVE (hub exposes both) |
| GET_DEVICE_INFO / SET_DEVICE_INFO | `D/device.dart:143-146,210-247` | DECODED |
| Quick restart / factory reset / unpair | `D/device.dart:137-141` | DECODED |
| Stocks card | `D/stocks.dart` | DECODED (never seen on glasses) |
| Health card | `D/health.dart` | DECODED |
| Watchface layout read/upload | `D/watchface.dart`, `P/routes/watchface.py` | DECODED, risky |
| Even AI full session (wake, enter, hb, mic, ask/analyse/reply, exit) | `D/even_ai.dart`, `P/routes/even_ai.py`, `H/even_ai.dart` | LIVE |
| Even AI minimal Q&A (no hb) | `S:feature/even-ai` | COMMUNITY |
| Mic stream 0x6402 layout | `D/mic_audio.dart` | LIVE (386 captured packets) |
| EvenHub audio control (cmd 15) | `D/evenhub.dart:159`, `P/routes/even_ai.py:534-538` | LIVE (hub strategy 1) |
| Translate open/text/close | `D/translate.dart`, `P/native_apps.py` | DECODED (live COMM_RESP seen, incl. FAIL) |
| Teleprompter (hub flow) | `D/teleprompter.dart`, `H/features.dart:495-540` | DECODED |
| Teleprompter (community flow w/ 0x0E blob) | `S/examples/teleprompter` | COMMUNITY |
| Navigation | `D/navigation.dart` | DECODED |
| Notification CTRL / whitelist-off | `D/file_service.dart:132-144` | LIVE (sent every connect) |
| Notification JSON over EvenFile (hub form) | `D/file_service.dart`, `H/file_transfer.dart` | DECODED/UNTESTED |
| Notification JSON over EvenFile (community form) | `S:feature/notification-simple` | COMMUNITY |
| App-sync foreground pushes | `D/app_sync.dart` | LIVE |
| Case / wear / onboarding queries | `D/device.dart:148-162` | LIVE |
| Ring (SID 0x90/0x91) | `S:feature/even-ai docs/R1_ANALYSIS.md` | COMMUNITY partial, not needed here |

---

## 8. Minimum viable offline dashboard (time, date, battery, weather) and keeping it alive

All frames to the **right** lens unless marked L. `m` = next magic (1..255), `s` = next frame seq. Flag 0x20 unless marked.

```
connect R and L, bond, MTU≥247, subscribe 5402 on both; wait 300 ms
 1. R  0x80 flag 00  auth   aa21 <m> 0c 0101 80 00  08 04 10 <m> 1a 04 08 01 10 04  crc   (wait ≤5 s for a reply ending 1a 00)
 2. L  0x80 flag 00  auth   same shape, new m; wait 300 ms
 3. R  0x01          prelude aa219213010101200802109c01220a1a081206120408001000a142 ; wait 1.5 s
 4. R  0xE0          start heartbeat {1:12,2:m,14:{1:0}} every 4 s  (080c10<m>72020800)
 5. R  0x01          prelude again; wait 1.5 s
 6. R  0x01          dashboard init  0802 10<m> 221b 0801 1215 0804 1003 1a03010203 2004 2a0401030202 3001 3801 1a00 ; wait ack flag 0
 7. R  0x0D          subscribe {1:0,2:m}
 8. R  0x80          role RIGHT   0805 10<m> 2202 0801
 9. R  0x80          time sync    0880 01 10<m> 8208 <len> 08 <unix s varint> 10 <tz 15-min units, negative as 10-byte varint>
10. R  0x09          settings READ 0802 10<m> 2202 0801      -> parse field 4: battery(12) charging(13) height(3) distance(4) fw(5,6)
11. R  0x09          units {9:{1:0,2:0,3:1,4:0,5:1}}  (3 = time format: 0 24 h / 1 12 h; 5 = 1 C / 2 F)
12. R  0x10          onboarding FINISH 0801 10<m> 1a02 0804
13. R  0x10          wear query        0803 10<m> 2a02 0801
14. R  0x81          case query        0801 10<m> 1a00
15. R  0x09          gestures default (see §1 #14)
16. R  0x04          notif CTRL 0801 10<m> 1a06 080110011805 ; then whitelist off 0803 10<m> 3202 0801
     (150 ms between 8-16)
17. R  0x01          weather  {1:2,2:m,4:{3:{1:{1:{1:float C,2:unit,3:type,4:epoch ms,5:"Sunny"}}}}} ; wait flag 0
18.    stop the 0xE0 heartbeat  -> native watchface dashboard appears in ~5 s with clock, date, battery, weather
```
Keep-alive loop (from here on): every 60 s send `0x80` flag 0x00 `{1:14,2:m,13:{}}` to **L**, wait 2 s, same (new m) to **R**; if the right lens misses two echoes, reconnect. Every 60 min re-send step 9. Every 15 min re-push weather: restart the `0xE0` heartbeat, wait 2 s, send step 17, wait for flag 0, stop the heartbeat again (same for calendar/news/tasks). Steps 10-16 are optional for the display but 10 is how you learn battery/calibration, 16 is required for notifications, and the hub always sends them (`H/glasses_session.dart:213-243`, `P/connection.py:46-57`).

Things to verify live on this unit before building on them: whether step 6 alone changes the widget order (oneof caveat, §2), what status slots 2/3 are, whether the glasses keep the dashboard without steps 7/12-15, and the display-height/distance SET fields (never sent by either hub).
