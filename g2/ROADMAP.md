# G2 parity roadmap: everything Even's app does, all data on this laptop

Goal set by the owner on 2026-10-23: "I want everything Even G2 has but data to be
with me", driven from the laptop (and the phone only as a sensor), built and
verified by agents, hardware-checked here.

## Architecture (contracts every module follows)

```
glasses  <-BLE->  gateway (vendor, patched)  <-HTTP/WS->  g2/run.py process
                                                   |-- launcher + apps        (EvenHub page: our screens)
                                                   |-- hub (g2/hub.py)        (native dashboard feed, menu, keep-alive)
                                                   |-- native Glasses         (g2/native via POST /api/raw + raw events)
phone (android/ companion) <-WebSocket relay (relay_server.py)-> laptop: GPS, phone notifications
```

- One native `Glasses` instance per process, owned by `g2/hub.py:Hub`, exposed to
  apps as `ctx.native` and to the hub's feeds. Apps never open their own transport.
- Native pushes (weather, calendar, news, tasks, menu, notifications) go through
  `Hub` methods so the heartbeat start/stop choreography lives in one place.
- Everything that needs the network is opt-in in `g2/config/hub.yaml` (gitignored)
  and limited to non-identifying requests to open services (Open-Meteo weather,
  RSS feeds, a Valhalla routing server). Default: off. Nothing talks to Even.
- Tests use `g2/fakes.py` (gateway, speech, brain) and
  `g2/native/transport.py:FakeTransport` + `fake_glasses_responder`.
- Every byte sequence that was never seen live is marked DECODED in code and in
  `g2/native/README.md`; hardware verification is logged in this file.

## Status board

| # | Feature (Even's app) | Our piece | State |
|---|---|---|---|
| 1 | Dashboard: time, date, battery | firmware + hub time sync | native READ/ack verified live; **visual check pending** |
| 2 | Dashboard: weather | hub weather feed (Open-Meteo opt-in) | pushes acked live; not yet drawn: waiting on park-stops-heartbeat gateway patch |
| 3 | Dashboard: calendar, news, tasks/quick notes | hub feeds (local .ics, RSS, thoughts.json) | wave 1 |
| 4 | Menu: our entries + launch | gateway menu entry + park/relaunch | Launch from the glasses menu did nothing (2026-10-23 15:30); capture in progress |
| 5 | Even AI (voice assistant) | `g2/even_ai.py:EvenAI` (wake -> ENTER -> 2 s heartbeat -> mic -> local STT -> ASK/ANALYSE/REPLY -> EXIT) + local Whisper + brain; hook: `self.even_ai = even_ai.attach(self)` once the hub's Glasses is up; `python g2/even_ai.py --simulate "..."` prints every frame | **implemented, hardware check pending** (21 tests on fakes; verify: wake frame lens/bytes, mic streams after ENTER without the EvenHub toggle, end flag `6:1`, EXIT on double tap) |
| 6 | Teleprompter | app over native teleprompter frames (text files) | wave 1 |
| 7 | Translate | app: local STT + offline translation (argos) | wave 1 |
| 8 | Conversate / live transcription + summary | Transcribe app + local summary | wave 1 extends existing app |
| 9 | QuickNote / Reminders | Notes app + hub quick-list push | partly live (Notes); push in wave 1 |
| 10 | Settings: brightness, height, distance, head-up angle, silent, wear, timeout | Settings app over native settings | wave 1 |
| 11 | Notifications from the phone | Android companion notification listener -> relay -> native notification frames | wave 2 (needs gateway 7401 char) |
| 12 | Navigate | Android GPS -> relay -> laptop; Valhalla routing (nav/); native navigation frames | wave 2 |
| 13 | Head movements (IMU) for apps | gateway imuControl + events; head-up handled by firmware settings | wave 2 |
| 14 | Health (R1 ring), Stocks | no ring here; stocks need a feed | later / optional |
| 15 | Firmware update without the app | community flasher (g2flash) | later, on request |

## Verification log

- 2026-10-23: gateway connect, display, taps, mic LC3 decode, speech (tone test), launcher apps with spoken input, park/relaunch via API, native settings READ, dashboard init / units / time sync / weather all acked (flag 0).
