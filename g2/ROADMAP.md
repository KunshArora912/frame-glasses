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
| 1 | Dashboard: time, date, battery | firmware + hub time sync (`hub.py` TimeFeed, hourly, acked) | verified live (owner: dashboard looks right; time sync acked); hub feed implemented, its hourly push hardware check pending (`python g2/hub.py --once time`) |
| 2 | Dashboard: weather | hub weather feed (Open-Meteo opt-in) | **VERIFIED LIVE 2026-10-23 15:46**: 21° + sun icon drawn bottom-left of the native dashboard once the gateway held its EvenHub heartbeat while parked (gateway patch v2) |
| 3 | Dashboard: calendar, news, tasks/quick notes | hub feeds (local .ics, RSS, thoughts.json) | implemented, hardware check pending (`--once calendar|news|tasks`) |
| 4 | Menu: our entries + launch | hub menu push (Even built-ins + "Laptop" = the gateway's Launch id 10421, pushed after the gateway's own menu) + launcher starts parked | implemented, hardware check pending (`--once menu`); earlier: Launch from the glasses menu did nothing (2026-10-23 15:30), capture in progress |
| 5 | Even AI (voice assistant) | `g2/even_ai.py:EvenAI` (wake -> ENTER -> 2 s heartbeat -> mic -> local STT -> ASK/ANALYSE/REPLY -> EXIT) + local Whisper + brain; hook: `self.even_ai = even_ai.attach(self)` once the hub's Glasses is up; `python g2/even_ai.py --simulate "..."` prints every frame | **implemented, hardware check pending** (21 tests on fakes; verify: wake frame lens/bytes, mic streams after ENTER without the EvenHub toggle, end flag `6:1`, EXIT on double tap) |
| 6 | Teleprompter | `g2/native_apps.py` hosts the firmware Teleprompt (menu launch: FILE_LIST / FILE_SELECT / START / PAGE_DATA / heartbeat) from `g2/data/teleprompter/*.txt`; hidden board fallback `apps/teleprompter.py` | implemented, hardware check pending (all SID 0x06 frames DECODED) |
| 7 | Translate | `g2/native_apps.py` hosts the firmware Translate (OPEN / RESULT / heartbeat / LANG_SWITCH) with local STT + argostranslate when installed, captions only otherwise; hidden board fallback `apps/translate.py` | implemented, hardware check pending (all SID 0x05 frames DECODED) |
| 8 | Conversate / live transcription + summary | `NativeApps.conversate_open`: live transcript on the firmware Translate surface (same-language pair) + brain summary pushed and saved; the real Conversate channel (menu id 11 / SID 0x0B) is not decoded; hidden board fallback `apps/conversate.py` | implemented, hardware check pending |
| 9 | QuickNote / Reminders | Notes app + hub quick-list push (TasksFeed) | implemented, hardware check pending (`--once tasks`; Notes app live) |
| 10 | Settings: brightness, height, distance, head-up angle, silent, wear, timeout | laptop CLI `g2/settings_cli.py show|set|keys` (`--dry-run` prints frames) over native settings; hidden board fallback `apps/settings.py` | implemented, hardware check pending (height/distance SET are DECODED) |
| 11 | Notifications from the phone | Android companion notification listener -> relay -> native notification frames | wave 2 (needs gateway 7401 char) |
| 12 | Navigate | Android GPS -> relay -> laptop; Valhalla routing (nav/); native navigation frames | wave 2 |
| 13 | Head movements (IMU) for apps | gateway imuControl + events; head-up handled by firmware settings | wave 2 |
| 14 | Health (R1 ring), Stocks | no ring here; stocks need a feed | later / optional |
| 15 | Firmware update without the app | community flasher (g2flash) | later, on request |

## Verification log

- 2026-10-23 15:46: native weather card visible on the dashboard (21°, sun). Precondition: no EvenHub heartbeat while parked.
- 2026-10-23: gateway connect, display, taps, mic LC3 decode, speech (tone test), launcher apps with spoken input, park/relaunch via API, native settings READ, dashboard init / units / time sync / weather all acked (flag 0).
