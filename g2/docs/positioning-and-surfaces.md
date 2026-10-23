# Even G2: display positioning, the EvenHub canvas, surfaces and gestures

Research notes, 2026-10-23. Sources are vendor trees under `g2/vendor/` (file:line) and Even's
public docs. "Known" = stated by a source or encoded in a protobuf; "inferred" = my reading.

Vendor roots (abbreviated below):
- `KIT` = `vendor/g2-kit-unofficial/ble`
- `GW`  = `vendor/men-g2-ble-gateway`
- `HUB` = `vendor/open-g2-hub`
- `EGP` = `vendor/even-g2-protocol`

---

## 1. Device settings protocol (SID 0x09, `G2SettingPackage`)

Canonical shape is the generated protobuf `KIT/gen/g2_setting_pb.ts` (package `g2.g2_setting`).
One envelope carries every read, write, push and ack:

| Field | # | Type | Meaning | Source |
|---|---|---|---|---|
| `commandId` | 1 | enum | 0 NONE, **1 DeviceReceiveInfo = SET**, **2 DeviceReceiveRequest = READ** (also device push), 3 DeviceSendToAPP (device event), 4 DeviceRespondToAPP (ack), 5 AppRespondToDevice | `g2_setting_pb.ts:675-705`; names SET/READ/DEVICE_EVENT/DEVICE_ACK/APP_ACK in `HUB/lib/protocol/settings.dart:14-15` |
| `magicRandom` | 2 | uint32 | request id echoed in the ack (keep < 256, `KIT/docs/gotchas.md:13-16`) | `g2_setting_pb.ts:553` |
| `deviceReceiveInfoFromApp` | 3 | msg | **SET payload** (one leaf per message) | `:556-558` |
| `deviceReceiveRequestFromApp` | 4 | msg | **READ request** (`settingInfoType`) and the **readback snapshot** | `:561-563` |
| `deviceSendInfoToApp` | 5 | msg | device push: `1 currentRecalibrationStatus`, `2 silentModeSwitch` | `:175-187` |
| `deviceRespondToApp` / `appRespondToDevice` | 6 / 7 | msg | `{1 packageId, 2 receiveFlag}` 0 success, 1 parameter error | `:33-74, 589-628` |

### SET leaves (`deviceReceiveInfoFromApp`, cmd 1, envelope field 3)

