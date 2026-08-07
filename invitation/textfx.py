"""Decorative text rendering: gradient fills, gold strokes, glow and glitter."""

from __future__ import annotations

from functools import lru_cache

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .config import FONTS
from .imaging import RGBA, apply_mask, drop_shadow, linear_gradient, outer_glow, to_image, trim

FONT_FILES = {
    "script": "GreatVibes-Regular.ttf",
    "script_bold": "Pacifico-Regular.ttf",
    "lobster": "Lobster-Regular.ttf",
    "slab": "AlfaSlabOne-Regular.ttf",
    "serif": "PlayfairDisplay-var.ttf",
    "caps": "Cinzel-var.ttf",
    "caps_deco": "CinzelDecorative-Bold.ttf",
    "sans": "Montserrat-var.ttf",
    "round": "Baloo2-var.ttf",
}

# Variable fonts need an explicit weight; these are sensible defaults.
DEFAULT_WEIGHT = {"serif": 700, "caps": 600, "sans": 700, "round": 700}

PAD = 90  # room around glyphs for glow / stroke before the sprite is trimmed


@lru_cache(maxsize=256)
def load_font(name: str, size: int, weight: int | None = None) -> ImageFont.FreeTypeFont:
    path = FONTS / FONT_FILES[name]
    font = ImageFont.truetype(str(path), size)
    w = weight if weight is not None else DEFAULT_WEIGHT.get(name)
    if w is not None:
        try:
            font.set_variation_by_axes([w])
        except OSError:
            pass  # static font, nothing to vary
    return font


def text_width(text: str, font: ImageFont.FreeTypeFont, tracking: float = 0.0) -> float:
    if not text:
        return 0.0
    return sum(font.getlength(ch) for ch in text) + tracking * (len(text) - 1)


def fit_font(
    text: str,
    name: str,
    max_width: int,
    start_size: int,
    weight: int | None = None,
    tracking: float = 0.0,
    min_size: int = 12,
) -> ImageFont.FreeTypeFont:
    """Largest font at or below ``start_size`` whose text fits ``max_width``."""
    size = start_size
    while size > min_size:
        font = load_font(name, size, weight)
        if text_width(text, font, tracking) <= max_width:
            return font
        size -= 2
    return load_font(name, min_size, weight)


def _draw_glyphs(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    font: ImageFont.FreeTypeFont,
    tracking: float,
    fill,
    stroke_width: int = 0,
    stroke_fill=None,
) -> None:
    """``ImageDraw.text`` with manual letter spacing."""
    if tracking == 0:
        draw.text(xy, text, font=font, fill=fill, stroke_width=stroke_width, stroke_fill=stroke_fill, anchor="ls")
        return
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill, stroke_width=stroke_width, stroke_fill=stroke_fill, anchor="ls")
        x += font.getlength(ch) + tracking


def glyph_mask(text: str, font: ImageFont.FreeTypeFont, tracking: float = 0.0, stroke: int = 0) -> tuple[Image.Image, tuple[int, int]]:
    """An ``L`` mask of the text plus the baseline origin used to draw it."""
    w = int(text_width(text, font, tracking)) + 2 * PAD + 2 * stroke
    ascent, descent = font.getmetrics()
    h = ascent + descent + 2 * PAD + 2 * stroke
    origin = (PAD + stroke, PAD + stroke + ascent)

    mask = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(mask)
    _draw_glyphs(d, origin, text, font, tracking, fill=255, stroke_width=stroke, stroke_fill=255)
    return mask, origin


def _glitter(mask: Image.Image, count: int, seed: int, color: RGBA = (255, 255, 255, 235)) -> Image.Image:
    """Sparkle specks scattered inside a mask."""
    arr = np.asarray(mask, np.uint8)
    ys, xs = np.where(arr > 200)
    layer = Image.new("RGBA", mask.size, (0, 0, 0, 0))
    if len(xs) == 0:
        return layer

    rng = np.random.default_rng(seed)
    idx = rng.choice(len(xs), size=min(count, len(xs)), replace=False)
    d = ImageDraw.Draw(layer)
    for i in idx:
        x, y = int(xs[i]), int(ys[i])
        r = float(rng.uniform(1.2, 3.4))
        a = int(rng.uniform(140, 255))
        d.ellipse([x - r, y - r, x + r, y + r], fill=(color[0], color[1], color[2], a))
        # Tiny four point star for the brightest specks.
        if rng.random() < 0.34:
            L = r * 3.0
            d.line([x - L, y, x + L, y], fill=(255, 255, 255, int(a * 0.75)), width=1)
            d.line([x, y - L, x, y + L], fill=(255, 255, 255, int(a * 0.75)), width=1)
    return layer.filter(ImageFilter.GaussianBlur(0.5))


