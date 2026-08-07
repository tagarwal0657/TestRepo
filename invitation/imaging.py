"""Low level image helpers shared by the artwork and effect modules."""

from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter
from scipy import ndimage

RGBA = tuple[int, int, int, int]


# --------------------------------------------------------------------------- #
#  numpy <-> PIL
# --------------------------------------------------------------------------- #
def to_array(img: Image.Image) -> np.ndarray:
    """RGBA image as float32 ``[h, w, 4]`` in 0..1."""
    return np.asarray(img.convert("RGBA"), dtype=np.float32) / 255.0


def to_image(arr: np.ndarray) -> Image.Image:
    return Image.fromarray(np.clip(arr * 255.0, 0, 255).astype(np.uint8), "RGBA")


def alpha_over(dst: np.ndarray, src: np.ndarray) -> np.ndarray:
    """Straight-alpha ``src`` over straight-alpha ``dst``, both ``[h, w, 4]``."""
    sa = src[..., 3:4]
    da = dst[..., 3:4]
    out_a = sa + da * (1.0 - sa)
    safe = np.where(out_a > 1e-6, out_a, 1.0)
    out_rgb = (src[..., :3] * sa + dst[..., :3] * da * (1.0 - sa)) / safe
    return np.concatenate([out_rgb, out_a], axis=-1)


def paste_array(dst: np.ndarray, src: np.ndarray, x: int, y: int) -> None:
    """Composite ``src`` onto ``dst`` in place at ``(x, y)``, clipping at edges."""
    dh, dw = dst.shape[:2]
    sh, sw = src.shape[:2]

    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(dw, x + sw), min(dh, y + sh)
    if x0 >= x1 or y0 >= y1:
        return

    patch = src[y0 - y : y1 - y, x0 - x : x1 - x]
    dst[y0:y1, x0:x1] = alpha_over(dst[y0:y1, x0:x1], patch)


# --------------------------------------------------------------------------- #
#  Cut-outs
# --------------------------------------------------------------------------- #
def chroma_key(
    img: Image.Image,
    key: tuple[int, int, int] = (0, 255, 0),
    tolerance: float = 0.36,
    softness: float = 0.10,
    despill: float = 1.0,
    min_blob: float = 0.002,
) -> Image.Image:
    """Key a flat chroma background out of generated artwork.

    Distance is measured in a chroma-only space so that bright highlights on the
    subject are not mistaken for background.
    """
    arr = to_array(img)
    rgb = arr[..., :3]
    kr, kg, kb = (c / 255.0 for c in key)

    # Difference between the green channel and the strongest of the other two:
    # a robust "greenness" measure that ignores luminance.
    greenness = rgb[..., 1] - np.maximum(rgb[..., 0], rgb[..., 2])
    dist = np.sqrt(((rgb - np.array([kr, kg, kb], np.float32)) ** 2).sum(-1))

    is_key = (greenness > 0.12) & (dist < tolerance + softness)
    alpha = np.clip((dist - tolerance) / max(softness, 1e-4), 0.0, 1.0)
    alpha = np.where(is_key, alpha, 1.0).astype(np.float32)

    # Drop keying speckle: any opaque blob smaller than ``min_blob`` of the frame.
    if min_blob > 0:
        labels, count = ndimage.label(alpha > 0.5)
        if count > 1:
            sizes = np.bincount(labels.ravel())
            too_small = np.flatnonzero(sizes < min_blob * alpha.size)
            too_small = too_small[too_small != 0]
            if too_small.size:
                alpha = np.where(np.isin(labels, too_small), 0.0, alpha)

    alpha = ndimage.gaussian_filter(alpha, 0.8)

    if despill > 0:
        # Only the fringe is contaminated by the backing colour. Applying this
        # across the whole subject would drain the green out of anything that is
        # legitimately green -- a mermaid tail, for one.
        core = ndimage.binary_erosion(alpha > 0.5, iterations=4)
        fringe = np.clip(1.0 - core.astype(np.float32), 0.0, 1.0)
        fringe = ndimage.gaussian_filter(fringe, 1.5)
        limit = (rgb[..., 0] + rgb[..., 2]) * 0.5 + 0.06
        spill = np.clip(rgb[..., 1] - limit, 0, None) * despill * fringe
        rgb = rgb.copy()
        rgb[..., 1] -= spill

    out = np.concatenate([rgb, alpha[..., None]], axis=-1)
    return to_image(out)


