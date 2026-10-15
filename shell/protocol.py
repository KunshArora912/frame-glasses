"""What the host sends the shell, and how taps come back.

Everything the glasses can draw is one of two things: a MENU (a list with
one row marked) or a PAGE (up to seven lines). Apps never invent their own
wire format, which is why a new app needs no new Lua and no re-upload.

Strings are length-prefixed rather than delimited so a line can contain any
character -- an apostrophe in a street name or a song title used to need
escaping through a Lua literal, and that is exactly the kind of thing that
breaks once, in the field, on a name nobody tested with.
"""

from __future__ import annotations

from ai.display import to_ascii

MENU_MSG = 0x60
PAGE_MSG = 0x61
SPRITE_MSG = 0x20
CLEAR_MSG = 0x43
TAP_CTRL_MSG = 0x41
AUDIO_CTRL_MSG = 0x30
IMU_CTRL_MSG = 0x40

# Rows are 52 px apart starting at y=6, and nothing may be drawn below
# y=352, so seven rows is the hard ceiling: 6 + 6*52 = 318.
MAX_ROWS = 7
# The panel fits ~26 characters at the left margin, measured, not assumed.
ROW_CHARS = 26

FLAG_HEADING = 0x01       # first line is a heading
FLAG_EMPHASIS = 0x02      # all but the second line are dimmed


def _field(text: str, limit: int = ROW_CHARS) -> bytes:
    raw = to_ascii(text or "").strip()[:limit].encode("ascii", "ignore")
    return bytes([len(raw)]) + raw


def pack_menu(labels: list[str], selected: int = 0) -> bytes:
    """A list with one row marked. `selected` is 0-based on the host and
    1-based on the device, which is the only place that conversion lives."""
    rows = labels[:MAX_ROWS]
    index = max(0, min(selected, len(rows) - 1)) + 1 if rows else 1
    out = bytes([len(rows), index])
    for label in rows:
        out += _field(label)
    return out


def pack_page(lines: list[str], *, heading: bool = False,
              emphasis: bool = False) -> bytes:
    rows = [line for line in lines][:MAX_ROWS]
    flags = (FLAG_HEADING if heading else 0) | (FLAG_EMPHASIS if emphasis else 0)
    out = bytes([flags, len(rows)])
    for line in rows:
        out += _field(line)
    return out


def unpack_menu(raw: bytes) -> dict:
    """Mirror of the Lua parser, so tests can assert on what gets drawn."""
    count, selected = raw[0], raw[1]
    labels, pos = [], 2
    for _ in range(count):
        n = raw[pos] if pos < len(raw) else 0
        labels.append(raw[pos + 1:pos + 1 + n].decode("ascii", "replace"))
        pos += 1 + n
    return {"labels": labels, "selected": selected - 1}


def unpack_page(raw: bytes) -> dict:
    flags, count = raw[0], raw[1]
    lines, pos = [], 2
    for _ in range(count):
        n = raw[pos] if pos < len(raw) else 0
        lines.append(raw[pos + 1:pos + 1 + n].decode("ascii", "replace"))
        pos += 1 + n
    return {"lines": lines, "heading": bool(flags & FLAG_HEADING),
            "emphasis": bool(flags & FLAG_EMPHASIS)}