def render_text(
    text: str,
    font: ImageFont.FreeTypeFont,
    *,
    fill: RGBA | None = (255, 255, 255, 255),
    gradient: list[tuple[float, RGBA]] | None = None,
    gradient_angle: float = 90.0,
    tracking: float = 0.0,
    stroke: int = 0,
    stroke_fill: RGBA = (255, 255, 255, 255),
    stroke2: int = 0,
    stroke2_fill: RGBA = (0, 0, 0, 255),
    glow: tuple[RGBA, float, float] | None = None,
    shadow: tuple[float, tuple[int, int], RGBA] | None = None,
    glitter: int = 0,
    seed: int = 0,
    inner_shade: bool = False,
) -> Image.Image:
    """Render decorated text to a tightly trimmed RGBA sprite.

    ``glow`` is ``(colour, radius, strength)``; ``shadow`` is
    ``(radius, offset, colour)``. Strokes are drawn outermost first.
    """
    outer = max(stroke + stroke2, 0)
    body_mask, origin = glyph_mask(text, font, tracking, stroke=0)
    size = body_mask.size
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))

    if shadow is not None:
        radius, offset, colour = shadow
        sh_mask, _ = glyph_mask(text, font, tracking, stroke=0)
        canvas.alpha_composite(drop_shadow(sh_mask, radius, offset, colour))

    if glow is not None:
        colour, radius, strength = glow
        canvas.alpha_composite(outer_glow(body_mask, colour, radius, strength, spread=max(1, outer // 2)))

    # Outer stroke (drawn first so the inner stroke sits on top of it).
    if stroke2 > 0:
        m = Image.new("L", size, 0)
        d = ImageDraw.Draw(m)
        _draw_glyphs(d, origin, text, font, tracking, fill=255, stroke_width=stroke + stroke2, stroke_fill=255)
        canvas.alpha_composite(apply_mask(Image.new("RGBA", size, stroke2_fill), m))

    if stroke > 0:
        m = Image.new("L", size, 0)
        d = ImageDraw.Draw(m)
        _draw_glyphs(d, origin, text, font, tracking, fill=255, stroke_width=stroke, stroke_fill=255)
        canvas.alpha_composite(apply_mask(Image.new("RGBA", size, stroke_fill), m))

    # Face
    if gradient:
        face = linear_gradient(size, gradient, gradient_angle)
    else:
        face = Image.new("RGBA", size, fill or (255, 255, 255, 255))
    face = apply_mask(face, body_mask)

    if inner_shade:
        # A soft dark edge inside the glyph gives the letters some depth.
        shade = body_mask.filter(ImageFilter.GaussianBlur(3))
        shade = Image.fromarray(
            np.clip(np.asarray(body_mask, np.int16) - np.asarray(shade, np.int16), 0, 255).astype(np.uint8), "L"
        )
        face.alpha_composite(apply_mask(Image.new("RGBA", size, (255, 255, 255, 120)), shade))

    canvas.alpha_composite(face)

    if glitter:
        canvas.alpha_composite(_glitter(body_mask, glitter, seed))

    # PAD gave the effects room to breathe; drop whatever they did not use so
    # callers can position sprites by their real ink extents.
    return trim(canvas, pad=2)


def core_alpha(sprite: Image.Image, gamma: float = 3.0) -> np.ndarray:
    """Approximate glyph coverage from a finished sprite.

    Raising the alpha to a power suppresses the soft glow and shadow, leaving
    the solid letterforms — and it stays aligned with the trimmed sprite.
    """
    a = np.asarray(sprite.getchannel("A"), np.float32) / 255.0
    return np.power(a, gamma)


def shine_overlay(mask: np.ndarray, phase: float, intensity: float = 0.95, width: float = 0.13) -> Image.Image:
    """A white gloss band swept across ``mask``. ``phase`` runs 0 -> 1."""
    h, w = mask.shape
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    # A diagonal sweep reads better than a purely horizontal one.
    proj = (xx / max(w - 1, 1)) * 0.86 + (yy / max(h - 1, 1)) * 0.30
    proj /= 1.16
    centre = phase * 1.5 - 0.25
    band = np.exp(-(((proj - centre) / width) ** 2))
    alpha = np.clip(band * mask * intensity, 0.0, 1.0)

    out = np.ones((h, w, 4), np.float32)
    out[..., 3] = alpha
    return to_image(out)


def paragraph(
    lines: list[str],
    font: ImageFont.FreeTypeFont,
    line_gap: float = 1.18,
    align: str = "center",
    **kwargs,
) -> Image.Image:
    """Stack several ``render_text`` results into one sprite."""
    sprites = [render_text(t, font, **kwargs) for t in lines]
    step = int(font.size * line_gap)
    width = max(s.width for s in sprites)
    height = step * (len(sprites) - 1) + max(s.height for s in sprites)
    out = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    for i, s in enumerate(sprites):
        if align == "center":
            x = (width - s.width) // 2
        elif align == "right":
            x = width - s.width
        else:
            x = 0
        out.alpha_composite(s, (x, i * step))
    return out
