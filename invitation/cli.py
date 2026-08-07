"""Command line entry point: ``python -m invitation``."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import audio as audio_mod
from . import config as config_mod
from .config import BUILD
from .render import Renderer, render_video
from .timeline import build_sheet


def _log(msg: str) -> None:
    print(f"[invite] {msg}", flush=True)


def write_cutout_preview(cfg, cache: Path) -> Path:
    """Show the segmented photo with the crop line drawn across it."""
    from PIL import Image, ImageDraw

    from .artwork import _prepare_subject

    subject = _prepare_subject(cfg, cache)
    canvas = Image.new("RGBA", subject.size, (18, 52, 104, 255))
    canvas.alpha_composite(subject)

    d = ImageDraw.Draw(canvas)
    y = int(subject.height * float(cfg.photo.get("crop_at", 0.56)))
    for x in range(0, subject.width, 36):
        d.line([(x, y), (min(x + 20, subject.width), y)], fill=(255, 80, 120, 255), width=5)
    d.text((12, max(0, y - 34)), f"crop_at = {cfg.photo.get('crop_at', 0.56)}",
           fill=(255, 210, 225, 255))

    out = cache.parent / "cutout_preview.png"
    canvas.convert("RGB").save(out)
    return out


def _prepare(cfg, cache: Path, want_music: bool):
    """Synthesise the voice (and optionally the music) and build the cue sheet."""
    t0 = time.time()
    voice, lines = audio_mod.build_voice(cfg, cache / "tts")
    duration = len(voice) / audio_mod.SR
    _log(f"voice: {len(lines)} lines, {duration:.2f}s ({time.time() - t0:.1f}s)")

    track = None
    if want_music:
        t0 = time.time()
        music = audio_mod.build_music(duration)
        mixed = audio_mod.mix(cfg, voice, music)
        track = cache / "soundtrack.wav"
        audio_mod.write_wav(track, mixed)
        _log(f"music + mix ready ({time.time() - t0:.1f}s)")

    sheet = build_sheet(lines, float(cfg.video.get("lead_in", 1.0)), duration, len(cfg.details))
    return sheet, duration, track, lines


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Render the animated invitation video.")
    p.add_argument("--config", default=None, help="path to invite.yaml")
    p.add_argument("--out", default=None, help="output .mp4 path")
    p.add_argument("--jobs", type=int, default=4, help="parallel encoder workers")
    p.add_argument("--still", type=float, default=None, metavar="SECONDS",
                   help="render a single frame to PNG instead of the full video")
    p.add_argument("--stills", type=str, default=None,
                   help="comma separated seconds; renders a PNG per timestamp")
    p.add_argument("--poster", action="store_true",
                   help="also write a still of the finished card as poster.png")
    p.add_argument("--cutout", action="store_true",
                   help="write build/cutout_preview.png showing the photo cut out with the "
                        "crop_at line marked, then exit; use it to dial photo.crop_at in")
    args = p.parse_args(argv)

    cfg = config_mod.load(args.config)
    BUILD.mkdir(parents=True, exist_ok=True)
    cache = BUILD / "cache"
    cache.mkdir(parents=True, exist_ok=True)

    if cfg.using_placeholder():
        _log("WARNING: using the bundled placeholder photograph. Put the real "
             f"picture at {cfg.photo['file']} and re-run.")

    if args.cutout:
        out = write_cutout_preview(cfg, cache)
        _log(f"wrote {out}")
        _log("The dashed line is photo.crop_at: everything below it is replaced by "
             "the tail. Raise crop_at to keep more of the body, lower it to cut higher.")
        return 0

    want_video = args.still is None and args.stills is None
    sheet, duration, track, lines = _prepare(cfg, cache, want_music=want_video)

    for i, ln in enumerate(lines):
        _log(f"  line {i}: {ln.start:6.2f}s -> {ln.end:6.2f}s  {ln.text}")

    if not want_video:
        renderer = Renderer(cfg, sheet, duration, cache)
        times = []
        if args.still is not None:
            times.append(args.still)
        if args.stills:
            times.extend(float(x) for x in args.stills.split(","))
        for sec in times:
            frame = renderer.frame(int(round(sec * cfg.fps)))
            out = BUILD / f"still_{sec:07.2f}s.png".replace(" ", "0")
            frame.save(out)
            _log(f"wrote {out}")
        return 0

    out_path = Path(args.out) if args.out else BUILD / "invitation.mp4"
    _log(f"rendering {int(round(duration * cfg.fps))} frames at "
         f"{cfg.width}x{cfg.height}@{cfg.fps} with {args.jobs} worker(s)")
    t0 = time.time()
    render_video(cfg, sheet, duration, track, out_path, jobs=args.jobs, cache_dir=cache)
    _log(f"wrote {out_path} ({time.time() - t0:.1f}s)")

    if args.poster:
        renderer = Renderer(cfg, sheet, duration, cache)
        poster = BUILD / "poster.png"
        renderer.frame(int(round((duration - 2.2) * cfg.fps))).save(poster)
        _log(f"wrote {poster}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
