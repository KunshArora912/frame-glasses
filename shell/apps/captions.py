"""What people around you are saying, on the glasses, as they say it.

This is the app with the highest human value on the list and the only big
thing Meta's glasses do that does not need the speaker Frame lacks. It also
plays to this hardware's single strongest subsystem: the microphone sustains
16 kHz/8-bit at 100% of realtime with no dropouts, measured.

The design question is not transcription, it is what to show. Six rows of
26 characters is about 25 words -- roughly ten seconds of speech. So:

  * the newest line is bright and the older ones dim, because the eye should
    land on what is being said now without hunting
  * lines never reflow once drawn. A caption that re-wraps as more words
    arrive is unreadable; each line is fixed the moment it is full
  * a tap FREEZES the view, which is the "wait, what did they just say"
    feature. Tap again to catch up. Nothing is lost while frozen -- the
    transcript keeps filling behind it

Translation is this same app with one extra step, which is why the
transcript is kept whole rather than only as display rows.
"""

from __future__ import annotations

from ai.display import wrap
from shell.app import App
from shell.gestures import Gesture
from shell.protocol import MAX_ROWS, ROW_CHARS

# Rows of caption, leaving the last row for status.
VISIBLE_ROWS = MAX_ROWS - 1


class Captions(App):
    def __init__(self, listener_factory=None) -> None:
        super().__init__(name="captions", label="Captions", tick_seconds=0.0)
        self.listener_factory = listener_factory
        self.listener = None
        self.rows: list[str] = []
        self.pending = ""
        self.frozen = False
        self.started = False
        self.notice = ""        # a problem to report, kept out of the text
        self._held: list[str] = []   # the rows on screen while frozen

    # ------------------------------------------------------------ the text

    def add_words(self, text: str) -> None:
        """Append words, closing each row once it is full.

        Rows are closed rather than reflowed: a caption whose earlier lines
        shuffle as new words arrive cannot be read while it moves.
        """
        self.pending = f"{self.pending} {text}".strip()
        if not self.pending:
            return
        lines = wrap(self.pending, ROW_CHARS)
        # Everything except the last line is final; the last stays open so
        # the next few words can join it.
        if len(lines) > 1:
            self.rows.extend(lines[:-1])
            self.pending = lines[-1]
        # Keep only what can be shown, plus a little history for freezing.
        if len(self.rows) > VISIBLE_ROWS * 3:
            self.rows = self.rows[-VISIBLE_ROWS * 3:]

    def _live_rows(self) -> list[str]:
        lines = self.rows + ([self.pending] if self.pending else [])
        shown = lines[-VISIBLE_ROWS:] if lines else []
        # Pad at the TOP so text grows downward from a stable position
        # rather than jumping up the panel as the first lines arrive.
        return [""] * (VISIBLE_ROWS - len(shown)) + shown

    def view(self) -> list[str]:
        if self.notice:
            return [""] * (VISIBLE_ROWS - 1) + [self.notice, ""]
        if self.frozen:
            # A snapshot, deliberately. Suppressing redraws alone looked
            # the same on the glasses, but any other caller of draw() would
            # have leaked new words onto a panel claiming to be frozen.
            return self._held + ["FROZEN  tap to resume"]
        rows = self._live_rows()
        status = ("listening   tap to hold" if self.started
                  else "starting microphone...")
        return rows + [status]

    async def draw(self) -> None:
        await self.screen.page(self.view())

    # -------------------------------------------------------------- hooks

    async def on_heard(self, text: str) -> None:
        self.add_words(text)
        if not self.frozen:
            await self.draw()

    async def start(self) -> None:
        self.rows, self.pending, self.notice = [], "", ""
        self.frozen = False
        self.started = False
        await self.draw()

        shell = self.context.get("shell")
        if shell is None or self.listener_factory is None:
            self.notice = "no microphone here"
            await self.draw()
            return

        self.listener = await self.listener_factory()
        if self.listener is None:
            self.notice = "speech unavailable"
            await self.draw()
            return
        await self.listener.start(self.on_heard)
        self.started = await shell.start_audio(self.listener)
        if not self.started:
            self.notice = "mic would not open"
        await self.draw()

    async def stop(self) -> None:
        shell = self.context.get("shell")
        if shell is not None:
            await shell.stop_audio()
        self.listener = None
        self.started = False

    async def gesture(self, gesture: Gesture) -> bool:
        if gesture in (Gesture.NEXT, Gesture.SELECT):
            # Freeze is the "what did they just say" gesture. The transcript
            # keeps filling behind it, so resuming catches up rather than
            # showing a gap.
            self.frozen = not self.frozen
            if self.frozen:
                self._held = self._live_rows()
            await self.draw()
        return True

    @property
    def transcript(self) -> str:
        return " ".join(self.rows + ([self.pending] if self.pending else []))
