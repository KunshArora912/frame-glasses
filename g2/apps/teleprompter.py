"""Teleprompt (hidden fallback): read .txt/.md scripts from <data_dir>/teleprompter on
our EvenHub page. The real Teleprompt is the firmware's own app, hosted by
g2/native_apps.py when the user picks it in the glasses menu.

Page path: 48 columns x 8 rows, swipes turn pages, long press toggles auto-scroll
(one line every N seconds; swipes change N while scrolling), tap returns to the
file list. A file whose first line is ``# native`` is handed to the firmware
teleprompter through ``NativeApps`` when ``ctx.native`` exists (DECODED flow;
any failure falls back to the page).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from g2.apps.base import App, AppContext

README = """\
Teleprompt reads .txt and .md files from this folder.

Swipe down for the next page, swipe up for the previous one.
Hold (long press) to start auto-scroll, one line every three seconds;
while it scrolls, swipe down to go faster and swipe up to go slower.
Tap to go back to the file list. Double tap leaves the app.

Put "# native" on the first line of a file to try the firmware's own
teleprompter instead of this page (experimental; it falls back here).
"""

NATIVE_FLAG = "# native"
DEFAULT_INTERVAL_S = 3
MIN_INTERVAL_S, MAX_INTERVAL_S = 1, 30
TICK_S = 0.25
HINT_LIST = "swipe move  tap open  2 taps exit"
HINT_READ = "swipe  hold auto  tap list"
HINT_SCROLL = "swipe speed  hold stop  tap list"
HINT_NATIVE = "on the glasses  tap = stop"


def load_script(path: Path) -> tuple[str, bool]:
    """(text, wants_native) with the ``# native`` header line removed."""
    text = path.read_text(encoding="utf-8", errors="replace").replace("\r", "")
    first, _, rest = text.partition("\n")
    if first.strip().lower() == NATIVE_FLAG:
        return rest, True
    return text, False


