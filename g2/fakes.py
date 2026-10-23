"""Stand-ins for the glasses, speech and brain, used by tests and by
`python g2/dev.py preview`. Nothing here touches Bluetooth."""
from __future__ import annotations

import inspect


class FakeGateway:
    def __init__(self):
        self.handlers = {}
        self.shown: list[str] = []
        self.layouts: list[list[dict]] = []
        self.mic_calls: list[bool] = []
        self.ready = True
        self.phase = "ready"
        self.battery, self.charging, self.firmware = 96, True, "2.3.2.14"
        self.gateway_up = True
        self.last_gesture = None
        self.base = "http://fake"
        self.state = {"left": {"connected": True}, "right": {"connected": True}}

    def on(self, kind, fn):
        self.handlers.setdefault(kind, []).append(fn)

    async def emit(self, kind, *args):
        for fn in self.handlers.get(kind, []):
            r = fn(*args)
            if inspect.isawaitable(r):
                await r

    async def text(self, s):
        self.shown.append(s)
        self.layouts.append([{"type": "text", "text": s, "x": 16, "y": 12, "width": 544, "height": 264,
                              "capture_events": True, "container_name": "main"}])

    async def text_inset(self, s, name="main"):
        await self.text(s)

    async def layout(self, elements, **extra):
        self.layouts.append(elements)
        self.shown.append("\n".join(e["text"] for e in elements if e.get("type") == "text"))

    async def mic(self, on):
        self.mic_calls.append(on)

    async def status(self):
        return {"glasses": self.state}


class FakeSpeech:
    def __init__(self, gw, text="buy milk tomorrow", pcm=b"\x01\x00" * 16000):
        self.gw, self.text_out, self.pcm = gw, text, pcm
        self.available = True
        self.model = "fake"
        self.captions = None

    async def capture_utterance(self, **kw):
        await self.gw.mic(True)
        await self.gw.mic(False)
        return self.pcm

    async def transcribe(self, pcm):
        return self.text_out, -25.0

    def captioner(self, callback):
        speech = self

        class Live:
            async def start(self_inner):
                speech.captions = callback
                await speech.gw.mic(True)

            async def resume(self_inner):
                await speech.gw.mic(True)

            async def stop(self_inner):
                speech.captions = None
                await speech.gw.mic(False)
        return Live()


class FakeBrain:
    def __init__(self, answer="The answer is forty-two. " * 12):
        self.answer = answer

    async def ask(self, q, image=None):
        return self.answer


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t
