"""Turn-arrow sprites for the Frame display.

Generated with PIL rather than shipped as assets, so there are no binary
files to manage and the palette is guaranteed to match what the glasses can
show. Each arrow is a 2-colour indexed PNG, which packs to 1 bit per pixel --
about 1 kB on the wire, negligible against the ~25 kB/s link.

Shapes are deliberately chunky: the panel is 20 degrees of FOV and the
wearer is glancing, not studying.

The 2-colour palette is load-bearing, not just a bandwidth choice:
`sprite.set_palette` assigns palette entries 1..num_colors, so two colours
touch only VOID and WHITE. A 4-colour arrow would overwrite GREY, and a
16-colour one would overwrite every named colour the HUD text draws with.

Everything is drawn on a canvas twice the final size and then fitted to the
sprite box by its actual ink. Drawing straight into a 128 px box silently
cut the heads off `left`, `sharp-left`, `right` and `sharp-right` -- a
sharp-left arrow reached x = -3 -- and fitting afterwards also makes every
icon fill the same area, so arrows do not visibly change size from one turn
to the next.
"""

from __future__ import annotations

import io
import math

ICONS = ("depart", "straight", "slight-left", "left", "sharp-left",
         "slight-right", "right", "sharp-right", "uturn", "roundabout",
         "arrive", "steps")

SIZE = 128
PAD = SIZE // 2          # room for a head that swings outside the box
MARGIN = 4               # breathing room inside the finished sprite
_cache: dict[str, bytes] = {}


class _Shifted:
    """ImageDraw with every coordinate moved into the padded canvas."""

    def __init__(self, draw, offset: int) -> None:
        self._d, self._o = draw, offset

    def _pts(self, pts):
        return [(x + self._o, y + self._o) for x, y in pts]

    def _box(self, box):
        return [box[0] + self._o, box[1] + self._o,
                box[2] + self._o, box[3] + self._o]

    def line(self, pts, **kw):
        self._d.line(self._pts(pts), **kw)

    def polygon(self, pts, **kw):
        self._d.polygon(self._pts(pts), **kw)

    def ellipse(self, box, **kw):
        self._d.ellipse(self._box(box), **kw)

    def arc(self, box, **kw):
        self._d.arc(self._box(box), **kw)

    def rectangle(self, box, **kw):
        self._d.rectangle(self._box(box), **kw)


def _blank():
    from PIL import Image, ImageDraw

    img = Image.new("P", (SIZE + 2 * PAD, SIZE + 2 * PAD), 0)
    # index 0 = black (VOID), index 1 = white: a 2-colour sprite
    img.putpalette([0, 0, 0, 255, 255, 255] + [0] * 762)
    return img, _Shifted(ImageDraw.Draw(img), PAD)


