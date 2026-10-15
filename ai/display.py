"""Rendering text onto Frame's 640x400 display.

Pure logic: no Bluetooth, no device. Takes text, produces the lines and pages
that fit the panel, so this is unit-testable without hardware.

Panel facts that drive every number here, all measured rather than assumed:
  * 640x400 with a variable-width font whose glyphs are 48 px tall
  * the per-glyph fit check is `y + 48 <= 400` and it fails SILENTLY, so
    anything below y=352 simply vanishes with no error
  * the display is double-buffered and the FPGA clears the write buffer on
    every show(), so a frame must be redrawn in full every time
"""

from __future__ import annotations

from dataclasses import dataclass

GLYPH_HEIGHT = 48
PANEL_W = 640
PANEL_H = 400
MAX_Y = PANEL_H - GLYPH_HEIGHT          # 352: below this, text disappears

LINE_SPACING = 52
TOP_MARGIN = 6
LEFT_MARGIN = 14

# Variable-width font, so this is a conservative character budget rather than
# an exact fit. Measured against real strings on the panel.
CHARS_PER_LINE = 26
LINES_PER_PAGE = 6


@dataclass(frozen=True)
class Page:
    lines: list[str]
    index: int          # 0-based
    total: int

    @property
    def is_last(self) -> bool:
        return self.index == self.total - 1


# Frame's bitmap font is ASCII. Language models write em-dashes, curly
# quotes and ellipses constantly, and those render as garbage on the panel,
# so fold them to ASCII before anything reaches the glasses.
_ASCII_FOLD = {
    "—": "-", "–": "-", "−": "-",      # em/en dash, minus
    "‘": "'", "’": "'", "‚": "'",      # curly single quotes
    "“": '"', "”": '"', "„": '"',      # curly double quotes
    "…": "...",                                   # ellipsis
    " ": " ", " ": " ", "​": "",        # exotic spaces
    "°": " deg", "×": "x", "→": "->",
    "•": "-", "«": '"', "»": '"',
}


def to_ascii(text: str) -> str:
    """Fold typographic characters to ASCII; drop anything else non-ASCII."""
    out = []
    for ch in text or "":
        if ch in _ASCII_FOLD:
            out.append(_ASCII_FOLD[ch])
        elif ord(ch) < 128:
            out.append(ch)
        else:
            # Last resort: strip accents (cafe<-café), else drop the char.
            import unicodedata
            decomposed = unicodedata.normalize("NFKD", ch)
            stripped = "".join(c for c in decomposed if ord(c) < 128)
            out.append(stripped)
    return "".join(out)


def wrap(text: str, width: int = CHARS_PER_LINE) -> list[str]:
    """Word-wrap, breaking over-long words rather than letting them vanish."""
    out: list[str] = []
    for paragraph in to_ascii(text).replace("\r", "").split("\n"):
        if not paragraph.strip():
            out.append("")
            continue
        line = ""
        for word in paragraph.split():
            while len(word) > width:                 # hard-break long words
                if line:
                    out.append(line)
                    line = ""
                out.append(word[:width - 1] + "-")
                word = word[width - 1:]
            if not line:
                line = word
            elif len(line) + 1 + len(word) <= width:
                line += " " + word
            else:
                out.append(line)
                line = word
        if line:
            out.append(line)
    return out or [""]


def paginate(text: str, lines_per_page: int = LINES_PER_PAGE) -> list[Page]:
    """Split wrapped text into screens that fit the panel."""
    lines = wrap(text)
    chunks = [lines[i:i + lines_per_page]
              for i in range(0, len(lines), lines_per_page)] or [[""]]
    return [Page(lines=c, index=i, total=len(chunks))
            for i, c in enumerate(chunks)]


def _escape(s: str) -> str:
    """Make a string safe inside a single-quoted Lua literal."""
    return (s.replace("\\", "\\\\")
             .replace("'", "\\'")
             .replace("\n", " ")
             .replace("\r", " "))


def page_to_lua(page: Page, header: str | None = None,
                colour: str = "WHITE") -> str:
    """Build the Lua that draws one page, as a single show()ed frame.

    Returns one chunk of Lua. The caller is responsible for keeping it under
    the device's Lua payload limit -- see `draw_calls` for the split version.
    """
    return ";".join(draw_calls(page, header, colour)) + ";frame.display.show()"


def draw_calls(page: Page, header: str | None = None,
               colour: str = "WHITE") -> list[str]:
    """Individual Lua draw statements, so a caller can send them in batches.

    Does NOT include show(); the caller issues that once at the end.
    """
    calls: list[str] = []
    y = TOP_MARGIN

    if header:
        calls.append(
            f"frame.display.text('{_escape(header[:CHARS_PER_LINE])}',"
            f"{LEFT_MARGIN},{y},{{color='SKYBLUE'}})")
        y += LINE_SPACING

    for line in page.lines:
        if not line:
            y += LINE_SPACING
            continue
        if y > MAX_Y:                 # would silently vanish -- stop instead
            break
        calls.append(
            f"frame.display.text('{_escape(line)}',{LEFT_MARGIN},{y},"
            f"{{color='{colour}'}})")
        y += LINE_SPACING

    if page.total > 1:
        marker = f"{page.index + 1}/{page.total}"
        calls.append(
            f"frame.display.text('{marker}',560,{MAX_Y},{{color='GREY'}})")

    return calls


def status_lua(message: str, colour: str = "WHITE") -> str:
    """A single centred-ish status line, for transient states."""
    lines = wrap(message, CHARS_PER_LINE)[:3]
    calls = []
    y = 140
    for line in lines:
        calls.append(
            f"frame.display.text('{_escape(line)}',{LEFT_MARGIN},{y},"
            f"{{color='{colour}'}})")
        y += LINE_SPACING
    return ";".join(calls) + ";frame.display.show()"


def blank_lua() -> str:
    return "frame.display.text(' ',1,1);frame.display.show()"