| Leaf (field #) | Sub-fields (# : type) | Allowed range (as validated by ports) | Notes / source |
|---|---|---|---|
| brightness (1) | 1 autoAdjust, 2 brightnessLevel, 3 leftCalibration, 4 rightCalibration | level **1..50** (`settings.dart:36,119-120`); calibration 0..255 (`:124-127`); auto 0/1 | `g2_setting_pb.ts:510-532`; `GW/mentraos/g2/protocol/g2_setting.py:7-21`; KIT docs say 0-100 (`KIT/docs/settings.md:40`) -- treat 1..50 as the app slider, unverified beyond |
| **Y coordinate (2)** | 1 yCoordinateLevel | **0..20** (`settings.dart:38,140-141`) | = app "Display Height". `g2_setting_pb.ts:493-500`; GW names it `set_screen_height` (`g2_setting.py:96-109`); HUB names it `display_height` (`settings.dart:23`) |
| **X coordinate (3)** | 1 xCoordinateLevel | **0..20** (`settings.dart:143-144`) | = app "Display Distance". `g2_setting_pb.ts:476-483`; GW `set_screen_depth` (`g2_setting.py:112-125`); HUB `display_distance` (`settings.dart:24`) |
| head-up (4) | 1 headUpSwitch, 2 headUpAngle, 3 headUpCalibrationSwitch, 4 headUpCalibration | angle **0..60** degrees (`settings.dart:37,134-138`; Even support: "set the angle from 0 to 60") | wake trigger, not a position. `g2_setting_pb.ts:444-466`; `g2_setting.py:64-93` |
| wear detection (5) | 1 wearDetectionSwitch | 0/1 | `g2_setting_pb.ts:427-434`; `settings.dart:132` |
| silent mode (6) | 1 silentModeSwitch | 0/1 | `g2_setting_pb.ts:410-417`; `settings.dart:130` |
| app page (7) | 1 appPage | unknown | `g2_setting_pb.ts:393-400` |
| advanced (8) | 1 killAllFeature | unknown | `:376-383` (HUB label `kill_all_features`) |
| universe/units (9) | 1 unitFormat, 2 distanceUnit, 3 timeFormat, 4 dateFormat, 5 temperatureUnit | each 0..15 | official app sends on connect; GW defaults 0,0,**1**,0,**1** (`g2_setting.py:37-61`) |
| gesture control list (10) | repeated {1 screenOn, 2 operationType, 3 apptype(0 app_unable,1 app_menu)} | operationType 0..15 | official app sends `[[0,0,0],[0,1,0],[0,2,0]]` on every connect (`settings.dart:41-46`; `HUB/lib/protocol/device.dart:200`). Semantics of operationType not documented |
| dominant hand (11) | 1 dominantHand, 2 ringMac(6 bytes) | 0/1 | `settings.dart:182-189` |
| app control device (12) | 1 turnOnDevice | 0/1 | `g2_setting_pb.ts:16-23` |

Wire for a SET: `{1: 1, 2: magic, 3: {leaf#: {sub#: value}}}` -- `KIT/settings.ts:113-126`,
`settings.dart:104-108`, `g2_setting.py` (any builder). Ack echoes current value.

### READ (query) and the readback snapshot

Send `{1: 2, 2: magic, 4: {1: settingInfoType}}` with `settingInfoType` 0 = BRIGHTNESS_INFO,
1 = BASIC_SETTING (`g2_setting_pb.ts:652-664`; `KIT/settings.ts:89-104`; `settings.dart:113-116`;
`g2_setting.py:24-34`). The ack's field 4 is the snapshot:

| # | Field | # | Field |
|---|---|---|---|
| 1 | settingInfoType | 11 | deviceRunningStatus |
| 2 | autoBrightnessLevel | 12 | battery (%) |
| **3** | **yCoordinateLevelRestored** (display height) | 13 | chargingStatus |
| **4** | **xCoordinateLevelRestored** (display distance) | 14 | silentModeSwitchRestored |
| 5 | leftSoftwareVersion (string) | 15 | leftCalibrationRestored |
| 6 | rightSoftwareVersion (string) | 16 | rightCalibrationRestored |
| 7 | headUpSwitchRestored | 17 | headUpRecalibrationSuccess |
| 8 | headUpAngleRestored | 18 | autoBrightnessSwitchRestored |
| 9 | headUpAngleCalibrationRestored | 19 | unreadMessageCount |
| 10 | wearDetectionSwitchRestored | | |

(`g2_setting_pb.ts:197-294`; `settings.dart:48-68`; decoder `settings.dart:217-228`.) The device also
pushes the same message unsolicited (no field 1) -- `settings.dart:298`.

### Related settings on other SIDs

- **SID 0x20 module configure**: display auto-off `cmd 2 {4: {1: seconds}}`, choices 3/5/10/15/30, never = 65365; read with cmd 1; UI language `cmd 0 {3: {1: index}}` 0..8 (`settings.dart:71-87,199-215`). Even support: dashboard turns off after **10 s** by default.
- **SID 0x80 dev config, cmd 12 GET_DEVICE_INFO**: items ALL_INFO 0, DEVICE_BRIGHTNESS 1 (ALSInfo), ANTI_SHAKE 2, DISPLAY_MODE 3, WORK_MODE 4, **DEVICE_WAKEUP_ANGLE 5 = HeadAngle{1 angle, 2 offset}**, SNs 6-7, BLE_MAC 8 (`KIT/gen/dev_infomation_pb.ts:42-54,140-261`; `device.dart:62-72,226-231`). Read-only use advised: an unknown 0x80 write soft-bricked a pair (`KIT/docs/gotchas.md:136-140`).
- Firmware defaults are **not** recoverable from any source here; read them with the query above.

---

## 2. Which settings move content, and does an EvenHub page follow them?

**Known**
- Only two settings are positional: `yCoordinateLevel` (field 2, "Display Height", app slider) and
  `xCoordinateLevel` (field 3, "Display Distance", app presets Near/Mid/Far). Both are device-side,
  per pair, 0..20, and read back in snapshot fields 3/4.
- Even support ("Display Brightness Adjustment" article, which actually documents Display Adjustment):
  "All content on the display, including foreground information and background interfaces, moves
  together as a unified whole." Distance presets: foreground layer about 1 m / 2 m / 3 m, background
  layer about 2 m / 3 m / 4 m; IPD can shift the perceived distance by 0.5-1.0 m.
- `KIT/docs/settings.md:70-76`: the offset is "applied to every rendered frame", per arm, small range.
- The EvenHub container protocol carries no calibration field; x/y/w/h are absolute canvas pixels
  (`GW/mentraos/g2/protocol/even_hub.py:131-149`, `HUB/lib/protocol/evenhub_pages.dart:38-41`).
- Head-up angle (0-60 degrees) only decides when the display wakes; it does not move the image.

**Inferred**
- The firmware composites all surfaces (native dashboard, first-party apps, EvenHub pages) onto one
  576x288 frame and then applies the X/Y offset, so **an EvenHub page gets exactly the same
  calibration as the native dashboard**. There is nothing for a host to set per page. "Distance" is
  almost certainly a per-eye horizontal shift (vergence), which is why it lives on the X axis.
- Therefore, if our screen looks "in the wrong place" next to Even's, it is the **layout inside the
  canvas** (we start text at 0,0 with 4 px padding, `GW/mentraos/g2/client.py:1324-1336`), not a
  calibration mismatch -- unless the pair's height/distance was changed from the phone while bonded
  there; the values persist on the glasses and can be read/set from our side (section 1).
- The panel is quoted as 640x350 by Even's support site while developers get 576x288 (mixed-news,
  vrarwiki). Whether the canvas is centred or offset inside the panel is **unknown**.

