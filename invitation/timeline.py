"""Animation timing: easing curves and the cue sheet that drives every layer."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .audio import SpokenLine


# --------------------------------------------------------------------------- #
#  Easing
# --------------------------------------------------------------------------- #
def clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def ease_out_cubic(t: float) -> float:
    return 1.0 - (1.0 - t) ** 3


def ease_out_quint(t: float) -> float:
    return 1.0 - (1.0 - t) ** 5


def ease_out_back(t: float, overshoot: float = 1.9) -> float:
    c1 = overshoot
    c3 = c1 + 1.0
    return 1.0 + c3 * (t - 1.0) ** 3 + c1 * (t - 1.0) ** 2


def ease_out_elastic(t: float) -> float:
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    p = 0.42
    return 2 ** (-9 * t) * math.sin((t - p / 4) * math.tau / p) + 1.0


def smoothstep(t: float) -> float:
    return t * t * (3.0 - 2.0 * t)


EASINGS = {
    "cubic": ease_out_cubic,
    "quint": ease_out_quint,
    "back": ease_out_back,
    "elastic": ease_out_elastic,
    "smooth": smoothstep,
    "linear": lambda t: t,
}


# --------------------------------------------------------------------------- #
#  Cues
# --------------------------------------------------------------------------- #
@dataclass
class Cue:
    """How one layer enters, plus its idle motion once it has arrived."""

    start: float
    duration: float = 0.7
    easing: str = "cubic"
    from_offset: tuple[float, float] = (0.0, 0.0)
    from_scale: float = 1.0
    fade: bool = True
    # Continuous drift after the entrance: amplitude in px, period in seconds.
    float_y: float = 0.0
    float_x: float = 0.0
    float_period: float = 4.0
    float_phase: float = 0.0

    def state(self, t: float) -> tuple[float, float, float, float]:
        """``(alpha, dx, dy, scale)`` at time ``t``."""
        if t < self.start:
            return 0.0, 0.0, 0.0, self.from_scale
        p = clamp01((t - self.start) / max(self.duration, 1e-6))
        e = EASINGS[self.easing](p)

        alpha = clamp01(p / 0.55) if self.fade else 1.0
        dx = self.from_offset[0] * (1.0 - e)
        dy = self.from_offset[1] * (1.0 - e)
        scale = self.from_scale + (1.0 - self.from_scale) * e

        if p >= 1.0 and (self.float_y or self.float_x):
            ft = t - (self.start + self.duration)
            w = math.tau / max(self.float_period, 1e-6)
            dy += self.float_y * math.sin(w * ft + self.float_phase)
            dx += self.float_x * math.cos(w * ft + self.float_phase)
        return alpha, dx, dy, scale


@dataclass
class Sheet:
    """Cue lookup with speech-relative anchors."""

    lines: list[SpokenLine]
    lead_in: float
    total: float
    cues: dict[str, Cue] = field(default_factory=dict)

    def at(self, index: int, offset: float = 0.0) -> float:
        """Start of spoken line ``index`` (clamped), shifted by ``offset``."""
        if not self.lines:
            return self.lead_in + offset
        i = max(0, min(index, len(self.lines) - 1))
        return self.lines[i].start + offset

    def end_of(self, index: int, offset: float = 0.0) -> float:
        if not self.lines:
            return self.lead_in + offset
        i = max(0, min(index, len(self.lines) - 1))
        return self.lines[i].end + offset

    def add(self, key: str, **kwargs) -> None:
        self.cues[key] = Cue(**kwargs)

    def state(self, key: str, t: float) -> tuple[float, float, float, float]:
        cue = self.cues.get(key)
        if cue is None:
            return 1.0, 0.0, 0.0, 1.0
        return cue.state(t)


def build_sheet(lines: list[SpokenLine], lead_in: float, total: float, detail_count: int) -> Sheet:
    """The cue sheet, hung off the spoken lines so picture and voice agree.

    Line order assumed by the default script:
      0 greeting · 1 name and age · 2 the party · 3 date and time · 4 venue
      5 blessings · 6 sign-off
    Extra lines are ignored; missing ones collapse onto the last line available.
    """
    s = Sheet(lines=lines, lead_in=lead_in, total=total)

    s.add("ribbon", start=max(0.0, lead_in - 1.05), duration=1.0, easing="back",
          from_offset=(0, -260), float_y=7.0, float_period=5.0)

    s.add("card", start=max(0.0, lead_in - 0.55), duration=0.95, easing="back",
          from_scale=0.80, float_y=5.0, float_period=6.4, float_phase=0.8)

    s.add("eyebrow", start=s.at(0, 0.35), duration=0.6, easing="cubic", from_offset=(0, 26))
    s.add("tagline", start=s.at(0, 0.62), duration=0.75, easing="back", from_scale=0.72)
    s.add("join", start=s.at(0, 0.95), duration=0.55, easing="cubic", from_offset=(0, 22))

    s.add("name", start=s.at(1, 0.15), duration=0.95, easing="elastic", from_scale=0.55)
    s.add("occasion", start=s.at(1, 1.25), duration=0.85, easing="back", from_scale=0.60)

    s.add("shell", start=s.at(2, -0.25), duration=1.35, easing="quint",
          from_offset=(0, 720), float_y=9.0, float_period=5.6)

    s.add("rule", start=s.at(3, -0.35), duration=0.6, easing="cubic", from_scale=0.4)

    # Date, then time, then venue: one per beat, each riding its own spoken line.
    detail_cues = [(3, 0.10), (3, 1.05), (4, 0.15)]
    for i in range(detail_count):
        line_i, off = detail_cues[i] if i < len(detail_cues) else (4, 0.15 + 0.5 * i)
        s.add(f"detail{i}_label", start=s.at(line_i, off), duration=0.55, easing="back", from_scale=0.5)
        s.add(f"detail{i}_value", start=s.at(line_i, off + 0.18), duration=0.6, easing="cubic", from_offset=(0, 26))
        s.add(f"detail{i}_sep", start=s.at(line_i, off + 0.3), duration=0.5, easing="cubic")

    s.add("footer", start=s.at(5, 0.3), duration=0.7, easing="cubic", from_offset=(0, 18))

    s.add("seahorse", start=s.at(2, 0.5), duration=1.0, easing="cubic",
          from_offset=(190, 0), float_y=16.0, float_period=3.4)

    # The gloss sweep across the name, repeated a few times.
    s.add("name_shine", start=s.at(1, 0.95), duration=0.9, easing="linear", fade=False)

    return s