class Teleprompt(App):
    name = "Teleprompt"
    blurb = "read a script from a text file"
    order = 40
    hidden = True                 # fallback: the firmware Teleprompt is hosted by g2/native_apps.py

    def __init__(self) -> None:
        self.mode = "list"            # list | read | native
        self.files: list[Path] = []
        self.cursor = 0
        self.lines: list[str] = []
        self.top = 0                  # first visible line
        self.title = ""
        self.scrolling = False
        self.interval = DEFAULT_INTERVAL_S
        self._due = 0.0
        self._scroller: asyncio.Task | None = None
        self.native = None            # g2.native_apps.NativeApps while the firmware teleprompter runs
        self.status = ""
        self._ctx: AppContext | None = None
        self._last = ""

    # ---- files ------------------------------------------------------------
    def folder(self, ctx: AppContext) -> Path:
        d = Path(ctx.data_dir) / "teleprompter"
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            (d / "README.txt").write_text(README, encoding="utf-8")
        return d

    def scan(self, ctx: AppContext) -> list[Path]:
        d = self.folder(ctx)
        self.files = sorted([p for p in d.iterdir() if p.suffix.lower() in (".txt", ".md") and p.is_file()],
                            key=lambda p: p.name.lower())
        self.cursor = max(0, min(self.cursor, len(self.files) - 1))
        return self.files

    # ---- screens ----------------------------------------------------------
    def list_text(self, ctx: AppContext) -> str:
        rows = ctx.BODY_ROWS - 1
        if not self.files:
            body = ["no scripts yet", "", "put .txt or .md files in", str(self.folder(ctx))[:ctx.COLS]]
        else:
            top = max(0, self.cursor - rows + 1)
            body = [("> " if i == self.cursor else "  ") + p.name[:ctx.COLS - 2]
                    for i, p in enumerate(self.files) if top <= i < top + rows]
        body.append(self.status[:ctx.COLS])
        self._last = ctx.screen(self.name, body, HINT_LIST)
        return self._last

    def page_count(self, ctx: AppContext) -> int:
        return max(1, -(-len(self.lines) // ctx.BODY_ROWS))

    def read_text(self, ctx: AppContext) -> str:
        rows = ctx.BODY_ROWS
        self.top = max(0, min(self.top, max(0, len(self.lines) - 1)))
        body = self.lines[self.top:self.top + rows] or [""]
        page = self.top // rows + 1
        hint = f"{HINT_SCROLL} {self.interval}s" if self.scrolling else f"{HINT_READ}  {page}/{self.page_count(ctx)}"
        self._last = ctx.screen(self.title[:11], body, hint)
        return self._last

    def native_text(self, ctx: AppContext) -> str:
        st = self.native.status() if self.native is not None else {}
        served = st.get("served")
        body = [self.title[:ctx.COLS], "",
                "running on the firmware teleprompter",
                f"{st.get('pages', 0)} pages",
                f"served page {served}" if served is not None else "waiting for the glasses ...",
                "", "experimental: DECODED, never captured live"]
        self._last = ctx.screen(self.name, body, HINT_NATIVE)
        return self._last

    def text(self, ctx: AppContext) -> str:
        if self.mode == "read":
            return self.read_text(ctx)
        if self.mode == "native":
            return self.native_text(ctx)
        return self.list_text(ctx)

    def redraw_text(self):
        return self._last or None

    # ---- lifecycle --------------------------------------------------------
    async def open(self, ctx: AppContext) -> None:
        self._ctx = ctx
        self.mode, self.status = "list", ""
        self.scan(ctx)
        await ctx.show(self.text(ctx))

    async def touch(self, ctx: AppContext, gesture: str) -> None:
        if self.mode == "list":
            if gesture == "swipe_down":
                self.cursor = min(self.cursor + 1, max(len(self.files) - 1, 0))
            elif gesture == "swipe_up":
                self.cursor = max(self.cursor - 1, 0)
            elif gesture == "single_tap":
                if not self.files:
                    self.scan(ctx)
                else:
                    await self.open_file(ctx, self.files[self.cursor])
                    return
            else:
                return
            await ctx.show(self.text(ctx))
            return
        if self.mode == "native":
            if gesture == "single_tap":
                await self.stop_native(ctx)
                self.mode, self.status = "list", "native session stopped"
                await ctx.show(self.text(ctx))
            return
        # reading
        if gesture == "single_tap":
            await self.stop_scroll()
            self.mode = "list"
        elif gesture == "long_press_release":
            if self.scrolling:
                await self.stop_scroll()
            else:
                self.start_scroll(ctx)
        elif gesture == "swipe_down":
            if self.scrolling:
                self.set_interval(ctx, self.interval - 1)
            else:
                self.top = min(self.top + ctx.BODY_ROWS, (self.page_count(ctx) - 1) * ctx.BODY_ROWS)
        elif gesture == "swipe_up":
            if self.scrolling:
                self.set_interval(ctx, self.interval + 1)
            else:
                self.top = max(0, self.top - ctx.BODY_ROWS)
        else:
            return
        await ctx.show(self.text(ctx))

    async def open_file(self, ctx: AppContext, path: Path) -> None:
        try:
            text, wants_native = load_script(path)
        except OSError as exc:
            self.status = f"cannot read {path.name}: {type(exc).__name__}"[:ctx.COLS]
            await ctx.show(self.text(ctx))
            return
        self.title = path.stem
        self.lines = ctx.wrap(text) or [""]
        self.top = 0
        native = getattr(ctx, "native", None)
        if wants_native and native is not None:
            if await self.start_native(ctx, native, text):
                self.mode = "native"
                await ctx.show(self.text(ctx))
                return
            self.status = "native teleprompter failed; showing page"
        elif wants_native:
            self.status = "no native link; showing page"
        self.mode = "read"
        await ctx.show(self.text(ctx))

    async def close(self, ctx: AppContext) -> None:
        await self.stop_scroll()
        await self.stop_native(ctx)

    # ---- auto-scroll ------------------------------------------------------
    def set_interval(self, ctx: AppContext, seconds: int) -> None:
        self.interval = max(MIN_INTERVAL_S, min(MAX_INTERVAL_S, seconds))
        self._due = ctx.clock() + self.interval          # re-arm so a speed change takes effect now

    def start_scroll(self, ctx: AppContext) -> None:
        self.scrolling = True
        self._due = ctx.clock() + self.interval
        if self._scroller is None or self._scroller.done():
            self._scroller = asyncio.ensure_future(self._scroll_loop(ctx))

    async def stop_scroll(self) -> None:
        self.scrolling = False
        t, self._scroller = self._scroller, None
        if t is not None and not t.done():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    async def scroll_tick(self, ctx: AppContext) -> bool:
        """Advance one line when the interval has elapsed. True when the screen moved."""
        if not self.scrolling or ctx.clock() < self._due:
            return False
        self._due = ctx.clock() + self.interval
        if self.top >= len(self.lines) - 1:
            self.scrolling = False
            await ctx.show(self.text(ctx))
            return False
        self.top += 1
        await ctx.show(self.text(ctx))
        return True

    async def _scroll_loop(self, ctx: AppContext) -> None:
        try:
            while self.scrolling:
                await asyncio.sleep(TICK_S)
                await self.scroll_tick(ctx)
        except asyncio.CancelledError:
            raise
        except Exception:
            ctx.log.exception("teleprompter scroll failed")
            self.scrolling = False

    # ---- native teleprompter via g2/native_apps.py (DECODED) ----------------
    async def start_native(self, ctx: AppContext, native, text: str) -> bool:
        try:
            from g2.native_apps import NativeApps
            apps = NativeApps(native, scripts_dir=self.folder(ctx), log=ctx.log, clock=ctx.clock, now=ctx.now,
                              on_change=self._on_native_change)
            apps.attach()
            res = await apps.teleprompter_start(text, name=self.title)
            if not res.get("ok"):
                ctx.log.warning("native teleprompter: %s", res.get("detail"))
                await apps.stop()
                return False
            self.native = apps
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            ctx.log.warning("native teleprompter: %s", exc)
            return False

    async def stop_native(self, ctx: AppContext) -> None:
        apps, self.native = self.native, None
        if apps is not None:
            try:
                await apps.stop()
            except Exception as exc:
                ctx.log.warning("stopping native teleprompter: %s", exc)

    async def _on_native_change(self, what: str) -> None:
        ctx = self._ctx
        if ctx is None or self.mode != "native":
            return
        if what in ("ended", "closed"):
            await self.stop_native(ctx)
            self.mode, self.status = "list", "glasses closed the teleprompter"
        await ctx.show(self.text(ctx))