---

## 3. The EvenHub canvas

**Coordinate system (known)**: 576x288 px per eye, origin top-left, X right, Y down, 4-bit grey
rendered as 16 green levels, unlit = transparent (Even docs *Display & UI System*; `GW/constants.py:24-25`;
`evenhub_pages.dart:23`). "The whole canvas is renderable" -- no documented safe area.

**Container rules (known)**

| Property | Range | Source |
|---|---|---|
| xPosition / yPosition | 0-576 / 0-288 | Even *Display* |
| width / height | 0-576 / 0-288 | Even *Display* |
| borderWidth / borderColor / borderRadius / paddingLength | 0-5 / 0-15 / 0-10 / 0-32 (text+list only) | Even *Display* |
| text content | 1000 B create/rebuild, 2000 B textContainerUpgrade | Even *Display*; `HUB/limits.dart:11-12`; `even_hub.py:111-112,220-221` |
| textColor | 0-4, default 4 (0 may be invisible) | Even *Display*; `evenhub_pages.dart:171-176` |
| list | <= 20 items x 64 chars, firmware highlight, no in-place update | Even *Display*; `limits.dart:13-14` |
| image | 20..288 x 20..144 per container, 4-bit BMP | `even_hub.py:77-80`; `evenhub_pages.dart:273` |
| per page | <= 12 containers, <= 8 text/list, <= 4 image, **exactly one isEventCapture** | `even_hub.py:89-95,127-128`; Even *Display* |
| containerName | 16 chars (Even), 15 B (HUB), **14 safe** (`KIT/docs/containers.md:12`) | |
| zOrder | all-or-nothing, unique; else later declaration draws on top | Even *Display* |

**Text metrics**: one fixed proportional LVGL font, no size/weight/alignment, left+top aligned,
wraps at container width, `\n` honoured; a full-screen container holds "roughly 400-500 characters"
(Even). Empirical grid **50 columns x 10 rows** on a full-screen container (`KIT/docs/text.md:11-17`),
which gives (inferred) a line pitch of about **28-29 px** and about **11.5 px** per average glyph.
Our launcher already budgets 48 cols x 10 rows (`g2/launcher.py:41-43`). Even's design page offers
patterns rather than zones: `>` prefix as cursor, toggle `borderWidth` for selection, "stack multiple
text containers vertically (e.g., 3 containers at 96px height)", progress bars with `━ ─`, navigation
glyphs `▲▼`, icons at 24x24, pre-paginate at 400-500 chars.

