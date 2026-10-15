"""Audio out, through whatever earphones the laptop is already driving.

Frame has a microphone and no speaker, which on paper removes everything
Meta's glasses do with sound: spoken navigation, read-aloud, audible
alerts, timers you can hear, answers you listen to instead of read.

None of that actually needs a speaker IN the glasses. The laptop holds the
Bluetooth link and runs every app, so it can drive your earbuds directly
while the glasses handle the visual. The earbuds pair to Windows, not to
Frame, so Frame's single bond slot -- the constraint that shapes this whole
project -- is untouched.

That turns the display and the ear into two channels with different jobs:

    the panel    what you GLANCE at: a number, an arrow, a name
    the ear      what you'd rather not read: a long answer, a street
                 name coming up, the fact that something happened

WHY OUT OF PROCESS. Windows' speech API is COM, and bleak's WinRT backend
has already initialised a COM apartment on the asyncio thread. This project
has paid for that collision once: ctranslate2 segfaulted the whole process
for exactly this reason, and the fix was a worker process. Speech goes the
same way -- one PowerShell worker, kept warm and fed over stdin. It also
means a crash in the speech stack can never take the glasses down with it.

Measured: a process per utterance cost ~6 s for a sentence that takes ~3 s
to say. Warm, the same sentence costs 1.5 s -- the speech itself.

No pip install: System.Speech ships with Windows and runs offline, so this
works on a train with no signal.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
from datetime import datetime

from ai.display import to_ascii

# Rate is SAPI's -10..10. Navigation prompts want to be brisk; a long
# answer read aloud wants to be ordinary.
RATE_BRISK = 2
RATE_NORMAL = 0

# Anything longer than this is almost certainly a mistake -- a whole email
# read aloud is not useful, and it would block the queue for minutes.
MAX_CHARS = 600


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# A PowerShell worker that stays alive and reads work from stdin. Spawning
# one process per utterance measured at ~6 s for a sentence that takes ~3 s
# to say -- the rest was PowerShell starting up. The same warm-process trick
# the speech-to-text worker uses, for the same reason.
#
# Each job arrives as "<rate> <base64 utf-8 text>", which sidesteps every
# quoting question: no apostrophe, quote, or newline in a street name can
# break the line protocol.
_WORKER = """
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
VOICE_PICK
[Console]::Out.WriteLine('ready')
[Console]::Out.Flush()
while ($true) {
    $line = [Console]::In.ReadLine()
    if ($null -eq $line) { break }
    if ($line.Trim() -eq '') { continue }
    $parts = $line.Split(' ', 2)
    if ($parts.Length -lt 2) { continue }
    try {
        $synth.Rate = [int]$parts[0]
        $text = [Text.Encoding]::UTF8.GetString(
            [Convert]::FromBase64String($parts[1]))
        $synth.Speak($text)
    } catch { }
    [Console]::Out.WriteLine('done')
    [Console]::Out.Flush()
}
"""


def _worker_script(voice: str | None) -> str:
    pick = f"$synth.SelectVoice('{voice}')" if voice else ""
    return _WORKER.replace("VOICE_PICK", pick)


def _job(text: str, rate: int) -> bytes:
    import base64

    payload = to_ascii(text)[:MAX_CHARS].encode("utf-8")
    return f"{int(rate)} {base64.b64encode(payload).decode('ascii')}\n".encode()


class Voice:
    """Speaks queued text through the default output device.

    Serialised deliberately: two overlapping voices are unintelligible, and
    a HUD that speaks over itself is worse than one that stays quiet.
    """

    def __init__(self, enabled: bool = True, rate: int = RATE_NORMAL,
                 voice: str | None = None) -> None:
        self.enabled = enabled and shutil.which("powershell") is not None
        self.rate = rate
        self.voice = voice
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._speaking = False
        self._restart = False

    async def start(self) -> None:
        if not self.enabled or self._task is not None:
            return
        try:
            self._proc = await asyncio.create_subprocess_exec(
                "powershell", "-NoProfile", "-NonInteractive", "-Command",
                _worker_script(self.voice),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)
            # Wait for the worker to load System.Speech before claiming ready.
            await asyncio.wait_for(self._proc.stdout.readline(), timeout=20.0)
        except Exception as exc:
            log(f"voice unavailable: {type(exc).__name__}: {exc}")
            self.enabled = False
            return
        self._task = asyncio.create_task(self._pump(), name="voice")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._kill()

    async def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
                await proc.wait()

    async def say(self, text: str, *, rate: int | None = None,
                  interrupt: bool = False) -> None:
        """Queue something to speak. `interrupt` drops what is pending."""
        if not self.enabled or not (text or "").strip():
            return
        if interrupt:
            await self.shush()
        await self._queue.put((text, self.rate if rate is None else rate))

    async def shush(self) -> None:
        """Stop talking now and forget the backlog.

        SAPI gives no way to cancel a Speak() already in flight from
        outside, so an interrupt kills the worker and starts a fresh one.
        That costs the start-up again, which is why `say(interrupt=True)`
        is for genuine interruptions, not routine updates.
        """
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        if self._speaking:
            await self._kill()
            self._speaking = False
            if self.enabled:
                self._restart = True

    @property
    def busy(self) -> bool:
        return self._speaking or not self._queue.empty()

    async def _pump(self) -> None:
        while True:
            text, rate = await self._queue.get()
            if self._restart:
                self._restart = False
                self._task = None          # start() would refuse otherwise
                await self.start()
                if not self.enabled:
                    return
                self._task = asyncio.current_task()

            proc = self._proc
            if proc is None or proc.returncode is not None:
                continue
            try:
                self._speaking = True
                proc.stdin.write(_job(text, rate))
                await proc.stdin.drain()
                await proc.stdout.readline()        # 'done'
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log(f"voice failed: {type(exc).__name__}: {exc}")
                await self._kill()
                self._restart = True
            finally:
                self._speaking = False


async def list_voices() -> list[str]:
    """Installed offline voices, for choosing one."""
    proc = await asyncio.create_subprocess_exec(
        "powershell", "-NoProfile", "-NonInteractive", "-Command",
        "Add-Type -AssemblyName System.Speech;"
        "(New-Object System.Speech.Synthesis.SpeechSynthesizer)"
        ".GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name }",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    return [line.strip() for line in out.decode("utf-8", "replace").splitlines()
            if line.strip()]
