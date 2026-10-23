# Building things for the G2 without waiting for anyone

Everything runs on this laptop and talks to the glasses through the local
gateway. No phone app, no Even account, no cloud.

## 0. Start the stack

```
cd C:\Users\Kunsh\Desktop\frame
.venv\Scripts\python.exe g2\run.py
```

That starts the Bluetooth gateway (restarting it if it dies) and the launcher.
Glasses out of the case, phone Bluetooth off. The home screen appears when both
arms connect. `python g2/dev.py doctor` tells you what is and is not up.

## 1. Add a feature in five minutes

```
python g2/dev.py new weather --blurb "rain or not"
```

creates `g2/apps/weather.py` from the template. Open it. An app is one class:

```python
from g2.apps.base import App, AppContext

class Weather(App):
    name = "Weather"           # menu label
    blurb = "rain or not"      # one line under the menu card
    order = 40                 # menu position (lower = higher)

    async def open(self, ctx):                      # user tapped the entry
        await ctx.show(ctx.screen("Weather", ["Loading ..."], "tap = refresh"))

    async def touch(self, ctx, gesture):            # single_tap, swipe_up, swipe_down, long_press_release
        ...

    async def close(self, ctx):                     # exit / park / link lost: stop what you started
        ...
```

Save the file. The running launcher notices within two seconds, reloads it,
and the menu updates. To open it on the glasses right now:

```
python g2/dev.py push weather
python g2/dev.py push home
```

Double tap on the glasses leaves the app (that is the firmware's rule; the
launcher calls your `close()`).

## 2. Design it before it goes on the glasses

```
python g2/dev.py preview weather --gestures tap,down,tap
```

runs the app against fake glasses, prints every screen in a 48 x 10 box,
flags lines that are too long, and saves PNGs to `g2/previews/weather/`.
`--say "..."` is what the fake microphone hears, `--answer "..."` what the
fake brain replies.

Rules of the display (the firmware enforces them, the preview warns):

| | |
|---|---|
| canvas | 576 x 288 px, one fixed font, no sizes, no colours, left aligned |
| plain screens | about 48 columns x 10 rows; `ctx.screen(title, body, hint)` builds header + body + hint |
| layouts | up to 8 text boxes (+4 images), exactly one with `capture_events: true`, names <= 16 chars, text <= 1000 bytes |
| position | the glasses apply your height/distance calibration to everything; keep 16 px off the edges and the look matches Even's |

Positioned layout, same arrangement as Even's dashboard (status left, card right):

```python
await ctx.show_layout([
  {"type": "text", "text": "12:34\nFri 23 Oct", "x": 16,  "y": 12, "width": 200, "height": 96,  "container_name": "status"},
  {"type": "text", "text": "Rain at 15:00",      "x": 232, "y": 12, "width": 328, "height": 164, "padding": 8,
   "border_width": 1, "border_radius": 4, "capture_events": True, "container_name": "card"},
  {"type": "text", "text": "tap  refresh     double tap  exit", "x": 16, "y": 252, "width": 544, "height": 32, "container_name": "hint"},
])
```

You can also design a static screen as JSON and iterate on it live:

```
python g2/dev.py render screen.json           # PNG, no glasses
python g2/dev.py layout screen.json --watch   # resend on every save
```

`screen.json` is `{"elements": [ ... ]}` with the same fields as above, or
`{"text": "plain text"}`.

## 3. What an app can use (`ctx`)

| | |
|---|---|
| `ctx.show(text)` / `ctx.show_layout(elements)` | draw |
| `ctx.screen(title, lines, hint)` | standard screen string; `ctx.wrap`, `ctx.paginate` |
| `ctx.speech.capture_utterance()` | mic on, returns PCM when you stop talking (None if silent) |
| `ctx.speech.transcribe(pcm)` | local Whisper, returns `(text, level_dbfs)` |
| `ctx.speech.captioner(callback)` | live captions: `await live.start()` / `await live.stop()` |
| `ctx.brain.ask(question)` | offline answers by default; `--brain cli` or `api` opts into Claude (text leaves the laptop) |
| `ctx.store` | notes store shared with the Frame tools: `add(text)`, `pending()` |
| `ctx.gw.battery / charging / firmware / ready` | glasses state |
| `ctx.run(coro)` | background job, cancelled automatically when the user leaves |
| `ctx.exit()` | go home yourself |
| `ctx.now()`, `ctx.clock()`, `ctx.log`, `ctx.transcripts_dir`, `ctx.data_dir` | housekeeping |

