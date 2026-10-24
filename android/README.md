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

For the Even G2 the laptop holds the glasses' Bluetooth link and this app never touches it. While
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
