# Even Realities app (com.even.sg) — what it provides for the G2, feature by feature

Compiled 2026-10-23 from Even's support center, hub.evenrealities.com developer docs, the App Store / Google Play
listings, Even's privacy notices, and major reviews. Goal: a parity reference for an offline replacement.
Confidence tags: **[official]** Even document, **[review]** press hands-on, **[community]** reverse-engineering /
third-party, **[unverified]** could not be confirmed.

## 0. Versions as of October 2026

| Item | Value | Notes |
|---|---|---|
| iOS app | 2.3.3 (App Store, ~21 Oct 2026), 514.9 MB, iOS 16.0+ | App Store id 6747017725 ("Even Realities"); the old "Even G1" app is id 6499140518 [official] |
| Android app | 2.3.2 (Play "Updated on Sep 30, 2026"), package `com.even.sg` | Play data-safety card claims "No data collected / No data shared" (contradicts the policy) [official] |
| G2 firmware | Even publishes no numbers or changelog. Firmware tracks app numbering: support refers to "firmware 2.2.9 or later", "2.2.6 or later", and "version 2.3.2 and later of the App, G2 firmware and R1 firmware" | Community: stock images `s200_v2.2.6.10` (EVENOTA), `g2_2.2.9.22`; 16 archived official G2 builds, 12 R1 builds [community]. Current G2 build is therefore a 2.3.2.x/2.3.3.x image [unverified] |
| R1 firmware | app `2.2.6.0009` analysed by openCFW; Nordic nRF52840 + S140 SoftDevice, Secure DFU | [community] |
| App 2.3.2 note | "requires updating the App and the firmware on both Even G2 and Even R1" | [official] |

## 1. Glasses-side experience

### 1.1 Dashboard (home screen) [official unless noted]
* **Status block (left):** time; date + day of week; glasses battery; weather + temperature; unread-notification count
  (bell icon). R1 ring battery was added to Dashboard in app 2.2.4. Reviews add: temperature, sunset and next-day
  forecast in the bottom-left corner [review]. Weather only appears if location permission is granted.
* **Widgets area (right):** one card visible at a time. Available cards: **News, Stocks, Calendar, Reminders
  (formerly QuickList), Health (R1 required), QuickNote (2.3.2+)**. Order and per-widget settings are managed in
  App > Dashboard > Widgets (Add widget / rearrange). Reviewers note widgets cannot all be removed [review].
* **Browsing:** swipe = next/previous card; single tap = expand card; double tap = collapse / back.
* **Layout modes:** app 2.2.4 added "Dashboard customization and new watch faces". The support center does not name
  the layouts; the terms "minimal / dual / full" could not be confirmed **[unverified]**. Even Hub docs list
  "dashboard widgets" and "dashboard layouts" as future third-party surfaces, so layouts are a first-class concept.
* **Depth layers:** foreground layer (notifications, pop-ups) is rendered brighter and nearer (~1/2/3 m) than the
  background layer (main UI, ~2/3/4 m) depending on the Near/Mid/Far preset; the two cannot be offset independently.
* **HeadUp Display:** on by default. With the display off, raising the head past the set angle turns it on.
  Angle slider **0°–60°**, "Reset to default", "Recalibrate 0° position" (guided). If the display was woken by touch
  or voice, lowering the head does *not* turn it off (double-tap to exit).
* **Auto display off:** Dashboard turns off after **10 s** of inactivity by default; selectable durations or "keep
  on"; persists on the glasses when the phone disconnects. Does not affect Conversate/Translate display-off rules.

### 1.2 Controls (G2 temple TouchPads; R1 ring mirrors them) [official]

