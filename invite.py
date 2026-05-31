"""Invitation image rendering for ПОЛЯНА.

Two modes share one text-overlay engine:
  • render_typographic(event, theme) — free tier, pure Pillow gradient, no AI.
  • render_on_background(bg_png, event, theme) — AI background + same text overlay.

Vertical 9:16 output (default 1080x1920 for the free template; for AI mode we
render the overlay at the background's native size).
"""

from __future__ import annotations

import os
import io
from PIL import Image, ImageDraw, ImageFont, ImageFilter

_HERE = os.path.dirname(os.path.abspath(__file__))
_FONT_DIR = os.path.join(_HERE, "assets", "fonts")
_FONT_TITLE = os.path.join(_FONT_DIR, "Montserrat.ttf")      # variable
_FONT_BOLD = os.path.join(_FONT_DIR, "PTSans-Bold.ttf")
_FONT_REG = os.path.join(_FONT_DIR, "PTSans-Regular.ttf")

BRAND = "Создано в ПОЛЯНА"

# ── Themes ────────────────────────────────────────────────────────────────────
# prompt    — fed to the image model (event text is NEVER injected here)
# top/bottom — gradient colors for the free typographic template (RGB)
# accent    — accent line / kicker color
THEMES: dict[str, dict] = {
    "шашлык": {
        "emoji": "🔥",
        "prompt": ("warm summer evening barbecue at a Russian country dacha, glowing coals "
                   "and grill, string lights, rustic wooden table softly out of focus, lush "
                   "green garden, golden hour"),
        "top": (38, 18, 12), "bottom": (140, 60, 24), "accent": (255, 176, 92),
    },
    "пикник": {
        "emoji": "🧺",
        "prompt": ("sunny meadow picnic, plaid blanket, wicker basket, wildflowers, soft "
                   "bokeh, bright airy daylight, countryside"),
        "top": (20, 40, 22), "bottom": (90, 140, 60), "accent": (200, 230, 140),
    },
    "день рождения": {
        "emoji": "🎂",
        "prompt": ("festive birthday party scene, colorful balloons, confetti, soft warm "
                   "bokeh lights, celebratory mood"),
        "top": (40, 16, 44), "bottom": (150, 60, 120), "accent": (255, 170, 210),
    },
    "новоселье": {
        "emoji": "🏠",
        "prompt": ("cozy housewarming gathering, warm living room, candles and plants, soft "
                   "evening light, welcoming mood"),
        "top": (14, 30, 40), "bottom": (40, 100, 120), "accent": (150, 220, 235),
    },
    "вечеринка": {
        "emoji": "🎉",
        "prompt": ("vibrant night party, neon lights, cocktails, bokeh, energetic festive "
                   "atmosphere"),
        "top": (28, 12, 46), "bottom": (110, 30, 140), "accent": (235, 150, 255),
    },
    "новый год": {
        "emoji": "🎄",
        "prompt": ("new year celebration, decorated tree, warm fairy lights, snow outside "
                   "window, festive cozy evening"),
        "top": (10, 18, 40), "bottom": (30, 70, 130), "accent": (255, 220, 150),
    },
    "минимал": {
        "emoji": "✨",
        "prompt": ("minimal elegant abstract gradient background, soft, tasteful, no objects"),
        "top": (22, 24, 30), "bottom": (54, 58, 72), "accent": (210, 200, 255),
    },
}
DEFAULT_THEME = "минимал"


def theme_or_default(name: str | None) -> str:
    return name if name in THEMES else DEFAULT_THEME


# ── Font helpers ──────────────────────────────────────────────────────────────
def _title_font(size: int) -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(_FONT_TITLE, size)
    try:
        f.set_variation_by_axes([800])  # heavy weight on the variable font
    except Exception:
        try:
            f = ImageFont.truetype(_FONT_BOLD, size)
        except Exception:
            pass
    return f


def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def _text_w(draw, text, font) -> int:
    return draw.textbbox((0, 0), text, font=font)[2]


