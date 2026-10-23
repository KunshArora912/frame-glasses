"""Draw a G2 screen (layout elements or plain text) as a PNG, so screens can
be designed on the laptop before they go to the glasses.

    from g2.render import render
    render(elements).save("preview.png")

The real display is 576 x 288, one fixed proportional font, 16 green levels.
This is an approximation: Consolas at 22 px gives about 11.5 px per column
and we use a 28 px line pitch, which matches the measured 50 x 10 grid.
"""
from __future__ import annotations

from pathlib import Path

W, H = 576, 288
SCALE = 2
LINE = 28
GREEN = (120, 255, 120)
DIM = (60, 140, 60)
BG = (8, 12, 8)

_FONT_CANDIDATES = [r"C:\Windows\Fonts\consola.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"]


def _font(size: int):
    from PIL import ImageFont
    for p in _FONT_CANDIDATES:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default(size=size)


def text_to_elements(text: str) -> list[dict]:
    return [{"type": "text", "text": text, "x": 16, "y": 12, "width": 544, "height": 264,
             "padding": 0, "capture_events": True, "container_name": "main"}]


def render(elements_or_text, grid: bool = True):
    from PIL import Image, ImageDraw
    elements = text_to_elements(elements_or_text) if isinstance(elements_or_text, str) else elements_or_text
    img = Image.new("RGB", (W * SCALE, H * SCALE), BG)
    d = ImageDraw.Draw(img)
    font = _font(22 * SCALE)
    if grid:
        for x in range(0, W, 48):
            d.line([(x * SCALE, 0), (x * SCALE, H * SCALE)], fill=(20, 30, 20))
        for y in range(0, H, LINE):
            d.line([(0, y * SCALE), (W * SCALE, y * SCALE)], fill=(20, 30, 20))
    for e in elements:
        x, y = int(e.get("x", 0)), int(e.get("y", 0))
        w, h = int(e.get("width", W)), int(e.get("height", H))
        pad = int(e.get("padding", 0))
        bw = int(e.get("border_width", 0))
        if bw:
            d.rounded_rectangle([x * SCALE, y * SCALE, (x + w) * SCALE, (y + h) * SCALE],
                                radius=int(e.get("border_radius", 0)) * SCALE, outline=DIM, width=bw * SCALE)
        if e.get("type") == "image":
            d.rectangle([x * SCALE, y * SCALE, (x + w) * SCALE, (y + h) * SCALE], outline=DIM)
            d.text(((x + 6) * SCALE, (y + 4) * SCALE), "image", fill=DIM, font=font)
            continue
        cx, cy = x + pad, y + pad
        max_w = max(1, w - 2 * pad)
        lines: list[str] = []
        for para in str(e.get("text", "")).split("\n"):
            if d.textlength(para, font=font) / SCALE <= max_w:
                lines.append(para)                 # fits: keep spacing verbatim
                continue
            cur = ""
            for word in para.split(" "):
                trial = (cur + " " + word).strip()
                if d.textlength(trial, font=font) / SCALE <= max_w or not cur:
                    cur = trial
                else:
                    lines.append(cur)
                    cur = word
            lines.append(cur)
        for i, line in enumerate(lines):
            ty = cy + i * LINE
            if ty + LINE > y + h + 1:
                break
            d.text((cx * SCALE, ty * SCALE), line, fill=GREEN, font=font)
    return img


def save(elements_or_text, path: Path | str, grid: bool = True) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    render(elements_or_text, grid=grid).save(path)
    return path
