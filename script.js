/* =========================================================
   Lavya turns ONE - mermaid birthday invitation
   Everything runs offline: no libraries, no media files.
   Edit PARTY below to personalise the invitation.
   ========================================================= */

const PARTY = {
  name: "Lavya",
  age: "ONE",

  // Used for the on-card text
  dateText: "Saturday, 12 September 2026",
  timeText: "4:00 pm - 7:00 pm",
  venue: "The Coral Cove Banquet Hall",
  address: "14 Pearl Street, Seaside Gardens",
  dressCode: "Ocean blues, seafoam greens & a sprinkle of shimmer",
  note: "Bring your fins - there will be cake, bubbles and a treasure hunt for tiny hands.",

  rsvpName: "Mum & Dad",
  rsvpPhone: "+910000000000",
  rsvpBy: "5 September",

  // Used for the downloadable calendar invite (local time)
  start: { y: 2026, m: 9, d: 12, hh: 16, mm: 0 },
  end:   { y: 2026, m: 9, d: 12, hh: 19, mm: 0 },
};

/* ---------------------------------------------------------
   Fill the card from PARTY
   --------------------------------------------------------- */

function text(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = value;
}

function hydrate() {
  document.title = `${PARTY.name} turns ${PARTY.age} \u00b7 A Mermaid Birthday Invitation`;
  text("d-when", PARTY.dateText);
  text("d-time", PARTY.timeText);
  text("d-where", PARTY.venue);
  text("d-address", PARTY.address);
  text("d-dress", PARTY.dressCode);
  text("d-note", PARTY.note);

  text("d-rsvp-by", PARTY.rsvpBy);

  const rsvp = document.getElementById("d-rsvp-link");
  if (rsvp) {
    rsvp.textContent = PARTY.rsvpName;
    rsvp.href = `tel:${PARTY.rsvpPhone.replace(/\s/g, "")}`;
  }

  document.querySelectorAll(".signature__name").forEach((el) => (el.textContent = PARTY.name));
  document.querySelector(".turns__one-text").textContent = PARTY.age;
}

/* Split the name so each letter can bob on its own timing. */
function buildName() {
  const host = document.getElementById("name-letters");
  host.setAttribute("aria-label", PARTY.name);
  host.innerHTML = "";
  [...PARTY.name].forEach((ch, i) => {
    const span = document.createElement("span");
    span.className = "name__letter";
    span.textContent = ch;
    span.style.setProperty("--ld", `${i * 0.16}s`);
    span.setAttribute("aria-hidden", "true");
    host.appendChild(span);
  });
}

/* ---------------------------------------------------------
   Ambient sea life: bubbles + glitter motes
   --------------------------------------------------------- */

const rand = (min, max) => Math.random() * (max - min) + min;
const bubbleLayer = document.getElementById("bubbles");

function spawnBubble() {
  if (document.body.classList.contains("calm")) return;
  const b = document.createElement("span");
  const size = rand(6, 26);
  const dur = rand(9, 20);
  b.className = "bubble";
  b.style.setProperty("--bx", `${rand(0, 100)}%`);
  b.style.setProperty("--bs", `${size}px`);
  b.style.setProperty("--bd", `${dur}s`);
  b.style.setProperty("--bw", `${rand(-60, 60)}px`);
  bubbleLayer.appendChild(b);
  b.addEventListener("animationend", () => b.remove());
}

function seedMotes(count = 46) {
  const host = document.getElementById("motes");
  const frag = document.createDocumentFragment();
  for (let i = 0; i < count; i++) {
    const m = document.createElement("span");
    m.className = "mote";
    m.style.left = `${rand(0, 100)}%`;
    m.style.top = `${rand(10, 100)}%`;
    m.style.setProperty("--s", `${rand(1.5, 4)}px`);
    m.style.setProperty("--dx", `${rand(-120, 120)}px`);
    m.style.setProperty("--dy", `${rand(120, 420)}px`);
    m.style.setProperty("--dur", `${rand(12, 30)}s`);
    m.style.setProperty("--del", `${-rand(0, 20)}s`);
    frag.appendChild(m);
  }
  host.appendChild(frag);
}

