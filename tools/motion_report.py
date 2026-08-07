#!/usr/bin/env python3
"""Measure how much actually moves, second by second.

Renders a per-second profile of frame-to-frame change so you can see at a glance
whether any stretch of the video has gone static, and whether the closing beats
are landing. Also reports motion inside a chosen region, which is how the
mermaid tail's bounce is confirmed.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import numpy as np


def probe(path: Path) -> tuple[int, int, float]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip().split(",")
    w, h = int(out[0]), int(out[1])
    num, den = out[2].split("/")
    return w, h, float(num) / float(den)


def frames(path: Path, w: int, h: int, scale: int = 4):
    sw, sh = w // scale, h // scale
    proc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", str(path),
         "-vf", f"scale={sw}:{sh}", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        stdout=subprocess.PIPE,
    )
    assert proc.stdout is not None
    size = sw * sh
    while True:
        buf = proc.stdout.read(size)
        if len(buf) < size:
            break
        yield np.frombuffer(buf, np.uint8).reshape(sh, sw).astype(np.int16)
    proc.wait()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("video")
    p.add_argument("--scale", type=int, default=4)
    p.add_argument("--min-motion", type=float, default=0.20,
                   help="mean absolute frame delta below which a second counts as static")
    args = p.parse_args()

    path = Path(args.video)
    w, h, fps = probe(path)
    sh = h // args.scale

    # Rows covering the shell and child, for the closing bounce.
    roi = slice(int(sh * 0.62), int(sh * 0.96))

    prev = None
    whole: list[float] = []
    lower: list[float] = []
    for f in frames(path, w, h, args.scale):
        if prev is not None:
            d = np.abs(f - prev)
            whole.append(float(d.mean()))
            lower.append(float(d[roi].mean()))
        prev = f

    n = len(whole)
    print(f"{path.name}: {w}x{h} @ {fps:g}fps, {n + 1} frames ({(n + 1) / fps:.1f}s)")
    print(f"\n{'sec':>5} {'whole frame':>12} {'shell region':>13}   profile")
    static = []
    per = int(round(fps))
    for s in range(0, n, per):
        chunk_w = whole[s : s + per]
        chunk_l = lower[s : s + per]
        if not chunk_w:
            break
        mw, ml = float(np.mean(chunk_w)), float(np.mean(chunk_l))
        bar = "#" * int(min(mw, 6.0) * 8)
        flag = "  <-- STATIC" if mw < args.min_motion else ""
        print(f"{s // per:5d} {mw:12.3f} {ml:13.3f}   {bar}{flag}")
        if mw < args.min_motion:
            static.append(s // per)

    print(f"\noverall mean motion : {np.mean(whole):.3f}")
    print(f"peak motion         : {np.max(whole):.3f} at {int(np.argmax(whole)) / fps:.2f}s")
    if static:
        print(f"\nFAIL: {len(static)} static second(s): {static}")
        return 1
    print("\nPASS: every second of the video contains motion")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
