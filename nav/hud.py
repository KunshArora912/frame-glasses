"""Packing a Guidance into the one message the glasses draw.

The obvious way to put five lines on the panel is five TxPlainText
messages, the way the assistant does it. That is wrong here: each one
triggers its own redraw, so a once-a-second HUD would visibly tear five
times a second, and five round trips per update is five times the link
traffic for data that is all known at the same instant.

So the whole HUD goes over as ONE packet -- a flag byte and five
length-prefixed strings -- and the device draws it in a single frame.
Length prefixes rather than a delimiter mean a street name can contain any
character at all without escaping.

`unpack_nav` exists so the Lua parser's behaviour can be checked from a
test rather than by squinting at the glasses.
"""

from __future__ import annotations

from ai.display import CHARS_PER_LINE, to_ascii
from nav.guidance import Guidance, distance_text, duration_text

# How the icon reads in the "then ..." preview line.
_ICON_WORD = {
    "depart": "start", "straight": "straight", "slight-left": "bear left",
    "left": "left", "sharp-left": "sharp left", "slight-right": "bear right",
    "right": "right", "sharp-right": "sharp right", "uturn": "u-turn",
    "roundabout": "roundabout", "arrive": "arrive", "steps": "stairs",
}


# CHARS_PER_LINE (26) is calibrated for text starting at LEFT_MARGIN = 14.
# The distance and street sit at TEXT_X = 150 to clear the arrow, which
# leaves 490 px rather than 626 -- about 20 glyphs. The per-glyph fit check
# fails SILENTLY, so an over-long street name simply loses its tail.
BESIDE_ARROW_CHARS = 20


def _field(text: str, limit: int = CHARS_PER_LINE) -> bytes:
    """One length-prefixed ASCII field, trimmed to what the panel can show."""
    raw = to_ascii(text or "").strip()[:limit].encode("ascii", "ignore")
    return bytes([len(raw)]) + raw


def pack_nav(distance: str, street: str, next_line: str, remaining: str,
             status: str, *, off_route: bool = False, arrived: bool = False,
             stale: bool = False, imminent: bool = False) -> bytes:
    flags = ((0x01 if off_route else 0) | (0x02 if arrived else 0)
             | (0x04 if stale else 0) | (0x08 if imminent else 0))
    return (bytes([flags])
            + _field(distance, BESIDE_ARROW_CHARS)
            + _field(street, BESIDE_ARROW_CHARS)
            + _field(next_line) + _field(remaining) + _field(status))


def unpack_nav(raw: bytes) -> dict:
    """Mirror of the Lua parser, so tests can assert on what gets drawn."""
    flags = raw[0]
    fields, pos = [], 1
    for _ in range(5):
        n = raw[pos] if pos < len(raw) else 0
        fields.append(raw[pos + 1:pos + 1 + n].decode("ascii", "replace"))
        pos += 1 + n
    return {
        "off_route": bool(flags & 0x01), "arrived": bool(flags & 0x02),
        "stale": bool(flags & 0x04), "imminent": bool(flags & 0x08),
        "distance": fields[0], "street": fields[1], "next_line": fields[2],
        "remaining": fields[3], "status": fields[4],
    }


def next_line(guidance: Guidance) -> str:
    if not guidance.next_icon or guidance.arrived:
        return ""
    word = _ICON_WORD.get(guidance.next_icon, guidance.next_icon)
    if guidance.next_street:
        return f"then {word}: {guidance.next_street}"
    return f"then {word}"


def remaining_line(guidance: Guidance, battery: int | None = None) -> str:
    # distance_text says "now" under 20 m, which is right beside an arrow
    # ("turn now") and wrong on the trip line ("now  <1 min").
    left = (f"{int(guidance.remaining_m)} m" if guidance.remaining_m < 20
            else distance_text(guidance.remaining_m))
    parts = [left,
             duration_text(guidance.remaining_s)]
    if battery is not None:
        parts.append(f"{battery}%")
    return "  ".join(parts)


def status_line(guidance: Guidance, rerouting: bool = False,
                no_fix: bool = False) -> str:
    if no_fix:
        return "waiting for GPS"
    if rerouting:
        return "rerouting..."
    if guidance.off_route:
        return "off route"
    if guidance.stale:
        return "GPS signal lost"
    if guidance.arrived:
        return "arrived"
    return ""


def from_guidance(guidance: Guidance, *, battery: int | None = None,
                  rerouting: bool = False, no_fix: bool = False) -> bytes:
    """The packet for this instant."""
    return pack_nav(
        distance="" if guidance.arrived else distance_text(guidance.distance_m),
        street=guidance.street,
        next_line=next_line(guidance),
        remaining=remaining_line(guidance, battery),
        status=status_line(guidance, rerouting=rerouting, no_fix=no_fix),
        off_route=guidance.off_route, arrived=guidance.arrived,
        stale=guidance.stale, imminent=guidance.imminent,
    )