/* ---------------------------------------------------------
   Bubble trail that follows the pointer
   --------------------------------------------------------- */

const trail = document.getElementById("trail");
let lastTrail = 0;

function trailBubble(x, y) {
  const now = performance.now();
  if (now - lastTrail < 70) return;
  lastTrail = now;
  const b = document.createElement("span");
  b.className = "trail-bubble";
  b.style.setProperty("--tx", `${x}px`);
  b.style.setProperty("--ty", `${y}px`);
  b.style.setProperty("--ts", `${rand(5, 14)}px`);
  trail.appendChild(b);
  b.addEventListener("animationend", () => b.remove());
}

/* ---------------------------------------------------------
   Celebration burst: pearls, bubbles and starfish
   --------------------------------------------------------- */

const celebrate = document.getElementById("celebrate");
const CONFETTI_COLORS = ["#7ff0d8", "#ffd6e8", "#ffd166", "#9a6ce0", "#8fd8ff", "#ffffff"];

function burst(x, y, amount = 46, spread = 460) {
  for (let i = 0; i < amount; i++) {
    const piece = document.createElement("span");
    const kind = Math.random();
    const size = rand(8, 20);
    const angle = rand(0, Math.PI * 2);
    const dist = rand(spread * 0.25, spread);

    piece.className = "confetti " + (kind < 0.4 ? "confetti--pearl" : kind < 0.72 ? "confetti--bubble" : "confetti--star");
    if (kind >= 0.72) {
      piece.innerHTML =
        `<svg viewBox="0 0 24 24" fill="${CONFETTI_COLORS[(Math.random() * CONFETTI_COLORS.length) | 0]}">` +
        `<use href="#star-shape" /></svg>`;
    }
    piece.style.setProperty("--cx", `${x}px`);
    piece.style.setProperty("--cy", `${y}px`);
    piece.style.setProperty("--cs", `${size}px`);
    piece.style.setProperty("--cc", CONFETTI_COLORS[(Math.random() * CONFETTI_COLORS.length) | 0]);
    piece.style.setProperty("--tx", `${Math.cos(angle) * dist}px`);
    // biased upward so it reads like a burst underwater, then drifts
    piece.style.setProperty("--ty", `${Math.sin(angle) * dist - rand(40, 200)}px`);
    piece.style.setProperty("--rot", `${rand(-540, 540)}deg`);
    piece.style.setProperty("--cd", `${rand(1.6, 3.2)}s`);
    celebrate.appendChild(piece);
    piece.addEventListener("animationend", () => piece.remove());
  }
}

function burstFrom(el, amount, spread) {
  const r = el.getBoundingClientRect();
  burst(r.left + r.width / 2, r.top + r.height / 2, amount, spread);
}

/* ---------------------------------------------------------
   Toast
   --------------------------------------------------------- */

const toast = document.getElementById("toast");
let toastTimer;

