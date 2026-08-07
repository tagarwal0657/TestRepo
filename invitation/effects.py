"""Procedural underwater effects: bubbles, caustics, god rays and sparkles."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter


# --------------------------------------------------------------------------- #
#  Bubbles
# --------------------------------------------------------------------------- #
def _bubble_sprite(radius: int, tint: tuple[int, int, int], alpha: float) -> Image.Image:
    """A hollow bubble: bright rim, faint fill, one specular highlight."""
    ss = 4
    d_out = radius * 2
    size = d_out + 6
    big = Image.new("RGBA", (size * ss, size * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    c = size * ss / 2
    r = radius * ss

    rim = max(ss, int(r * 0.16))
    d.ellipse([c - r, c - r, c + r, c + r], fill=(*tint, int(46 * alpha)))
    d.ellipse([c - r, c - r, c + r, c + r], outline=(255, 255, 255, int(215 * alpha)), width=rim)
    # Inner refraction arc at the lower right.
    d.arc([c - r * 0.72, c - r * 0.72, c + r * 0.72, c + r * 0.72], 20, 140,
          fill=(220, 250, 255, int(120 * alpha)), width=max(ss, int(r * 0.10)))

    small = big.resize((size, size), Image.LANCZOS)
    d2 = ImageDraw.Draw(small)
    hr = max(1.0, radius * 0.22)
    hx, hy = size / 2 - radius * 0.38, size / 2 - radius * 0.40
    d2.ellipse([hx - hr, hy - hr, hx + hr, hy + hr], fill=(255, 255, 255, int(230 * alpha)))
    return small.filter(ImageFilter.GaussianBlur(0.4))


@dataclass
class Bubble:
    x: float
    y: float
    radius: int
    speed: float
    wobble: float
    freq: float
    phase: float
    sprite_index: int


class BubbleField:
    """Bubbles drifting upwards on looping paths."""

    def __init__(self, width: int, height: int, count: int, seed: int,
                 r_range: tuple[int, int] = (5, 26), speed: tuple[float, float] = (26.0, 78.0),
                 alpha: float = 1.0, tint: tuple[int, int, int] = (185, 240, 255)):
        rng = np.random.default_rng(seed)
        self.width, self.height = width, height
        radii = sorted({int(r) for r in np.linspace(r_range[0], r_range[1], 9)})
        self.sprites = [_bubble_sprite(r, tint, alpha) for r in radii]
        self.radii = radii
        self.margin = max(radii) + 8

        self.bubbles: list[Bubble] = []
        for _ in range(count):
            idx = int(rng.integers(0, len(radii)))
            r = radii[idx]
            self.bubbles.append(Bubble(
                x=float(rng.uniform(-self.margin, width + self.margin)),
                y=float(rng.uniform(-self.margin, height + self.margin)),
                radius=r,
                # Bigger bubbles rise faster, as they do in water.
                speed=float(rng.uniform(*speed)) * (0.6 + 0.9 * r / max(radii)),
                wobble=float(rng.uniform(8.0, 34.0)),
                freq=float(rng.uniform(0.35, 1.15)),
                phase=float(rng.uniform(0, math.tau)),
                sprite_index=idx,
            ))

    def draw(self, canvas: Image.Image, t: float, opacity: float = 1.0,
             avoid: tuple[int, int, int, int] | None = None) -> None:
        """Draw the field. ``avoid`` is a rect bubbles must not drift over."""
        if opacity <= 0.01:
            return
        span = self.height + 2 * self.margin
        for b in self.bubbles:
            y = (b.y - b.speed * t + self.margin) % span - self.margin
            x = b.x + b.wobble * math.sin(b.freq * t + b.phase)
            if avoid is not None:
                ax0, ay0, ax1, ay1 = avoid
                r = b.radius + 6
                if ax0 - r < x < ax1 + r and ay0 - r < y < ay1 + r:
                    continue
            sprite = self.sprites[b.sprite_index]
            if opacity < 0.995:
                sprite = with_opacity(sprite, opacity)
            canvas.alpha_composite(sprite, (int(x - sprite.width / 2), int(y - sprite.height / 2)))


# --------------------------------------------------------------------------- #
#  Caustics and light shafts (additive)
# --------------------------------------------------------------------------- #
class Caustics:
    """A looping water-caustic shimmer, pre-rendered at low resolution."""

    def __init__(self, width: int, height: int, frames: int = 96, strength: float = 26.0,
                 scale: int = 5, seed: int = 5):
        self.frames = frames
        self.size = (width, height)
        w, h = width // scale, height // scale
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        xx /= w
        yy /= h
        rng = np.random.default_rng(seed)

        # A handful of drifting plane waves interfere into a caustic-like mesh.
        waves = []
        for _ in range(5):
            ang = rng.uniform(0, math.pi)
            waves.append((
                float(rng.uniform(6.0, 17.0)),                       # spatial frequency
                float(math.cos(ang)), float(math.sin(ang)),          # direction
                float(rng.uniform(0.6, 1.7)),                        # temporal frequency
                float(rng.uniform(0, math.tau)),                     # phase
            ))

        self.tiles: list[Image.Image] = []
        for f in range(frames):
            t = f / frames * math.tau
            acc = np.zeros((h, w), np.float32)
            for k, dx, dy, tf, ph in waves:
                acc += np.sin(k * (xx * dx + yy * dy) * math.tau + tf * t + ph)
            acc /= len(waves)
            # Sharpen the bright ridges the way real caustics look.
            band = np.clip(np.abs(acc), 0, 1)
            light = np.power(1.0 - band, 3.4)
            # Fade the effect towards the bottom of the frame.
            light *= np.clip(1.25 - yy * 1.25, 0.0, 1.0)
            rgb = np.stack([light * 0.55, light * 0.92, light * 1.0], axis=-1) * strength
            tile = Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8), "RGB")
            self.tiles.append(tile.resize((width, height), Image.BICUBIC))

    def get(self, frame: int) -> Image.Image:
        return self.tiles[frame % self.frames]


def build_god_rays(width: int, height: int, seed: int = 9, strength: float = 1.0) -> Image.Image:
    """Soft sunbeams fanning down from the top of the frame (RGB, additive)."""
    rng = np.random.default_rng(seed)
    layer = Image.new("L", (width, height), 0)
    d = ImageDraw.Draw(layer)
    apex_x = width * 0.52
    apex_y = -height * 0.35

    for _ in range(16):
        a0 = rng.uniform(-0.95, 0.95)
        wdt = rng.uniform(0.035, 0.14)
        reach = height * rng.uniform(0.85, 1.35)
        x0 = apex_x + math.tan(a0) * (0 - apex_y)
        x1 = apex_x + math.tan(a0 + wdt) * (0 - apex_y)
        x2 = apex_x + math.tan(a0 + wdt) * (reach - apex_y)
        x3 = apex_x + math.tan(a0) * (reach - apex_y)
        d.polygon([(x0, 0), (x1, 0), (x2, reach), (x3, reach)], fill=int(rng.uniform(70, 150)))

    layer = layer.filter(ImageFilter.GaussianBlur(width * 0.022))
    arr = np.asarray(layer, np.float32) / 255.0
    # Beams are strongest near the surface.
    fade = np.linspace(1.0, 0.0, height, dtype=np.float32) ** 1.6
    arr *= fade[:, None]
    arr *= strength
    rgb = np.stack([arr * 190, arr * 225, arr * 255], axis=-1)
    return Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8), "RGB")


# --------------------------------------------------------------------------- #
#  Sparkles
# --------------------------------------------------------------------------- #
def _star_sprite(size: int, colour: tuple[int, int, int]) -> Image.Image:
    ss = 4
    s = size * ss
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = s / 2
    arm = s * 0.5
    thin = s * 0.055
    d.polygon([(c, c - arm), (c + thin, c), (c, c + arm), (c - thin, c)], fill=(*colour, 255))
    d.polygon([(c - arm, c), (c, c - thin), (c + arm, c), (c, c + thin)], fill=(*colour, 255))
    d.ellipse([c - thin * 2.1, c - thin * 2.1, c + thin * 2.1, c + thin * 2.1], fill=(255, 255, 255, 255))
    return img.resize((size, size), Image.LANCZOS).filter(ImageFilter.GaussianBlur(0.5))


class SparkleField:
    """Twinkling stars scattered over the frame."""

    def __init__(self, width: int, height: int, count: int, seed: int,
                 sizes: tuple[int, int] = (10, 34), region: tuple[float, float] = (0.0, 1.0)):
        rng = np.random.default_rng(seed)
        self.items = []
        cache: dict[int, Image.Image] = {}
        colours = [(255, 255, 255), (255, 240, 190), (200, 250, 255), (255, 205, 240)]
        for _ in range(count):
            s = int(rng.integers(sizes[0], sizes[1]))
            col = colours[int(rng.integers(0, len(colours)))]
            key = s * 10 + colours.index(col)
            if key not in cache:
                cache[key] = _star_sprite(s, col)
            self.items.append((
                float(rng.uniform(0, width)),
                float(rng.uniform(region[0] * height, region[1] * height)),
                cache[key],
                float(rng.uniform(0.5, 1.9)),   # twinkle rate
                float(rng.uniform(0, math.tau)),
            ))

    def draw(self, canvas: Image.Image, t: float, opacity: float = 1.0,
             avoid: tuple[int, int, int, int] | None = None) -> None:
        if opacity <= 0.01:
            return
        for x, y, sprite, rate, phase in self.items:
            if avoid is not None:
                ax0, ay0, ax1, ay1 = avoid
                if ax0 < x < ax1 and ay0 < y < ay1:
                    continue
            a = 0.5 + 0.5 * math.sin(t * rate * math.tau * 0.5 + phase)
            a = (a ** 2.2) * opacity
            if a < 0.04:
                continue
            s = with_opacity(sprite, a)
            canvas.alpha_composite(s, (int(x - s.width / 2), int(y - s.height / 2)))


# --------------------------------------------------------------------------- #
#  Shared helpers
# --------------------------------------------------------------------------- #
class FallingSparkles:
    """Glittering motes drifting down the frame, for the 'blessings' beat."""

    def __init__(self, width: int, height: int, count: int, seed: int,
                 sizes: tuple[int, int] = (12, 40), speed: tuple[float, float] = (90.0, 210.0)):
        rng = np.random.default_rng(seed)
        self.width, self.height = width, height
        self.margin = sizes[1]
        cache: dict[int, Image.Image] = {}
        colours = [(255, 238, 176), (255, 255, 255), (255, 206, 236), (198, 246, 255)]
        self.items = []
        for _ in range(count):
            s = int(rng.integers(*sizes))
            col = colours[int(rng.integers(0, len(colours)))]
            key = s * 10 + colours.index(col)
            if key not in cache:
                cache[key] = _star_sprite(s, col)
            self.items.append((
                float(rng.uniform(0, width)),
                float(rng.uniform(-self.margin, height)),
                cache[key],
                float(rng.uniform(*speed)),
                float(rng.uniform(14.0, 46.0)),   # sway
                float(rng.uniform(0.4, 1.3)),     # sway rate
                float(rng.uniform(0, math.tau)),
                float(rng.uniform(1.4, 3.2)),     # twinkle rate
            ))

    def draw(self, canvas: Image.Image, t: float, opacity: float = 1.0,
             avoid: tuple[int, int, int, int] | None = None) -> None:
        if opacity <= 0.01:
            return
        span = self.height + 2 * self.margin
        for x0, y0, sprite, speed, sway, rate, phase, twinkle in self.items:
            y = (y0 + speed * t + self.margin) % span - self.margin
            x = x0 + sway * math.sin(rate * t + phase)
            if avoid is not None:
                ax0, ay0, ax1, ay1 = avoid
                if ax0 < x < ax1 and ay0 < y < ay1:
                    continue
            a = (0.5 + 0.5 * math.sin(t * twinkle * math.tau * 0.5 + phase)) ** 1.6 * opacity
            if a < 0.05:
                continue
            s = with_opacity(sprite, a)
            canvas.alpha_composite(s, (int(x - s.width / 2), int(y - s.height / 2)))


_ALPHA_LUTS: dict[int, list[int]] = {}


def with_opacity(img: Image.Image, opacity: float) -> Image.Image:
    """A copy of ``img`` with its alpha channel scaled."""
    opacity = max(0.0, min(1.0, opacity))
    q = int(opacity * 255)
    if q >= 255:
        return img
    lut = _ALPHA_LUTS.get(q)
    if lut is None:
        lut = [(v * q) // 255 for v in range(256)]
        _ALPHA_LUTS[q] = lut
    out = img.copy()
    out.putalpha(img.getchannel("A").point(lut))
    return out


def add_light(base: Image.Image, light: Image.Image, amount: float = 1.0) -> Image.Image:
    """Additively blend an RGB light pass onto an RGBA base."""
    if amount <= 0.001:
        return base
    rgb = base.convert("RGB")
    if amount < 0.999:
        light = light.point(lambda v: int(v * amount))
    merged = ImageChops.add(rgb, light)
    out = merged.convert("RGBA")
    out.putalpha(base.getchannel("A"))
    return out


def build_vignette(width: int, height: int, strength: float = 0.42) -> Image.Image:
    """A dark corner falloff, applied multiplicatively."""
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    cx, cy = width / 2, height / 2
    r = np.sqrt(((xx - cx) / cx) ** 2 + ((yy - cy) / cy) ** 2) / math.sqrt(2)
    v = 1.0 - strength * np.clip((r - 0.35) / 0.65, 0, 1) ** 1.7
    arr = np.stack([v, v, v], axis=-1) * 255.0
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")
