# Phone link (Even G2): notifications, GPS and display settings from the Android companion

Two modes, chosen in the app (**Hub: Laptop | Phone**). This page first describes **Laptop**
mode, then [Phone hub mode](#phone-hub-mode-no-laptop), where the phone holds the glasses itself.

In Laptop mode the laptop holds the glasses' Bluetooth link (gateway on 127.0.0.1:8765), and the
phone never connects to the glasses. It forwards its notifications and GPS fixes to the laptop and mirrors
the glasses' display settings. Everything stays local: no cloud, no Even app.

```
phone (android/, "G2 laptop link")  --USB: adb reverse tcp:8768 tcp:8768-->  g2/phone_link.py (in g2/run.py)
   notification listener, GPS, Display card                                  |-- hub.glasses.notify(...)          -> native notification card
                                                                             |-- PhoneLink.location / on_location -> navigation
                                                                             |-- read_settings / apply_setting   <-> Display card
```

## Run

1. Laptop: `python g2/run.py` starts the phone link with the hub on `ws://127.0.0.1:8768/phone`
   (`--no-phone` turns it off). Config: the `phone:` section of `g2/config/hub.yaml` (see
   `g2/config.example.yaml`). Status: `curl http://127.0.0.1:8768/phone/status`.
2. USB: `adb reverse tcp:8768 tcp:8768`. Run it again after every replug or adb restart, because
   the mapping does not survive either.
3. Phone: open the app, go to **G2 laptop link**, tap **Connect** (allow location and notifications),
   then tap **Notification access** and switch the app on. Location must be on in Android's quick settings.

Standalone without the launcher: `python g2/phone_link.py [--port N] [--no-push]`.

## Protocol (JSON over WebSocket)

| direction | message |
|---|---|
| phone -> laptop | `{"t":"hello","device":".."}` |
| | `{"t":"notif","app":pkg,"app_name":..,"title":..,"text":..,"time":ms,"key":..,"ongoing":b,"silent":b}` |
| | `{"t":"notif_removed","key":..}` |
| | `{"t":"loc","lat":..,"lon":..,"acc":..,"speed":..,"bearing":..,"time":ms}` |
| | `{"t":"set","key":"height"/"distance"/"brightness"/"auto","value":n}` |
| laptop -> phone | `{"t":"ack","for":t}`, `{"t":"error","msg":..}` (the socket stays open) |
| | `{"t":"config","notif_allow":[pkgs],"loc_interval_s":n}` after hello |
| | `{"t":"display","height","distance","brightness","auto","battery","charging"}` on connect, every 10 s, after each change |

## Behaviour

- **Notifications.** The phone skips itself, `com.even.sg`, ongoing notifications and group
  summaries. The laptop drops denied packages (and packages not on the allow list when one is
  set), ongoing, silent and empty notifications. It also drops a repeat with the same key and
  text within 10 s. At most one card goes out every 3 s: a same-key update replaces the queued
  card, and several queued cards become one "N notifications" card. Cards are pushed under
  `hub.lock` with `enable_notifications()` once per phone connection and again after the glasses
  reconnect. Every accepted notification is appended to `g2/logs/notifications.jsonl`
  (gitignored), for a later Notifications screen.
- **GPS.** The phone uses LocationManager (GPS + network, no Play Services), sending one fix
  every 5 s while the foreground service runs. The laptop keeps `PhoneLink.location` (a
  `nav.position.Fix`) and a ring of 512 fixes, and calls `on_location(cb)` callbacks.
  `PhoneLink.latest()` makes it a nav position source. Apps reach it as `ctx.phone`; the hub
  reaches it as `hub.phone`.
- **Display settings.** Values are read with `read_settings()` (absent proto3 fields count as 0).
  Phone changes are checked first: height and distance 0..20, brightness 1..50, auto 0/1. They are
  applied at most every 300 ms with the last value winning, then re-read and sent to every phone.
  Changes made from `g2/position.py` or `settings_cli.py` reach the phone through the 10 s poll.
  The phone's Near / Mid / Far buttons set distance 4 / 10 / 16; the fine slider covers the rest.

Card delivery (`Glasses.notify`, hub form) is DECODED/UNTESTED on hardware (native-protocol.md section 6).
Tests: `python -m unittest g2.test_phone_link`.

## Phone hub mode (no laptop)

The app's **Hub: Laptop | Phone** switch picks the host. In **Phone** mode the phone holds both
arms itself through `android/g2core` and the laptop is not involved at all. Stop `g2/run.py`
(the gateway) first, because only one central can hold the glasses. If they are not advertising,
the app says they are probably connected to something else.

```
phone (android/, Hub: Phone)  --BLE (g2core G2Ble + G2Session)-->  both G2 arms
   PhoneHub: startup (heartbeat off), keep-alive, per-arm reinit, status, Display card
   notification listener -> filter -> G2Session.notify (hub form, community fallback)
   Even AI WAKE_UP -> ENTER/heartbeat -> phone mic SpeechRecognizer -> ASK/ANALYSE -> Claude (+ phone tools) -> REPLY -> EXIT
   dashboard features (off by default): stocks, calendar, ideas -> setStocks / setCalendar / setTasks
```

Owner rules that the code follows: no EvenHub page and no menu push from the phone; nothing is
pushed to a dashboard widget, and nothing is display-enabled, unless the user switched that
feature on; switching off stops pushing and restores nothing; a double tap (EXIT from the
glasses) closes an answer.

| Feature | How | Confidence |
|---|---|---|
| Connect, startup, keep-alive, reinit | `G2Ble` (no bond first, `bondBeforeConnect` fallback) + `G2Session.startup()` | g2core unit-tested; **not yet run on a phone** |
| Display card | `readSettings` / `applySetting` (height, distance, brightness, auto) | brightness/auto LIVE on the laptop; height/distance SET DECODED |
| Notifications | hub form, then the community form after 2 hub failures; the log says which acked | hub form DECODED/UNTESTED, community form COMMUNITY |
| Ask questions | SID 0x07 session as in `g2/even_ai.py`, but the phone mic replaces the glasses' LC3 stream | session LIVE on the laptop hub; the phone-mic path is untested |
| Stocks card | `setStocks` (stock display-enable + one push per quote) | DECODED, never seen on the glasses |
| Calendar card | `setCalendar` (today and tomorrow, up to 8, Even's time format) | LIVE (laptop) |
| Ideas | `setTasks` quick list | LIVE (laptop) |

Privacy: only the question text (and Claude's tool calls/results) goes to Anthropic's API
(`api.anthropic.com`), and stock lookups go to Yahoo/Stooq. Everything else stays on the phone
(calendar via CalendarContract, ideas in `files/ideas.jsonl`, notifications). Even's servers are
never contacted. The Claude key lives only on the phone (Keystore-encrypted). See
`android/README.md` for setup and provisioning.