**What Even's native dashboard looks like (known, from Even support "Dashboard")**: a *status area*
holding time, date and weekday, glasses battery, weather/temperature and unread-message count, with the
*widget area* (cards: News, Stocks, Calendar, Reminders, Health) **to the right of it**; swipe moves
between cards, single tap expands a card, double tap collapses it. Pixel positions are not published.

**Recommended home layout (inferred; mimics status-left / content-right)** -- one page, five
containers, list captures input:

| id | name | kind | x | y | w | h | content |
|---|---|---|---|---|---|---|---|
| 1 | `status` | text | 0 | 0 | 208 | 96 | `12:34`\n`Thu 23 Oct` (textColor 4) |
| 2 | `battery` | text | 0 | 104 | 208 | 60 | `▮▮▮▯ 72%` or `charging` (textColor 3) |
| 3 | `menu` | list, **isEventCapture 1**, borderWidth 1, radius 4, padding 8 | 224 | 0 | 352 | 232 | Assistant / Transcribe / Notes / Status (<= 20 rows, firmware highlights) |
| 4 | `hint` | text | 0 | 248 | 576 | 40 | `swipe = move   tap = open   double tap = exit` (textColor 1) |
| 5 | (optional) `note` | text | 0 | 172 | 208 | 68 | weather / next event |

Keep text 8-16 px from the canvas edge (use `paddingLength`), keep one line of air above the hint
row, and keep everything inside y < 288 so the bottom glyph row is never clipped. A full-screen
text fallback (what we send today) should become `x=16,y=12,w=544,h=264,padding=0` with the first
line as a header (title left, status right) and the last line as a dim hint -- that alone removes the
"stuck in the corner" look.

---

## 4. Surfaces and gestures

### Event codes (firmware OS events, SID 0xE0 cmd 2 field 13)

`OsEventTypeList`: CLICK 0, SCROLL_TOP 1 (swipe up), SCROLL_BOTTOM 2 (swipe down), DOUBLE_CLICK 3,
FOREGROUND_ENTER 4, FOREGROUND_EXIT 5, ABNORMAL_EXIT 6, SYSTEM_EXIT 7, IMU_DATA_REPORT 8, LONG_PRESS 9,
LONG_PRESS_RELEASE 10 (`KIT/gen/EvenHub_pb.ts:1181-1223`; `HUB/lib/protocol/evenhub.dart:27-43`;
Even *Device APIs*). `EventSourceType`: 0 none, 1 glasses R, 2 ring, 3 glasses L (`EvenHub_pb.ts:1034-1046`).
Sys event = `{1 EventType, 2 EventSource, 3 IMUData, 4 systemExitReasonCode}` (`:892-907`); text event
`{1 ContainerID, 2 ContainerName, 3 EventType}` (`:951-961`); list event adds `4 CurrentSelectItemIndex,
5 EventType` (`:993-998`). A separate `DevSysEvent` (field 17) reports SYSTEM_ENTER_TIMEOUT_PAGE_EVENT 0 /
SYSTEM_TIMEOUT_PAGE_EXIT_EVENT 1 -- the auto-off page (`:257-264,1013-1020`).

### Gesture matrix

