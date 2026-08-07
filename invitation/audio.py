"""Voice-over synthesis and the procedural underwater music bed."""

from __future__ import annotations

import asyncio
import math
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .config import Config

SR = 48000


# --------------------------------------------------------------------------- #
#  ffmpeg helpers
# --------------------------------------------------------------------------- #
def decode(path: Path, filters: str | None = None) -> np.ndarray:
    cmd = ["ffmpeg", "-v", "error", "-i", str(path)]
    if filters:
        cmd += ["-af", filters]
    cmd += ["-ac", "1", "-ar", str(SR), "-f", "f32le", "-"]
    raw = subprocess.run(cmd, check=True, capture_output=True).stdout
    return np.frombuffer(raw, np.float32).astype(np.float32)


def write_wav(path: Path, samples: np.ndarray, sr: int = SR) -> None:
    data = np.clip(samples, -1.0, 1.0)
    pcm = (data * 32767.0).astype("<i2")
    import wave

    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1 if pcm.ndim == 1 else pcm.shape[1])
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


# --------------------------------------------------------------------------- #
#  Voice
# --------------------------------------------------------------------------- #
@dataclass
class SpokenLine:
    text: str
    start: float
    end: float


def _trim_silence(x: np.ndarray, threshold: float = 0.006, pad: int = 480) -> np.ndarray:
    loud = np.flatnonzero(np.abs(x) > threshold)
    if loud.size == 0:
        return x
    a = max(0, loud[0] - pad)
    b = min(len(x), loud[-1] + pad)
    return x[a:b]


async def _synthesise(text: str, voice: str, rate: str, pitch: str, out: Path) -> None:
    import edge_tts

    for attempt in range(4):
        try:
            comm = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
            await comm.save(str(out))
            if out.exists() and out.stat().st_size > 512:
                return
        except Exception:  # noqa: BLE001 - transient network failures are expected
            if attempt == 3:
                raise
        await asyncio.sleep(2 ** attempt)
    raise RuntimeError(f"Text-to-speech produced no audio for: {text!r}")


def build_voice(cfg: Config, work: Path) -> tuple[np.ndarray, list[SpokenLine]]:
    """Render every spoken line, pitch it up into toddler range, and lay it out."""
    work.mkdir(parents=True, exist_ok=True)
    v = cfg.voice
    semis = float(v.get("semitones", 0.0))

    # rubberband keeps the length and the consonants intact while moving the
    # pitch *and* the formants up, which is what makes an adult-sized voice read
    # as a toddler. Edge's own `pitch` parameter smears the audio, so it is left
    # at zero by default and the shift happens here instead.
    filters = None
    if abs(semis) >= 0.01:
        filters = f"rubberband=pitch={2 ** (semis / 12.0):.6f}"

    clips: list[np.ndarray] = []
    for i, text in enumerate(v["lines"]):
        mp3 = work / f"line_{i:02d}.mp3"
        if not mp3.exists():
            asyncio.run(_synthesise(text, v["name"], v.get("rate", "+0%"), v.get("pitch", "+0Hz"), mp3))
        clips.append(_trim_silence(decode(mp3, filters)))

    gap = int(float(v.get("gap", 0.25)) * SR)
    lead = int(float(cfg.video.get("lead_in", 1.0)) * SR)
    tail = int(float(cfg.video.get("tail_out", 2.0)) * SR)

    total = lead + sum(len(c) for c in clips) + gap * len(clips) + tail
    track = np.zeros(total, np.float32)

    lines: list[SpokenLine] = []
    cursor = lead
    for text, clip in zip(v["lines"], clips):
        track[cursor : cursor + len(clip)] += clip
        lines.append(SpokenLine(text, cursor / SR, (cursor + len(clip)) / SR))
        cursor += len(clip) + gap

    gain = 10 ** (float(v.get("gain_db", 0.0)) / 20.0)
    track = _normalise(track, peak=0.90) * gain
    return track.astype(np.float32), lines


def _normalise(x: np.ndarray, peak: float = 0.9) -> np.ndarray:
    m = float(np.max(np.abs(x))) if x.size else 0.0
    return x * (peak / m) if m > 1e-6 else x


# --------------------------------------------------------------------------- #
#  Music bed
# --------------------------------------------------------------------------- #
def _adsr(n: int, a: float, d: float, s: float, r: float) -> np.ndarray:
    a_n, d_n, r_n = int(a * SR), int(d * SR), int(r * SR)
    s_n = max(0, n - a_n - d_n - r_n)
    env = np.concatenate([
        np.linspace(0, 1, a_n, endpoint=False, dtype=np.float32) if a_n else np.zeros(0, np.float32),
        np.linspace(1, s, d_n, endpoint=False, dtype=np.float32) if d_n else np.zeros(0, np.float32),
        np.full(s_n, s, np.float32),
        np.linspace(s, 0, r_n, dtype=np.float32) if r_n else np.zeros(0, np.float32),
    ])
    if len(env) < n:
        env = np.pad(env, (0, n - len(env)))
    return env[:n]


