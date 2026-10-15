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
