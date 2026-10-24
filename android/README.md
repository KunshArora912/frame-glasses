# Frame companion for Android

Native Kotlin app. Holds the Bluetooth link to the glasses in a foreground
service (survives the screen turning off), shows battery / charging /
firmware from the wake screen's status line, has a Lua console, and can
relay the link to the laptop so the Python apps run through the phone.

- **Install:** `adb install -r android/frame-companion-debug.apk`, or copy the
  APK to the phone and open it (allow installs from that source).
- **Build:** JDK 17+ and the Android SDK; `gradle assembleDebug` in `android/`
  (Android Studio's bundled JDK and the cached Gradle 8.14.3 work).
- **First connect:** Frame keeps one bond. If the laptop is bonded, do the
  cradle-pin reset first. Then Connect, accept Android's pairing prompt.
  The address is remembered; Forget makes the next Connect scan again.
- **Relay:** run `python relay_server.py` on the laptop, enter its URL
  (`ws://<laptop-ip>:8765/phone` on the same Wi-Fi, or the Tailscale name),
  tap Link. Then `python frame_host.py --relay` or
  `python frame_relay.py <app.py>` on the laptop.

Package `com.kunsh.frame`, minSdk 26, targetSdk 36. Files:
`FrameBle.kt` (scan/connect/MTU/notify/serialised writes), `Relay.kt`
(OkHttp WebSocket, same JSON protocol as `phone/app.js`), `FrameService.kt`
(foreground service, status parsing, notification), `MainActivity.kt`.

## G2 laptop link (Even G2)

In **Hub: Laptop** mode the laptop holds the G2 glasses' Bluetooth link and this app never touches it
(for Phone mode see the next section). While
the G2 link is on, Frame Connect is refused, and turning the link on closes any Frame link. The
app forwards phone notifications and GPS to `g2/phone_link.py` and shows a **Display** card
(Display Height, Display Distance Near / Mid / Far + fine slider, brightness + Auto, glasses
battery). The card is synced both ways through the laptop. See `g2/PHONE.md`.

- **Laptop:** `python g2/run.py` (the phone link listens on 127.0.0.1:8768), then
  `adb reverse tcp:8768 tcp:8768` over USB (repeat after a replug or adb restart).
- **Phone:** in **G2 laptop link**, keep the URL `ws://localhost:8768/phone` and tap Connect
  (allow location + notifications). Then tap **Notification access** and enable the app. Location
  must be on in Android's settings for GPS fixes.
- The link reconnects on its own (2 s .. 30 s backoff). When the system rebinds the notification
  listener (reboot, update) it resumes notification forwarding. Opening the app brings back GPS
  and the foreground service (types connectedDevice + location).
- Never forwarded: this app, `com.even.sg`, ongoing notifications, group summaries.
- Files: `G2Link.kt` (WebSocket + state), `G2NotificationListener.kt`, `G2Location.kt`
  (LocationManager), `DisplayCard.kt`; wiring in `FrameService.kt` / `MainActivity.kt`.

## G2 phone hub (Even G2 on the go, no laptop)

The **Even G2 hub** card at the top picks who holds the glasses: **Hub: Laptop** (default, the
section above) or **Phone**. Only one host can hold them. Phone mode stops the laptop link; the
laptop gateway (`g2/run.py`) must be stopped by hand on the laptop, because the phone cannot see it.

In Phone mode the foreground service talks to both arms itself through the `android/g2core`
library (`G2Ble` + `G2Session`). Even's native dashboard stays the only UI on the glasses: no
EvenHub page ("board"), no menu push, and no dashboard widget push or display-enable unless you
switch that feature on in the app. A double tap on the glasses closes things as usual.

- **Connect:** scan (15 s) for `Even G2_*_L_/_R_`, connect both arms without bonding, and fall
  back to `createBond()` if the arms do not come up or do not authenticate (the result is
  remembered). Then `G2Session.startup()` runs with the EvenHub heartbeat off, followed by the
  60 s keep-alive and an hourly time sync. A per-arm reconnect re-runs `reinitSide`; the
  keep-alive watchdog (two missed echoes) triggers a full rescan with backoff (5 s .. 60 s).
  If no arm advertises, the card says so: the glasses are held by something else (usually
  the laptop gateway), in the closed case, or out of range.
- **Status:** left/right arm state and the glasses' battery (settings READ every 60 s, plus the
  glasses' own pushes).
- **Display card:** height, distance, brightness and Auto are read and applied directly
  (`readSettings` / `applySetting`, at most every 300 ms, last value wins, re-read after).
- **Notifications:** the same listener and filters as laptop mode, plus: silent notifications
  are dropped, the same key and text is dropped within 10 s, at most one card goes out every
  3 s, and several queued notifications become one "N notifications" card. A card first goes
  in the hub form (acked). After two hub-form failures in a row, the app uses the community form
  (fire-and-forget). The log says which form acked. Switch: "Phone notifications to the glasses".
- **Ask questions (Even AI replacement):** pick Even AI on the glasses (or use your long-press
  gesture). The glasses send `CTRL WAKE_UP`, and the phone answers with ENTER, a heartbeat every
  2 s, and listens on the **phone microphone** with `SpeechRecognizer`. It prefers the on-device
  recognizer (Android 12+) and otherwise asks for offline recognition. The glasses' own mic
  sends LC3, which is not decoded on Android yet. The app then sends ASK (the transcript) and
  ANALYSE, calls Claude (`claude-opus-5-5`, falling back to `claude-sonnet-5-5`; max_tokens 400;
  "plain text, at most 60 words"), sends REPLY in chunks of 512 bytes or less with the end flag,
  and EXITs after the reading time (8 s + 60 ms per character, at most 90 s; a scroll on the
  glasses restarts it). EXIT from the glasses (double tap) cancels the turn at any point.
  Claude's tools run on the phone: `add_calendar_event` / `list_calendar_events`
  (CalendarContract; new events go to the calendar chosen under Dashboard features, default
  the primary writable one), `save_idea` (`files/ideas.jsonl`), and `get_stock_quote` (Yahoo
  chart JSON, falling back to Stooq CSV; no key). The loop allows up to 4 tool rounds.
- **Claude API key:** never in the repo or the APK. Enter it in the masked field, or provision
  it with `adb shell am start -n com.kunsh.frame/.MainActivity --es claude_key <key>`; the
  app stores it and strips the extra from the intent. It is encrypted with a non-exportable
  Android Keystore key (AES-GCM) and kept in app-private prefs. The UI shows only
  "key set (...last4)".
- **Dashboard features (all OFF by default):** "Stocks on dashboard" (tickers, up to 5; quotes
  are pushed to the Stocks card every 15 min with `setStocks`, which also display-enables the
  Stocks widget), "Calendar on dashboard" (today and tomorrow, up to 8 events, every 15 min with
  `setCalendar`), and "Ideas on dashboard" (the latest 20 ideas to the quick list with
  `setTasks`, once when switched on and again after each new idea). Switching one on pushes
  once and shows what was pushed and the ack flags. Switching it off stops pushing and restores
  nothing.
- **Permissions:** Bluetooth scan/connect, microphone, calendar read/write, notifications
  (Android 13+), and notification access for forwarding. The foreground service adds the
  `microphone` type while the phone hub is on.
- **Privacy:** only the spoken question (as text) and Claude's tool traffic go to Anthropic's
  API. Stock lookups go to Yahoo/Stooq, and only when asked or when the Stocks feature is on.
  Calendar, ideas and notifications stay on the phone, and Even's servers are never contacted.
- Files: `PhoneHub.kt` (connect/startup/keep-alive/reconnect, status, display, notifications,
  feeds), `EvenAiTurns.kt` (the SID 0x07 session), `PhoneSpeech.kt`, `ClaudeAgent.kt` (Messages
  API over OkHttp + the tool loop + `GlassesText`), `PhoneTools.kt`, `AndroidCalendar.kt`,
  `Stocks.kt`, `Ideas.kt`, `ApiKeyStore.kt`, `Json.kt`. Tests (JVM, no Robolectric):
  `app/src/test` (tool loop with a fake HTTP layer, chunking and reading time, stock parsing for
  both sources, idea storage). Run them with `gradle :app:testDebugUnitTest :g2core:testDebugUnitTest`.

## Updating the glasses while the phone holds the bond

No re-pairing. The laptop goes through the phone for everything except the
firmware transfer itself, and the bootloader accepts an unbonded laptop:

| what | command on the laptop (app connected and linked) |
|---|---|
| wake screen / main.lua | `python frame_relay.py install_main.py` |
| any app | `python frame_relay.py assistant.py` (or `frame_host.py --relay`) |
| Lua one-liners | the console in the app, or `python frame_relay.py wake/lua_exec.py "print(1)"` |
| firmware | `python dfu_update.py --zip firmware/<build>.zip --via-phone` |

The firmware path asks the glasses to reboot into the bootloader through the
phone, flashes them directly from the laptop (no bond needed there), then
reads the version back through the phone once its app has reconnected.
A DFU wipes main.lua, so run the install line afterwards.