| Gesture | (a) Native dashboard (Even support "How to Control", "Dashboard") | (b) Inside an EvenHub app (Even *Device APIs*, *Contextual Menu*, *App Submission*) | (c) Native full-screen app (Navigate/Teleprompt/Translate; Even support) |
|---|---|---|---|
| single tap | display off: (nothing listed); on: confirm in a list, expand card / toggle full-compact | **delivered** as CLICK 0 on the capturing container | confirm / next |
| double tap | display off: open Dashboard; on: **Back** -- collapse card, leave detail view, or turn display off if it was woken by touch/voice | **delivered** as DOUBLE_CLICK 3; the *app* must answer with `shutDownPageContainer(1)` on its root page (system exit dialog); mode 0 = immediate exit, internal pages only | opens the exit-confirmation dialog |
| swipe up / down | switch widget cards, scroll lists | **delivered** as SCROLL_TOP 1 / SCROLL_BOTTOM 2 | scroll |
| tap then hold | **consumed**: contextual Menu | **consumed**: OS raises the contextual menu over the page; app gets FOREGROUND_ENTER 4, then `menuItemClickEvent` (cmd 21 field 24) or FOREGROUND_EXIT 5 on dismiss; system slots "Display off", brightness, "Close <app>" | menu |
| long press (no prior tap) | not documented | LONG_PRESS 9 / LONG_PRESS_RELEASE 10 on sysEvent (SDK 0.0.14+, Even app 2.2.9+); "an enhancement, not a guaranteed path" | -- |
| hold both temples 1 s | toggle **silent mode** | same (system) | same |
| 5 quick taps both sides | restart glasses | same (system) | same |
| head up to angle | turns display on (0-60 degrees, recalibrate 0 in app); lowering head turns it off only if head-up woke it | same wake path; no head event reaches the app (only IMU stream if enabled) | same |
| display timeout | off after 10 s default (app: Auto display off) | firmware timeout page event (above) | same |

Known limits of what reaches us: text-container taps carry no container name on some paths
(`KIT/docs/text.md:78-97`), `capture_events` must be set (`:65-69`), and only the right arm emits
events (`KIT/docs/events.md:80-85`).

### How the gateway models it

- Surfaces: `DISPLAY_SURFACE_UNKNOWN / APP / DASHBOARD`. `_mark_dashboard_surface()` on
  FOREGROUND_EXIT, SYSTEM_EXIT, ABNORMAL_EXIT; `_mark_app_surface()` on FOREGROUND_ENTER
  (`GW/mentraos/g2/client.py:185-211`). Any later display call rebuilds the page first if the surface
  is DASHBOARD (`:199-206`, used at `:260,1008,1048,1316`). Note the gateway treats FOREGROUND_EXIT as
  "dashboard visible", whereas Even documents it as "contextual menu dismissed, app not torn down" --
  harmless (one extra rebuild) but it is why a menu dismiss flickers.
- `system_exit` (events 6/7): sets `page.system_exit`, flips surface; with
  `ENABLE_SYSTEM_EXIT_RUNTIME_RESET = False` (default, `constants.py:45-48`) nothing else happens and
  cached content survives; with True the cache is dropped (`client.py:1917-1921`).
- `page_shutdown`: set when an ack carries error code 9 (`APP_REQUEST_UPGRADE_TEXT_DATA_FAILED`,
  `EvenHub_pb.ts` error list) or when cmd 9/10 arrives (`events.py:155-170`). Handling: reset page
  flags, emit `system.reinitialize{reason:"page_shutdown"}`, **immediately** re-create the startup page
  and redisplay the last request (`client.py:1914-1931,1984-1996`).
- Relaunch from the glasses: the gateway registers a menu entry `men-g2-ble-gateway.launch`
  (SID 0x03, `client.py:42,1549-1564`); when the user picks it the glasses send cmd 17 (field 20 app id),
  the gateway emits `glasses.dashboard{action:"selected"}` and rebuilds (`:1906-1911,593-610`).
- Gesture names reaching our launcher: single_tap, double_tap, swipe_up, swipe_down, foreground_enter,
  foreground_exit, abnormal_exit, system_exit (`events.py:55-67`). **LONG_PRESS 9/10 and
  menuItemClickEvent are dropped** (`constants.py:93-103` has no 9/10; `events.py:122-137` parses only
  cmd 2 and cmd 17).

### Why our double tap "disappears" (inferred)

Officially the double tap *is* delivered and the app is expected to call shutdown(1). Our page is not an
SDK app (no app id/session, no `menuObject`), and what we observe is a page teardown (cmd 9/10 or
code 9) with no DOUBLE_CLICK first. The most consistent reading: for a page created outside the Even
app the firmware applies its native "double tap = Back/exit" rule itself, tears the page down and
returns to the dashboard; the gateway then re-creates the page at once, so the user never sees the
dashboard and we never see the gesture. `constants.py:45-48` notes some firmware also emits
SYSTEM_EXIT on ordinary recovery, which is why the reset on 6/7 is disabled.