function say(message) {
  toast.textContent = message;
  toast.classList.add("is-visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("is-visible"), 3200);
}

/* ---------------------------------------------------------
   Ambient ocean sound, synthesised in the browser
   (filtered noise for waves + a slow bell arpeggio)
   --------------------------------------------------------- */

const ocean = {
  ctx: null,
  master: null,
  timer: null,
  on: false,

  build() {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return false;
    this.ctx = new Ctx();

    this.master = this.ctx.createGain();
    this.master.gain.value = 0;
    this.master.connect(this.ctx.destination);

    // 2 seconds of looping pink-ish noise = the sea
    const len = this.ctx.sampleRate * 2;
    const buffer = this.ctx.createBuffer(1, len, this.ctx.sampleRate);
    const data = buffer.getChannelData(0);
    let last = 0;
    for (let i = 0; i < len; i++) {
      const white = Math.random() * 2 - 1;
      last = (last + 0.02 * white) / 1.02;
      data[i] = last * 3.2;
    }
    const noise = this.ctx.createBufferSource();
    noise.buffer = buffer;
    noise.loop = true;

    const lp = this.ctx.createBiquadFilter();
    lp.type = "lowpass";
    lp.frequency.value = 520;
    lp.Q.value = 0.6;

    // slow swell so the waves rise and fall
    const swell = this.ctx.createGain();
    swell.gain.value = 0.35;
    const lfo = this.ctx.createOscillator();
    lfo.frequency.value = 0.09;
    const lfoDepth = this.ctx.createGain();
    lfoDepth.gain.value = 0.22;
    lfo.connect(lfoDepth).connect(swell.gain);

    noise.connect(lp).connect(swell).connect(this.master);
    noise.start();
    lfo.start();

    this.chime();
    return true;
  },

  // sparse pentatonic bells, like light on the water
  chime() {
    const notes = [523.25, 587.33, 659.25, 783.99, 880.0, 1046.5];
    const play = () => {
      if (!this.on) return;
      const t = this.ctx.currentTime;
      const osc = this.ctx.createOscillator();
      const gain = this.ctx.createGain();
      osc.type = "sine";
      osc.frequency.value = notes[(Math.random() * notes.length) | 0];
      gain.gain.setValueAtTime(0, t);
      gain.gain.linearRampToValueAtTime(0.07, t + 0.04);
      gain.gain.exponentialRampToValueAtTime(0.0001, t + 2.6);
      osc.connect(gain).connect(this.master);
      osc.start(t);
      osc.stop(t + 2.8);
      this.timer = setTimeout(play, rand(1800, 5200));
    };
    this.timer = setTimeout(play, 900);
  },

  async toggle() {
    if (!this.ctx && !this.build()) return false;
    await this.ctx.resume();
    this.on = !this.on;
    const t = this.ctx.currentTime;
    this.master.gain.cancelScheduledValues(t);
    this.master.gain.setTargetAtTime(this.on ? 0.5 : 0, t, 0.6);
    if (this.on) this.chime();
    else clearTimeout(this.timer);
    return this.on;
  },
};

/* ---------------------------------------------------------
   Calendar invite (.ics generated on the fly)
   --------------------------------------------------------- */

const pad = (n) => String(n).padStart(2, "0");
const icsStamp = (t) => `${t.y}${pad(t.m)}${pad(t.d)}T${pad(t.hh)}${pad(t.mm)}00`;

function downloadInvite() {
  const lines = [
    "BEGIN:VCALENDAR",
    "VERSION:2.0",
    "PRODID:-//mermaid-invitation//EN",
    "CALSCALE:GREGORIAN",
    "BEGIN:VEVENT",
    `UID:lavya-first-birthday-${Date.now()}@mermaid.invite`,
    `DTSTAMP:${icsStamp(PARTY.start)}`,
    `DTSTART:${icsStamp(PARTY.start)}`,
    `DTEND:${icsStamp(PARTY.end)}`,
    `SUMMARY:${PARTY.name}'s 1st Birthday - Mermaid Party`,
    `LOCATION:${PARTY.venue}\\, ${PARTY.address}`,
    `DESCRIPTION:${PARTY.note} Dress code: ${PARTY.dressCode}. RSVP to ${PARTY.rsvpName} (${PARTY.rsvpPhone}) by ${PARTY.rsvpBy}.`,
    "BEGIN:VALARM",
    "TRIGGER:-P1D",
    "ACTION:DISPLAY",
    "DESCRIPTION:Mermaid party tomorrow!",
    "END:VALARM",
    "END:VEVENT",
    "END:VCALENDAR",
  ];
  const blob = new Blob([lines.join("\r\n")], { type: "text/calendar;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${PARTY.name.toLowerCase()}-first-birthday.ics`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  say("Saved to your calendar \u2014 see you under the sea!");
}

/* ---------------------------------------------------------
   Share
   --------------------------------------------------------- */

async function shareInvite() {
  const summary =
    `${PARTY.name} is turning ${PARTY.age}! Join the mermaid party on ` +
    `${PARTY.dateText}, ${PARTY.timeText} at ${PARTY.venue}, ${PARTY.address}. ` +
    `RSVP to ${PARTY.rsvpName} by ${PARTY.rsvpBy}.`;

  if (navigator.share) {
    try {
      await navigator.share({ title: `${PARTY.name} turns ${PARTY.age}`, text: summary, url: location.href });
      return;
    } catch (err) {
      if (err && err.name === "AbortError") return;
    }
  }
  try {
    await navigator.clipboard.writeText(`${summary} ${location.href}`);
    say("Invitation copied \u2014 paste it to anyone you like");
  } catch {
    say(summary);
  }
}

/* ---------------------------------------------------------
   Opening the shell
   --------------------------------------------------------- */

const body = document.body;
const shellBtn = document.getElementById("shell-btn");
const card = document.getElementById("card");
let opened = false;

function openCard() {
  if (opened) return;
  opened = true;
  body.classList.add("is-opening");
  shellBtn.setAttribute("aria-expanded", "true");

  burstFrom(shellBtn, 30, 260);
  for (let i = 0; i < 18; i++) setTimeout(spawnBubble, i * 60);

  setTimeout(() => {
    body.classList.add("is-open");
    card.setAttribute("aria-hidden", "false");
    burstFrom(card, 60, 620);
    document.getElementById("card-close").focus({ preventScroll: true });
  }, 620);
}

function closeCard() {
  if (!opened) return;
  opened = false;
  body.classList.remove("is-open", "is-opening");
  card.setAttribute("aria-hidden", "true");
  shellBtn.setAttribute("aria-expanded", "false");
  shellBtn.focus({ preventScroll: true });
}

/* ---------------------------------------------------------
   Wire everything up
   --------------------------------------------------------- */

hydrate();
buildName();
seedMotes();

// a few bubbles already in the water on load, then a steady stream
for (let i = 0; i < 12; i++) spawnBubble();
setInterval(spawnBubble, 900);

shellBtn.addEventListener("click", openCard);
document.getElementById("card-close").addEventListener("click", closeCard);

document.getElementById("rsvp-btn").addEventListener("click", (e) => {
  const r = e.currentTarget.getBoundingClientRect();
  burst(r.left + r.width / 2, r.top + r.height / 2, 70, 560);
  say(`Yay! ${PARTY.name} will be so happy to sea you!`);
});

document.getElementById("cal-btn").addEventListener("click", downloadInvite);
document.getElementById("share-btn").addEventListener("click", shareInvite);

const soundBtn = document.getElementById("sound-toggle");
soundBtn.addEventListener("click", async () => {
  const on = await ocean.toggle();
  if (on === false && !ocean.ctx) {
    say("This browser will not let us play sound");
    return;
  }
  soundBtn.setAttribute("aria-pressed", String(on));
  soundBtn.querySelector(".chip__text").textContent = `Ocean sound: ${on ? "on" : "off"}`;
});

const motionBtn = document.getElementById("motion-toggle");
motionBtn.addEventListener("click", () => {
  const calm = body.classList.toggle("calm");
  motionBtn.setAttribute("aria-pressed", String(calm));
  motionBtn.querySelector(".chip__text").textContent = `Calm water: ${calm ? "on" : "off"}`;
});

// pointer bubbles + a shallow parallax on the scene, so the water has depth
let parallaxQueued = false;

window.addEventListener("pointermove", (e) => {
  if (e.pointerType === "touch") return;
  trailBubble(e.clientX, e.clientY);

  if (parallaxQueued || body.classList.contains("calm")) return;
  parallaxQueued = true;
  requestAnimationFrame(() => {
    parallaxQueued = false;
    body.style.setProperty("--px", (e.clientX / window.innerWidth - 0.5).toFixed(3));
    body.style.setProperty("--py", (e.clientY / window.innerHeight - 0.5).toFixed(3));
  });
});

// tapping the water anywhere sends up a little cluster of bubbles
window.addEventListener("pointerdown", (e) => {
  if (e.target.closest("button, a")) return;
  burst(e.clientX, e.clientY, 10, 130);
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeCard();
  if ((e.key === "Enter" || e.key === " ") && !opened && document.activeElement === body) {
    e.preventDefault();
    openCard();
  }
});