def _fit(padded):
    """Crop to the ink and scale it into a SIZE x SIZE sprite, centred."""
    from PIL import Image

    box = padded.getbbox()                     # ink only; index 0 is black
    if box is None:
        return padded.crop((PAD, PAD, PAD + SIZE, PAD + SIZE))

    ink = padded.crop(box)
    # Scale UP as well as down: a squarish arrow like `left` is naturally
    # smaller than a tall one like `straight`, and an arrow that changes
    # size between turns reads as a glitch rather than a direction.
    room = SIZE - 2 * MARGIN
    scale = min(room / ink.width, room / ink.height)
    ink = ink.resize((max(1, round(ink.width * scale)),
                      max(1, round(ink.height * scale))),
                     Image.Resampling.NEAREST)

    out = Image.new("P", (SIZE, SIZE), 0)
    out.putpalette([0, 0, 0, 255, 255, 255] + [0] * 762)
    out.paste(ink, ((SIZE - ink.width) // 2, (SIZE - ink.height) // 2))
    return out


def _arrow(draw, angle_deg: float, *, width: int = 20, head: int = 34) -> None:
    """A shaft from the bottom centre turning to `angle_deg`, with a head.

    0 = straight up, negative = left, positive = right.
    """
    cx, cy = SIZE / 2, SIZE - 14
    turn_y = SIZE * 0.52
    draw.line([(cx, cy), (cx, turn_y)], fill=1, width=width)

    rad = math.radians(angle_deg)
    length = SIZE * 0.34
    ex = cx + math.sin(rad) * length
    ey = turn_y - math.cos(rad) * length
    draw.line([(cx, turn_y), (ex, ey)], fill=1, width=width)

    # triangular head, perpendicular to the direction of travel
    hx, hy = math.sin(rad), -math.cos(rad)
    px, py = -hy, hx
    tip = (ex + hx * head * 0.9, ey + hy * head * 0.9)
    left = (ex + px * head * 0.62, ey + py * head * 0.62)
    right = (ex - px * head * 0.62, ey - py * head * 0.62)
    draw.polygon([tip, left, right], fill=1)


def _draw(icon: str):
    img, draw = _blank()

    if icon in ("straight", "depart"):
        _arrow(draw, 0)
    elif icon == "slight-left":
        _arrow(draw, -32)
    elif icon == "left":
        _arrow(draw, -70)
    elif icon == "sharp-left":
        _arrow(draw, -115)
    elif icon == "slight-right":
        _arrow(draw, 32)
    elif icon == "right":
        _arrow(draw, 70)
    elif icon == "sharp-right":
        _arrow(draw, 115)
    elif icon == "uturn":
        cx, cy = SIZE / 2, SIZE - 14
        draw.line([(cx + 22, cy), (cx + 22, SIZE * 0.42)], fill=1, width=18)
        draw.arc([cx - 22, SIZE * 0.20, cx + 22, SIZE * 0.64],
                 start=180, end=360, fill=1, width=18)
        draw.line([(cx - 22, SIZE * 0.42), (cx - 22, SIZE * 0.70)],
                  fill=1, width=18)
        draw.polygon([(cx - 22, SIZE * 0.86), (cx - 44, SIZE * 0.62),
                      (cx, SIZE * 0.62)], fill=1)
    elif icon == "roundabout":
        draw.ellipse([SIZE * 0.26, SIZE * 0.20, SIZE * 0.74, SIZE * 0.68],
                     outline=1, width=16)
        draw.line([(SIZE / 2, SIZE - 14), (SIZE / 2, SIZE * 0.70)],
                  fill=1, width=18)
        draw.polygon([(SIZE * 0.92, SIZE * 0.44), (SIZE * 0.70, SIZE * 0.26),
                      (SIZE * 0.70, SIZE * 0.62)], fill=1)
    elif icon == "arrive":
        # a flag, which reads faster than a chequerboard at this size
        draw.line([(SIZE * 0.34, SIZE - 14), (SIZE * 0.34, SIZE * 0.16)],
                  fill=1, width=14)
        draw.polygon([(SIZE * 0.34, SIZE * 0.16), (SIZE * 0.86, SIZE * 0.30),
                      (SIZE * 0.34, SIZE * 0.48)], fill=1)
    elif icon == "steps":
        x, y = SIZE * 0.18, SIZE * 0.82
        for _ in range(4):
            draw.rectangle([x, y, x + SIZE * 0.17, y + SIZE * 0.10], fill=1)
            x += SIZE * 0.17
            y -= SIZE * 0.17
    else:
        _arrow(draw, 0)

    return _fit(img)


def png(icon: str) -> bytes:
    """Indexed 2-colour PNG for an icon name. Cached."""
    if icon not in _cache:
        buf = io.BytesIO()
        _draw(icon).save(buf, format="PNG", optimize=False)
        _cache[icon] = buf.getvalue()
    return _cache[icon]


def sprite(icon: str):
    """TxSprite ready to send to the glasses."""
    from brilliant_msg import TxSprite

    return TxSprite.from_indexed_png_bytes(png(icon))