def _wrap(draw, text, font, max_w) -> list[str]:
    words = text.split()
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if _text_w(draw, trial, font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _fit_title(draw, text, max_w, start_size, min_size) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """Shrink the title font until it wraps into at most 3 lines that fit."""
    size = start_size
    while size >= min_size:
        font = _title_font(size)
        lines = _wrap(draw, text, font, max_w)
        if len(lines) <= 3 and all(_text_w(draw, ln, font) <= max_w for ln in lines):
            return font, lines
        size -= 6
    font = _title_font(min_size)
    return font, _wrap(draw, text, font, max_w)


# ── Gradient (free template background) ─────────────────────────────────────────
def _vertical_gradient(w, h, top, bottom) -> Image.Image:
    base = Image.new("RGB", (w, h), top)
    grad = Image.new("L", (1, h))
    for y in range(h):
        grad.putpixel((0, y), int(255 * y / max(1, h - 1)))
    grad = grad.resize((w, h))
    bottom_img = Image.new("RGB", (w, h), bottom)
    return Image.composite(bottom_img, base, grad)


def _radial_glow(w, h, color, cx, cy, radius) -> Image.Image:
    """Soft accent glow overlay for the free template."""
    layer = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(layer)
    d.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=90)
    layer = layer.filter(ImageFilter.GaussianBlur(radius // 2))
    glow = Image.new("RGB", (w, h), color)
    return Image.composite(glow, Image.new("RGB", (w, h), (0, 0, 0)), layer), layer


# ── Shared text overlay ─────────────────────────────────────────────────────────
def _draw_scrim(img: Image.Image, from_top=True, strength=180) -> None:
    """Darken the text area so overlay text stays legible over any background."""
    w, h = img.size
    scrim = Image.new("L", (1, h), 0)
    span = int(h * 0.55)
    for y in range(h):
        if from_top:
            a = max(0, strength - int(strength * y / span)) if y < span else 0
        else:
            a = 0
        scrim.putpixel((0, y), a)
    scrim = scrim.resize((w, h))
    black = Image.new("RGB", (w, h), (0, 0, 0))
    img.paste(black, (0, 0), scrim)


def _draw_text_layer(img: Image.Image, event: dict, theme_key: str,
                     top_frac: float = 0.085) -> None:
    """Draw kicker, title, date/time, place, host and brand. Top-anchored.
    No emoji glyphs (the bundled fonts have none) — uses drawn accent markers."""
    w, h = img.size
    draw = ImageDraw.Draw(img)
    th = THEMES[theme_key]
    accent = th["accent"]
    margin = int(w * 0.08)
    max_w = w - 2 * margin
    y = int(h * top_frac)

    # Kicker with a drawn accent diamond marker
    ks = int(w * 0.036)
    kicker_font = _font(_FONT_BOLD, ks)
    dm = ks // 2  # diamond half-size
    cy = y + ks // 2
    draw.polygon([(margin + dm, cy - dm), (margin + 2 * dm, cy),
                  (margin + dm, cy + dm), (margin, cy)], fill=accent)
    draw.text((margin + int(ks * 1.6), y), "ПРИГЛАШЕНИЕ", font=kicker_font, fill=accent)
    y += ks + int(h * 0.018)

    # Title (event name)
    name = (event.get("name") or "Событие").strip()
    title_font, lines = _fit_title(draw, name, max_w, int(w * 0.13), int(w * 0.07))
    asc, desc = title_font.getmetrics()
    line_h = asc + desc
    for ln in lines:
        draw.text((margin, y), ln, font=title_font, fill=(255, 255, 255))
        y += int(line_h * 1.04)

    # Accent divider
    y += int(h * 0.012)
    draw.line([(margin, y), (margin + int(w * 0.18), y)], fill=accent, width=max(3, w // 220))
    y += int(h * 0.028)

    # Date · time
    dt = " · ".join([s for s in [event.get("date_str"), event.get("time_str")] if s])
    if dt:
        dt_font = _font(_FONT_BOLD, int(w * 0.058))
        draw.text((margin, y), dt, font=dt_font, fill=(255, 255, 255))
        y += int(w * 0.058) + int(h * 0.016)

    # Place (drawn pin marker instead of emoji)
    place = (event.get("place") or "").strip()
    if place:
        ps = int(w * 0.045)
        place_font = _font(_FONT_REG, ps)
        indent = int(ps * 1.5)
        # small pin: circle + tail
        r = ps // 3
        px, py = margin + r, y + r + int(ps * 0.12)
        draw.ellipse([px - r, py - r, px + r, py + r], outline=accent, width=max(2, w // 360))
        draw.ellipse([px - r // 3, py - r // 3, px + r // 3, py + r // 3], fill=accent)
        lines_p = _wrap(draw, place, place_font, max_w - indent)
        for i, ln in enumerate(lines_p):
            draw.text((margin + indent, y), ln, font=place_font, fill=(235, 235, 235))
            y += ps + int(h * 0.006)

    # Host
    host = (event.get("host_name") or "").strip()
    if host:
        y += int(h * 0.006)
        host_font = _font(_FONT_REG, int(w * 0.04))
        draw.text((margin, y), f"Приглашает: {host}", font=host_font, fill=(210, 210, 210))

    # Brand (bottom)
    brand_font = _font(_FONT_REG, int(w * 0.034))
    bw = _text_w(draw, BRAND, brand_font)
    draw.text(((w - bw) // 2, h - int(h * 0.055)), BRAND, font=brand_font, fill=(220, 220, 220))


# ── Public API ────────────────────────────────────────────────────────────────
def render_typographic(event: dict, theme: str | None = None,
                       size: tuple[int, int] = (1080, 1920)) -> bytes:
    """Free tier: gradient background + text. No AI."""
    theme_key = theme_or_default(theme)
    th = THEMES[theme_key]
    w, h = size
    img = _vertical_gradient(w, h, th["top"], th["bottom"]).convert("RGB")
    glow, _ = _radial_glow(w, h, th["accent"], int(w * 0.8), int(h * 0.18), int(w * 0.5))
    img = Image.blend(img, glow, 0.18)
    _draw_text_layer(img, event, theme_key, top_frac=0.16)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def render_on_background(bg_png: bytes, event: dict, theme: str | None = None) -> bytes:
    """Paid tier: AI background (PNG bytes) + shared text overlay."""
    theme_key = theme_or_default(theme)
    img = Image.open(io.BytesIO(bg_png)).convert("RGB")
    _draw_scrim(img, from_top=True, strength=190)
    _draw_text_layer(img, event, theme_key)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