def _hz(midi: float) -> float:
    return 440.0 * 2 ** ((midi - 69) / 12.0)


def _bell(freq: float, dur: float, amp: float = 1.0, seed: int = 0) -> np.ndarray:
    """A music-box / celesta style struck tone."""
    n = int(dur * SR)
    t = np.arange(n, dtype=np.float32) / SR
    partials = [(1.0, 1.00, 0.0), (2.0, 0.42, 0.0), (3.01, 0.20, 0.0), (4.9, 0.10, 0.0), (6.2, 0.05, 0.0)]
    out = np.zeros(n, np.float32)
    for mult, level, ph in partials:
        decay = np.exp(-t * (2.6 + mult * 1.5)).astype(np.float32)
        out += level * decay * np.sin(math.tau * freq * mult * t + ph)
    # A short breathy attack transient.
    rng = np.random.default_rng(seed)
    click = rng.normal(0, 1, min(n, int(0.006 * SR))).astype(np.float32)
    out[: len(click)] += click * 0.09
    return out * amp * 0.30


def _pad(freqs: list[float], dur: float, amp: float = 1.0) -> np.ndarray:
    n = int(dur * SR)
    t = np.arange(n, dtype=np.float32) / SR
    out = np.zeros(n, np.float32)
    for i, f in enumerate(freqs):
        detune = 1.0 + 0.0015 * (i - len(freqs) / 2)
        vib = 1.0 + 0.0018 * np.sin(math.tau * (0.7 + 0.11 * i) * t)
        out += np.sin(math.tau * f * detune * t * vib) * (0.6 ** i)
        out += 0.25 * np.sin(math.tau * 2 * f * detune * t)
    env = _adsr(n, 0.6, 0.4, 0.72, 0.9)
    return out / max(len(freqs), 1) * env * amp * 0.22


def _shaker(dur: float, seed: int) -> np.ndarray:
    n = int(dur * SR)
    rng = np.random.default_rng(seed)
    noise = rng.normal(0, 1, n).astype(np.float32)
    # Crude high-pass by differencing, then a fast decay.
    noise = np.diff(noise, prepend=noise[:1])
    env = np.exp(-np.arange(n, dtype=np.float32) / SR * 42.0)
    return noise * env * 0.10


