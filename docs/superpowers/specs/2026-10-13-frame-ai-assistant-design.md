# Frame AI Assistant — design

**Date:** 2026-10-13
**Status:** approved, implementing
**Hardware:** Brilliant Labs Frame, firmware v25.278.1113, bonded to this laptop

## Goal

Tap the glasses, ask a question aloud, read the answer on the display.
Optionally point the camera at something and ask about that.

This is the thing Meta's glasses structurally cannot do: the brain is yours,
swappable, and runs whatever model you choose.

## Why this first

Navigation needs a routing API, phone GPS and a bond decision. The assistant
needs none of that — it uses only hardware already verified working today
(mic at 100% of realtime, display, taps, camera) and runs entirely from the
laptop that already holds the bond. The display/input/paging layer it builds
is then reused by navigation.

## Hard constraints (measured, not assumed)

| Constraint | Value | Consequence |
|---|---|---|
| Display | 640x400, 16 colours | ~6 lines x ~25 chars at 48px glyphs |
| Glyph fit check | `y + 48 <= 400`, fails SILENTLY | never place text below y=352 |
| Buffer | double-buffered, FPGA clears on show() | redraw the whole frame every time |
| Link | ~25 kB/s sustained | fine for text; camera costs ~10 kB/frame |
| Audio | 16 kHz/8-bit = 16 kB/s | 100% of realtime; 16-bit at 16 kHz is NOT |
| Tap | no debounce in hardware | one physical tap fires many interrupts |
| stay_awake | cleared by `run_lua` every boot | must be re-set after any reboot |
| Speaker | none | answers are READ, never spoken |

The answer-length limit is the dominant design fact: **a useful answer is
under ~150 characters.** This is a prompting problem as much as a rendering
one.

## Components

Four units, each independently testable, communicating through narrow
interfaces.

### 1. `ai/display.py` — FrameDisplay
Owns rendering. Word-wraps text to the panel, paginates into screens of 6
lines, draws a page indicator. Knows about the y<=352 trap and the
redraw-everything rule. Pure function from `(text, page) -> Lua draw calls`.

### 2. `ai/listen.py` — Listener
Streams mic audio at 16 kHz/8-bit into a buffer. Starts on command, stops on
a second tap, a silence threshold, or a hard timeout. Returns raw signed PCM.
Does not know what the audio is for.

### 3. `ai/transcribe.py` — Transcriber
Wraps `faster-whisper`. PCM in, text out. Local, free, private, CPU-only
(`base.en`, int8). Audio never leaves the machine. Model loads once and is
reused — first load downloads ~150 MB.

### 4. `ai/brain.py` — Brain
A protocol with one method:

```python
async def ask(self, question: str, image: bytes | None = None) -> str
```

Implementations:
- **`ClaudeBrain`** — Anthropic API. System prompt enforces brevity hard.
  Vision-capable, so camera questions work. Requires `ANTHROPIC_API_KEY`.
- **`OfflineBrain`** — no network. Answers a few built-in queries (time,
  battery, help) and otherwise explains that no key is set. Exists so the
  whole loop is testable and demonstrable without an account.

A future `AgentBrain` backed by a Claude Code session is a third
implementation, not a rewrite. Its latency profile (15-60 s) suits task
requests, not curiosity, so it is deliberately out of scope here.

### 5. `assistant.py` — orchestrator
Owns the state machine and the only place the pieces meet.

## State machine

```
IDLE ──tap──> LISTENING ──tap/silence──> THINKING ──> SHOWING
 ^                                                       │
 └───────────────── double-tap (new question) ───────────┘
                    single tap while SHOWING = next page
```

Camera: a tap held across the listening window (detected as a tap burst)
captures a frame and attaches it to the question.

## Device-side Lua

One frame app, `app/lua/assistant_frame_app.lua`:
- forwards taps as `RxTap` messages
- renders `TxPlainText` lines sent by the host
- streams mic audio on command (reuses `audio.min.lua`)
- captures on command (reuses `camera.min.lua`)

No new device-side protocol: every message type already exists and is proven.

## Error handling

Every failure must leave something readable on the glasses, because the user
cannot see the terminal.

| Failure | On the glasses |
|---|---|
| No speech detected | `didn't catch that` |
| Transcription empty | `didn't catch that` |
| No API key | `no API key set` |
| Network/API error | `brain unreachable` |
| Link drops mid-answer | nothing; host retries connect |

## Testing

- `display.py` wrapping/pagination: pure unit tests, no hardware
- `transcribe.py`: a known WAV in, expected text out
- `brain.py`: `OfflineBrain` deterministic; `ClaudeBrain` behind a key check
- end-to-end: manual, on hardware

## Out of scope (deliberately)

Wake word, conversation memory across questions, TTS (no speaker),
navigation, phone involvement, the agent backend.
