"""The 'brain': question in, short answer out.

Deliberately one narrow interface, so the backend is swappable without
touching the glasses, the mic, or the renderer. A Claude Code session with
tools is a future third implementation, not a rewrite -- its 15-60 s turn
latency suits task requests, not curiosity, which is why it is not here yet.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

# The display fits roughly 6 lines of 26 characters. Everything about the
# prompt below exists to respect that -- it is the dominant design fact.
MAX_ANSWER_CHARS = 180

SYSTEM_PROMPT = f"""You answer questions shown on a pair of smart glasses.

The display is tiny: about 6 lines of 26 characters. You MUST answer in
under {MAX_ANSWER_CHARS} characters. That is roughly 30 words.

Rules:
- Lead with the answer. No preamble, no "Great question", no restating.
- Plain text only. No markdown, no bullets, no emoji.
- Specific beats hedged. If you must guess, say so in two words.
- If asked about an image, describe only what matters to the question.
- If you genuinely cannot answer briefly, give the one-line version and
  stop.

The wearer is reading this while walking around. Be useful, not chatty."""


class Brain(Protocol):
    async def ask(self, question: str, image: bytes | None = None) -> str: ...


class OfflineBrain:
    """Works with no API key, so the whole loop is testable and demoable.

    Handles a few genuinely useful local queries and is honest about the
    rest rather than pretending to think.
    """

    name = "offline"

    def __init__(self, battery_getter=None) -> None:
        self._battery = battery_getter

    async def ask(self, question: str, image: bytes | None = None) -> str:
        import datetime

        q = (question or "").lower().strip()
        if not q:
            return "didn't catch that"
        if "time" in q:
            return datetime.datetime.now().strftime("It is %H:%M.")
        if "date" in q or "day is it" in q:
            return datetime.datetime.now().strftime("%A %d %B %Y.")
        if "battery" in q and self._battery:
            try:
                return f"Battery {await self._battery():.0f}%."
            except Exception:
                return "Battery unknown."
        if "help" in q or "what can you do" in q:
            return "Ask anything. Tap=page, 2 taps=new question. Set an API key for real answers."
        if image is not None:
            return "No API key set, so I cannot see the photo."
        return "No API key set. Export ANTHROPIC_API_KEY to enable real answers."


class ClaudeBrain:
    """Anthropic API. Vision-capable, so camera questions work."""

    name = "claude"

    def __init__(self, model: str = "claude-sonnet-5",
                 api_key: str | None = None) -> None:
        from anthropic import AsyncAnthropic

        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        self._client = AsyncAnthropic(api_key=key)
        self._model = model

    async def ask(self, question: str, image: bytes | None = None) -> str:
        content: list[dict] = []
        if image is not None:
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg",
                           "data": base64.b64encode(image).decode("ascii")},
            })
        content.append({"type": "text", "text": question})

        msg = await self._client.messages.create(
            model=self._model,
            max_tokens=200,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
        )
        text = "".join(b.text for b in msg.content if b.type == "text").strip()

        # The model is told to be brief; enforce it anyway rather than
        # letting a long answer silently overflow the panel.
        if len(text) > MAX_ANSWER_CHARS * 2:
            text = text[: MAX_ANSWER_CHARS * 2].rsplit(" ", 1)[0] + "..."
        return text or "no answer"


class ClaudeCliBrain:
    """Uses the installed `claude` CLI in print mode.

    This is the no-API-key path: it goes through the official CLI and the
    user's existing subscription rather than lifting OAuth credentials out
    of the Claude Code config, which would be the wrong thing to do.

    Slower than the API (~7 s versus ~2 s) because each call is a fresh CLI
    invocation with no conversation state -- which is fine for one-shot
    curiosity questions, and is exactly why the agent backend is a separate
    idea rather than this one.
    """

    name = "claude-cli"

    def __init__(self, binary: str = "claude", timeout: float = 60.0) -> None:
        resolved = shutil.which(binary)
        if resolved is None:
            raise RuntimeError(f"{binary} not on PATH")
        # On Windows npm installs a .cmd shim. CreateProcess cannot run a
        # .cmd directly, and create_subprocess_exec does no PATHEXT
        # resolution, so route it through cmd.exe.
        self._argv0: list[str]
        if os.name == "nt" and resolved.lower().endswith((".cmd", ".bat")):
            self._argv0 = ["cmd.exe", "/c", resolved]
        else:
            self._argv0 = [resolved]
        self._timeout = timeout

    async def ask(self, question: str, image: bytes | None = None) -> str:
        prompt = question
        tmp: Path | None = None
        if image is not None:
            # The CLI can read an image off disk; hand it a path rather than
            # trying to pipe bytes through argv.
            tmp = Path(tempfile.gettempdir()) / "frame_question.jpg"
            tmp.write_bytes(image)
            prompt = (f"Look at the image at {tmp} and answer this about it, "
                      f"briefly: {question}")

        # The question goes in on stdin and the system prompt via a file, so
        # neither has to survive argv quoting through cmd.exe -- a question
        # containing & | < > would otherwise break the command line.
        sys_file = Path(tempfile.gettempdir()) / "frame_sys_prompt.txt"
        sys_file.write_text(SYSTEM_PROMPT, encoding="utf-8")

        proc = await asyncio.create_subprocess_exec(
            *self._argv0, "-p",
            "--append-system-prompt-file", str(sys_file),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(prompt.encode("utf-8")),
                timeout=self._timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("claude CLI timed out")

        text = out.decode(errors="replace").strip()
        if proc.returncode != 0 and not text:
            raise RuntimeError(
                err.decode(errors="replace").strip()[:200] or "claude failed")

        # Strip any stray markdown the model slipped in despite the prompt.
        text = text.replace("**", "").replace("`", "").strip()
        if len(text) > MAX_ANSWER_CHARS * 2:
            text = text[: MAX_ANSWER_CHARS * 2].rsplit(" ", 1)[0] + "..."
        return text or "no answer"


def make_brain(battery_getter=None, prefer: str | None = None) -> Brain:
    """Best available brain: API key > claude CLI > offline.

    `prefer` forces one of "api", "cli", "offline".
    """
    order = [prefer] if prefer else ["api", "cli", "offline"]
    for choice in order:
        if choice == "api" and os.environ.get("ANTHROPIC_API_KEY"):
            try:
                log.info("using ClaudeBrain (API)")
                return ClaudeBrain()
            except Exception as exc:
                log.warning("ClaudeBrain unavailable (%s)", exc)
        if choice == "cli":
            try:
                brain = ClaudeCliBrain()
                log.info("using ClaudeCliBrain")
                return brain
            except Exception as exc:
                log.warning("claude CLI unavailable (%s)", exc)
        if choice == "offline":
            break
    log.info("using OfflineBrain")
    return OfflineBrain(battery_getter=battery_getter)
