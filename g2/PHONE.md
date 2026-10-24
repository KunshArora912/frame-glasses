# Phone link (Even G2): notifications, GPS and display settings from the Android companion

The laptop holds the glasses' Bluetooth link (gateway on 127.0.0.1:8765). The phone never
connects to the glasses. It forwards its notifications and GPS fixes to the laptop and mirrors
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