Long work (listening, thinking, fetching) belongs in `ctx.run(...)` so a
double tap can cancel it. Keep files you write under `g2/` folders that are
gitignored (`transcripts/`, `recordings/`, `logs/`, `previews/`) or elsewhere
on the laptop, never with device addresses in the repo.

## 4. Test and ship

```
python g2/dev.py test
git add g2/apps/weather.py && git commit -m "G2: weather app" && git push
```

`g2/test_launcher.py` has a `FakeGateway`, `FakeSpeech`, `FakeBrain` in
`g2/fakes.py`; copy `test_template_app_runs` to test your app's screens.

## 5. Asking Claude Code to build one for you

Paste something like:

> Read g2/DEVELOPING.md and g2/apps/base.py. Create g2/apps/pomodoro.py: a
> Pomodoro timer for the Even G2 (25 min work, 5 min break, tap starts and
> pauses, swipes change the length). Keep screens inside 48 columns x 10 rows,
> add a test in g2/test_launcher.py using g2/fakes.py, run `python g2/dev.py
> preview pomodoro --gestures tap` and fix anything it flags. Do not touch the
> gateway or launcher.

## 6. The apps that ship

| file | menu entry | what |
|---|---|---|
| `apps/assistant.py` | Assistant | tap, ask by voice, read the answer |
| `apps/transcribe.py` | Transcribe | live captions saved to a file |
| `apps/notes.py` | Notes | speak a note, keep it in thoughts.json |
| `apps/status.py` | Status | glasses, gateway, speech, brain |
| `apps/teleprompter.py` | hidden | page fallback; the real Teleprompt is the firmware app hosted by `g2/native_apps.py` |
| `apps/translate.py` | hidden | captions fallback; the real Translate is the firmware app hosted by `g2/native_apps.py` |
| `apps/conversate.py` | hidden | transcript + summary fallback; native version is `NativeApps.conversate_open` |
| `apps/settings.py` | hidden | fallback; use `python g2/settings_cli.py show|set <key> <value> [--dry-run]` |

Hidden apps stay out of the menu but open with `python g2/dev.py push <name>` and preview as usual.
Firmware-native Teleprompt / Translate: `python g2/native_apps.py` (standalone) or `NativeApps(hub.glasses, ...).attach()`
in the hub; scripts go in `g2/data/teleprompter/`.

## 7. The lower layers (when an app is not enough)

- `g2/gateway_client.py`: the gateway's HTTP/WebSocket API in Python (display, mic, events).
- `g2/native/`: the firmware's native protocol (dashboard widgets, settings, menu, notifications, Even AI) over the gateway's `/api/raw`. See `g2/native/README.md` for what is verified and what is not.
- `g2/vendor/men-g2-ble-gateway/`: the Bluetooth gateway itself (third party, MIT), with our patch in `g2/patches/`.
- `g2/docs/`: protocol research, positioning rules, and the Even-app feature catalogue.

## 8. When something is off

| symptom | try |
|---|---|
| home screen never appears | `python g2/dev.py doctor`; glasses in the case or phone Bluetooth on means no advertising |
| app not in the menu | `python g2/dev.py list`; a file starting with `_` or without an `App` subclass is skipped; look at the launcher log for "failed to load" |
| screen looks cut | `python g2/dev.py preview <app>`; shorten lines, fewer rows |
| mic "nothing heard" every time | speak within 6 s of the Listening screen; `python g2/mic_record.py 5` to check the mic path |
| glasses show Even's dashboard, not us | that is the parked state after a double tap; pick our entry from the glasses menu or `python g2/dev.py push home` |