| Gesture | Display off | Display on |
|---|---|---|
| Single tap | — | Confirm (list) / toggle full vs compact view (card) |
| Double tap | Open Dashboard (wake) | Back / close / exit prompt |
| Tap then long-press 1 s (fw >= 2.2.9; plain long-press 1 s before) | Menu | Menu, or in-feature menu while a feature runs |
| Swipe front / back (R1: up / down) | — | Scroll up / down, switch widget or menu item |
| Long-press (hold while speaking) | QuickNote recording; customisable (2.3.2) | feature-specific; customisable on one TouchPad |
| Hold both TouchPads 1 s | Enter/exit Silent Mode | same |
| 5 quick taps on both sides | Restart G2 | same |
| 10 rapid taps | Regulatory info | — |
| Voice "Hey Even" | wakes Even AI (works with display off, not in Silent Mode) | same |
| Head raise | wakes Dashboard (HeadUp) | — |

Long-press customisation (Settings > Gesture Customization > Long-press, 2.3.2+): choose Left/Right/None TouchPad on
the glasses and one action for the ring. Actions: Wake Even AI, Show Menu, Record a QuickNote, Show Notifications,
Open Conversate / Translate / Teleprompt / Navigate / Terminal. Ring option "Enable while display is off?".

### 1.3 Menu shown on the glasses [official]
Entries (order/visibility customisable in the app since 2.0.6): **Dashboard, Conversate, Teleprompt, QuickNote,
Translate, Navigate, Notification, Even AI, Silent Mode**. Installed Even Hub plug-ins and Terminal also launch from
the glasses (2.3.1 "feature launch from G2") [official release notes]. In-feature menus (Conversate, Translate,
QuickNote, Navigate) offer Display off, Brightness, language change, End session, Undo last input, Close.

## 2. Native features

### 2.1 Even AI [official]
* Wake: "Hey Even" (toggle) or Menu > Even AI; "Even AI is listening" shown; responses stream as text (no audio).
* Settings: **Hey Even** on/off; **Continuous listening** (follow-ups without wake word, prior turns as context);
  **Speech Language**; **Search**: *Even Search* or *Perplexity*; **Reading speed**; **Delete history**;
  Settings > Privacy > **Use AI memory** (cross-session memory; consent-based, notice dated 25 Jul 2026);
  **Shortcut center** (list of voice commands); developer-only **Agent Configuration** (OpenAI-compatible endpoint
  URL + API key, e.g. a LAN OpenClaw at `http://IP:18789/v1/chat/completions`) — the one official BYO-LLM hook.
