"""Tests for g2/even_ai.py (the local Even AI replacement) against FakeTransport + fakes.
No hardware, no network.  Run from C:/Users/Kunsh/Desktop/frame:

    .venv/Scripts/python.exe -m unittest g2.test_even_ai
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from g2 import even_ai as mod  # noqa: E402
from g2.even_ai import EvenAI, describe  # noqa: E402
from g2.fakes import FakeBrain, FakeGateway  # noqa: E402
from g2.native import even_ai as proto, pb  # noqa: E402
from g2.native.framing import crc16_bytes, parse_frame  # noqa: E402
from g2.native.transport import LEFT, RIGHT, FakeTransport, Glasses, fake_glasses_responder, rx_frame  # noqa: E402

SCALE = 0.01                        # protocol seconds -> real seconds (Windows sleeps resolve at ~15 ms)

# The glasses' wake frame, spelled out from the doc: AA 12 seq len total num SID flag payload crc16.
# payload {1: CTRL, 2: magic 5, 3: {1: WAKE_UP}} = 08 01 10 05 1a 02 08 01  (native-protocol.md 5.4 step 1)
WAKE_PAYLOAD = bytes.fromhex("08011005" "1a02" "0801")
WAKE_FRAME = bytes([0xAA, 0x12, 0x01, len(WAKE_PAYLOAD) + 2, 0x01, 0x01, 0x07, 0x00]) + WAKE_PAYLOAD + crc16_bytes(WAKE_PAYLOAD)
EXIT_FRAME = rx_frame(0x07, proto.ctrl(6, proto.CTRL_EXIT), seq=2)


class ScriptedSpeech:
    """A Speech stand-in whose capture behaves per ``mode``:
    instant   returns pcm right away
    none      nobody spoke (None)
    no_audio  raises Speech's RuntimeError (mic stream never started)
    audio     pushes PCM events through the gateway, waits 3 s, returns pcm
    silent    waits 3 s without any PCM, returns pcm (the fallback path)
    block     waits until released / cancelled
    Like the real Speech it toggles gw.mic(True/False) around the capture."""

    def __init__(self, gw, mode="instant", text="what time is it", pcm=b"\x01\x00" * 16000):
        self.gw, self.mode, self.text_out, self.pcm = gw, mode, text, pcm
        self.available = True
        self.release = asyncio.Event()
        self.kw: dict = {}

    async def capture_utterance(self, **kw):
        self.kw = kw
        await self.gw.mic(True)
        try:
            if self.mode == "none":
                return None
            if self.mode == "no_audio":
                raise RuntimeError("no audio from the glasses (mic stream never started)")
            if self.mode in ("audio", "silent"):
                for _ in range(6):
                    if self.mode == "audio":
                        await self.gw.emit("audio", b"\x00\x00" * 1600)
                    await asyncio.sleep(0.5 * SCALE)
            if self.mode == "block":
                await self.release.wait()
            return self.pcm
        finally:
            await self.gw.mic(False)

    async def transcribe(self, pcm):
        return self.text_out, -25.0


class BlockingBrain:
    def __init__(self):
        self.asked: list[str] = []
        self.release = asyncio.Event()

    async def ask(self, q, image=None):
        self.asked.append(q)
        await self.release.wait()
        return "late"


class FailingBrain:
    async def ask(self, q, image=None):
        raise ValueError("boom")


class FakeSleep:
    def __init__(self):
        self.calls: list[float] = []

    async def __call__(self, s):
        self.calls.append(s)
        await asyncio.sleep(0)


def names(ai: EvenAI) -> list[str]:
    """Command names of everything sent, heartbeats collapsed to 'HB'."""
    out = []
    for _label, payload in ai.sent:
        m = proto.parse(payload)
        out.append("HB" if m.cmd == proto.CMD_HEARTBEAT else (f"{m.name} {m.value_name}" if m.value_name else m.name))
    return out


def no_hb(ai: EvenAI) -> list[str]:
    return [n for n in names(ai) if n != "HB"]


def replies(payloads) -> list[tuple[bytes, bool]]:
    out = []
    for p in payloads:
        m = proto.parse(p)
        if m.cmd == proto.CMD_REPLY:
            sub = pb.read_bytes(p, 7)
            out.append((m.text, pb.read_varint(sub, 6, 0) == 1))
    return out


async def until(pred, timeout=3.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not pred():
        if loop.time() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.001)


class EvenAITests(unittest.IsolatedAsyncioTestCase):
    def make(self, speech_mode="instant", brain=None, **kw):
        self.t = FakeTransport(fake_glasses_responder())
        self.g = Glasses(self.t)
        self.g.time_scale = SCALE
        self.gw = FakeGateway()
        self.speech = ScriptedSpeech(self.gw, speech_mode)
        self.logs: list[str] = []
        kw.setdefault("time_scale", SCALE)
        self.ai = EvenAI(self.g, self.speech, FakeBrain() if brain is None else brain, log=self.logs.append, **kw)
        return self.ai

    async def asyncTearDown(self):
        if getattr(self, "ai", None) is not None:
            await self.ai.stop()
            await self.g.stop()

    async def run_turn(self, ai: EvenAI, wait=True):
        await self.g.start()
        await ai.start()
        self.t.push(RIGHT, WAKE_FRAME)
        await until(lambda: ai.turn_running)
        if wait:
            await ai.wait(timeout=5.0)
        return ai

    # ── wake + enter ───────────────────────────────────────────────────────────────────
    async def test_wake_frame_bytes_are_detected_and_enter_is_sent_first(self):
        self.assertEqual(WAKE_FRAME, rx_frame(0x07, WAKE_PAYLOAD, seq=1))        # same layout as the fakes use
        p = parse_frame(WAKE_FRAME)
        self.assertTrue(p.crc_ok and p.sid == 0x07 and proto.parse(p.payload).is_wake_up)
        ai = self.make()
        await self.run_turn(ai)
        self.assertEqual(names(ai)[0], "CTRL ENTER")
        wire = self.t.payloads("right", 0x07)
        first = proto.parse(wire[0])
        self.assertEqual((first.cmd, first.value), (proto.CMD_CTRL, proto.CTRL_ENTER))
        self.assertEqual(wire[0], proto.enter(first.magic))                       # {1:1, 2:m, 3:{1:2}}
        self.assertEqual(wire[0].hex()[:4] + wire[0].hex()[-8:], "0801" + "1a020802")
        f = parse_frame(self.t.writes[0][1])
        self.assertEqual((self.t.writes[0][0], f.sid, f.flag), ("right", 0x07, 0x20))

    async def test_full_turn_sequence_matches_the_hub(self):
        ai = self.make()
        await self.run_turn(ai)
        self.assertEqual(no_hb(ai), ["CTRL ENTER", "ASK", "ANALYSE", "REPLY", "CTRL EXIT"])
        asked = [proto.parse(p) for _, p in ai.sent if proto.parse(p).cmd == proto.CMD_ASK][0]
        self.assertEqual(asked.text, b"what time is it")
        self.assertEqual(ai.history[-1].question, "what time is it")
        self.assertEqual(ai.history[-1].answer, FakeBrain().answer.strip())
        self.assertIsNone(ai.history[-1].error)
        self.assertFalse(ai.session_open)
        self.assertFalse(ai.heartbeat_running)
        self.assertEqual(ai.phase, "idle")
        # the speech capture got the documented limits
        self.assertEqual(self.speech.kw["max_s"], mod.LISTEN_MAX_S)
        # magics are fresh and increasing (hub/Dart nextMagic), none reuse the wake magic 5
        magics = [proto.parse(p).magic for _, p in ai.sent]
        self.assertEqual(magics, sorted(magics))
        self.assertNotIn(5, magics)

    async def test_wake_on_left_lens_and_duplicate_wake_ignored(self):
        ai = self.make(speech_mode="block")
        await self.g.start()
        await ai.start()
        self.t.push(LEFT, WAKE_FRAME)
        await until(lambda: ai.turn_running)
        self.t.push(RIGHT, WAKE_FRAME)
        await asyncio.sleep(0.01)
        self.assertEqual(names(ai).count("CTRL ENTER"), 1)
        self.assertTrue(any("wake ignored" in m for m in self.logs))
        self.speech.release.set()
        await ai.wait(timeout=5.0)

    # ── heartbeat ──────────────────────────────────────────────────────────────────────
    async def test_heartbeat_cadence_with_fake_sleep(self):
        sleep = FakeSleep()
        ai = self.make(sleep=sleep, time_scale=1.0)
        await self.g.start()
        ai._start_heartbeat()
        for _ in range(12):
            await asyncio.sleep(0)
        ai._stop_heartbeat()
        await asyncio.sleep(0)
        hbs = [proto.parse(p) for _, p in ai.sent]
        self.assertGreaterEqual(len(hbs), 3)
        self.assertTrue(all(m.cmd == proto.CMD_HEARTBEAT for m in hbs))
        self.assertEqual([m.value for m in hbs], list(range(1, len(hbs) + 1)))      # {11: {1: count}} counts 1, 2, 3 ...
        self.assertEqual(set(sleep.calls), {proto.HEARTBEAT_INTERVAL_S})            # one 2 s sleep before each
        self.assertEqual(len(sleep.calls), len(hbs) + 1)                           # the sleep in flight when stopped
        self.assertEqual(hbs[0].magic + 1, hbs[1].magic)
        self.assertFalse(ai.heartbeat_running)

    async def test_heartbeat_runs_between_enter_and_exit_only(self):
        ai = self.make(speech_mode="silent")           # 3 s of listening at scale -> a heartbeat lands
        await self.run_turn(ai)
        n = names(ai)
        self.assertIn("HB", n)
        self.assertLess(n.index("CTRL ENTER"), n.index("HB"))
        self.assertLess(n.index("HB"), n.index("CTRL EXIT"))
        self.assertEqual(n[-1], "CTRL EXIT")
        self.assertFalse(ai.heartbeat_running)

    # ── reply chunking ─────────────────────────────────────────────────────────────────
    async def test_reply_chunks_at_512_byte_boundaries_with_final_flag_on_last_only(self):
        ai = self.make(brain=FakeBrain("é" * 600))     # 1200 UTF-8 bytes
        await self.run_turn(ai)
        sent = replies([p for _, p in ai.sent])
        self.assertEqual([len(c) for c, _ in sent], [512, 512, 176])
        self.assertEqual([f for _, f in sent], [False, False, True])
        self.assertEqual(b"".join(c for c, _ in sent).decode("utf-8"), "é" * 600)
        wire = replies(self.t.payloads("right", 0x07))            # re-assembled from the 232-byte BLE packets
        self.assertEqual(wire, sent)
        for c, _ in wire:
            c.decode("utf-8")                                     # no character cut in half

    async def test_short_reply_is_one_final_chunk(self):
        ai = self.make(brain=FakeBrain("It is 10:42."))
        await self.run_turn(ai)
        self.assertEqual(replies([p for _, p in ai.sent]), [(b"It is 10:42.", True)])
        reply_payload = [p for _, p in ai.sent if proto.parse(p).cmd == proto.CMD_REPLY][0]
        self.assertIn("END", describe(reply_payload))

    # ── exit paths ─────────────────────────────────────────────────────────────────────
    async def test_glasses_exit_stops_heartbeat_and_mic(self):
        ai = self.make(speech_mode="block")
        await self.run_turn(ai, wait=False)
        await until(lambda: self.gw.mic_calls == [True])          # 1.5 s without PCM -> fallback mic on
        self.assertTrue(ai.heartbeat_running)
        self.t.push(RIGHT, EXIT_FRAME)
        await until(lambda: not ai.turn_running)
        self.assertFalse(ai.heartbeat_running)
        self.assertFalse(ai.session_open)
        self.assertEqual(self.gw.mic_calls, [True, False])
        self.assertNotIn("CTRL EXIT", names(ai))                  # the glasses closed it; we do not echo EXIT
        self.assertNotIn("ASK", names(ai))
        self.assertEqual(ai.phase, "idle")
        self.assertIs(self.speech.gw, self.gw)                    # the mic gate was removed again

    async def test_cancellation_mid_session_sends_exit_and_cleans_up(self):
        brain = BlockingBrain()
        ai = self.make(brain=brain)
        await self.run_turn(ai, wait=False)
        await until(lambda: brain.asked == ["what time is it"])
        self.assertTrue(ai.heartbeat_running)
        await ai.stop()
        self.assertFalse(ai.turn_running)
        self.assertFalse(ai.heartbeat_running)
        self.assertEqual(no_hb(ai), ["CTRL ENTER", "ASK", "ANALYSE", "CTRL EXIT"])
        self.assertEqual(self.gw.mic_calls, [])                    # native stream path: toggle never forwarded
        # a wake after stop() is ignored
        self.t.push(RIGHT, WAKE_FRAME)
        await asyncio.sleep(0.01)
        self.assertEqual(names(ai).count("CTRL ENTER"), 1)

    async def test_auto_close_after_reading_time(self):
        ai = self.make(brain=FakeBrain("ok"))
        await self.run_turn(ai)
        self.assertEqual(names(ai)[-1], "CTRL EXIT")
        self.assertTrue(any("reading time" in m for m in self.logs))
        self.assertAlmostEqual(mod.reading_time("ok"), 8.12)
        self.assertEqual(mod.reading_time("x" * 5000), 90.0)

    # ── no speech / no audio / brain failures ───────────────────────────────────────────
    async def test_no_speech_path(self):
        ai = self.make(speech_mode="none")
        await self.run_turn(ai)
        self.assertEqual(no_hb(ai), ["CTRL ENTER", "ANALYSE", "REPLY", "CTRL EXIT"])   # no ASK without a transcript
        self.assertEqual(replies([p for _, p in ai.sent]), [(mod.NO_SPEECH_REPLY.encode(), True)])
        self.assertEqual(ai.history[-1].question, "")

    async def test_no_audio_from_the_mic(self):
        ai = self.make(speech_mode="no_audio")
        await self.run_turn(ai)
        self.assertEqual(replies([p for _, p in ai.sent]), [(mod.NO_AUDIO_REPLY.encode(), True)])
        self.assertEqual(ai.history[-1].error, "no audio")

    async def test_brain_failure_is_a_short_reply(self):
        ai = self.make(brain=FailingBrain())
        await self.run_turn(ai)
        (text, final), = replies([p for _, p in ai.sent])
        self.assertTrue(final)
        self.assertTrue(text.startswith(mod.BRAIN_FAILED_REPLY.encode()))
        self.assertIn(b"ValueError", text)
        self.assertEqual(ai.history[-1].error, "brain failed: ValueError")
        self.assertEqual(names(ai)[-1], "CTRL EXIT")

    async def test_brain_timeout_is_a_short_reply(self):
        ai = self.make(brain=BlockingBrain(), think_s=2.0)
        await self.run_turn(ai)
        self.assertEqual(replies([p for _, p in ai.sent]), [(mod.TIMEOUT_REPLY.encode(), True)])
        self.assertEqual(ai.history[-1].error, "brain timed out")

    async def test_listening_cap(self):
        ai = self.make(speech_mode="block", listen_s=1.0, audio_wait_s=10.0)
        await self.run_turn(ai)
        self.assertTrue(any("listening cap" in m for m in self.logs))
        self.assertEqual(replies([p for _, p in ai.sent]), [(mod.NO_SPEECH_REPLY.encode(), True)])
        self.assertEqual(self.gw.mic_calls, [])                    # cap (1 s) hit before the (10 s) fallback

    async def test_no_speech_engine(self):
        ai = self.make()
        ai.speech = None
        await self.run_turn(ai)
        self.assertEqual(replies([p for _, p in ai.sent]), [(mod.NO_STT_REPLY.encode(), True)])

    # ── mic policy ─────────────────────────────────────────────────────────────────────
    async def test_native_audio_suppresses_the_evenhub_mic_toggle(self):
        ai = self.make(speech_mode="audio")
        await self.run_turn(ai)
        self.assertEqual(self.gw.mic_calls, [])
        self.assertFalse(ai.history[-1].mic_fallback)
        self.assertGreater(ai.audio_packets, 0)
        self.assertTrue(any("not needed" in m for m in self.logs))

    async def test_mic_fallback_when_no_audio_arrives(self):
        ai = self.make(speech_mode="silent")
        await self.run_turn(ai)
        self.assertEqual(self.gw.mic_calls, [True, False])
        self.assertTrue(ai.history[-1].mic_fallback)
        self.assertEqual(no_hb(ai), ["CTRL ENTER", "ASK", "ANALYSE", "REPLY", "CTRL EXIT"])

    # ── events / describe / attach ─────────────────────────────────────────────────────
    async def test_events_are_tracked(self):
        ai = self.make(brain=BlockingBrain())
        await self.run_turn(ai, wait=False)
        await until(lambda: ai.phase == "thinking")
        self.t.push(RIGHT, rx_frame(0x07, proto.vad_info(7, proto.VAD_END), seq=3))
        self.t.push(RIGHT, rx_frame(0x07, proto.event(8, proto.EVENT_STREAM_COMPLETE), seq=4))
        self.t.push(RIGHT, rx_frame(0x07, proto.prompt(9, 6), seq=5))
        await until(lambda: ai.turn is not None and ai.turn.stream_complete.is_set())
        self.assertTrue(ai.turn.vad_end.is_set())
        self.assertIn("AUDIO_ERROR", ai.turn.error)
        await ai.stop()

    def test_describe(self):
        self.assertEqual(describe(proto.enter(129)), "CTRL ENTER (magic 129)")
        self.assertEqual(describe(proto.heartbeat(6, 3)), "HEARTBEAT count=3 (magic 6)")
        self.assertEqual(describe(proto.reply(9, "ok", True)), 'REPLY text=2B "ok" END (magic 9)')
        self.assertEqual(describe(proto.reply(9, "ok", False)), 'REPLY text=2B "ok" (magic 9)')
        self.assertEqual(describe(WAKE_PAYLOAD), "CTRL WAKE_UP (magic 5)")

    async def test_attach_duck_types_launcher_and_hub(self):
        t = FakeTransport()
        g = Glasses(t)
        speech, brain = ScriptedSpeech(FakeGateway()), FakeBrain()
        launcher = SimpleNamespace(gw=FakeGateway(), speech=speech, brain=brain, hub=SimpleNamespace(glasses=g), clock=lambda: 1.0)
        ai = mod.attach(launcher)
        self.assertIsInstance(ai, EvenAI)
        self.assertIs(launcher.even_ai, ai)
        self.assertIs(ai.glasses, g)
        self.assertIs(ai.speech, speech)
        await asyncio.sleep(0)
        self.assertTrue(ai._active)
        await ai.stop()
        hub = SimpleNamespace(native=g, speech=None, brain=None)
        ai2 = mod.attach(hub)
        self.assertIs(ai2.glasses, g)
        await asyncio.sleep(0)
        await ai2.stop()
        self.assertIsNone(mod.attach(SimpleNamespace(gw=FakeGateway())))
        self.ai = None


if __name__ == "__main__":
    unittest.main()