def _bubble_blip(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    dur = float(rng.uniform(0.10, 0.20))
    n = int(dur * SR)
    t = np.arange(n, dtype=np.float32) / SR
    f0 = float(rng.uniform(380, 760))
    f1 = f0 * float(rng.uniform(2.4, 4.2))
    freq = f0 * (f1 / f0) ** (t / max(dur, 1e-6))
    phase = math.tau * np.cumsum(freq) / SR
    env = np.exp(-t * 26.0) * (1 - np.exp(-t * 400.0))
    return (np.sin(phase) * env).astype(np.float32) * 0.16


def _reverb(x: np.ndarray, mix: float = 0.28, decay: float = 1.9) -> np.ndarray:
    """A small Schroeder-style reverb: parallel combs into serial all-passes."""
    out = np.zeros_like(x)
    for delay_ms, gain in ((29.7, 0.80), (37.1, 0.76), (41.1, 0.73), (43.7, 0.70)):
        d = int(delay_ms * 0.001 * SR)
        buf = np.copy(x)
        g = gain ** (1.0 / max(decay, 0.1))
        # Feedback comb, applied blockwise for speed.
        for start in range(d, len(buf), d):
            end = min(start + d, len(buf))
            buf[start:end] += buf[start - d : start - d + (end - start)] * g
        out += buf
    out /= 4.0
    for delay_ms, g in ((5.0, 0.7), (1.7, 0.7)):
        d = int(delay_ms * 0.001 * SR)
        tmp = np.copy(out)
        for start in range(d, len(tmp), d):
            end = min(start + d, len(tmp))
            tmp[start:end] += tmp[start - d : start - d + (end - start)] * -g
        out = tmp
    out = np.nan_to_num(out, nan=0.0, posinf=0.0, neginf=0.0)
    out = _normalise(out, peak=float(np.max(np.abs(x))) if x.size else 0.0)
    return x * (1 - mix) + out * mix


def build_music(duration: float, seed: int = 42) -> np.ndarray:
    """A gentle music-box waltz with an underwater ambience underneath."""
    n = int(duration * SR)
    music = np.zeros(n + SR, np.float32)

    bpm = 92.0
    beat = 60.0 / bpm
    bar = beat * 4

    # I - V - vi - IV in D major, one chord per bar.
    progression = [
        (50, [62, 66, 69, 74]),   # D
        (45, [61, 64, 69, 73]),   # A/C#
        (47, [59, 62, 66, 71]),   # Bm
        (43, [55, 59, 62, 67]),   # G
    ]

    def place(buf: np.ndarray, clip: np.ndarray, at: float) -> None:
        i = int(at * SR)
        if i >= len(buf):
            return
        j = min(len(buf), i + len(clip))
        buf[i:j] += clip[: j - i]

    melody = np.zeros_like(music)
    pads = np.zeros_like(music)
    perc = np.zeros_like(music)
    bass = np.zeros_like(music)

    bar_index = 0
    t = 0.0
    while t < duration:
        root, chord = progression[bar_index % len(progression)]
        place(pads, _pad([_hz(m) for m in chord], bar * 1.05, amp=1.0), t)
        place(bass, _bell(_hz(root - 12), bar * 0.9, amp=0.85, seed=bar_index), t)

        # Music box arpeggio: eight notes per bar, wandering up and down.
        pattern = [0, 1, 2, 3, 2, 3, 1, 2] if bar_index % 2 == 0 else [3, 2, 1, 0, 1, 2, 3, 2]
        for k, step in enumerate(pattern):
            note = chord[step % len(chord)] + (12 if k % 4 == 3 else 0)
            when = t + k * beat / 2
            if when >= duration:
                break
            amp = 0.9 if k % 2 == 0 else 0.62
            place(melody, _bell(_hz(note), 1.1, amp=amp, seed=bar_index * 8 + k), when)

        for k in range(8):
            when = t + k * beat / 2
            if when < duration:
                place(perc, _shaker(0.11, bar_index * 8 + k), when)

        t += bar
        bar_index += 1

    # Underwater ambience: filtered noise plus the odd bubble.
    rng = np.random.default_rng(seed)
    amb = rng.normal(0, 1, len(music)).astype(np.float32)
    kernel = np.ones(320, np.float32) / 320.0
    amb = np.convolve(amb, kernel, mode="same").astype(np.float32)
    amb *= 0.055
    swell = 0.6 + 0.4 * np.sin(np.arange(len(music), dtype=np.float32) / SR * 0.25 * math.tau)
    amb *= swell

    blips = np.zeros_like(music)
    bt = 0.6
    i = 0
    while bt < duration:
        place(blips, _bubble_blip(seed + i), bt)
        bt += float(rng.uniform(0.9, 2.6))
        i += 1

    mix = melody * 0.85 + pads * 0.95 + bass * 0.55 + perc * 0.5 + amb + blips * 0.5
    mix = _reverb(mix, mix=0.30)

    # Fade in and out.
    fade = int(1.6 * SR)
    mix[:fade] *= np.linspace(0, 1, fade, dtype=np.float32)
    tail = int(2.2 * SR)
    mix[-tail:] *= np.linspace(1, 0, tail, dtype=np.float32)

    return _normalise(mix[:n], peak=0.85).astype(np.float32)


# --------------------------------------------------------------------------- #
#  Mixdown
# --------------------------------------------------------------------------- #
def _envelope(x: np.ndarray, attack: float, release: float) -> np.ndarray:
    """A one-pole follower over the absolute value of ``x``."""
    a = math.exp(-1.0 / (attack * SR))
    r = math.exp(-1.0 / (release * SR))
    mag = np.abs(x)
    # Downsample for speed, then interpolate back.
    step = 64
    coarse = mag[: len(mag) // step * step].reshape(-1, step).max(axis=1)
    env = np.empty_like(coarse)
    prev = 0.0
    for i, v in enumerate(coarse):
        coef = a if v > prev else r
        prev = coef * prev + (1 - coef) * v
        env[i] = prev
    full = np.interp(np.arange(len(x)), np.arange(len(env)) * step, env).astype(np.float32)
    return full


def mix(cfg: Config, voice: np.ndarray, music: np.ndarray) -> np.ndarray:
    n = max(len(voice), len(music))
    voice = np.pad(voice, (0, n - len(voice)))
    music = np.pad(music, (0, n - len(music)))

    m_gain = 10 ** (float(cfg.music.get("gain_db", -14.0)) / 20.0)
    duck_db = float(cfg.music.get("duck_db", -8.0))

    env = _envelope(voice, attack=0.02, release=0.35)
    env = np.clip(env / max(float(env.max()), 1e-6), 0, 1)
    duck = 10 ** ((duck_db * env) / 20.0)

    out = voice + music * m_gain * duck
    peak = float(np.max(np.abs(out)))
    if peak > 0.97:
        out *= 0.97 / peak
    return out.astype(np.float32)


def build_audio(cfg: Config, work: Path) -> tuple[Path, list[SpokenLine], float]:
    """Produce the final mixed soundtrack and report where each line lands."""
    voice, lines = build_voice(cfg, work / "tts")
    duration = len(voice) / SR
    music = build_music(duration)
    final = mix(cfg, voice, music)

    out = work / "soundtrack.wav"
    write_wav(out, final)
    return out, lines, len(final) / SR