### What the official app does after exit, and what a host should do

Even's contract (*App Submission*): after the user confirms exit on the glasses, the page is gone, the
phone WebView closes, "glasses can launch other apps ... without restart", and the app is launched
again only by the user (glasses menu -> cmd 17, or from the phone). open-g2-hub follows this: on
FOREGROUND_EXIT/ABNORMAL_EXIT/SYSTEM_EXIT or a shutdown ack it **stops the app and does not recreate the
page** (`HUB/hub/evenhub_runtime.py:929,1247-1264,1666-1668`). Nobody auto-recreates. So after
`page_shutdown` a host should: mark itself parked, stop the mic, **not** redraw, and wait for
`glasses.dashboard` (menu relaunch) or a host-side trigger; the gateway's immediate
`_restore_runtime_after_page_reset` is the behaviour to change (it fights the firmware's exit).

---

## 5. Recommendation

1. **Position**: read the snapshot once at connect (SID 0x09 cmd 2, type 1) and log fields 3/4
   (height/distance) plus 8 (head-up angle). Do not change them unless the user asks; they are the
   same values Even's app sets and they apply to our page too. If the user wants Even's exact feel,
   set them from the phone app once -- they persist on the glasses -- or expose `set_screen_height` /
   `set_screen_depth` (`GW/.../g2_setting.py:96-125`) as a one-time calibration screen (slider 0..20).
2. **Look**: stop sending one 0,0,576x288 block. Use the layout API (`/api/display` elements,
   `GW/API.md:292-339`) with the five-box home above (status left, list right, dim hint bottom,
   12-16 px inset). Use a **list container** for the menu so the firmware draws the highlight and
   swipe/tap arrive with the row index; use `textColor` 4/3/1 for title/body/hint. For text screens
   keep 48 cols x 8 body rows, header row + hint row, inset 16 px.
3. **Exit**: accept Even's model -- double tap leaves our app and shows the native dashboard. In
   `launcher.on_reinit("page_shutdown")` do **not** call `home()`; set `screen="parked"`, stop the
   mic, and redraw only on `glasses.dashboard` (our menu entry, already registered by the gateway) or
   `system.reinitialize{reason:"dashboard_launch"}`. Patch the gateway so `page_shutdown` does not
   auto-restore (guard `_restore_runtime_after_page_reset` with a config flag).
4. **Back inside our app**: single tap on a first list row `< Back` / `< Home` (Even's own `>`
   cursor idiom), with swipe to reach it; this is the only path guaranteed on every firmware.
   Enhancement: map LONG_PRESS_RELEASE 10 to "back" -- add 9/10 to `OsEventType` and
   `map_event_type_to_gesture` (`GW/constants.py:93-103`, `events.py:55-67`); pure long press is
   delivered on firmware >= Even app 2.2.9, tap-then-hold is not (menu). Later: send a `menuObject`
   with a "Home" item (create page field 6, `evenhub_pages.dart:102-110`) and parse cmd 21 field 24.
5. **Verify** on hardware: (a) snapshot values before/after moving the phone slider, (b) that a
   page at y=12 and the dashboard's clock sit at the same height, (c) whether DOUBLE_CLICK 3 ever
   arrives on the text container when the page carries a `menuObject`/session id.

### Sources
- Even Hub docs: Display & UI System, UI/UX Design Guidelines, Device APIs, Page Lifecycle,
  Contextual Menu, App Submission (ship), Glossary -- hub.evenrealities.com/docs/...
- Even support: How to Control (13754911116047), Dashboard (14269247458319), Display Brightness
  Adjustment (13755064994831), User guide (14301335051023, not fetchable; summarised via search).
- vrarwiki.com/wiki/Even_Realities_G2 (640x350 panel, 27.5 degree FOV, ~2 m focus); mixed-news on the
  640x350 vs 576x288 discrepancy; dev.to/yokomachi (SDK gesture set).
- Local: files and lines cited inline.