* Voice commands: start/end Translate ("translate from X to Y"), Conversate, Navigate ("navigate to Home by
  walking"), Teleprompt, Reminders ("remind me to ... at 2 pm"), QuickNote, Notifications, "Brightness set to 80",
  end any running feature (2.2.9). Command languages: EN, FR, DE, IT, JA, ZH, ES; chat languages: 28.
* Runs in the foreground while Navigate/Teleprompt pause in the background. Interrupt with "Hey Even" again.
* Backend: Even's own "Even LLM" plus third-party models; marketing names ChatGPT (G1 only) and Perplexity;
  one spec aggregator claims a Gemini backend [unverified]. Audio is transcribed by an unnamed cloud ASR provider.

### 2.2 Conversate (live meeting assistant) [official]
* Live transcription; **AI Cues** of four kinds: Concepts, Bios, Answers, Suggestions; **Real-time translation**
  (from = language or Auto-detected, to = target); **Prep Notes** (text <= 5,000 chars or up to 5 files PDF/DOCX/TXT
  <= 5 MB each; 100 notes per account; one per session); post-session **Summary** (Overview, Highlights, Details,
  Next Steps -> add all to Reminders), **Cue List** (search, sort by time/A-Z), **Transcription**; export PDF/DOCX/TXT;
  offline search of locally stored sessions.
* Settings: Voice input = Glasses Mic / Phone Mic (fixed per session); Glasses interface: AI Cues on/off, Live
  Transcription on/off, Auto pop-up, Cue duration. End by double tap; Pause/Resume/End in app. Requires internet.
* Languages: 34 transcription languages; translation from 36 / to 33.

### 2.3 Teleprompt [official]
* Scripts typed or imported (TXT/DOCX/PDF, <= 255 KB each); stored locally on the phone; last 20 shown on the glasses.
* Modes: **AI Mode** (speech-follow, read text turns grey; on-device iOS ASR offline for 13 languages, online 22;
  Android online 33), **Auto Mode** (fixed speed), **Manual Mode** (swipe, or phone volume keys / any Bluetooth
  volume remote with "Third-party remote control").
* Preview settings: display height **5–9 lines**, prompting-area width slider; **Countdown** none/5 s/10 s;
  **Auto-resume to last position**; unsupported glyphs highlighted in the app (fw >= 2.2.9).
* Controls: tap = pause/resume; double tap = exit prompt; swipe = reposition.

### 2.4 Translate [official]
* From/To language pair; two-way pairs marked with a bidirectional arrow (31 two-way languages); 36 input, 33 output.
* Glasses display modes (tap to switch): **Translation only** (half screen) / **Original + translation** (full
  screen); only the input->output direction shown on glasses; app shows both directions (All / ZH->EN / EN->ZH tabs).
* Glasses menu: Display off, Translate from, Brightness, End session. Voice input device Glasses/Phone.
* Records saved per session (sort, rename, export PDF/DOCX/TXT, delete). No session time limit.
* Pricing page still lists **Translate Pro** ($4.99/1 h, $23.99/5 h, $44.99/10 h) as a G1-era paid tier; G2
  Translate is free. Requires internet ("Even G2 does not support offline use").

### 2.5 Navigate [official]
* Walking and cycling only; destination via app search / categories / long-press on map; up to 5 waypoints;
  saved addresses (Home, Office...) appear in the glasses Menu; voice start via Even AI.
* On glasses: turn instruction, distance, current/next road, waypoints, total distance, ETA/arrival; tap toggles
  **Mini Map** (heading-up, rotates with the glasses' own compass) and **Full Map** (north-up, arrow rotates).
  Arrival message -> auto-end after ~4 s.
* Data: **Google Maps** map data via the phone; phone GPS + internet required; "Always" location lets map tiles
  keep loading with the phone locked. Launchable from the G2 menu with the phone locked since 2.2.6.
* Provider was "updated in certain regions" in 2.3.2 (regions/providers unnamed).

### 2.6 Notifications [official]
* Setup: grant notification access; per-app toggles (only apps that have already posted a notification appear).
* Settings: **Auto Display** (pop up immediately in the foreground), **Don't disturb other features** (suppress
  during full-screen features), **Display Time** (default ~10 s), **Apps** list.
* On glasses: tap = mark read + close; double tap = close unread; bell count on Dashboard; glasses buffer the
  **10 most recent** notifications; list under Menu > Notification. No replies/actions; dismissal does not sync
  back to the phone [review]. Data path is phone->glasses over BLE only, never Even's cloud.

### 2.7 QuickNote (2.3.2+) [official]
* Create in app (title + body) or on glasses: Menu > QuickNote > Create new note, **hold TouchPad while speaking**,
  release = one transcribed paragraph; double tap -> Save/Discard/Cancel; title auto-generated; "Undo last input".
* View on glasses via Menu or the QuickNote Dashboard widget; app: edit, pin, sort (created/updated/title), delete.
* Transcription (and title generation) is cloud ASR/LLM [official policy]; G1 notes were queued when offline.

### 2.8 Reminders (formerly QuickList) [official]
To-dos with notes, dates, times, order; created in app or by voice; glasses show first 20 items; tap = complete
(syncs back). Conversate "Next Steps" can bulk-add.

### 2.9 Calendar, Weather, News, Stocks, Health [official]
* **Calendar widget:** reads the phone's system calendar (iOS EventKit / Android provider), shows in-progress +
  today + tomorrow; event-type filter. Marketing also says "view, edit and sync events".
* **Weather:** status-block item (condition + temperature); provider is an unnamed "weather service provider"
  that receives the phone's location [official policy]; Celsius/Fahrenheit option (2.0.5); units setting (2.0.6).
* **News widget:** languages EN, DE, FR, ES, IT, ZH-Hans, JA, KO, RU; source and interest pickers; refresh every
  3 h; sets of 5 stories, swipe for more; story detail view. Aggregator unnamed [unverified].
* **Stocks widget:** watchlist by name/symbol; shows price, % change vs open, intraday chart; new exchanges added
  2.2.8. Data vendor unnamed [unverified].
* **Health widget (R1):** Productivity Score (85–100 Optimal, 70–84 Good, 55–69 Fair, 0–54 Pay Attention), steps +
  goal, calories + goal (Active Calories goal 2.2.5), last-night sleep, heart rate (last/avg), SpO2 (last/avg),
  Highlights page. R1 also reports HRV, skin temperature, respiratory rate, sleep stages; health export (2.2.9).

### 2.10 Even Hub (third-party plug-ins) [official]
Plug-in gallery inside the app (introduced 2.1.1, Discovery page redesigned 2.3.2). Plug-ins are web apps running
in a WebView in the phone app; SDK `@evenrealities/even_hub_sdk` (0.0.16). Glasses canvas 576x288 per eye, 16-level
green, <= 4 image + 8 other containers, images <= 288x144 4-bit, list <= 20 items x 64 chars, text <= 1,000/2,000 chars,
one LVGL font, 100 ms send pacing, LZ4 in transit. Device APIs: mic PCM 16 kHz S16LE mono (glasses or phone),
IMU x/y/z stream, phone GPS, device status (battery, isWearing, isCharging, isInCase), phone camera/album, local
storage, whitelisted `fetch`. PWA hosting outside the gallery is allowed.

### 2.11 Terminal Mode (2.2.0+) [official / review]
Status of AI coding agents (Claude Code, Codex 2.2.1) on the glasses, approve/reply via ring menu or voice;
screen capture (2.2.4); multi-session streaming (2.2.2); runs alongside other features (2.3.0).

### 2.12 Device management [official]
* **Firmware update:** My Devices > Even G2 > Glasses Info > Firmware Version > Update; glasses must be charging
  and > 50 %; keep app foregrounded; OTA split into six payloads (EVENOTA) [community].
* **Display adjustment:** home-screen icon -> **Display Distance** Near/Mid/Far, **Display Height** vertical slider.
* **Brightness:** manual slider or **Auto-Brightness** (ambient light sensor; 2.2.4); **Brightness Calibration**
  per-eye sliders under My Devices > Even G2; voice "Brightness set to N" (0–100).
* **Wear Detection** (My Devices toggle): display only while worn, tone on don; off-head taps/voice ignored;
  features keep running.
* **Silent Mode:** hold both TouchPads ~1 s or home-screen switch; no display, TouchPads disabled, Hey Even off.
* **Battery/case:** My Devices shows glasses, ring and case battery; case LED shows charge state; shipping mode on
  new units. Case-battery and charging-state BLE commands exist [community].
* **Find my glasses:** no such feature exists in the app or support center **[confirmed absent]**; only
  "double-tap to wake / 5-tap restart" troubleshooting.
* **Privacy settings** (2.2.7+): Use AI memory; Share usage data (+ Include audio recordings); File and Record
  Cloud Sync; delete account/data.

## 3. App settings that change glasses behaviour (names as shown)

| Location | Setting | Values |
|---|---|---|
| Home | Brightness | slider; Auto-Brightness toggle |
| Home | Display Adjustment | Distance Near / Mid / Far; Height slider |
| Home | Silent Mode | on/off |
| Dashboard | Widgets | add / remove / reorder; per-widget settings |
| Dashboard | HeadUp Display | on/off; angle 0–60°; Reset; Recalibrate 0° |
| Dashboard | Auto display off | durations or keep on (default 10 s) |
| Dashboard | Watch face / layout | several presets (names undocumented) |
| My Devices > Even G2 | Wear Detection | on/off |
| My Devices > Even G2 | Brightness Calibration | left / right sliders |
| My Devices > Even G2 | Glasses Info > Firmware Version | Update |
| My Devices > Even R1 | Hand preference, restart, firmware | — |
| Settings > Notifications | Notifications, Auto Display, Don't disturb other features, Display Time, Apps | toggles / duration |
| Settings > Gesture Customization | Long-press (Glasses L/R/None, Ring); Enable while display is off | action list in 1.2 |
| Settings > Even AI | Hey Even, Continuous listening, Speech Language, Search (Even Search / Perplexity), Reading speed, Delete history, Shortcut center, Agent Configuration (dev) | — |
| Settings > Privacy | Use AI memory; Share usage data; Include audio recordings; File and Record Cloud Sync | toggles |
| Settings | Units (C/F etc.), app language (9), Menu customisation | — |
| Conversate settings | Voice input; AI Cues; Live Transcription; Auto pop-up; Cue duration; language/translation | — |
| Teleprompt settings | Mode AI/Auto/Manual; speed; Countdown 0/5/10 s; Auto-resume; Third-party remote control; height 5–9 lines; width | — |
| Translate settings | Voice input Glasses/Phone; From/To | — |
| Navigate | Saved addresses; travel mode Walking/Cycling | — |

## 4. Where the data comes from (and what leaves the phone)

| Feature | Source / processor | Local? |
|---|---|---|
| Time, date, battery, unread count | phone clock + BLE status | yes |
| Weather | phone location -> unnamed "weather service provider" via Even cloud | no |
| Calendar | phone system calendar | yes |
| Notifications | phone notification listener -> BLE | yes (Even states never uploaded) |
| News | Even cloud aggregator, unnamed outlets | no |
| Stocks | Even cloud, unnamed market-data vendor | no |
| Health | R1 BLE -> app; synced to Even cloud (health-data notice) | partly |
| Even AI | glasses mic -> cloud ASR (policy: "ASR voice" / "iOS ASR voice service provider") -> Even LLM + Even Search or Perplexity; AI Memory in Even cloud; optional developer endpoint | no (except BYO endpoint) |
| Conversate / Translate | cloud ASR + "real-time translation service provider" + Even LLM; records optionally cloud-synced | no |
| Teleprompt AI mode | iOS on-device speech (offline, 13 langs) or online ASR; Android online only | partly |
| QuickNote | cloud ASR + LLM title | no |
| Navigate | phone GPS + Google Maps data (regional alternates) | no |
| Even Hub plug-ins | WebView on phone; network per plug-in whitelist | depends |
| Servers | Netherlands (policy); affiliates in HK, Shenzhen, Tokyo, Delaware may access | — |

Voice recordings are "deleted immediately following transcription" unless usage-data sharing with audio is on.
No official statement names Microsoft Azure, OpenAI, Deepgram or any specific ASR vendor **[unverified]**.
Even's G2 FAQ: "all functions of the Even G2 require a Bluetooth connection and an internet connection".

## 5. G2 hardware the app relies on

| Spec | Value |
|---|---|
| Display | binocular green micro-LED waveguides (Even HAO 2.0); panel 640x350 per Even spec page; app/SDK canvas **576x288** per eye; unofficial firmware reports 640x480 panel [community]; 16 grey levels (4-bit); 27.5 deg FoV; 60 Hz; 1,200 nits; 98 % passthrough; focus ~2 m |
| Audio | **4 microphones**, single 16 kHz PCM stream; **no speaker**; no camera |
| Input | capacitive TouchPad on each temple (PSoC 4000T); optional R1 ring; IMU + magnetometer (compass); ambient light sensor (OPT3001 [community]); wear/on-head detection |
| Compute | Ambiq Apollo510B Cortex-M55 per temple, EM9305 BLE controller, GX8002 voice codec, STM32G0 in case [community] |
| Radio | BLE 5.4 (PAwR), ~28 m range claimed; glasses sides linked by wired FPC |
| Power | 192 mAh / 0.744 Wh; "2 days regular use"; 1–1.5 h charge; case 2,000 mAh / 7.4 Wh ~ 7 recharges, pogo contacts, status LED, shipping mode |
| Body | 36 g; magnesium frame, titanium temples; IP65; Rx -12.00 to +12.00; sizes A/B; colours dark grey / olive / brown |
| R1 | nRF52840; touch surface; HR, HRV, SpO2, skin temp, respiration, sleep; ~4 days; IP68; $249 |

## 6. App changelog highlights (App Store history) [official]

| Version | Date | Highlights |
|---|---|---|
| 2.0.1–2.0.2 | Dec 2025 | two-way translation; health algorithm |
| 2.0.4–2.0.6 | Jan–Feb 2026 | 8 app languages; Translate 35 languages; C/F; menu customisation; unit settings; wear-detection fixes |
| 2.0.7–2.0.9 | Feb–Mar 2026 | AI Cues improved; News list + JA/ZH sources; Prep Notes; faster OTA |
| 2.1.1–2.1.3 | Apr 2026 | **Even Hub** plug-in gallery; long-press uninstall; case detection fixes |
| 2.2.0–2.2.2 | Apr–May 2026 | **Terminal Mode**; Conversate/Translate export; continuous conversation; Codex; PT-BR + Catalan; 11 Conversate languages |
| 2.2.4 | 8 Jun | **Dashboard customisation + new watch faces**; Terminal screen capture; R1 hand preference; R1 battery on Dashboard; **auto-brightness** |
| 2.2.5 | 22 Jun | real-time translation inside Conversate; Active Calories goal |
| 2.2.6 | 13 Jul | Teleprompt size before start; single-tap pause; Navigate from G2 menu with phone locked |
| 2.2.7 | 31 Jul | **Privacy settings**; Teleprompt AI-mode dynamic STT + noise reduction |
| 2.2.8 | 8 Aug | Korean app language; new news sources; new stock exchanges |
| 2.2.9 | 20 Aug | **Menu gesture -> tap then long-press**; features run with display off; end features via Even AI; health export |
| 2.2.10 | 2 Sep | duplicate-notification fix |
| 2.3.0 | 15 Sep | Terminal alongside other features; AI Cues search/sort; BLE pairing + firmware reliability |
| 2.3.1 | 21 Sep | iOS reconnection stability; feature launch from G2 |
| 2.3.2 | 2 Oct | **custom long-press shortcuts (G2 + R1)**; **QuickNote**; QuickList -> **Reminders**; Hub Discovery redesign; map-provider changes; needs new G2 + R1 firmware |
| 2.3.3 | ~21 Oct | lower background battery use; stability |

## 7. Parity checklist

| Feature | What Even's app does | Data source | Local replacement idea |
|---|---|---|---|
| Dashboard status | time, date, battery, weather, unread count, R1 battery | phone, Even weather API | render locally; weather from Open-Meteo or a cached local forecast |
| Widgets | News / Stocks / Calendar / Reminders / Health / QuickNote cards, swipe-tap-doubletap | mixed cloud | same card model; feeds via local RSS, local market-data cache, OS calendar |
| Watch faces / layout | preset layouts selectable in app | app | ship 2–3 layouts (time-only, time+widget, full) selectable in config |
| HeadUp wake | IMU angle 0–60°, calibrate 0°, 10 s auto-off | glasses firmware | firmware-side; expose angle + timeout settings |
| Menu | 9 entries, reorder/hide, tap-then-long-press | app config | local menu list pushed to glasses |
| Gesture customisation | long-press action per TouchPad / ring | app config | config map gesture -> action |
| Even AI | wake word, streaming answers, memory, Even Search / Perplexity, feature voice control | cloud ASR + LLM | local Whisper/Vosk + local LLM (Ollama) + SearXNG; keep an OpenAI-compatible endpoint like Agent Configuration |
| Conversate | transcript, AI Cues, Prep Notes, summary, export | cloud | local streaming ASR + local LLM cue generator + local RAG over Prep Notes |
| Teleprompt | import TXT/DOCX/PDF, AI/Auto/Manual, 5–9 lines | phone + ASR | local parsing; speech-follow via on-device ASR alignment |
| Translate | 36 -> 33 languages, half/full view, records, export | cloud ASR + MT | local ASR + offline MT (Argos/NLLB/Marian) |
| Navigate | walking/cycling, saved places, mini/full map, compass arrow | Google Maps + GPS | OSM tiles + GraphHopper/Valhalla offline; phone GPS + glasses magnetometer |
| Notifications | per-app filter, auto-display, display time, 10-deep buffer | phone listener | identical, already local |
| QuickNote | hold-to-dictate paragraphs, auto title, pin/sort | cloud ASR/LLM | local ASR; title from local LLM or first line |
| Reminders | to-dos with dates, voice creation, first 20 on glasses | app DB + cloud sync | local DB; voice intent via local LLM |
| Calendar | system calendar, today+tomorrow, filter | phone | identical |
| News | 9 languages, sources, interests, 3 h refresh | Even aggregator | RSS/Atom feeds fetched locally |
| Stocks | watchlist, price, % change, intraday chart | Even market feed | local fetch from a user-chosen free quote API or none |
| Health | R1 metrics + score | R1 BLE + cloud | out of scope without ring; read R1 BLE locally if desired |
| Even Hub | WebView plug-ins, 576x288 container API | Even cloud distribution | host PWAs locally; reimplement the bridge API surface |
| Terminal Mode | agent status + approvals | app <-> agent | local websocket from coding agent |
| Firmware update | OTA via app, > 50 % + charging | Even CDN | out of scope / archive official images |
| Display adjust | Near/Mid/Far, height, brightness, auto-brightness, per-eye calibration | glasses commands | expose same BLE commands in config UI |
| Wear detection / Silent mode | toggles | glasses firmware | pass-through toggles |
| Battery / case | glasses, ring, case % | BLE | identical |
| Find my glasses | not offered | — | optional: last-seen location + "flash display" command |

## Sources
* App Store listing & version history: https://apps.apple.com/us/app/even-realities/id6747017725
* Google Play listing (com.even.sg): https://play.google.com/store/apps/details?id=com.even.sg
* Support — Dashboard: https://support.evenrealities.com/hc/en-us/articles/14269247458319-Dashboard
* Support — Menu: https://support.evenrealities.com/hc/en-us/articles/14269160297999-Menu
* Support — How to Control: https://support.evenrealities.com/hc/en-us/articles/13754911116047-How-to-Control
* Support — Gesture Customization: https://support.evenrealities.com/hc/en-us/articles/17948162341391-Gesture-Customization
* Support — Even AI: https://support.evenrealities.com/hc/en-us/articles/14274515708559-Even-AI
* Support — Conversate: https://support.evenrealities.com/hc/en-us/articles/14273795154319-Conversate
* Support — Teleprompt: https://support.evenrealities.com/hc/en-us/articles/14273863878415-Teleprompt
* Support — Translate: https://support.evenrealities.com/hc/en-us/articles/14273831059983-Translate
* Support — Navigate: https://support.evenrealities.com/hc/en-us/articles/14273871101071-Navigate
* Support — Notifications: https://support.evenrealities.com/hc/en-us/articles/14274501482639-Notification
* Support — QuickNote: https://support.evenrealities.com/hc/en-us/articles/17948999033999-QuickNote
* Support — Language Support: https://support.evenrealities.com/hc/en-us/articles/13755354799247-Language-Support
* Support — Display Adjustment: https://support.evenrealities.com/hc/en-us/articles/13755064994831-Display-Adjustment
* Support — Wear Detection: https://support.evenrealities.com/hc/en-us/articles/17451244132879-Wear-Detection
* Support — Silent Mode: https://support.evenrealities.com/hc/en-us/articles/17451199636495
* Support — Update Even G2 Firmware: https://support.evenrealities.com/hc/en-us/articles/13499311356943-Version-Update
* Support — Manage G2 and R1: https://support.evenrealities.com/hc/en-us/articles/13744433342863
* Support — Battery & Charging: https://support.evenrealities.com/hc/en-us/articles/13499279151375-Battery-Charging
* Support — Specifications: https://support.evenrealities.com/hc/en-us/articles/13499229138959-Specifications
* Support — What's Included: https://support.evenrealities.com/hc/en-us/articles/13754865887375-What-s-Included
* Support — G2 General Q&A: https://support.evenrealities.com/hc/en-us/articles/14601104557839-G2-General-Q-A
* Support — Features & pricing / Translate Pro: https://support.evenrealities.com/hc/en-us/articles/14301353212815 , https://support.evenrealities.com/hc/en-us/articles/14301378759695
* Support — OpenClaw / Agent Configuration tutorial: https://support.evenrealities.com/hc/en-us/articles/16280926743695
* Support — Privacy Policy (Device & APP): https://support.evenrealities.com/hc/en-us/articles/14270525749519
* Support — Data Storage and Cross-Border Transfers: https://support.evenrealities.com/hc/en-us/articles/15685480925583
* Support — Privacy Notice for AI Memory: https://support.evenrealities.com/hc/en-us/articles/16944220979471
* Support — Privacy Notice for User Experience Improvement: https://support.evenrealities.com/hc/en-us/articles/16944206181647
* Even — smart-glasses product page (specs): https://www.evenrealities.com/smart-glasses
* Even — AI glasses page (Even LLM, ChatGPT G1-only footnote, Perplexity): https://www.evenrealities.com/ai-glasses
* Even — HUD glasses page: https://www.evenrealities.com/hud-glasses
* Even — "How we rebuilt G2" engineering blog: https://www.evenrealities.com/en-CA/blog/how-we-rebuilt-g2-from-the-inside-out
* Even Hub docs — overview / architecture / display / device APIs / glossary: https://hub.evenrealities.com/docs/get-started/overview , https://hub.evenrealities.com/docs/get-started/architecture , https://hub.evenrealities.com/docs/build/display , https://hub.evenrealities.com/docs/build/device-apis , https://hub.evenrealities.com/docs/reference/glossary
* npm @evenrealities/even_hub_sdk: https://www.npmjs.com/package/@evenrealities/even_hub_sdk
* Mixed News — app 2.3.2 firmware requirement: https://mixed-news.com/en/even-realities-app-2-3-2-firmware-g2-glasses-r1-ring/
* Mixed News — 640x350 vs 576x288 vs 640x480: https://mixed-news.com/en/even-g2-support-page-640x350-developer-glossary-576x288/ , https://mixed-news.com/en/even-g2-panel-576x288-glossary-640x480-firmware-patch-set/
* Mixed News — official firmware archive (16 G2 / 12 R1 builds): https://mixed-news.com/en/even-realities-webflasher-official-g2-r1-firmware-no-custom-builds/
* openCFW (Apollo510B, EVENOTA s200_v2.2.6.10): https://github.com/kalanihelekunihi/evenRealities-openCFW
* g2flash custom firmware: https://github.com/jimrandomh/g2flash
* nickustinov/even-g2-notes (SDK event model): https://github.com/nickustinov/even-g2-notes
* Suzu Labs privacy analysis: https://suzulabs.com/suzu-labs-blog/internal-analysis-even-realities-g2-smart-glasses-security-privacy-investigation
* Reviews: Android Police https://www.androidpolice.com/even-realities-even-g2-review/ ; Wareable https://www.wareable.com/wearable-tech/even-realities-g2-r1-review ; 9to5Google https://9to5google.com/review-even-realities-g2-smart-glasses/ ; Gizmodo https://gizmodo.com/even-realities-even-g2-review-2000687632 ; Engadget https://www.engadget.com/2288658/even-realities-g2-smart-glasses-review/ ; VR/AR Wiki https://vrarwiki.com/wiki/Even_Realities_G2 ; 9to5Google Even Hub launch https://9to5google.com/2026/03/26/even-realities-even-hub-apps-and-better-conversate-mode/ ; Engadget Terminal Mode https://www.engadget.com/2161082/even-realities-terminal-mode-ai-agent/
