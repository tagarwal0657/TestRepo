"""Loading and validation of ``invite.yaml``."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
ART = ASSETS / "art"
FONTS = ASSETS / "fonts"
BUILD = ROOT / "build"


@dataclass
class Detail:
    label: str
    lines: list[str]


@dataclass
class Config:
    raw: dict[str, Any]

    child: dict[str, Any] = field(default_factory=dict)
    card: dict[str, Any] = field(default_factory=dict)
    details: list[Detail] = field(default_factory=list)
    photo: dict[str, Any] = field(default_factory=dict)
    voice: dict[str, Any] = field(default_factory=dict)
    music: dict[str, Any] = field(default_factory=dict)
    video: dict[str, Any] = field(default_factory=dict)
    footer: str = ""

    @property
    def width(self) -> int:
        return int(self.video["width"])

    @property
    def height(self) -> int:
        return int(self.video["height"])

    @property
    def fps(self) -> int:
        return int(self.video["fps"])

    def photo_path(self) -> Path:
        """The photograph to composite, falling back to the bundled stand-in."""
        primary = ROOT / self.photo["file"]
        if primary.exists():
            return primary
        # Accept any extension the user happened to drop in.
        for candidate in sorted((ROOT / primary.parent).glob(primary.stem + ".*")):
            if candidate.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}:
                return candidate
        return ROOT / self.photo["fallback"]

    def using_placeholder(self) -> bool:
        return self.photo_path() == ROOT / self.photo["fallback"]


def load(path: Path | str | None = None) -> Config:
    path = Path(path) if path else ROOT / "invite.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))

    missing = [k for k in ("child", "card", "details", "photo", "voice", "video") if k not in raw]
    if missing:
        raise ValueError(f"{path.name} is missing required section(s): {', '.join(missing)}")

    return Config(
        raw=raw,
        child=raw["child"],
        card=raw["card"],
        details=[Detail(d["label"], list(d["lines"])) for d in raw["details"]],
        photo=raw["photo"],
        voice=raw["voice"],
        music=raw.get("music", {}),
        video=raw["video"],
        footer=raw.get("footer", ""),
    )
