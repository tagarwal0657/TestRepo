# Lavya turns ONE — a mermaid birthday invitation

An animated, interactive first-birthday invitation card with an under-the-sea theme.
Everything is plain HTML, CSS and JavaScript: no build step, no frameworks, no image
or audio files to download.

## Open it

Double-click `index.html`, or serve the folder:

```bash
python3 -m http.server 8000
# then visit http://localhost:8000
```

Works offline. The only network request is the Google Fonts stylesheet; without it the
card falls back to system serif and sans-serif faces and still looks right.

## What is in it

The invitation starts sealed inside a pearl clam shell floating in the ocean. Tapping
the shell cracks it open, launches the pearl and floats the card up out of the water,
line by line.

- Layered ocean backdrop: drifting caustics, swaying sun rays, glitter motes, two schools of fish, jellyfish, kelp, coral and a constant stream of rising bubbles
- Mermaid-scale card surface that slowly shifts hue, with a light sweep passing over it
- The name is split per letter so each one bobs on its own delay through an iridescent gradient
- A flapping mermaid tail with bubbles trailing off the fins
- Bubbles follow the mouse; tapping the water anywhere releases a small cluster
- "I'll be there!" fires a burst of pearls, bubbles and starfish
- "Save the date" builds a `.ics` calendar file in the browser and downloads it
- "Share" uses the native share sheet, falling back to copying the invitation text
- Ambient ocean sound generated live with the Web Audio API — filtered noise for the
  waves plus a sparse pentatonic bell line. No audio file is fetched.
- A "Calm water" toggle pauses every animation, and `prefers-reduced-motion` is
  honoured automatically

## Personalise it

All the wording and the calendar times live in the `PARTY` object at the top of
`script.js`:

```js
const PARTY = {
  name: "Lavya",
  age: "ONE",
  dateText: "Saturday, 12 September 2026",
  timeText: "4:00 pm - 7:00 pm",
  venue: "The Coral Cove Banquet Hall",
  address: "14 Pearl Street, Seaside Gardens",
  ...
};
```

Change those values and the card, the page title, the share text and the calendar file
all follow. `start` and `end` are the local times written into the `.ics` invite, so
keep them in step with `dateText` and `timeText`.

The colour palette is a set of CSS custom properties at the top of `styles.css`
(`--deep`, `--teal`, `--seafoam`, `--shell`, `--gold`, `--violet`) if you want a
different shade of sea.