def trim(img: Image.Image, pad: int = 0, threshold: int = 4) -> Image.Image:
    """Crop away fully transparent margins."""
    alpha = np.asarray(img.convert("RGBA"))[..., 3]
    ys, xs = np.where(alpha > threshold)
    if len(xs) == 0:
        return img
    x0, x1 = max(0, xs.min() - pad), min(img.width, xs.max() + 1 + pad)
    y0, y1 = max(0, ys.min() - pad), min(img.height, ys.max() + 1 + pad)
    return img.crop((x0, y0, x1, y1))


@lru_cache(maxsize=8)
def _rembg_session(model: str):
    from rembg import new_session

    return new_session(model)


def remove_background(img: Image.Image, model: str = "isnet-general-use") -> Image.Image:
    """Segment the subject out of a photograph."""
    from rembg import remove

    cut = remove(
        img.convert("RGBA"),
        session=_rembg_session(model),
        alpha_matting=True,
        alpha_matting_foreground_threshold=248,
        alpha_matting_background_threshold=12,
        alpha_matting_erode_size=6,
    )
    return cut.convert("RGBA")


# --------------------------------------------------------------------------- #
#  Fills, glows, borders
# --------------------------------------------------------------------------- #
def linear_gradient(
    size: tuple[int, int],
    stops: list[tuple[float, RGBA]],
    angle: float = 90.0,
) -> Image.Image:
    """A linear gradient. ``angle`` 90 is top-to-bottom, 0 is left-to-right."""
    w, h = size
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    rad = math.radians(angle)
    proj = xx * math.cos(rad) + yy * math.sin(rad)
    proj -= proj.min()
    span = max(proj.max(), 1e-6)
    t = proj / span

    stops = sorted(stops, key=lambda s: s[0])
    pos = np.array([s[0] for s in stops], np.float32)
    cols = np.array([s[1] for s in stops], np.float32) / 255.0

    out = np.empty((h, w, 4), np.float32)
    for c in range(4):
        out[..., c] = np.interp(t, pos, cols[:, c])
    return to_image(out)


def radial_gradient(size: tuple[int, int], stops: list[tuple[float, RGBA]]) -> Image.Image:
    w, h = size
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    cx, cy = (w - 1) / 2.0, (h - 1) / 2.0
    r = np.sqrt(((xx - cx) / max(cx, 1e-6)) ** 2 + ((yy - cy) / max(cy, 1e-6)) ** 2)
    t = np.clip(r / math.sqrt(2), 0, 1)

    stops = sorted(stops, key=lambda s: s[0])
    pos = np.array([s[0] for s in stops], np.float32)
    cols = np.array([s[1] for s in stops], np.float32) / 255.0

    out = np.empty((h, w, 4), np.float32)
    for c in range(4):
        out[..., c] = np.interp(t, pos, cols[:, c])
    return to_image(out)


def apply_mask(fill: Image.Image, mask: Image.Image) -> Image.Image:
    """Multiply ``fill``'s alpha by an ``L`` mask."""
    out = fill.convert("RGBA").copy()
    a = out.getchannel("A")
    out.putalpha(ImageChops.multiply(a, mask.convert("L")))
    return out


