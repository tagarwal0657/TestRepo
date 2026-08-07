#!/usr/bin/env python3
"""Check the finished soundtrack still says what the script says.

Pitching a neural voice up into toddler range, then burying it under a music
bed, can wreck intelligibility. This runs the *final mix* -- exactly what ships
in the mp4 -- through Whisper and reports how much of the script survived.

Proper nouns are reported separately: a recogniser will happily write "Dial
International" for "Dayal International" even when the audio is perfectly
clear, so those are matched phonetically rather than exactly.
"""

from __future__ import annotations

import argparse
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from invitation import config as config_mod  # noqa: E402

# Digits a recogniser writes where the script spells the word out.
NUMBER_WORDS = {
    "1": "one", "2": "two", "3": "three", "4": "four", "5": "five",
    "6": "six", "7": "seven", "8": "eight", "9": "nine", "10": "ten",
    "11": "eleven", "12": "twelve", "15": "fifteen", "15th": "fifteenth",
    "1st": "first", "2nd": "second", "pm": "p m",
}


def normalise(text: str) -> list[str]:
    text = text.lower().replace("'", "")
    out: list[str] = []
    for word in re.findall(r"[a-z0-9]+", text):
        out.extend(NUMBER_WORDS.get(word, word).split())
    return out


def soundalike(word: str) -> str:
    """A crude phonetic key, enough to accept 'saki' for 'sakchi'."""
    w = re.sub(r"[^a-z]", "", word.lower())
    w = w.replace("ph", "f").replace("ck", "k").replace("ch", "k")
    w = re.sub(r"[aeiouy]+", "", w)
    w = re.sub(r"(.)\1+", r"\1", w)
    return w or word


def transcribe(audio: Path, model_size: str = "base.en") -> str:
    from faster_whisper import WhisperModel

    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(audio), beam_size=5, language="en")
    return " ".join(s.text.strip() for s in segments)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("audio", help="mp4 or wav to check")
    p.add_argument("--config", default=None)
    p.add_argument("--model", default="base.en")
    p.add_argument("--min-ratio", type=float, default=0.80)
    args = p.parse_args()

    cfg = config_mod.load(args.config)
    expected = normalise(" ".join(cfg.voice["lines"]))
    raw = transcribe(Path(args.audio), args.model)
    heard = normalise(raw)

    matcher = SequenceMatcher(None, expected, heard)
    ratio = matcher.ratio()
    matched = sum(b.size for b in matcher.get_matching_blocks())

    print("script    :", " ".join(expected))
    print("recognised:", " ".join(heard))
    print(f"\nwords matched in order : {matched}/{len(expected)}")
    print(f"similarity ratio       : {ratio:.3f}  (threshold {args.min_ratio})")

    # Facts a guest must be able to act on.
    facts = {
        "date": ["fifteenth", "august"],
        "time": ["twelve", "onwards"],
        "venue": ["international"],
        "age": ["turning", "one"],
    }
    print("\nkey facts heard:")
    ok = True
    for label, terms in facts.items():
        missing = [t for t in terms if t not in heard]
        print(f"  {'OK    ' if not missing else 'MISSING'}  {label:6s} {terms}"
              + (f"  -> missing {missing}" if missing else ""))
        ok &= not missing

    # Names only need to be phonetically close.
    heard_keys = {soundalike(w) for w in heard}
    names = [cfg.child["name"].split()[0]] + cfg.child["name"].split()[1:] + ["Sakchi", "Dayal"]
    print("\nnames (phonetic match):")
    for n in names:
        hit = soundalike(n) in heard_keys
        print(f"  {'OK    ' if hit else 'weak  '}  {n}")

    if ratio < args.min_ratio:
        print("\nFAIL: the transcript drifted too far from the script")
        return 1
    if not ok:
        print("\nFAIL: a key fact was not intelligible")
        return 1
    print("\nPASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
