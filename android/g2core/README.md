# g2core: Even Realities G2 native protocol for Android (pure Kotlin)

Lets an Android phone act as the hub: it talks to both G2 arms directly over BLE, with no laptop and no gateway.
The protocol layer is a byte-exact port of the Python library in `g2/native/` (stdlib only, tested against every hex
vector in `g2/docs/native-protocol.md`). The BLE transport is a port of the transport half of
`g2/vendor/men-g2-ble-gateway/mentraos/g2/client.py` + `scan.py`.

Package `com.kunsh.g2core`. Android library, minSdk 26, AGP 8.13.0, Kotlin 2.2.20. Dependencies:
kotlinx-coroutines-core/android only (JUnit 4 for tests). The module has no app-specific code, holds no device
identifiers or keys, and declares no permissions. The host app declares `BLUETOOTH_SCAN` / `BLUETOOTH_CONNECT`
(API 31+) or `BLUETOOTH` + `ACCESS_FINE_LOCATION` (API 26-30) and requests them at runtime.

## Module map

| Kotlin | Python source | What | Key names |
|---|---|---|---|
| `Pb.kt` | `pb.py` | protobuf wire codec (varint, negatives as 10-byte two's complement, len-delimited, fixed32/64, packed), `Writer`, `Reader`, `Text.head/utf8Fit` | `Pb.encodeVarint/iterFields/readVarint/readBytes/...`, `Writer`, `Reader` |
| `Framing.kt` | `framing.py` | `AA 21 seq len total num sid flag payload crc16` envelope, 232-byte fragmentation, parsing, inbound reassembly, counters | `Framing.framePb/authFrame/parseFrame/parseRx/readAckMagic/crc16`, `FrameAssembler`, `MagicAllocator` |
| `Session.kt` | `session.py` | prelude, EvenHub heartbeat, time sync, keep-alive, role, onboarding/wear/case, connect sequence, `MinimalDashboardPlan`, `HubStartupPlan`, parsers | `Session.*`, `Step`, `Expect`, `Side`, `MinimalDashboardPlan`, `HubStartupPlan` |
| `Settings.kt` | `settings.py` | SID 0x09 SET/READ + snapshot, SID 0x20 module config, SID 0x80 device info | `Settings.readSettings/brightness/autoBrightness/displayHeight/displayDistance/displayTimeout/units/gestures/...`, `Settings.parse` |
| `Dashboard.kt` | `dashboard.py` | SID 0x01 init/layout, weather, calendar, news V3, stocks; 0x0C quicklist; 0x0E health; 0x1F watchface read | `Dashboard.dashboardInit/weather/calendarEvent(s)/calendarSequence/newsPush/quicklistFullUpdate/tasksSequence/stockPush/ackFlag` |
| `Menu.kt` | `menu.py` | SID 0x03 whole-menu replace, selection events | `Menu.menuPush/validate/parseSelectionEvent` |
| `Notifications.kt` | `notifications.py` | SID 0x04 ctrl/whitelist, notification JSON, EvenFile hub + community forms, CRC-32 (EvenFile: poly 0x1EDC6F41 MSB-first, init 0, no xor) | `Notifications.notificationCtrl/whitelistDisable/notificationJson/hubNotificationSteps/communityNotificationFrames/crc32Efs` |
| `EvenAi.kt` | `even_ai.py` | SID 0x07 session (enter/ask/analyse/reply chunks/exit/heartbeat), mic packet layout, EvenHub audio control | `EvenAi.enter/ask/analyse/reply/replyChunks/replySequence/exit/heartbeat/parse/parseMicPacket` |
| `Apps.kt` | `apps.py` | Teleprompter (hub + community), Translate, Navigation | `Apps.tp*`, `Apps.communityTp*`, `Apps.tr*`, `Apps.nav*` |
| `G2Transport.kt` | `transport.Transport` | link interface: `write(side, frame, char)` + `inbound` flow + `maxWrite` | `G2Transport`, `G2Char`, `G2Inbound` |
| `G2Session.kt` | `transport.Glasses` | coroutines: reader/reassembly, request/reply matching on (sid, magic) with timeouts, startup plan, keep-alive loop + watchdog, heartbeat start/stop, push helpers | `start/stop/send/sendPb/request/startup/reinitSide/readSettings/applySetting/setWeather/setCalendar/pushNews/setTasks/setStocks/setMenu/notify/evenAiAnswer/keepaliveLoop` |
| `G2Ble.kt` | gateway `client.py`, `scan.py`, `constants.py` | android.bluetooth: scan `Even G2_` names, pair L/R by serial, connect (autoConnect=false), MTU 247, discover, notify 5402 (both) / 6402 (left) / 7402 (best effort), serialized write-without-response queue per arm with an 8 ms packet gap, per-arm reconnect with backoff | `scanForPair/connect/awaitReady/disconnect/state/readyEvents/inbound/write` |

Python names map to camelCase (`frame_pb` becomes `framePb`, `exit_` becomes `exit`). Keyword-heavy builders take a small data class
(`Dashboard.Weather`, `Dashboard.Schedule`, `Dashboard.Task`, `Dashboard.Stock`, `Menu.PluginItem`).

## Usage

```kotlin
val ble = G2Ble(context, lifecycleScope, log = { Log.d("G2", it) })
val pair = ble.scanForPair() ?: error("no G2 found")
ble.connect(pair)
check(ble.awaitReady())

val g2 = G2Session(ble, lifecycleScope, log = { Log.d("G2", it) })
val res = g2.startup()                  // auth L, auth R, init, role, time, settings READ, units, onboarding, wear, case, gestures, notif ctrl
if (!res.ok) Log.w("G2", "no reply for ${res.missing.map { it.label }}")
g2.setWeather(21f, weatherType = 1, status = "Sunny")
g2.setCalendar(listOf(Dashboard.Schedule("Standup", "Room 1", "10:00 AM-10:30 AM", endTs = 1792500000)))
g2.setTasks(listOf(Dashboard.Task(1, "Milk"), Dashboard.Task(2, "Eggs", completed = true)))
g2.applySetting { Settings.brightness(it, 20) }
g2.notify("Hello", "from the phone", form = G2Session.NotifyForm.COMMUNITY)
g2.onLinkLost = { /* keep-alive watchdog: two missed right-arm echoes */ }
lifecycleScope.launch { ble.readyEvents.collect { side -> g2.reinitSide(side) } }   // after a per-arm reconnect
lifecycleScope.launch { g2.micPackets.collect { pkt -> /* 5 x 40-byte LC3 frames, 10 ms each, 16 kHz */ } }
```

The EvenHub heartbeat (SID 0xE0) is OFF by default: this hub never shows an EvenHub page, and on hardware the
native dashboard only redraws pushed content while no heartbeat is being sent. `G2Session.Config(heartbeatForPushes = true)`
brackets every push with heartbeat-on / heartbeat-off like the Python `Glasses`, and `startupHeartbeat = true` reproduces
the prelude + heartbeat steps of `MinimalDashboardPlan`.

## Confidence per feature (copied from g2/native/README.md, native-protocol.md section 7)

| Feature | Kotlin | Confidence |
|---|---|---|
| Auth frame, role, time sync, keep-alive, flag rule | `Framing`, `Session` | **LIVE** (captured from the app, acked) |
| Prelude (SID 0x01 magic 156) + EvenHub heartbeat gating | `Session` | **LIVE** |
| Dashboard init (display_mode 4) | `Dashboard.dashboardInit` | **LIVE** (flag 0; effect on layout unverified) |
| Custom widget order | `Dashboard.dashboardLayout` | EXPERIMENTAL (only the default order confirmed) |
| Weather status bar | `Dashboard.weather` | **LIVE** |
| Calendar (1/msg and multi) | `Dashboard.calendarEvent(s)` | **LIVE** |
| News V3 FIFO | `Dashboard.news*` | **LIVE** (every step acked 2026-09-29) |
| Quicklist/tasks + ticks + paging | `Dashboard.quicklist*` | **LIVE** |
| Menu replacement (built-ins) | `Menu.menuPush` | **LIVE** |
| Menu with plugin items | `Menu.PluginItem` | DECODED |
| Settings SET brightness/auto/silent/wear/head-up/units/gestures | `Settings` | **LIVE** |
| Settings SET display height/distance, dominant hand | `Settings.displayHeight/displayDistance/dominantHand` | DECODED (never sent by either hub) |
| Settings READ + readback fields | `Settings.readSettings`, `Settings.Snapshot` | **LIVE** |
| Module config (timeout, language) | `Settings.displayTimeout/language` | **LIVE** |
| GET_DEVICE_INFO | `Settings.getDeviceInfo` (no SET builder) | DECODED |
| Quick restart / factory reset / unpair | `Settings.quickRestart/factoryReset/unpair` | DECODED (destructive) |
| Stocks card | `Dashboard.stockPush`, `G2Session.setStocks` | DECODED (never seen on glasses) |
| Health card | `Dashboard.healthSnapshot` | DECODED |
| Watchface layout read | `Dashboard.watchfaceInfoRequest` | DECODED, risky; **no upload builder on purpose** |
| Even AI full session | `EvenAi` | **LIVE** |
| Even AI minimal Q&A (no heartbeat) | `EvenAi.ask/reply(community = true)` | COMMUNITY |
| Mic stream 0x6402 layout | `EvenAi.parseMicPacket` | **LIVE** (386 captured packets) |
| EvenHub audio control (cmd 15) | `EvenAi.evenhubAudioControl`, `G2Session.setMicEnabled` | **LIVE** |
| Translate open/text/close | `Apps.tr*` | DECODED (live COMM_RESP seen, incl. FAIL) |
| Teleprompter (hub flow) | `Apps.tp*` | DECODED |
| Teleprompter (community 0x0E-blob flow) | `Apps.communityTp*` | COMMUNITY |
| Navigation | `Apps.nav*` | DECODED |
| Notification CTRL / whitelist-off | `Notifications.notificationCtrl/whitelistDisable` | **LIVE** |
| Notification JSON over EvenFile (hub form) | `Notifications.hubNotificationSteps`, `notify(HUB)` | DECODED/UNTESTED |
| Notification JSON over EvenFile (community form) | `Notifications.communityNotificationFrames`, `notify(COMMUNITY)` | COMMUNITY |
| App-sync foreground pushes | `Session.parseAppSync` | **LIVE** |
| Case / wear / onboarding queries | `Session.caseQuery/wearQuery/onboardingFinish` | **LIVE** |

Hardware facts verified live from a central (no app-level secrets): both arms connect; settings READ, dashboard init,
units, time sync, weather, calendar, quick list (tasks) and menu are all acked; the mic is LC3 10 ms / 40-byte frames
at 16 kHz from the left arm, 5 frames per notification. `G2Ble` and `G2Session.startup` have not yet run on a phone:
they compile and the session logic is unit-tested against a fake transport only.

## Golden vectors (how they are regenerated)

`src/test/resources/vectors.json` is generated FROM the Python library. Every entry is
`{"name", "fn", "args", "out"}`: the Python builder, its exact inputs (fixed magics, seqs, clocks, time zones), and the
expected bytes as hex. `VectorsTest` (JUnit `Parameterized`, one test per vector) calls the Kotlin builder with the same
inputs and compares byte for byte. After changing anything in `g2/native/`:

```
cd C:/Users/Kunsh/Desktop/frame
.venv/Scripts/python.exe -m g2.tools.export_vectors          # rewrites android/g2core/src/test/resources/vectors.json
.venv/Scripts/python.exe -m unittest g2.test_native          # Python side still green
```

then rebuild and run the tests:

```
cd C:/Users/Kunsh/Desktop/frame/android
JAVA_HOME="C:\Program Files\Android\Android Studio\jbr" cmd //c "C:\Users\Kunsh\.gradle\wrapper\dists\gradle-8.14.3-bin\cv11ve7ro1n3o1j4so8xd9n66\gradle-8.14.3\bin\gradle.bat :g2core:testDebugUnitTest :g2core:assembleDebug --no-daemon --console=plain -q"
```

A new builder in a vector needs a matching `fn` branch in `VectorsTest.run()`; an unknown `fn` fails the test.

Other tests: `ProtocolTest` (parsers, validation, framing property round trip, CRC check values, ported from
`g2/test_native.py`) and `G2SessionTest` (startup plan, (sid, magic) matching, BOTH, fragmentation, pushes, notify,
Even AI, keep-alive watchdog, reinit), both run against `FakeTransport` + `fakeGlassesResponder` (ports of the Python fakes).

## Known differences from the Python reference

- `Apps.tpPaginate` ports `textwrap.wrap`, including its word-separator regex, hyphen breaking and long-word splitting,
  to `java.util.regex` with `UNICODE_CHARACTER_CLASS`. The vectors cover plain words, multiple spaces, tabs and long words.
  Exotic Unicode word classes may differ slightly.
- `notify()` derives the JSON `date` offset from the fixed `tzUnits` when one is given; Python always uses the host zone.
- `HubStartupPlan` follows the gateway's auth order (LEFT fire-and-forget, then RIGHT awaited) and allocates the auth
  magics from the allocator. `MinimalDashboardPlan` keeps the Python order (RIGHT then LEFT, magics 1 and 2) and is
  vector-tested byte for byte.
