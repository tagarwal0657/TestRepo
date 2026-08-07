"""Builds the static artwork: the card panel, the ribbon and the photo/shell stack."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from . import palette as P
from .config import ART, Config
from .imaging import (
    apply_mask,
    chroma_key,
    drop_shadow,
    fit_within,
    linear_gradient,
    open_scaled,
    outer_glow,
    radial_gradient,
    remove_background,
    ring_mask,
    scalloped_mask,
    trim,
)
from .textfx import core_alpha, fit_font, load_font, paragraph, render_text, text_width

CARD_RECT = (95, 356, 985, 1215)  # x0, y0, x1, y1 on the 1080x1920 canvas
CARD_PAD_X = 66

# Where the clam shell sits. The child sits on the bowl rather than inside it,
# so only the bottom rim is drawn in front of the figure.
SHELL_HEIGHT = 568
SHELL_BOTTOM = 1806
SHELL_SPLIT = 0.88  # everything below this fraction is drawn in front


@dataclass
class Layer:
    """A positioned sprite. ``key`` is what the timeline animates."""

    key: str
    image: Image.Image
    x: int
    y: int
    anchor: str = "topleft"  # or "center"

    def origin(self) -> tuple[int, int]:
        if self.anchor == "center":
            return self.x - self.image.width // 2, self.y - self.image.height // 2
        return self.x, self.y


# --------------------------------------------------------------------------- #
#  Card panel
# --------------------------------------------------------------------------- #
def build_card_panel() -> Image.Image:
    """The ornate scalloped ivory card, with shadow and double gold border."""
    x0, y0, x1, y1 = CARD_RECT
    w, h = x1 - x0, y1 - y0
    pad = 60
    size = (w + 2 * pad, h + 2 * pad)

    base = scalloped_mask((w, h), bump=24, corner=58, bumps_x=9, bumps_y=10)
    mask = Image.new("L", size, 0)
    mask.paste(base, (pad, pad))

    out = Image.new("RGBA", size, (0, 0, 0, 0))
    out.alpha_composite(drop_shadow(mask, 26, (0, 14), (10, 4, 40, 190)))
    out.alpha_composite(outer_glow(mask, (150, 240, 255, 170), 34, 0.7, spread=3))

    # Outer gold rim.
    rim = ring_mask(mask, width=9, outset=0)
    out.alpha_composite(apply_mask(linear_gradient(size, P.GRAD_GOLD, 62.0), rim))

    # Ivory face with a warm vignette.
    face = radial_gradient(size, [(0.0, P.CREAM_LIGHT), (0.62, P.CREAM), (1.0, P.CREAM_DEEP)])
    out.alpha_composite(apply_mask(face, mask))

    # Inner hairline, inset from the edge.
    inner_src = scalloped_mask((w - 44, h - 44), bump=17, corner=44, bumps_x=9, bumps_y=10)
    inner = Image.new("L", size, 0)
    inner.paste(inner_src, (pad + 22, pad + 22))
    hairline = ring_mask(inner, width=3, outset=0)
    out.alpha_composite(apply_mask(linear_gradient(size, P.GRAD_GOLD, 62.0), hairline))

    # Soft pearl highlights in the four corners.
    d = ImageDraw.Draw(out)
    for cx, cy in ((pad + 46, pad + 46), (size[0] - pad - 46, pad + 46),
                   (pad + 46, size[1] - pad - 46), (size[0] - pad - 46, size[1] - pad - 46)):
        d.ellipse([cx - 11, cy - 11, cx + 11, cy + 11], fill=(255, 246, 222, 235), outline=P.GOLD, width=3)

    return out


def build_ribbon(text: str, width: int = 760, height: int = 128) -> Image.Image:
    """A purple banner with notched ends and folded tails, carrying script text."""
    pad = 70
    tail_w = 62
    size = (width + 2 * pad + 2 * tail_w, height + 2 * pad)
    cx0, cy0 = pad + tail_w, pad

    def notched(w: int, h: int, notch: int) -> Image.Image:
        m = Image.new("L", (w, h), 0)
        d = ImageDraw.Draw(m)
        d.polygon(
            [(0, 0), (w, 0), (w - notch, h // 2), (w, h), (0, h), (notch, h // 2)],
            fill=255,
        )
        return m

    out = Image.new("RGBA", size, (0, 0, 0, 0))

    # Folded tails behind the main band.
    tail_h = int(height * 0.62)
    ty = cy0 + (height - tail_h) // 2
    for side in (-1, 1):
        tm = Image.new("L", size, 0)
        td = ImageDraw.Draw(tm)
        if side < 0:
            pts = [(cx0 - tail_w, ty + 12), (cx0 + 24, ty), (cx0 + 24, ty + tail_h),
                   (cx0 - tail_w, ty + tail_h - 12), (cx0 - tail_w + 26, ty + tail_h // 2)]
        else:
            xr = cx0 + width
            pts = [(xr + tail_w, ty + 12), (xr - 24, ty), (xr - 24, ty + tail_h),
                   (xr + tail_w, ty + tail_h - 12), (xr + tail_w - 26, ty + tail_h // 2)]
        td.polygon(pts, fill=255)
        out.alpha_composite(apply_mask(linear_gradient(size, [(0.0, (72, 26, 118, 255)), (1.0, (40, 12, 70, 255))], 90), tm))
        out.alpha_composite(apply_mask(linear_gradient(size, P.GRAD_GOLD, 60), ring_mask(tm, 5)))

    band = Image.new("L", size, 0)
    band.paste(notched(width, height, 34), (cx0, cy0))

    out.alpha_composite(drop_shadow(band, 16, (0, 8), (8, 2, 30, 170)))
    out.alpha_composite(apply_mask(linear_gradient(size, P.GRAD_RIBBON, 90.0), band))
    # Glossy top half.
    gloss = linear_gradient(size, [(0.0, (255, 255, 255, 78)), (0.45, (255, 255, 255, 10)), (0.5, (255, 255, 255, 0)), (1.0, (255, 255, 255, 0))], 90.0)
    out.alpha_composite(apply_mask(gloss, band))
    out.alpha_composite(apply_mask(linear_gradient(size, P.GRAD_GOLD, 60.0), ring_mask(band, 7)))

    font = fit_font(text, "script", width - 90, 96)
    label = render_text(
        text,
        font,
        gradient=P.GRAD_GOLD,
        gradient_angle=90.0,
        stroke=3,
        stroke_fill=(90, 46, 8, 255),
        glow=((255, 224, 150, 210), 16, 0.95),
        shadow=(6, (0, 4), (30, 8, 50, 180)),
        glitter=26,
        seed=11,
    )
    lx = cx0 + (width - label.width) // 2
    ly = cy0 + (height - label.height) // 2 + 2
    out.alpha_composite(label, (lx, ly))
    return out


# --------------------------------------------------------------------------- #
#  Card text
# --------------------------------------------------------------------------- #
@dataclass
class Shine:
    """A gloss band swept across the name; evaluated per frame."""

    mask: np.ndarray  # float32 [h, w], 0..1
    pos: tuple[int, int]  # top-left on the canvas


def build_card_text(cfg: Config) -> tuple[list[Layer], Shine]:
    x0, y0, x1, _ = CARD_RECT
    cx = (x0 + x1) // 2
    inner_w = (x1 - x0) - 2 * CARD_PAD_X
    layers: list[Layer] = []

    # "YOU ARE INVITED TO"
    f = fit_font(cfg.card["eyebrow"], "caps", inner_w - 120, 40, weight=600, tracking=9)
    layers.append(Layer("eyebrow", render_text(
        cfg.card["eyebrow"], f, fill=P.PURPLE, tracking=9,
        shadow=(3, (0, 2), (120, 90, 60, 90)),
    ), cx, y0 + 86, "center"))

    # "A Magical Celebration"
    f = fit_font(cfg.card["tagline"], "script", inner_w, 92)
    layers.append(Layer("tagline", render_text(
        cfg.card["tagline"], f, gradient=P.GRAD_TEAL, gradient_angle=90.0,
        stroke=3, stroke_fill=(255, 255, 255, 245),
        glow=((90, 210, 215, 130), 13, 0.7),
        shadow=(6, (0, 4), (10, 60, 70, 120)),
        glitter=16, seed=3,
    ), cx, y0 + 162, "center"))

    # "Join us for"
    f = fit_font(cfg.card["join"], "serif", inner_w - 200, 46, weight=600)
    layers.append(Layer("join", render_text(
        cfg.card["join"], f, fill=P.INK, shadow=(3, (0, 2), (120, 90, 60, 80)),
    ), cx, y0 + 234, "center"))

    # The name, in glittering pink.
    name = (cfg.child["name"] + cfg.card.get("name_suffix", "")).upper()
    f = fit_font(name, "slab", inner_w, 108, tracking=1)
    name_sprite = render_text(
        name, f, gradient=P.GRAD_PINK_GLITTER, gradient_angle=90.0, tracking=1,
        stroke=5, stroke_fill=(255, 248, 252, 255),
        stroke2=5, stroke2_fill=P.GOLD,
        glow=((255, 110, 195, 150), 20, 0.7),
        shadow=(9, (0, 6), (60, 4, 40, 170)),
        glitter=80, seed=7, inner_shade=True,
    )
    name_layer = Layer("name", name_sprite, cx, y0 + 344, "center")
    layers.append(name_layer)
    shine = Shine(mask=core_alpha(name_sprite), pos=name_layer.origin())

    # "1st Birthday Celebration" on one line.
    layers.append(Layer("occasion", _build_occasion(cfg, inner_w), cx, y0 + 478, "center"))

    # Dotted rule.
    layers.append(Layer("rule", _build_rule(inner_w - 40), cx, y0 + 570, "center"))

    # Date / time / venue columns.
    layers.extend(_build_details(cfg, y0 + 616))

    if cfg.footer:
        # Below the card, over the sand, so the child never covers it.
        f = fit_font(cfg.footer, "caps", cfg.width - 200, 30, weight=600, tracking=5)
        layers.append(Layer("footer", render_text(
            cfg.footer, f, fill=(255, 244, 214, 255), tracking=5,
            stroke=2, stroke_fill=(40, 14, 60, 210),
            shadow=(6, (0, 3), (0, 0, 20, 190)),
        ), cfg.width // 2, 1884, "center"))

    return layers, shine


def _build_occasion(cfg: Config, inner_w: int) -> Image.Image:
    """Crowned age numeral with a superscript, followed by the occasion in script."""
    num = str(cfg.child["age_number"])
    suffix = str(cfg.child.get("age_suffix", ""))
    occasion = cfg.card["occasion"]

    s_num = render_text(num, load_font("slab", 124), gradient=P.GRAD_RED, gradient_angle=90.0,
                        stroke=5, stroke_fill=(255, 250, 250, 255), stroke2=4, stroke2_fill=P.GOLD,
                        glow=((255, 90, 110, 140), 18, 0.7), shadow=(8, (0, 5), (60, 0, 20, 160)),
                        glitter=30, seed=17)
    s_sfx = render_text(suffix, load_font("slab", 52), gradient=P.GRAD_RED, gradient_angle=90.0,
                        stroke=3, stroke_fill=(255, 250, 250, 255), stroke2=3, stroke2_fill=P.GOLD,
                        shadow=(5, (0, 3), (60, 0, 20, 150)), glitter=6, seed=19) if suffix else None
    crown = _build_crown(84)

    numeral_w = s_num.width + (s_sfx.width - 6 if s_sfx else 0)
    budget = inner_w - numeral_w - 26
    f_occ = fit_font(occasion, "script", max(160, budget), 84)
    s_occ = render_text(occasion, f_occ, gradient=P.GRAD_TEAL, gradient_angle=90.0,
                        stroke=3, stroke_fill=(255, 255, 255, 245),
                        glow=((90, 210, 215, 130), 14, 0.7),
                        shadow=(6, (0, 4), (10, 60, 70, 120)), glitter=20, seed=23)

    gap = 24
    crown_lift = int(crown.height * 0.62)
    width = numeral_w + gap + s_occ.width
    height = max(s_num.height + crown_lift, s_occ.height + crown_lift) + 12
    out = Image.new("RGBA", (width, height), (0, 0, 0, 0))

    num_y = height - s_num.height - 6
    out.alpha_composite(s_num, (0, num_y))
    if s_sfx:
        out.alpha_composite(s_sfx, (s_num.width - 6, num_y - 4))
    out.alpha_composite(crown, (s_num.width // 2 - crown.width // 2, num_y - crown_lift))

    occ_y = num_y + (s_num.height - s_occ.height) // 2 + 4
    out.alpha_composite(s_occ, (numeral_w + gap, occ_y))
    return trim(out, pad=2)


def _build_crown(size: int) -> Image.Image:
    pad = 14
    img = Image.new("RGBA", (size + 2 * pad, size + 2 * pad), (0, 0, 0, 0))
    m = Image.new("L", img.size, 0)
    d = ImageDraw.Draw(m)
    w = size
    h = int(size * 0.62)
    x, y = pad, pad + (size - h) // 2
    pts = [(x, y + h), (x, y + h * 0.30), (x + w * 0.25, y + h * 0.62), (x + w * 0.5, y + h * 0.02),
           (x + w * 0.75, y + h * 0.62), (x + w, y + h * 0.30), (x + w, y + h)]
    d.polygon(pts, fill=255)
    d.rectangle([x, y + h * 0.80, x + w, y + h], fill=255)
    img.alpha_composite(outer_glow(m, (255, 220, 140, 200), 12, 0.9))
    img.alpha_composite(apply_mask(linear_gradient(img.size, P.GRAD_GOLD, 90.0), m))
    img.alpha_composite(apply_mask(Image.new("RGBA", img.size, (120, 70, 10, 255)), ring_mask(m, 2)))
    d2 = ImageDraw.Draw(img)
    for px in (x + w * 0.5, x + w * 0.06, x + w * 0.94):
        d2.ellipse([px - 6, y - 2, px + 6, y + 10], fill=(255, 120, 180, 255), outline=(255, 240, 200, 255))
    return img


def _build_rule(width: int) -> Image.Image:
    h = 30
    img = Image.new("RGBA", (width, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    y = h // 2
    x = 0
    while x < width:
        d.ellipse([x, y - 2, x + 4, y + 2], fill=(196, 150, 70, 220))
        x += 14
    # A small diamond in the middle.
    cx = width // 2
    d.polygon([(cx - 14, y), (cx, y - 9), (cx + 14, y), (cx, y + 9)], fill=P.GOLD, outline=(120, 70, 10, 255))
    d.polygon([(cx - 6, y), (cx, y - 4), (cx + 6, y), (cx, y + 4)], fill=(255, 240, 200, 255))
    return img


def _build_details(cfg: Config, top: int) -> list[Layer]:
    x0, _, x1, _ = CARD_RECT
    inner_x0 = x0 + CARD_PAD_X
    inner_w = (x1 - x0) - 2 * CARD_PAD_X
    n = len(cfg.details)
    col_w = inner_w // n

    layers: list[Layer] = []
    for i, det in enumerate(cfg.details):
        cx = inner_x0 + col_w * i + col_w // 2

        f_lab = fit_font(det.label, "caps", col_w - 40, 34, weight=700, tracking=6)
        label = render_text(det.label, f_lab, fill=P.PINK_DEEP, tracking=6,
                            shadow=(3, (0, 2), (120, 80, 60, 90)))
        layers.append(Layer(f"detail{i}_label", label, cx, top, "center"))

        size = 40
        while size > 20 and max(text_width(t, load_font("serif", size, 600)) for t in det.lines) > col_w - 26:
            size -= 2
        f_val = load_font("serif", size, 600)
        value = paragraph(det.lines, f_val, line_gap=1.20, fill=P.INK,
                          shadow=(3, (0, 2), (120, 90, 60, 80)))
        layers.append(Layer(f"detail{i}_value", value, cx, top + 62, "center"))

        if i < n - 1:
            sep = _build_column_separator(150)
            layers.append(Layer(f"detail{i}_sep", sep, inner_x0 + col_w * (i + 1), top + 54, "center"))

    return layers


def _build_column_separator(height: int) -> Image.Image:
    img = Image.new("RGBA", (12, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    y = 0
    while y < height:
        d.ellipse([4, y, 8, y + 4], fill=(196, 150, 70, 200))
        y += 12
    return img


# --------------------------------------------------------------------------- #
#  Photo + clam shell
# --------------------------------------------------------------------------- #
@dataclass
class PhotoStack:
    shell_back: Image.Image
    subject: Image.Image
    shell_front: Image.Image
    shell_pos: tuple[int, int]
    subject_pos: tuple[int, int]
    front_pos: tuple[int, int]


def build_photo_stack(cfg: Config, cache_dir=None) -> PhotoStack:
    """The child cut out of their photo, seated inside the clam shell."""
    shell = trim(chroma_key(Image.open(ART / "clam_shell.png")), pad=2)
    shell = scale_to_height(shell, SHELL_HEIGHT)
    sw, sh = shell.size

    shell_x = (cfg.width - sw) // 2
    shell_y = SHELL_BOTTOM - sh

    # Split the shell: the front lip is composited over the child.
    split = int(sh * SHELL_SPLIT)
    back = shell.copy()
    back.paste((0, 0, 0, 0), (0, split, sw, sh))
    front = shell.crop((0, split, sw, sh))

    subject = _prepare_subject(cfg, cache_dir)

    # The figure is sized by its *upper body* (head down to the waist), because
    # that is the part the viewer actually reads.
    body_scale = float(cfg.photo.get("body_scale", 0.62)) * float(cfg.photo.get("scale", 1.0))
    upper_target = max(60, int(round(sh * body_scale)))

    if cfg.photo.get("mermaid_tail", True):
        subject, waist = _add_mermaid_tail(subject, cfg, upper_target)
    else:
        subject = scale_to_height(subject, upper_target)
        waist = subject.height

    # The waist rests on the bowl, part way down the shell.
    waist_y = shell_y + int(round(sh * float(cfg.photo.get("seat", 0.38))))

    ox, oy = cfg.photo.get("offset", [0, 0])
    subj_x = (cfg.width - subject.width) // 2 + int(ox)
    subj_y = waist_y - waist + int(oy)

    return PhotoStack(
        shell_back=back,
        subject=subject,
        shell_front=front,
        shell_pos=(shell_x, shell_y),
        subject_pos=(subj_x, subj_y),
        front_pos=(shell_x, shell_y + split),
    )


def _prepare_subject(cfg: Config, cache_dir=None) -> Image.Image:
    src = cfg.photo_path()
    if not src.exists():
        raise FileNotFoundError(f"No photograph found at {src}")

    cache = None
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache = cache_dir / f"cutout_{src.stem}_{src.stat().st_mtime_ns}.png"
        if cache.exists():
            return Image.open(cache).convert("RGBA")

    img = Image.open(src).convert("RGBA")
    # Work at a sane resolution; the cutout is only ever ~700px tall.
    if img.height > 1600:
        img = img.resize((round(img.width * 1600 / img.height), 1600), Image.LANCZOS)

    if cfg.photo.get("remove_background", True):
        img = remove_background(img)
    subject = trim(img, pad=4)
    subject = _polish_cutout(subject)

    if cache is not None:
        subject.save(cache)
    return subject


def _polish_cutout(img: Image.Image) -> Image.Image:
    """Tidy the matte edge and add a faint rim light so the cutout sits in the scene."""
    arr = np.asarray(img, np.uint8).copy()
    alpha = Image.fromarray(arr[..., 3], "L")
    # Contract slightly to remove any halo from the original background, then soften.
    alpha = alpha.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(0.9))
    arr[..., 3] = np.asarray(alpha, np.uint8)
    cleaned = Image.fromarray(arr, "RGBA")

    out = Image.new("RGBA", cleaned.size, (0, 0, 0, 0))
    out.alpha_composite(outer_glow(alpha, (170, 240, 255, 150), 10, 0.9, spread=2))
    out.alpha_composite(cleaned)
    return out


def _add_mermaid_tail(subject: Image.Image, cfg: Config, upper_target: int) -> tuple[Image.Image, int]:
    """Crop the child at the waist and swap the legs for a mermaid tail.

    Returns the composite and the y of the waist within it, so the caller can
    seat the figure in the shell without guessing.
    """
    crop_at = float(cfg.photo.get("crop_at", 0.56))
    cut = max(40, min(subject.height - 1, int(subject.height * crop_at)))

    # Measure the body where it is cut, so the tail lines up with the torso.
    band = np.asarray(subject.getchannel("A"), np.float32)[max(0, cut - 40) : cut]
    cols = band.sum(axis=0)
    if cols.sum() > 1.0:
        centre = float((cols * np.arange(cols.size)).sum() / cols.sum())
    else:
        centre = subject.width / 2.0

    upper = subject.crop((0, 0, subject.width, cut))
    # Scale so the visible upper body matches the requested height.
    k = upper_target / max(upper.height, 1)
    upper = upper.resize((max(1, round(upper.width * k)), upper_target), Image.LANCZOS)
    centre *= k

    tail = trim(chroma_key(Image.open(ART / "mermaid_tail.png")), pad=2)
    tail_h = max(40, int(upper_target * float(cfg.photo.get("tail_length", 0.80))
                         * float(cfg.photo.get("tail_scale", 1.0))))
    tail = scale_to_height(tail, tail_h)
    tilt = float(cfg.photo.get("tail_tilt", 0.0))
    if abs(tilt) > 0.5:
        tail = trim(tail.rotate(tilt, resample=Image.BICUBIC, expand=True), pad=2)

    tx, ty = cfg.photo.get("tail_offset", [0, 0])
    # Overlap the waist a little so there is no seam.
    tail_x = int(round(centre - tail.width / 2)) + int(tx)
    tail_y = upper.height - int(tail.height * 0.14) + int(ty)

    left = min(0, tail_x)
    right = max(upper.width, tail_x + tail.width)
    bottom = max(upper.height, tail_y + tail.height)
    out = Image.new("RGBA", (right - left, bottom), (0, 0, 0, 0))

    glow = outer_glow(tail.getchannel("A"), (120, 255, 230, 120), 12, 0.8)
    out.alpha_composite(glow, (tail_x - left, tail_y))
    out.alpha_composite(tail, (tail_x - left, tail_y))
    out.alpha_composite(upper, (-left, 0))

    trimmed = trim(out, pad=2)
    # `trim` may shave transparent rows off the top; keep the waist honest.
    top_lost = _top_transparent_rows(out)
    return trimmed, upper.height - top_lost + 2


def _top_transparent_rows(img: Image.Image) -> int:
    alpha = np.asarray(img.getchannel("A"), np.uint8)
    rows = np.flatnonzero(alpha.max(axis=1) > 4)
    return int(rows[0]) if rows.size else 0


# --------------------------------------------------------------------------- #
#  Swimming decorations
# --------------------------------------------------------------------------- #
def scale_to_height(img: Image.Image, height: int) -> Image.Image:
    w = max(1, round(img.width * height / img.height))
    return img.resize((w, height), Image.LANCZOS)


def load_seahorse(height: int = 300) -> Image.Image:
    img = trim(chroma_key(Image.open(ART / "seahorse.png")), pad=2)
    return scale_to_height(img, height)


def load_fishes(height: int = 120) -> list[Image.Image]:
    """Split the generated triple-fish sheet into individual sprites."""
    sheet = chroma_key(Image.open(ART / "fishes.png"))
    alpha = np.asarray(sheet.getchannel("A"), np.float32) / 255.0
    cols = alpha.sum(axis=0)
    occupied = cols > 2.0

    spans: list[tuple[int, int]] = []
    start = None
    for i, v in enumerate(occupied):
        if v and start is None:
            start = i
        elif not v and start is not None:
            if i - start > 40:
                spans.append((start, i))
            start = None
    if start is not None and len(occupied) - start > 40:
        spans.append((start, len(occupied)))

    out = []
    for a, b in spans:
        piece = trim(sheet.crop((a, 0, b, sheet.height)), pad=2)
        out.append(scale_to_height(piece, height))
    return out or [scale_to_height(trim(sheet, pad=2), height)]


def load_background(width: int, height: int) -> Image.Image:
    """The sea background, cropped to fill the canvas."""
    bg = Image.open(ART / "sea_background.png").convert("RGBA")
    scale = max(width / bg.width, height / bg.height)
    # Extra headroom so the background can drift slowly without exposing edges.
    scale *= 1.06
    bg = bg.resize((math.ceil(bg.width * scale), math.ceil(bg.height * scale)), Image.LANCZOS)
    left = (bg.width - width) // 2
    top = (bg.height - height) // 2
    return bg.crop((left, top, left + width, top + height))
