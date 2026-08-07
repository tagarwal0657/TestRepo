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
    args = p.parse_args(argv)

    cfg = config_mod.load(args.config)
    BUILD.mkdir(parents=True, exist_ok=True)
    cache = BUILD / "cache"
    cache.mkdir(parents=True, exist_ok=True)

    if cfg.using_placeholder():
        _log("WARNING: using the bundled placeholder photograph. Put the real "
             f"picture at {cfg.photo['file']} and re-run.")

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
