#!/usr/bin/env python3
"""Fail loudly if anything covers the text.

The card, the child cut-out and the decorations are positioned from measured
sprite sizes, so a longer name or a taller photo can silently push something on
top of a word. This walks the whole timeline, rasterises the ink of every text
layer and the opaque body of every sprite drawn above it, and reports any
overlap — plus anything that has drifted off the edge of the frame.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from invitation import audio as audio_mod  # noqa: E402
from invitation import config as config_mod  # noqa: E402
from invitation.config import BUILD  # noqa: E402
from invitation.render import Renderer  # noqa: E402
from invitation.timeline import build_sheet  # noqa: E402

INK = 0.55  # alpha above which a pixel counts as solid


def mask_of(sprite, pos, size, threshold=INK) -> np.ndarray:
    """Place a sprite's alpha onto a full-frame boolean mask."""
    w, h = size
    out = np.zeros((h, w), bool)
    a = np.asarray(sprite.getchannel("A"), np.float32) / 255.0
    x, y = pos
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(w, x + sprite.width), min(h, y + sprite.height)
    if x0 >= x1 or y0 >= y1:
        return out
    out[y0:y1, x0:x1] = a[y0 - y : y1 - y, x0 - x : x1 - x] > threshold
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default=None)
    p.add_argument("--max-overlap", type=int, default=0,
                   help="pixels of text ink allowed to be covered")
    args = p.parse_args()

    cfg = config_mod.load(args.config)
    cache = BUILD / "cache"
    voice, lines = audio_mod.build_voice(cfg, cache / "tts")
    duration = len(voice) / audio_mod.SR
    sheet = build_sheet(lines, float(cfg.video.get("lead_in", 1.0)), duration, len(cfg.details))
    r = Renderer(cfg, sheet, duration, cache)
    size = (cfg.width, cfg.height)

    failures: list[str] = []

    # 1. Text ink versus the things drawn on top of it.
    ps = r.photo
    covering = {
        "child": mask_of(ps.subject, ps.subject_pos, size),
        "shell_front": mask_of(ps.shell_front, ps.front_pos, size),
        "shell_back": mask_of(ps.shell_back, ps.shell_pos, size),
        "seahorse": mask_of(r.seahorse.image, r.seahorse.origin(), size),
    }

    print(f"{'text layer':22s} {'covered by':14s} {'px':>7s}")
    print("-" * 46)
    for layer in r.text_layers:
        ink = mask_of(layer.image, layer.origin(), size, threshold=0.7)
        total = int(ink.sum())
        if total == 0:
            continue
        for name, other in covering.items():
            hit = int((ink & other).sum())
            if hit > args.max_overlap:
                print(f"{layer.key:22s} {name:14s} {hit:7d}   <-- {100 * hit / total:.1f}% of its ink")
                failures.append(f"{name} covers {hit}px of '{layer.key}' ({100 * hit / total:.1f}%)")

    # 2. Text against other text: a long name or a big numeral can collide.
    print("\ntext against text:")
    inks = {}
    for layer in r.text_layers:
        m = mask_of(layer.image, layer.origin(), size, threshold=0.7)
        if m.any():
            inks[layer.key] = m
    keys = list(inks)
    clean = True
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            hit = int((inks[a] & inks[b]).sum())
            if hit > args.max_overlap:
                clean = False
                print(f"  OVERLAP  {a} <-> {b}: {hit} px")
                failures.append(f"'{a}' and '{b}' overlap by {hit}px")
    if clean:
        print("  ok       no text collides with other text")

    # 3. Anything hanging off the edge of the frame.
    print("\nframe bounds:")
    everything = [("ribbon", r.ribbon), ("card", r.card_panel), *((l.key, l) for l in r.text_layers)]
    for key, layer in everything:
        x, y = layer.origin()
        x1, y1 = x + layer.image.width, y + layer.image.height
        # Sprites carry transparent padding, so measure the ink, not the box.
        ink = mask_of(layer.image, (x, y), size, threshold=0.7)
        if ink.sum() == 0:
            continue
        ys, xs = np.where(ink)
        bad = xs.min() < 6 or xs.max() > cfg.width - 6 or ys.min() < 6 or ys.max() > cfg.height - 6
        clipped = x < 0 or y < 0 or x1 > cfg.width or y1 > cfg.height
        if bad:
            print(f"  OFF-FRAME  {key}: ink x[{xs.min()},{xs.max()}] y[{ys.min()},{ys.max()}]")
            failures.append(f"'{key}' ink reaches the frame edge")
        elif clipped:
            print(f"  ok(pad)    {key}: padding clipped, ink safe at "
                  f"x[{xs.min()},{xs.max()}] y[{ys.min()},{ys.max()}]")
        else:
            print(f"  ok         {key}")

    # 4. Text must stay inside the card.
    from invitation.artwork import CARD_RECT

    print("\ninside the card panel:")
    for layer in r.text_layers:
        if layer.key == "footer":
            continue  # deliberately below the card
        ink = mask_of(layer.image, layer.origin(), size, threshold=0.7)
        if ink.sum() == 0:
            continue
        ys, xs = np.where(ink)
        inside = (xs.min() >= CARD_RECT[0] and xs.max() <= CARD_RECT[2]
                  and ys.min() >= CARD_RECT[1] and ys.max() <= CARD_RECT[3])
        print(f"  {'ok      ' if inside else 'OUTSIDE '} {layer.key}")
        if not inside:
            failures.append(f"'{layer.key}' spills outside the card")

    print()
    if failures:
        print(f"FAIL ({len(failures)} problem(s)):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS: no text is covered, clipped or outside the card")
    return 0


if __name__ == "__main__":
    sys.exit(main())
