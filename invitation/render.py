"""Frame compositor and video encoder."""

from __future__ import annotations

import math
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops

from . import artwork
from .artwork import CARD_RECT, Layer, PhotoStack, Shine
from .config import BUILD, Config
from .effects import (
    BubbleField,
    Caustics,
    SparkleField,
    add_light,
    build_god_rays,
    build_vignette,
    with_opacity,
)
from .textfx import shine_overlay
from .timeline import Sheet, clamp01, smoothstep


@dataclass
class Swimmer:
    sprite: Image.Image
    y: float
    speed: float          # px/sec, sign gives direction
    bob: float
    bob_period: float
    phase: float
    start: float


class Renderer:
    """Builds every static asset once, then composites frames on demand."""

    def __init__(self, cfg: Config, sheet: Sheet, duration: float, cache_dir: Path | None = None):
        self.cfg = cfg
        self.sheet = sheet
        self.duration = duration
        self.w, self.h = cfg.width, cfg.height
        self.fps = cfg.fps

        self.bg_full = artwork.load_background(self.w, self.h)
        self.drift_x = self.bg_full.width - self.w
        self.drift_y = self.bg_full.height - self.h

        self.caustics = Caustics(self.w, self.h, frames=96, strength=15.0, scale=6)
        self.god_rays = build_god_rays(self.w, self.h, strength=0.42)
        self.vignette = build_vignette(self.w, self.h, strength=0.46)

        self.bubbles_back = BubbleField(self.w, self.h, count=34, seed=1, r_range=(4, 15),
                                        speed=(18.0, 48.0), alpha=0.55)
        self.bubbles_front = BubbleField(self.w, self.h, count=26, seed=2, r_range=(9, 30),
                                         speed=(34.0, 92.0), alpha=0.85)
        self.bubbles_burst = BubbleField(self.w, 900, count=30, seed=3, r_range=(6, 24),
                                         speed=(90.0, 210.0), alpha=0.9)

        self.sparkles = SparkleField(self.w, self.h, count=26, seed=4, sizes=(11, 30))
        self.finale_sparkles = SparkleField(self.w, self.h, count=40, seed=8, sizes=(16, 52))
        self.finale_bubbles = BubbleField(self.w, self.h, count=40, seed=9, r_range=(8, 34),
                                          speed=(120.0, 260.0), alpha=0.9)
        name_layer_y = CARD_RECT[1] + 356
        self.name_sparkles = SparkleField(self.w, self.h, count=18, seed=6, sizes=(14, 40),
                                          region=(name_layer_y / self.h - 0.055,
                                                  name_layer_y / self.h + 0.055))

        self.card_avoid = (CARD_RECT[0] - 10, CARD_RECT[1] - 10, CARD_RECT[2] + 10, CARD_RECT[3] + 10)
        self.card_panel = Layer("card", artwork.build_card_panel(),
                                (CARD_RECT[0] + CARD_RECT[2]) // 2,
                                (CARD_RECT[1] + CARD_RECT[3]) // 2, "center")
        self.ribbon = Layer("ribbon", artwork.build_ribbon(cfg.card["ribbon"]), self.w // 2, 262, "center")
        self.text_layers, self.shine = artwork.build_card_text(cfg)
        self.photo = artwork.build_photo_stack(cfg, cache_dir)

        seahorse = artwork.load_seahorse(340)
        self.seahorse = Layer("seahorse", seahorse, self.w - 108, 1418, "center")

        fishes = artwork.load_fishes(112)
        self.swimmers: list[Swimmer] = []
        specs = [(0, 176.0, 34.0, 0.0), (1, -132.0, 1500.0, 1.6), (2, 108.0, 1676.0, 3.1)]
        for idx, speed, y, phase in specs:
            self.swimmers.append(Swimmer(
                sprite=fishes[idx % len(fishes)] if speed > 0
                else fishes[idx % len(fishes)].transpose(Image.FLIP_LEFT_RIGHT),
                y=y, speed=speed, bob=18.0, bob_period=2.6, phase=phase,
                start=0.0,
            ))

    # ------------------------------------------------------------------ #
    def _draw_layer(self, canvas: Image.Image, layer: Layer, t: float, key: str | None = None) -> None:
        alpha, dx, dy, scale = self.sheet.state(key or layer.key, t)
        if alpha <= 0.004:
            return
        img = layer.image
        x, y = layer.origin()
        if abs(scale - 1.0) > 0.004:
            nw = max(1, round(img.width * scale))
            nh = max(1, round(img.height * scale))
            x += (img.width - nw) // 2
            y += (img.height - nh) // 2
            img = img.resize((nw, nh), Image.BILINEAR)
        if alpha < 0.996:
            img = with_opacity(img, alpha)
        canvas.alpha_composite(img, (int(round(x + dx)), int(round(y + dy))))

    def _draw_photo_stack(self, canvas: Image.Image, t: float) -> None:
        alpha, dx, dy, _ = self.sheet.state("shell", t)
        if alpha <= 0.004:
            return
        ps: PhotoStack = self.photo
        ox, oy = int(round(dx)), int(round(dy))

        def blit(img: Image.Image, pos: tuple[int, int]) -> None:
            s = with_opacity(img, alpha) if alpha < 0.996 else img
            canvas.alpha_composite(s, (pos[0] + ox, pos[1] + oy))

        blit(ps.shell_back, ps.shell_pos)
        blit(ps.subject, ps.subject_pos)
        blit(ps.shell_front, ps.front_pos)

    def _draw_swimmers(self, canvas: Image.Image, t: float) -> None:
        span = self.w + 420
        for s in self.swimmers:
            travelled = abs(s.speed) * t % span
            x = travelled - 210 if s.speed > 0 else span - 210 - travelled
            y = s.y + s.bob * math.sin(math.tau * t / s.bob_period + s.phase)
            canvas.alpha_composite(s.sprite, (int(x), int(y - s.sprite.height / 2)))

    def _draw_shine(self, canvas: Image.Image, t: float) -> None:
        cue = self.sheet.cues.get("name_shine")
        if cue is None:
            return
        period = 4.6
        elapsed = t - cue.start
        if elapsed < 0:
            return
        phase = (elapsed % period) / cue.duration
        if phase > 1.0:
            return
        overlay = shine_overlay(self.shine.mask, phase, intensity=0.9)
        canvas.alpha_composite(overlay, self.shine.pos)

    # ------------------------------------------------------------------ #
    def frame(self, index: int) -> Image.Image:
        t = index / self.fps
        prog = clamp01(t / max(self.duration, 1e-6))

        # Background: a slow push-in plus a diagonal drift. Zooming only the
        # water keeps the card and its text pixel sharp.
        zoom = 1.0 + 0.055 * smoothstep(prog)
        cw, ch = int(self.w / zoom), int(self.h / zoom)
        ox = int((self.bg_full.width - cw) * (0.5 + 0.5 * math.sin(math.tau * t / 34.0)))
        oy = int((self.bg_full.height - ch) * (0.5 + 0.5 * math.sin(math.tau * t / 41.0 + 1.2)))
        canvas = self.bg_full.crop((ox, oy, ox + cw, oy + ch))
        if (cw, ch) != (self.w, self.h):
            canvas = canvas.resize((self.w, self.h), Image.BILINEAR)
        else:
            canvas = canvas.copy()

        canvas = add_light(canvas, self.caustics.get(index), 1.0)
        ray_pulse = 0.62 + 0.30 * math.sin(math.tau * t / 9.0)
        canvas = add_light(canvas, self.god_rays, ray_pulse)

        self.bubbles_back.draw(canvas, t, 0.62)
        self._draw_swimmers(canvas, t)

        self._draw_layer(canvas, self.ribbon, t)
        self._draw_layer(canvas, self.card_panel, t)
        for layer in self.text_layers:
            self._draw_layer(canvas, layer, t)
        self._draw_shine(canvas, t)

        # Sparkle flourish as the name lands.
        name_cue = self.sheet.cues.get("name")
        if name_cue is not None and t >= name_cue.start:
            burst = smoothstep(clamp01((t - name_cue.start) / 0.5))
            burst *= 1.0 - 0.45 * clamp01((t - name_cue.start - 1.4) / 2.0)
            self.name_sparkles.draw(canvas, t, burst)

        self._draw_layer(canvas, self.seahorse, t)
        self._draw_photo_stack(canvas, t)

        # Bubbles kicked up as the shell rises.
        shell_cue = self.sheet.cues.get("shell")
        if shell_cue is not None and t >= shell_cue.start:
            since = t - shell_cue.start
            strength = clamp01(since / 0.3) * (1.0 - clamp01((since - 1.1) / 1.6))
            if strength > 0.02:
                sub = Image.new("RGBA", (self.w, 900), (0, 0, 0, 0))
                self.bubbles_burst.draw(sub, t, strength * 0.85)
                canvas.alpha_composite(sub, (0, self.h - 900))

        # Foreground bubbles keep clear of the card so the text stays readable.
        card_alpha = self.sheet.state("card", t)[0]
        avoid = self.card_avoid if card_alpha > 0.1 else None
        self.bubbles_front.draw(canvas, t, 0.9, avoid=avoid)
        self.sparkles.draw(canvas, t, 0.55 + 0.25 * math.sin(math.tau * t / 7.0), avoid=avoid)

        # Closing flourish over the sign-off.
        finale = self.sheet.cues.get("finale")
        if finale is not None and t >= finale.start:
            since = t - finale.start
            strength = smoothstep(clamp01(since / 0.45))
            self.finale_bubbles.draw(canvas, t, strength * 0.75, avoid=avoid)
            self.finale_sparkles.draw(canvas, t, strength * 0.95)

        # Gentle fade up from black at the very start and out at the end.
        rgb = ImageChops.multiply(canvas.convert("RGB"), self.vignette)
        fade_in = clamp01(t / 0.7)
        fade_out = clamp01((self.duration - t) / 0.9)
        k = min(fade_in, fade_out)
        if k < 0.999:
            rgb = Image.blend(Image.new("RGB", rgb.size, (0, 0, 0)), rgb, smoothstep(k))
        return rgb


# --------------------------------------------------------------------------- #
#  Encoding
# --------------------------------------------------------------------------- #
def _encode_segment(renderer: Renderer, first: int, last: int, out: Path, crf: int) -> None:
    cmd = [
        "ffmpeg", "-v", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{renderer.w}x{renderer.h}", "-r", str(renderer.fps),
        "-i", "-",
        "-an",
        "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-g", str(renderer.fps * 2), "-keyint_min", str(renderer.fps),
        "-x264-params", "scenecut=0",
        str(out),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    assert proc.stdin is not None
    try:
        for i in range(first, last):
            proc.stdin.write(renderer.frame(i).tobytes())
    finally:
        proc.stdin.close()
        rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"ffmpeg failed while encoding frames {first}-{last}")


_WORKER: dict[str, object] = {}


def _worker_init(cfg: Config, sheet: Sheet, duration: float, cache_dir: Path) -> None:
    _WORKER["renderer"] = Renderer(cfg, sheet, duration, cache_dir)


def _worker_segment(job: tuple[int, int, str, int]) -> str:
    first, last, out, crf = job
    renderer: Renderer = _WORKER["renderer"]  # type: ignore[assignment]
    _encode_segment(renderer, first, last, Path(out), crf)
    return out


def render_video(
    cfg: Config,
    sheet: Sheet,
    duration: float,
    audio: Path,
    out_path: Path,
    jobs: int = 4,
    cache_dir: Path | None = None,
    progress=None,
) -> Path:
    total_frames = int(round(duration * cfg.fps))
    crf = int(cfg.video.get("crf", 19))
    cache_dir = cache_dir or BUILD / "cache"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    work = Path(tempfile.mkdtemp(prefix="invite_", dir=str(BUILD)))
    try:
        if jobs <= 1:
            renderer = Renderer(cfg, sheet, duration, cache_dir)
            seg = work / "seg_000.mp4"
            _encode_segment_with_progress(renderer, 0, total_frames, seg, crf, progress)
            segments = [seg]
        else:
            import multiprocessing as mp

            # Build the photo cutout once up front so workers reuse the cache.
            artwork.build_photo_stack(cfg, cache_dir)

            bounds = np.linspace(0, total_frames, jobs + 1).round().astype(int)
            jobs_list = [
                (int(bounds[i]), int(bounds[i + 1]), str(work / f"seg_{i:03d}.mp4"), crf)
                for i in range(jobs)
                if bounds[i + 1] > bounds[i]
            ]
            ctx = mp.get_context("fork")
            with ctx.Pool(len(jobs_list), initializer=_worker_init,
                          initargs=(cfg, sheet, duration, cache_dir)) as pool:
                segments = [Path(p) for p in pool.map(_worker_segment, jobs_list)]

        listing = work / "segments.txt"
        listing.write_text("".join(f"file '{s.name}'\n" for s in segments), encoding="utf-8")

        subprocess.run(
            [
                "ffmpeg", "-v", "error", "-y",
                "-f", "concat", "-safe", "0", "-i", str(listing),
                "-i", str(audio),
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
                "-movflags", "+faststart",
                "-shortest",
                str(out_path),
            ],
            check=True,
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return out_path


def _encode_segment_with_progress(renderer, first, last, out, crf, progress) -> None:
    cmd = [
        "ffmpeg", "-v", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{renderer.w}x{renderer.h}", "-r", str(renderer.fps),
        "-i", "-", "-an",
        "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-g", str(renderer.fps * 2),
        str(out),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    assert proc.stdin is not None
    try:
        for i in range(first, last):
            proc.stdin.write(renderer.frame(i).tobytes())
            if progress and (i - first) % 15 == 0:
                progress(i - first, last - first)
    finally:
        proc.stdin.close()
        rc = proc.wait()
    if rc != 0:
        raise RuntimeError("ffmpeg failed")
