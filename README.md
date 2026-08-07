# Mermaid Birthday Invitation Video

Generates a vertical (1080x1920) animated birthday-invitation reel with an
under-the-sea mermaid theme, a narrated voice-over in a toddler voice, and a
music bed — all from a single configuration file.

Built to match the style of a "Gentle Reminder" invitation reel: an ornate ivory
card over an underwater scene, the child cut out of their own photograph and
seated in a clam shell wearing a mermaid tail, with details revealed in time
with the narration.

## Quick start

```bash
# 1. Drop the photograph in place
cp /path/to/your/photo.jpg assets/photo/baby.jpg

# 2. Edit the names, date, venue and spoken script
$EDITOR invite.yaml

# 3. Render
./make_invite.sh
```

The result lands in `build/invitation.mp4`. The first run creates a virtualenv,
installs dependencies, and downloads the background-removal model (~180 MB), so
it takes a few minutes; later runs take well under a minute.

`ffmpeg` must be on `PATH`, built with `libx264`, `librubberband` and `aac`
(the stock Debian/Ubuntu `ffmpeg` package qualifies).

## Everything lives in `invite.yaml`

Names, date, time, venue, footer, the spoken script, voice settings, music
level and video settings are all in that one file. Nothing else needs editing
for a different child, a different date, or a different age.

Two things are worth knowing:

- **The `voice.lines` are spelled for pronunciation, not for display.** The
  default script says `Luvya` so that a US English voice pronounces *Lavya*
  correctly. Nothing in that list appears on screen — respell freely until it
  sounds right.
- **`voice.pitch` should stay at `+0Hz`.** Edge's own pitch shift smears
  consonants; `voice.semitones` does the same job with `rubberband` and sounds
  much cleaner. Around `1.5` reads as a toddler and stays easy to follow.

## Fitting the photograph

The child is cut out automatically, cropped at the waist, given a mermaid tail
and seated on the clam shell. Any full-length or waist-up photo works. If the
fit is off, these keys under `photo:` adjust it:

| Key | What it does |
| --- | --- |
| `crop_at` | Where the body is cut for the tail, as a fraction of the cutout height. Lower cuts higher up. |
| `body_scale` | Height of the visible upper body relative to the shell. |
| `seat` | How far down the shell the waist rests. |
| `offset` | Pixel nudge of the whole figure. |
| `tail_length`, `tail_tilt`, `tail_offset` | Tail size, rotation and position. |
| `mermaid_tail` | Set to `false` to use the photo as-is, with no tail. |
| `remove_background` | Set to `false` if the photo is already a cutout PNG. |

Iterate quickly with a single frame instead of the whole video:

```bash
./make_invite.sh --still 22          # writes build/still_0022.00s.png
./make_invite.sh --stills 2,6,12,22  # several at once
```

## Checking the result

Three checks, none of which need a human to sit and watch the render.

**`tools/verify_audio.py`** transcribes the finished mix — voice *and* music,
exactly what ships in the mp4 — and checks the script survived the pitch shift.
It reports a similarity ratio, confirms the date, time, venue and age are
audible, and matches names phonetically (a recogniser writes "Dial
International" for *Dayal International* even when the audio is perfectly
clear).

```bash
.venv/bin/python tools/verify_audio.py build/invitation.mp4
```

**`tools/check_layout.py`** walks the timeline, rasterises the ink of every text
layer, and fails if anything is covered by the child or the shell, collides with
other text, runs off the frame, or spills outside the card. Worth running after
changing a name, a venue or any `photo:` key, since those change sprite sizes.

```bash
.venv/bin/python tools/check_layout.py
```

**`tools/motion_report.py`** reports frame-to-frame change per second, so a
stretch that has gone visually static shows up as a low bar. It also reports
motion within the shell region, which is how the closing bounce is confirmed.

```bash
.venv/bin/python tools/motion_report.py build/invitation.mp4
```

One caveat when reviewing the output: the fine particle effects are thin, bright
specks. Re-encoding the mp4 small and low-quality to share it will delete them
entirely, and the video will look static even though the master is not. Judge it
from `build/invitation.mp4`, or from an encode at 720p or better.

## How it fits together

| Module | Responsibility |
| --- | --- |
| `invitation/config.py` | Loads and validates `invite.yaml`. |
| `invitation/imaging.py` | Chroma keying, background removal, gradients, glows, the scalloped card mask. |
| `invitation/textfx.py` | Gradient-filled, gold-stroked, glittering text; the gloss sweep across the name. |
| `invitation/artwork.py` | Builds the card, the ribbon, and the photo/shell composite. |
| `invitation/effects.py` | Bubbles, water caustics, god rays and sparkles, all procedural. |
| `invitation/audio.py` | Voice-over via Edge TTS, a synthesised music-box score, and the ducked mixdown. |
| `invitation/timeline.py` | Easing curves and the cue sheet that hangs each animation off a spoken line. |
| `invitation/render.py` | Per-frame compositing and parallel H.264 encoding. |

Animation timing is derived from the voice-over: each line is synthesised
separately, measured, and the cue sheet hangs entrances off those timings, so
the date appears exactly as it is spoken. Change the script and the picture
re-syncs itself.

The music is synthesised from scratch (a music-box arpeggio over a pad, with
underwater ambience and bubbles), so there is nothing to license.

## Artwork

`assets/art/` holds the generated theme artwork — the underwater background,
clam shell, mermaid tail, seahorse and fish. The cut-out elements are keyed off
a flat green background at load time. Replacing any of these files with
same-shaped artwork is enough to re-theme the video.

`assets/photo/placeholder_child.png` is a generic stand-in used only when
`assets/photo/baby.jpg` is missing, so the pipeline always renders. The CLI
prints a warning whenever it falls back to it.