def outer_glow(
    mask: Image.Image,
    color: RGBA,
    radius: float,
    strength: float = 1.0,
    spread: int = 0,
) -> Image.Image:
    """A soft coloured halo grown from an ``L`` mask."""
    m = mask.convert("L")
    if spread:
        m = m.filter(ImageFilter.MaxFilter(spread * 2 + 1))
    blurred = m.filter(ImageFilter.GaussianBlur(radius))
    arr = np.asarray(blurred, np.float32) / 255.0
    arr = np.clip(arr * strength, 0, 1)
    glow = np.zeros((*arr.shape, 4), np.float32)
    glow[..., 0] = color[0] / 255.0
    glow[..., 1] = color[1] / 255.0
    glow[..., 2] = color[2] / 255.0
    glow[..., 3] = arr * (color[3] / 255.0)
    return to_image(glow)


def drop_shadow(
    mask: Image.Image,
    radius: float,
    offset: tuple[int, int] = (0, 0),
    color: RGBA = (0, 0, 0, 150),
) -> Image.Image:
    m = mask.convert("L")
    shadow = Image.new("L", m.size, 0)
    shadow.paste(m, offset)
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius))
    arr = np.asarray(shadow, np.float32) / 255.0
    out = np.zeros((*arr.shape, 4), np.float32)
    out[..., 0] = color[0] / 255.0
    out[..., 1] = color[1] / 255.0
    out[..., 2] = color[2] / 255.0
    out[..., 3] = arr * (color[3] / 255.0)
    return to_image(out)


def ring_mask(mask: Image.Image, width: int, outset: int = 0) -> Image.Image:
    """An ``L`` mask of a band hugging the outside edge of ``mask``."""
    arr = np.asarray(mask.convert("L"), np.float32) / 255.0
    solid = arr > 0.5
    # Distance from the shape, measured outside it.
    dist = ndimage.distance_transform_edt(~solid)
    band = (dist > outset) & (dist <= outset + width)
    # Feather by one pixel so the border does not alias.
    soft = np.clip(1.0 - np.abs(dist - (outset + width / 2.0)) / (width / 2.0 + 0.8), 0, 1)
    out = np.where(band, 1.0, soft * 0.55)
    out = np.clip(out, 0, 1)
    return Image.fromarray((out * 255).astype(np.uint8), "L")


def scalloped_mask(
    size: tuple[int, int],
    bump: int = 26,
    corner: int = 60,
    bumps_x: int = 9,
    bumps_y: int = 9,
    supersample: int = 2,
) -> Image.Image:
    """A rounded rectangle trimmed with semicircular scallops, as an ``L`` mask."""
    w, h = size
    s = supersample
    W, H = w * s, h * s
    b, c = bump * s, corner * s

    mask = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle([b, b, W - b - 1, H - b - 1], radius=c, fill=255)

    def scallops(count: int, x0: float, x1: float, y: float) -> None:
        if count < 1:
            return
        step = (x1 - x0) / count
        for i in range(count):
            cx = x0 + step * (i + 0.5)
            d.ellipse([cx - b, y - b, cx + b, y + b], fill=255)

    scallops(bumps_x, b + c * 0.5, W - b - c * 0.5, b)
    scallops(bumps_x, b + c * 0.5, W - b - c * 0.5, H - b - 1)
    # Vertical edges: reuse the helper with swapped axes via a transposed draw.
    step_y = (H - 2 * b - c) / max(bumps_y, 1)
    for i in range(bumps_y):
        cy = b + c * 0.5 + step_y * (i + 0.5)
        d.ellipse([b - b, cy - b, b + b, cy + b], fill=255)
        d.ellipse([W - b - 1 - b, cy - b, W - b - 1 + b, cy + b], fill=255)

    return mask.resize((w, h), Image.LANCZOS)


def open_scaled(path: Path, width: int | None = None, height: int | None = None) -> Image.Image:
    img = Image.open(path).convert("RGBA")
    if width and not height:
        height = round(img.height * width / img.width)
    elif height and not width:
        width = round(img.width * height / img.height)
    if width and height:
        img = img.resize((width, height), Image.LANCZOS)
    return img


def fit_within(img: Image.Image, box: tuple[int, int]) -> Image.Image:
    bw, bh = box
    scale = min(bw / img.width, bh / img.height)
    return img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.LANCZOS)
