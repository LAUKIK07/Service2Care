"""Image cards sent over iMessage, drawn with Pillow. Large type and strong contrast so they are easy to read.

Everything on a card comes from code (numbers, text, curves). No model draws or words anything here.
Cards are drawn at 2x and scaled down for smooth edges.
"""
from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W = 1080
S = 2                      # supersampling
INK = (34, 34, 34)
GRAY = (106, 106, 106)
LINE = (235, 235, 235)
BG = (247, 247, 248)
BRAND = (255, 56, 92)
WHITE = (255, 255, 255)

_FONT_CANDIDATES = {
    False: ["/System/Library/Fonts/Supplemental/Arial.ttf", "/Library/Fonts/Arial.ttf", "/System/Library/Fonts/Helvetica.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
    True: ["/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/Library/Fonts/Arial Bold.ttf", "/System/Library/Fonts/Helvetica.ttc",
           "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
}
_cache: dict[tuple[int, bool], ImageFont.FreeTypeFont] = {}


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    key = (size, bold)
    if key not in _cache:
        for path in _FONT_CANDIDATES[bold]:
            if Path(path).exists():
                try:
                    f = ImageFont.truetype(path, size * S, index=1 if (bold and path.endswith(".ttc")) else 0)
                except OSError:
                    continue
                _cache[key] = f
                break
        else:
            _cache[key] = ImageFont.load_default()
    return _cache[key]


def _wrap(draw: ImageDraw.ImageDraw, text: str, f, width: int) -> list[str]:
    lines: list[str] = []
    for para in text.split("\n"):
        words, cur = para.split(" "), ""
        for w in words:
            trial = (cur + " " + w).strip()
            if draw.textlength(trial, font=f) <= width * S or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
    return lines


class _Canvas:
    def __init__(self, height: int, bg=BG):
        self.h = height
        self.img = Image.new("RGB", (W * S, height * S), bg)
        self.d = ImageDraw.Draw(self.img)

    def rect(self, box, r=0, fill=WHITE, outline=None, width=1):
        x0, y0, x1, y1 = [v * S for v in box]
        if r:
            self.d.rounded_rectangle((x0, y0, x1, y1), radius=r * S, fill=fill, outline=outline, width=width * S)
        else:
            self.d.rectangle((x0, y0, x1, y1), fill=fill, outline=outline, width=width * S)

    def text(self, xy, s, size, bold=False, fill=INK, anchor="la"):
        self.d.text((xy[0] * S, xy[1] * S), s, font=font(size, bold), fill=fill, anchor=anchor)

    def para(self, x, y, s, size, width, bold=False, fill=INK, gap=1.35) -> int:
        f = font(size, bold)
        for line in _wrap(self.d, s, f, width):
            self.d.text((x * S, y * S), line, font=f, fill=fill)
            y += int(size * gap)
        return y

    def height_of(self, s, size, width, bold=False, gap=1.35) -> int:
        return int(len(_wrap(self.d, s, font(size, bold), width)) * size * gap)

    def line(self, pts, fill, width=4):
        self.d.line([(x * S, y * S) for x, y in pts], fill=fill, width=width * S, joint="curve")

    def poly(self, pts, fill):
        self.d.polygon([(x * S, y * S) for x, y in pts], fill=fill)

    def circle(self, cx, cy, r, fill, outline=None, width=0):
        self.d.ellipse(((cx - r) * S, (cy - r) * S, (cx + r) * S, (cy + r) * S), fill=fill, outline=outline, width=width * S)

    def png(self) -> bytes:
        out = self.img.resize((W, self.h), Image.LANCZOS)
        buf = io.BytesIO()
        out.save(buf, "PNG", optimize=True)
        return buf.getvalue()


def _header(c: _Canvas, tag: str = "Demo data") -> int:
    c.circle(84, 86, 24, BRAND)
    c.line([(72, 88), (79, 88), (84, 76), (89, 98), (94, 88), (98, 88)], WHITE, 4)
    c.text((124, 66), "Signal2Care", 38, True, BRAND)
    tw = c.d.textlength(tag, font=font(28)) / S
    c.rect((W - 60 - tw - 36, 62, W - 60, 110), 24, fill=(238, 238, 238))
    c.text((W - 60 - tw - 18, 70), tag, 28, False, GRAY)
    return 140


def _chart(c: _Canvas, x0, y0, x1, y1, values, band, color, mark_from=None, mark_to=None, labels=("48 hours ago", "now")):
    """A line with the person's usual range as a soft band. values/band share one index; None means no data."""
    finite = [v for v in values if v is not None]
    if len(finite) < 2:
        c.text((x0, (y0 + y1) / 2), "Not enough data to draw yet", 32, False, GRAY)
        return
    lo, hi = min(finite), max(finite)
    for b in band or []:
        if b:
            lo, hi = min(lo, b[0]), max(hi, b[1])
    pad = (hi - lo or 1) * 0.12
    lo, hi = lo - pad, hi + pad
    n = len(values)
    X = lambda i: x0 + (x1 - x0) * i / (n - 1)
    Y = lambda v: y1 - (y1 - y0) * (v - lo) / (hi - lo)
    for k in range(4):                                               # light grid
        yy = y0 + (y1 - y0) * k / 3
        c.line([(x0, yy), (x1, yy)], LINE, 2)
    if mark_from is not None and mark_to is not None:                # the window we are asking about
        c.rect((X(mark_from), y0, X(max(mark_to, mark_from + 1)), y1), 0, fill=(255, 232, 237))
    if band and sum(1 for b in band if b) > 2:
        top = [(X(i), Y(b[1])) for i, b in enumerate(band) if b]
        bot = [(X(i), Y(b[0])) for i, b in enumerate(band) if b]
        tint = tuple(int(255 - (255 - ch) * 0.16) for ch in color)
        c.poly(top + bot[::-1], tint)
    run: list[tuple[float, float]] = []
    for i, v in enumerate(values + [None]):
        if v is None:
            if len(run) > 1:
                c.line(run, color, 6)
            run = []
        else:
            run.append((X(i), Y(v)))
    last = max(i for i, v in enumerate(values) if v is not None)
    c.circle(X(last), Y(values[last]), 13, WHITE, color, 6)
    c.text((x0, y1 + 14), labels[0], 26, False, GRAY)
    c.text((x1, y1 + 14), labels[1], 26, False, GRAY, anchor="ra")


def metric_card(*, label: str, value: str, unit: str, status_text: str, status_color=BRAND, usual: str | None = None,
                values: list | None = None, band: list | None = None, color=BRAND, mark: tuple[int, int] | None = None,
                when: str = "", footer: str = "", tag: str = "Demo data") -> bytes:
    """The picture that goes with an alert: big number, plain status, and a chart of the last two days."""
    foot_lines = footer or "Compared with your own usual range at this time of day."
    foot_h = _Canvas(10).height_of(foot_lines, 28, W - 120)
    total = 140 + 640 + 80 + 22 + foot_h + 70                 # header, chart block, gap, footer, margin
    c = _Canvas(total)
    c.rect((30, 30, W - 30, total - 30), 44, fill=WHITE)
    y = _header(c, tag)
    c.text((60, y + 10), label, 52, True)
    c.text((60, y + 78), status_text, 38, True, status_color)
    c.text((60, y + 150), value, 150, True)
    vw = c.d.textlength(value, font=font(150, True)) / S
    c.text((60 + vw + 20, y + 252), unit, 44, False, GRAY)
    if usual:
        tw = c.d.textlength(usual, font=font(32, True)) / S
        c.rect((W - 60 - tw - 44, y + 168, W - 60, y + 232), 32, fill=(240, 240, 241))
        c.text((W - 60 - tw - 22, y + 180), usual, 32, True, INK)
    cy0, cy1 = y + 360, y + 640
    _chart(c, 70, cy0, W - 70, cy1, values or [], band, color, mark[0] if mark else None, mark[1] if mark else None)
    if mark:
        c.text((W - 70, cy0 - 44), "Pink area: the time I am asking about", 26, False, GRAY, anchor="ra")
    if when:
        c.text((70, cy0 - 44), when, 28, True, GRAY)
    yy = cy1 + 80
    c.rect((60, yy, W - 60, yy + 2), 0, fill=LINE)
    c.para(60, yy + 22, foot_lines, 28, W - 120, False, GRAY)
    return c.png()


def story_card(*, title: str, when: str, rows: list[tuple[str, str]], note: str = "", event_id: str = "", tag: str = "Saved to your record") -> bytes:
    """A short, labelled timeline: what I noticed, what you told me, what the numbers show, what happens next."""
    c0 = _Canvas(3000)                                       # measuring pass
    inner = W - 160
    h = 250
    for head, body in rows:
        h += 56 + c0.height_of(body, 34, inner - 30) + 34
    h += (c0.height_of(note, 28, W - 120) + 40) if note else 0
    h += 110
    c = _Canvas(h)
    c.rect((30, 30, W - 30, h - 30), 44, fill=WHITE)
    y = _header(c, tag)
    c.text((60, y + 6), title, 50, True)
    c.text((60, y + 70), when, 30, False, GRAY)
    y += 140
    for i, (head, body) in enumerate(rows):
        c.circle(88, y + 22, 16, BRAND if i == 0 else (34, 34, 34))
        if i < len(rows) - 1:
            c.rect((86, y + 42, 90, y + 56 + c.height_of(body, 34, inner - 30) + 34), 0, fill=LINE)
        c.text((130, y), head, 34, True)
        y = c.para(130, y + 48, body, 34, inner - 30, False, INK) + 34
    if note:
        y = c.para(60, y + 10, note, 28, W - 120, False, GRAY)
    if event_id:
        c.text((60, h - 78), f"Event {event_id}", 24, False, (160, 160, 160))
    return c.png()


def profile_card(*, name: str, items: list[tuple[str, str]], note: str = "") -> bytes:
    c0 = _Canvas(3000)
    h = 260
    for k, v in items:
        h += 44 + c0.height_of(v, 34, W - 160) + 30
    h += (c0.height_of(note, 28, W - 120) + 30) if note else 0
    h += 70
    c = _Canvas(h)
    c.rect((30, 30, W - 30, h - 30), 44, fill=WHITE)
    y = _header(c, "Your record")
    c.text((60, y + 6), f"{name}'s health notes" if name else "Your health notes", 50, True)
    y += 110
    for k, v in items:
        c.text((60, y), k, 28, True, GRAY)
        y = c.para(60, y + 40, v, 34, W - 160, False, INK) + 30
    if note:
        c.para(60, y + 6, note, 28, W - 120, False, GRAY)
    return c.png()


def day_card(*, title: str, when: str, tiles: list[tuple[str, str, str]], note: str = "") -> bytes:
    """Up to six tiles: (label, value, small line). For 'show me my day'."""
    rows = (len(tiles) + 1) // 2
    h = 250 + rows * 250 + (110 if note else 40)
    c = _Canvas(h)
    c.rect((30, 30, W - 30, h - 30), 44, fill=WHITE)
    y = _header(c, "Demo data")
    c.text((60, y + 6), title, 50, True)
    c.text((60, y + 70), when, 30, False, GRAY)
    y += 140
    cw = (W - 180) / 2
    for i, (label, value, small) in enumerate(tiles[:6]):
        col, row = i % 2, i // 2
        x0, y0 = 60 + col * (cw + 60), y + row * 250
        c.rect((x0, y0, x0 + cw, y0 + 220), 28, fill=(247, 247, 248))
        c.text((x0 + 28, y0 + 22), label, 28, True, GRAY)
        c.text((x0 + 28, y0 + 64), value, 64, True)
        c.text((x0 + 28, y0 + 152), small, 28, False, GRAY)
    if note:
        c.para(60, y + rows * 250 + 10, note, 28, W - 120, False, GRAY)
    return c.png()
