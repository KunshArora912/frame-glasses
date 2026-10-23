"""Local replacement for Even AI, the G2's built-in voice assistant (SID 0x07).

The firmware opens Even AI by itself when the wearer says "Hey Even", taps-and-holds, or picks
it from the glasses menu, and tells the app with ``CTRL WAKE_UP``.  From then on this module
plays the phone app's part, entirely on the laptop (docs/native-protocol.md section 5.4,
``H/even_ai.dart``, ``P/routes/even_ai.py``)::

    glasses -> CTRL WAKE_UP                      _on_frame()
    laptop  -> CTRL ENTER                        at once; then HEARTBEAT {count} every 2 s
    glasses -> LC3 on 0x6402                     the gateway decodes it; Speech gets PCM ("audio" events)
                                                 no audio 1.5 s after ENTER -> speech.gw.mic(True) fallback
    (laptop endpoints the utterance: Speech.capture_utterance, <= 15 s)
    laptop  -> ASK {text}                        the local transcript ("You: ...")
    laptop  -> ANALYSE                           "thinking" (brain.ask, <= 30 s)
    laptop  -> REPLY chunks <= 512 B, 150 ms apart, end flag {6: 1} only on the last
    glasses -> EVENT STREAM_COMPLETE / SCROLL    re-arm the reading time
    either  -> CTRL EXIT                         heartbeat off, mic off (glasses: user double-tapped;
                                                 laptop: reading time over, error, or stop())

Usage (the launcher / hub, one line once the hub's native ``Glasses`` exists)::

    from g2 import even_ai; self.even_ai = even_ai.attach(self)      # host has .glasses/.native, .speech, .brain

(while ``ai.turn_running`` the launcher must not draw: its home ticker / double-tap handler would
put an EvenHub page over the firmware's Even AI screen; ``await ai.cancel()`` ends a turn from our side)

or explicitly::

    ai = EvenAI(glasses, speech, brain)      # g2.native.transport.Glasses, g2.stt.Speech, ai.brain.Brain
    await ai.start()                         # listens for the wake frame; one turn at a time
    ...
    await ai.stop()                          # ends an open session with CTRL EXIT

Simulation against fakes (prints every frame, hex + decoded), no hardware::

    python g2/even_ai.py --simulate "what time is it" [--long] [--no-native-audio] [--real-time]

Nothing here talks to the network; the brain decides that (ai/brain.py).  Builders and parsers
come from ``g2.native.even_ai`` and are not duplicated here.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from g2.native import even_ai as proto  # noqa: E402
from g2.native.framing import ParsedFrame  # noqa: E402
from g2.native.transport import LEFT, RIGHT  # noqa: E402

SID = proto.SID_EVEN_AI

LISTEN_MAX_S = 15.0          # hard cap on listening (Speech endpoints earlier on silence)
THINK_MAX_S = 30.0           # transcription + brain
AUDIO_WAIT_S = 1.5           # no PCM this long after ENTER -> EvenHub mic toggle fallback
ASK_GAP_S = 0.3              # ASK -> ANALYSE (hub)
HEARTBEAT_S = proto.HEARTBEAT_INTERVAL_S
REPLY_PACE_S = proto.REPLY_PACE_MS / 1000
SILENCE_S = 1.2
START_TIMEOUT_S = 6.0

NO_SPEECH_REPLY = "Sorry, I didn't catch that."
NO_AUDIO_REPLY = "I couldn't hear you: the glasses microphone sent no audio. Please try again."
BRAIN_FAILED_REPLY = "Sorry, the assistant is unavailable right now."
TIMEOUT_REPLY = "Sorry, that took too long."
NO_STT_REPLY = "Speech-to-text is not running on the laptop."
NO_BRAIN_REPLY = "No brain configured on the laptop."

PHASES = ("idle", "open", "listening", "transcribing", "thinking", "answered", "error")


def reading_time(text: str) -> float:
    """Seconds the answer stays before we EXIT by ourselves: 8 s + 60 ms per character, max 90 s
    (H/even_ai.dart readingTime)."""
    return min(90.0, 8.0 + 0.06 * len(text))


def describe(payload: bytes) -> str:
    """Human label for a SID 0x07 payload: ``CTRL ENTER (magic 101)``, ``REPLY 1/3 ...``."""
    try:
        m = proto.parse(payload)
    except Exception as e:  # pragma: no cover - defensive
        return f"undecodable ({e!r})"
    s = m.name
    if m.value_name is not None:
        s += " " + m.value_name
    elif m.cmd == proto.CMD_HEARTBEAT and m.value is not None:
        s += f" count={m.value}"
    if m.text is not None:
        txt = m.text.decode("utf-8", "replace")
        s += f' text={len(m.text)}B "{txt[:40]}{"..." if len(txt) > 40 else ""}"'
    if m.cmd == proto.CMD_REPLY:
        from g2.native import pb
        sub = pb.read_bytes(payload, proto.FIELD_FOR_CMD[proto.CMD_REPLY]) or b""
        if pb.read_varint(sub, 6, 0) == 1:
            s += " END"
    if m.error:
        s += f" error={m.error}"
    return s + f" (magic {m.magic})"


class Turn:
    """One wake -> answer cycle.  Events are set by inbound frames; fields are for tests/logs."""

    def __init__(self) -> None:
        self.exit = asyncio.Event()          # the glasses sent CTRL EXIT (user double-tapped)
        self.vad_end = asyncio.Event()       # VAD_END / VAD_TIMEOUT (never seen live; logged only)
        self.stream_complete = asyncio.Event()
        self.scrolled = asyncio.Event()
        self.question = ""
        self.answer = ""
        self.error: str | None = None
        self.mic_fallback = False
        self.audio_seconds = 0.0


class _MicGate:
    """Stands in for ``speech.gw`` during one capture.

    ``Speech.capture_utterance`` always calls ``gw.mic(True)`` (the EvenHub audio-control toggle).
    After CTRL ENTER the firmware has opened the mic itself and the gateway already forwards the
    0x6402 stream, so the toggle is held back; it is only forwarded when no PCM has arrived
    ``wait_s`` after the request.  ``mic(False)`` is forwarded only if ``mic(True)`` was.
    Everything else (``decoder`` ...) passes through to the real gateway.
    """

    def __init__(self, gw: Any, owner: "EvenAI", turn: Turn) -> None:
        self._gw, self._ai, self._turn = gw, owner, turn
        self._fallback: asyncio.Task | None = None
        self.forwarded = False
        self.calls: list[bool] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._gw, name)

    async def mic(self, on: bool) -> None:
        self.calls.append(on)
        if on:
            if self._fallback is None or self._fallback.done():
                seen = self._ai.audio_packets
                self._fallback = asyncio.ensure_future(self._arm(seen))
            return
        if self._fallback is not None:
            self._fallback.cancel()
            self._fallback = None
        if self.forwarded:
            self.forwarded = False
            await self._gw.mic(False)

    async def _arm(self, seen_before: int) -> None:
        await self._ai._sleep(self._ai.audio_wait_s)
        if self._ai.audio_packets > seen_before:
            self._ai.log("Even AI: native mic stream is flowing; EvenHub mic toggle not needed")
            return
        self._ai.log(f"Even AI: no audio {self._ai.audio_wait_s:g} s after ENTER; falling back to gw.mic(True)")
        self.forwarded = True
        self._turn.mic_fallback = True
        try:
            await self._gw.mic(True)
        except Exception as e:
            self._ai.log(f"Even AI: mic fallback failed: {e!r}")


class EvenAI:
    """Answers the glasses' Even AI screen with local speech-to-text and the configured brain.

    ``glasses``: ``g2.native.transport.Glasses`` (``send_pb``, ``on``, ``alloc``);
    ``speech``: ``g2.stt.Speech`` or a fake with ``capture_utterance``/``transcribe`` and ``gw``;
    ``brain``: anything with ``async ask(question) -> str``.  ``log(str)`` defaults to logging;
    ``clock()`` only stamps events; ``sleep`` and ``time_scale`` let tests drive the timing.
    """

    def __init__(self, glasses, speech, brain, *, log: Callable[[str], Any] | None = None,
                 clock: Callable[[], float] = time.time, sleep=asyncio.sleep, time_scale: float = 1.0,
                 listen_s: float = LISTEN_MAX_S, think_s: float = THINK_MAX_S, audio_wait_s: float = AUDIO_WAIT_S,
                 heartbeat_s: float = HEARTBEAT_S, reply_pace_s: float = REPLY_PACE_S, ask_gap_s: float = ASK_GAP_S,
                 auto_close: bool = True) -> None:
        self.glasses, self.speech, self.brain = glasses, speech, brain
        self.log = log or logging.getLogger("g2.even_ai").info
        self.clock, self.sleep, self.time_scale = clock, sleep, time_scale
        self.listen_s, self.think_s, self.audio_wait_s = listen_s, think_s, audio_wait_s
        self.heartbeat_s, self.reply_pace_s, self.ask_gap_s = heartbeat_s, reply_pace_s, ask_gap_s
        self.auto_close = auto_close
        self.phase = "idle"
        self.turn: Turn | None = None
        self.history: list[Turn] = []
        self.sent: list[tuple[str, bytes]] = []          # (label, payload) of every SID 0x07 frame we sent
        self.on_send: Callable[[str, bytes], Any] | None = None
        self.hb_count = 0
        self.audio_packets = 0
        self.session_open = False
        self._task: asyncio.Task | None = None
        self._hb: asyncio.Task | None = None
        self._active = False
        self._audio_hooked = False

    # ── lifecycle ──────────────────────────────────────────────────────────────────────
    async def start(self) -> None:
        """Subscribe to inbound frames (and the gateway's PCM events, to detect the native stream)."""
        if self._active:
            return
        self._active = True
        self.glasses.on(self._on_frame)                 # Glasses has no off(); _active gates it
        gw = getattr(self.speech, "gw", None)
        if gw is not None and hasattr(gw, "on") and not self._audio_hooked:
            gw.on("audio", self._on_audio)
            self._audio_hooked = True
        self.log("Even AI: waiting for CTRL WAKE_UP on SID 0x07")

    async def stop(self) -> None:
        """Cancel an open turn (sends CTRL EXIT if the glasses did not), stop the heartbeat."""
        self._active = False
        await self.cancel()

    async def cancel(self) -> None:
        """End the current turn from our side (e.g. the launcher saw a double tap)."""
        t, self._task = self._task, None
        if t is not None and not t.done():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._stop_heartbeat()

    async def wait(self, timeout: float | None = None) -> None:
        """Wait for the current turn (if any) to finish; for tests and the simulator."""
        t = self._task
        if t is None:
            return
        await asyncio.wait({t}, timeout=timeout)

    @property
    def turn_running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def heartbeat_running(self) -> bool:
        return self._hb is not None and not self._hb.done()

    def _set_phase(self, p: str) -> None:
        if p != self.phase:
            self.phase = p
            self.log(f"Even AI: phase {p}")

    async def _sleep(self, s: float) -> None:
        if s > 0:
            await self.sleep(s * self.time_scale)

    # ── inbound ────────────────────────────────────────────────────────────────────────
    def _on_audio(self, pcm: bytes) -> None:
        self.audio_packets += 1

    def _on_frame(self, side: str, p: ParsedFrame) -> None:
        if not self._active or p.sid != SID:
            return
        try:
            m = proto.parse(p.payload)
        except Exception as e:
            self.log(f"Even AI <- undecodable 0x07 frame {p.payload.hex()} ({e!r})")
            return
        if m.cmd != proto.CMD_HEARTBEAT:
            self.log(f"Even AI <- {side} {describe(p.payload)}")
        turn = self.turn if self.turn_running else None
        if m.is_wake_up:
            if turn is not None or self.session_open:
                self.log("Even AI: wake ignored, a turn is already running")
            else:                                        # right lens normally; some firmware reports it on the left
                self._task = asyncio.ensure_future(self._run_turn())
            return
        if m.is_exit:
            self._stop_heartbeat()
            self.session_open = False
            if turn is not None:
                turn.exit.set()
                self._task.cancel()                      # ends listening / thinking / lingering at once
            self._set_phase("idle")
            return
        if turn is None:
            return
        if m.cmd == proto.CMD_VAD_INFO and m.value in (proto.VAD_END, proto.VAD_TIMEOUT):
            turn.vad_end.set()
        elif m.cmd == proto.CMD_EVENT:
            if m.value == proto.EVENT_STREAM_COMPLETE:
                turn.stream_complete.set()
            turn.scrolled.set()                          # any event restarts the reading time
        elif m.cmd == proto.CMD_PROMPT or (m.cmd == proto.CMD_COMM_RSP and m.error):
            turn.error = turn.error or describe(p.payload)

    # ── outbound ───────────────────────────────────────────────────────────────────────
    async def _send(self, payload: bytes, label: str, quiet: bool = False) -> None:
        self.sent.append((label, payload))
        if not quiet:
            self.log(f"Even AI -> {label}  pb={payload.hex()}")
        if self.on_send is not None:
            self.on_send(label, payload)
        await self.glasses.send_pb(SID, payload)

    def _magic(self) -> int:
        return self.glasses.alloc.next_magic()

    async def _enter(self) -> None:
        await self._send(proto.enter(self._magic()), "CTRL ENTER")
        self.session_open = True
        self._start_heartbeat()

    async def _exit(self) -> None:
        self._stop_heartbeat()
        if not self.session_open:
            return
        self.session_open = False
        try:
            await self._send(proto.exit_(self._magic()), "CTRL EXIT")
        except Exception as e:
            self.log(f"Even AI: EXIT not sent: {e!r}")

    def _start_heartbeat(self) -> None:
        self._stop_heartbeat()
        self.hb_count = 0
        self._hb = asyncio.ensure_future(self._hb_loop())

    def _stop_heartbeat(self) -> None:
        t, self._hb = self._hb, None
        if t is not None and not t.done():
            t.cancel()

    async def _hb_loop(self) -> None:
        n = 0
        while True:
            await self._sleep(self.heartbeat_s)
            n += 1
            self.hb_count = n
            try:
                await self._send(proto.heartbeat(self._magic(), n), f"HEARTBEAT {n}", quiet=True)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.log(f"Even AI: heartbeat stopped: {e!r}")
                return

    async def _reply(self, turn: Turn, text: str) -> None:
        chunks = proto.reply_chunks(text)
        for i, chunk in enumerate(chunks):
            if turn.exit.is_set():
                return
            last = i == len(chunks) - 1
            await self._send(proto.reply(self._magic(), chunk, final=last),
                             f"REPLY {i + 1}/{len(chunks)}{' (end)' if last else ''}")
            if not last:
                await self._sleep(self.reply_pace_s)

    # ── the turn ───────────────────────────────────────────────────────────────────────
    async def _run_turn(self) -> None:
        turn = Turn()
        self.turn = turn
        self.history.append(turn)
        del self.history[:-50]
        answer = ""
        try:
            await self._enter()
            self._set_phase("open")
            speech_ok = self.speech is not None and getattr(self.speech, "available", True)
            pcm = await self._listen(turn) if speech_ok else None
            if not speech_ok:
                turn.error = "no speech engine"
            if turn.exit.is_set():
                return
            question = ""
            if pcm:
                self._set_phase("transcribing")
                try:
                    question, _level = await asyncio.wait_for(self.speech.transcribe(pcm), self.think_s * self.time_scale or None)
                    question = (question or "").strip()
                except asyncio.TimeoutError:
                    turn.error = "transcription timed out"
                except Exception as e:
                    turn.error = f"transcription failed: {type(e).__name__}"
                    self.log(f"Even AI: {turn.error}: {e}")
            turn.question = question
            if question:
                self.log(f'Even AI heard: "{question}"')
                await self._send(proto.ask(self._magic(), question), f'ASK "{question[:40]}"')
                await self._sleep(self.ask_gap_s)
            if turn.exit.is_set():
                return
            await self._send(proto.analyse(self._magic()), "ANALYSE")
            self._set_phase("thinking")
            if turn.error == "no speech engine":
                answer = NO_STT_REPLY
            elif not question:
                answer = NO_AUDIO_REPLY if turn.error == "no audio" else NO_SPEECH_REPLY
            elif self.brain is None:
                answer = NO_BRAIN_REPLY
            else:
                try:
                    answer = await asyncio.wait_for(self.brain.ask(question), self.think_s * self.time_scale or None)
                    answer = (answer or "").strip() or "(no answer)"
                except asyncio.TimeoutError:
                    turn.error = "brain timed out"
                    answer = TIMEOUT_REPLY
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    turn.error = f"brain failed: {type(e).__name__}"
                    self.log(f"Even AI: {turn.error}: {e}")
                    answer = f"{BRAIN_FAILED_REPLY} ({type(e).__name__})"
            turn.answer = answer
            if turn.exit.is_set():
                return
            await self._reply(turn, answer)
            self._set_phase("answered")
            await self._linger(turn, answer)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            turn.error = turn.error or f"{type(e).__name__}: {e}"
            self.log(f"Even AI turn failed: {e!r}")
            self._set_phase("error")
            if self.session_open and not turn.exit.is_set():
                try:
                    await self._reply(turn, f"Even AI failed: {type(e).__name__}"[:120])
                except Exception:
                    pass
        finally:
            if not turn.exit.is_set():
                await self._exit()                        # our side ends the session (CancelledError-safe)
            self._stop_heartbeat()
            self.session_open = False
            self.turn = None
            if self.phase != "error":
                self._set_phase("idle")

    async def _listen(self, turn: Turn) -> bytes | None:
        """Capture one utterance through Speech (its endpointer), with the mic gate, the 15 s cap and
        the glasses' EXIT able to cut it short.  None = nothing usable."""
        self._set_phase("listening")
        speech = self.speech
        real_gw = getattr(speech, "gw", None)
        gate = _MicGate(real_gw, self, turn) if real_gw is not None else None
        if gate is not None:
            speech.gw = gate
        cap = asyncio.ensure_future(speech.capture_utterance(
            start_timeout=min(START_TIMEOUT_S, self.listen_s), silence_s=SILENCE_S, max_s=self.listen_s))
        limit = asyncio.ensure_future(self._sleep(self.listen_s + 0.5))      # 15 s total, whenever speech started
        try:
            done, _ = await asyncio.wait({cap, limit}, return_when=asyncio.FIRST_COMPLETED)
            if cap in done:
                try:
                    pcm = cap.result()
                except RuntimeError as e:                # Speech: "no audio from the glasses (mic stream never started)"
                    turn.error = "no audio"
                    self.log(f"Even AI: {e}")
                    return None
            else:
                self.log(f"Even AI: listening cap of {self.listen_s:g} s reached")
                cap.cancel()
                try:
                    await cap
                except (asyncio.CancelledError, Exception):
                    pass
                return None
            turn.audio_seconds = round(len(pcm or b"") / 32000, 1)
            self.log(f"Even AI: captured {turn.audio_seconds} s of audio"
                     f"{' (EvenHub mic fallback)' if turn.mic_fallback else ''}")
            return pcm or None
        finally:
            limit.cancel()
            if not cap.done():
                cap.cancel()
                try:
                    await cap
                except (asyncio.CancelledError, Exception):
                    pass
            if gate is not None:
                speech.gw = real_gw
                if gate.forwarded:                       # capture was cut before its own mic(False)
                    try:
                        await real_gw.mic(False)
                    except Exception:
                        pass
                    gate.forwarded = False

    async def _linger(self, turn: Turn, answer: str) -> None:
        """Keep the session (heartbeat) alive while the answer is read; EXIT after the reading time,
        restarted by SCROLL / STREAM_COMPLETE events; the glasses' own EXIT cancels us."""
        if not self.auto_close:
            await turn.exit.wait()
            return
        while not turn.exit.is_set():
            turn.scrolled.clear()
            timer = asyncio.ensure_future(self._sleep(reading_time(answer)))
            scroll = asyncio.ensure_future(turn.scrolled.wait())
            try:
                done, _ = await asyncio.wait({timer, scroll}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for f in (timer, scroll):
                    f.cancel()
            if timer in done:
                self.log(f"Even AI: reading time ({reading_time(answer):.0f} s) over; closing")
                return


# ── integration ────────────────────────────────────────────────────────────────────────


def _find(host, names: tuple[str, ...]):
    for obj in (host, getattr(host, "hub", None), getattr(host, "ctx", None)):
        if obj is None:
            continue
        for n in names:
            v = getattr(obj, n, None)
            if v is not None:
                return v
    return None


def attach(host, *, log: Callable[[str], Any] | None = None, **kw) -> EvenAI | None:
    """Bind Even AI to a launcher or hub: ``host.glasses`` / ``host.native`` (or ``host.hub.*``) must be
    the native ``Glasses``; ``speech`` and ``brain`` are looked up the same way.  Returns the started
    (or, with no running loop, start-pending) ``EvenAI``; None when there is no Glasses yet.

        self.even_ai = even_ai.attach(self)       # after the hub's Glasses.start()
    """
    glasses = _find(host, ("glasses", "native"))
    if glasses is None or not hasattr(glasses, "send_pb") or not hasattr(glasses, "on"):
        (log or logging.getLogger("g2.even_ai").info)("Even AI: no native Glasses on the host yet; not attached")
        return None
    speech = _find(host, ("speech",))
    brain = _find(host, ("brain",))
    ai = EvenAI(glasses, speech, brain, log=log, clock=getattr(host, "clock", time.time), **kw)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        loop.create_task(ai.start(), name="g2-even-ai")
    try:
        host.even_ai = ai
    except Exception:
        pass
    return ai


# ── simulation harness ─────────────────────────────────────────────────────────────────


def _simulate(question: str, answer: str, native_audio: bool, time_scale: float, glasses_exit: bool) -> None:
    from g2.fakes import FakeBrain, FakeGateway
    from g2.native.framing import FrameAssembler, parse_frame
    from g2.native.transport import FakeTransport, Glasses, fake_glasses_responder, rx_frame

    t0 = time.monotonic()

    def stamp() -> str:
        return f"[t={(time.monotonic() - t0) / time_scale:6.2f}s]"

    def say(msg: str) -> None:
        print(f"{stamp()} {msg}")

    class PrintingTransport(FakeTransport):
        asm = {LEFT: FrameAssembler(), RIGHT: FrameAssembler()}

        async def write(self, side, frame, response=False):
            p = parse_frame(frame)
            frag = f" fragment {p.num}/{p.total}" if p is not None and p.total > 1 else ""
            print(f"{stamp()} TX {side:5} {bytes(frame).hex()}{frag}")
            whole = self.asm[side].feed(bytes(frame))
            if whole is not None:
                w = parse_frame(whole)
                if w is not None and w.sid == SID:
                    print(f"{' ' * 12}  = SID 0x07 flag 0x{w.flag:02x} seq {w.seq}: {describe(w.payload)}")
            await super().write(side, frame, response)

    class SimSpeech:
        """FakeSpeech that takes 2.5 s to hear the utterance; with native audio it also pushes PCM
        events through the gateway so the mic gate sees the stream."""

        def __init__(self, gw):
            self.gw, self.available, self.model = gw, True, "fake"

        async def capture_utterance(self, **kw):
            await self.gw.mic(True)
            for _ in range(5):
                await asyncio.sleep(0.5 * time_scale)
                if native_audio:
                    await self.gw.emit("audio", b"\x00\x00" * 8000)
            await self.gw.mic(False)
            return b"\x01\x00" * 16000

        async def transcribe(self, pcm):
            return question, -25.0

    transport = PrintingTransport(fake_glasses_responder())
    glasses = Glasses(transport, log=lambda m: say(f"glasses: {m}"))
    glasses.time_scale = time_scale
    gw = FakeGateway()

    async def mic(on):
        say(f"GW  /api/mic enabled={on}  (EvenHub audio control; forces a startup page on the gateway)")
        gw.mic_calls.append(on)
    gw.mic = mic
    speech = SimSpeech(gw)
    ai = EvenAI(glasses, speech, FakeBrain(answer), log=say, time_scale=time_scale)

    def rx_printer(side, p):
        if p.sid == SID:
            print(f"{stamp()} RX {side:5} {describe(p.payload)}")
    glasses.on(rx_printer)

    async def main():
        print(f"protocol time scale {time_scale:g} (printed t = protocol seconds); native audio: {native_audio}")
        async with glasses:
            await ai.start()
            wake = rx_frame(SID, proto.ctrl(5, proto.CTRL_WAKE_UP), seq=1)
            print(f"{stamp()} RX right {wake.hex()}   <- the glasses' CTRL WAKE_UP frame (magic 5)")
            transport.push(RIGHT, wake)
            await asyncio.sleep(0)
            for _ in range(200):
                if ai.turn_running:
                    break
                await asyncio.sleep(0.01)
            if glasses_exit:
                while ai.phase != "answered" and ai.turn_running:
                    await asyncio.sleep(0.01)
                await asyncio.sleep(2.0 * time_scale)
                ex = rx_frame(SID, proto.ctrl(6, proto.CTRL_EXIT), seq=2)
                print(f"{stamp()} RX right {ex.hex()}   <- the user double-tapped: CTRL EXIT (magic 6)")
                transport.push(RIGHT, ex)
            await ai.wait(timeout=120 * time_scale + 10)
            await ai.stop()
        print()
        print("frames we sent on SID 0x07, in order:")
        for i, (label, payload) in enumerate(ai.sent, 1):
            print(f"  {i:2} {label:<22} {payload.hex()}")
        print(f"gateway mic calls: {gw.mic_calls}   question: {ai.history[-1].question!r}   error: {ai.history[-1].error}")

    asyncio.run(main())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Even AI replacement; --simulate runs a whole turn against fakes.")
    ap.add_argument("--simulate", metavar="QUESTION", help="simulated transcript of what the wearer says")
    ap.add_argument("--answer", default=None, help="the fake brain's answer (default: a 300-char sentence)")
    ap.add_argument("--long", action="store_true", help="1200-byte answer, to show the 512 B REPLY chunking")
    ap.add_argument("--no-native-audio", action="store_true", help="no PCM arrives after ENTER: show the mic fallback")
    ap.add_argument("--glasses-exit", action="store_true", help="the user double-taps 2 s after the answer")
    ap.add_argument("--real-time", action="store_true", help="real protocol timing (otherwise 20x faster)")
    a = ap.parse_args(argv)
    if not a.simulate:
        ap.print_help()
        return 2
    answer = a.answer if a.answer is not None else ("The answer is forty-two. " * (48 if a.long else 12)).strip()
    _simulate(a.simulate, answer, not a.no_native_audio, 1.0 if a.real_time else 0.05, a.glasses_exit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
