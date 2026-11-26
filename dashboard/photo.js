// Daily picture: fetched from the Mac's photoflow server and kept in the browser cache,
// so it stays on screen while the Mac is asleep or off.
import config from "./config.js";

const CACHE_NAME = "daily-photo";
const META_KEY = "photo";
const PEEK_MS = 60_000;

const img = document.getElementById("photo-img");
const caption = document.getElementById("caption");
const clock = document.getElementById("clock");
const weather = document.getElementById("weather");
const agenda = document.getElementById("agenda");
const generate = document.getElementById("generate");
const generateNote = document.getElementById("generate-note");

// ---------- Layout: keep the clock and the cards off the people in the picture ----------

// Distance from the screen edge, in rem; matches the [data-corner] rules in style.css.
const EDGE_X = 3;
const EDGE_Y = 2.5;
// Grow each detected face or animal by this fraction on every side.
const SUBJECT_MARGIN = 0.2;
// The clock switches to outlined digits once this much of it covers a subject.
const OUTLINE_OVERLAP = 0.03;
// Space between the weather card and the agenda under it, in rem.
const STACK_GAP = 1;

// Boxes around the faces and animals, as fractions of the picture (from the manifest).
let subjects = [];

/** Subject boxes in screen pixels, allowing for the cropping done by object-fit: cover. */
function subjectRects() {
  const { naturalWidth: width, naturalHeight: height } = img;
  if (!width) return [];
  const scale = Math.max(innerWidth / width, innerHeight / height);
  const left = (innerWidth - width * scale) / 2;
  const top = (innerHeight - height * scale) / 2;
  return subjects.map(([x, y, w, h]) => ({
    x: left + (x - w * SUBJECT_MARGIN) * width * scale,
    y: top + (y - h * SUBJECT_MARGIN) * height * scale,
    w: w * (1 + 2 * SUBJECT_MARGIN) * width * scale,
    h: h * (1 + 2 * SUBJECT_MARGIN) * height * scale,
  }));
}

const remPx = () => parseFloat(getComputedStyle(document.documentElement).fontSize);

/** Where a box of the given size would sit in a corner ("tl", "tr", "bl" or "br"). */
function cornerRect({ w, h }, corner) {
  const rem = remPx();
  return {
    x: corner[1] === "l" ? EDGE_X * rem : innerWidth - EDGE_X * rem - w,
    y: corner[0] === "t" ? EDGE_Y * rem : innerHeight - EDGE_Y * rem - h,
    w,
    h,
  };
}

const sizeOf = (element) => ({ w: element.offsetWidth, h: element.offsetHeight });

function overlap(a, b) {
  const w = Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x);
  const h = Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y);
  return w > 0 && h > 0 ? w * h : 0;
}

/**
 * The corner where a box of the given size covers the least of the subjects, preferring
 * earlier corners on a tie and never overlapping the `blocked` rects. Returns the
 * corner, the rect there and how much of the subjects it still covers.
 */
function pick(size, corners, rects, blocked) {
  let best = null;
  for (const corner of corners) {
    const rect = cornerRect(size, corner);
    if (blocked.some((other) => overlap(rect, other))) continue;
    const covered = rects.reduce((sum, subject) => sum + overlap(rect, subject), 0);
    if (!best || covered < best.covered) best = { corner, rect, covered };
  }
  // Every corner taken: settle for the first choice rather than leave it unplaced.
  return best ?? { corner: corners[0], rect: cornerRect(size, corners[0]), covered: 0 };
}

function place(element, corners, rects, blocked) {
  const best = pick(sizeOf(element), corners, rects, blocked);
  element.dataset.corner = best.corner;
  return best;
}

/** Weather with the agenda directly under it, placed in a corner as one block. */
function placeCards(rects) {
  const top = sizeOf(weather);
  const bottom = agenda.hidden ? { w: 0, h: 0 } : sizeOf(agenda);
  const gap = top.h && bottom.h ? STACK_GAP * remPx() : 0;
  const size = { w: Math.max(top.w, bottom.w), h: top.h + gap + bottom.h };
  const best = pick(size, ["br", "bl", "tr", "tl"], rects, []);
  const atTop = best.corner[0] === "t";
  weather.dataset.corner = agenda.dataset.corner = best.corner;
  // Whichever card is further from the screen edge steps past the other one.
  weather.style.setProperty("--shift", `${atTop ? 0 : bottom.h + gap}px`);
  agenda.style.setProperty("--shift", `${atTop ? top.h + gap : 0}px`);
  return best;
}

function layout() {
  if (!document.body.classList.contains("has-photo")) return;
  const rects = subjectRects();
  const cards = placeCards(rects);
  const time = place(clock, ["tl", "tr", "bl", "br"], rects, [cards.rect]);
  const share = time.covered / (time.rect.w * time.rect.h || 1);
  clock.classList.toggle("outline", share > OUTLINE_OVERLAP);
  place(generate, ["bl", "tr", "tl", "br"], rects, [cards.rect, time.rect]);
}

// The cards grow when their data arrives and the clock changes width with the time.
const resizes = new ResizeObserver(layout);
resizes.observe(clock);
resizes.observe(weather);
resizes.observe(agenda);
img.addEventListener("load", layout);
addEventListener("resize", layout);

// ---------- Fetching and caching ----------

function readMeta() {
  try {
    return JSON.parse(localStorage.getItem(META_KEY));
  } catch {
    return null;
  }
}

function display(src, meta) {
  const previous = img.src;
  subjects = meta.subjects ?? [];
  img.src = src;
  if (previous.startsWith("blob:")) URL.revokeObjectURL(previous);
  caption.textContent = meta.scene ?? "";
  document.body.classList.add("has-photo");
  layout();
}

async function showCached(meta) {
  const cache = await caches.open(CACHE_NAME);
  const response = await cache.match(meta.url);
  if (response) display(URL.createObjectURL(await response.blob()), meta);
}

const serverUrl = () => config.server.replace(/\/$/, "");

async function poll() {
  try {
    const base = serverUrl();
    const manifestResponse = await fetch(`${base}/manifest.json`, { cache: "no-store" });
    if (!manifestResponse.ok) throw new Error(`HTTP ${manifestResponse.status}`);
    const { current } = await manifestResponse.json();
    if (!current || current.id === readMeta()?.id) return;

    const url = `${base}/${current.file}?v=${current.id}`;
    const meta = {
      id: current.id,
      url,
      scene: current.scene,
      style: current.style,
      subjects: current.subjects,
    };

    // The Cache API only exists on secure origins; without it, show the picture uncached.
    if (!("caches" in window)) {
      display(url, meta);
      return;
    }

    const response = await fetch(url);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const cache = await caches.open(CACHE_NAME);
    await cache.put(url, response);
    for (const key of await cache.keys()) {
      if (key.url !== url) await cache.delete(key);
    }
    localStorage.setItem(META_KEY, JSON.stringify(meta));
    await showCached(meta);
  } catch (error) {
    console.warn("Photo refresh failed:", error);
  }
}

// ---------- "New picture" button ----------

const STATUS_POLL_MS = 3_000;
const NOTE_MS = 10_000;
let noteTimer = null;

function note(text, clearAfter = 0) {
  clearTimeout(noteTimer);
  generateNote.textContent = text;
  if (clearAfter) noteTimer = setTimeout(() => (generateNote.textContent = ""), clearAfter);
}

async function requestPicture() {
  if (generate.classList.contains("busy")) return;
  generate.classList.add("busy");
  note("Creating a new picture…");
  try {
    const base = serverUrl();
    let status = await (await fetch(`${base}/generate`, { method: "POST" })).json();
    while (status.state === "running") {
      await new Promise((resolve) => setTimeout(resolve, STATUS_POLL_MS));
      status = await (await fetch(`${base}/status.json`, { cache: "no-store" })).json();
    }
    if (status.state === "error") throw new Error(status.message);
    await poll();
    note("");
  } catch (error) {
    // fetch() reports an unreachable server as a TypeError.
    note(error instanceof TypeError ? "Can't reach the Mac" : error.message, NOTE_MS);
  } finally {
    generate.classList.remove("busy");
  }
}

document.getElementById("generate-button").addEventListener("click", (event) => {
  // Keep the tap from also toggling the picture-only view.
  event.stopPropagation();
  requestPicture();
});

// Tap to see the picture on its own; the clock and weather return by themselves.
let peekTimer = null;
document.addEventListener("click", () => {
  if (!document.body.classList.contains("has-photo")) return;
  clearTimeout(peekTimer);
  if (!document.body.classList.toggle("photo-mode")) return;
  peekTimer = setTimeout(() => document.body.classList.remove("photo-mode"), PEEK_MS);
});

export function startPhotos() {
  if (!config.server || !config.photos) return;
  generate.hidden = false;
  const meta = readMeta();
  if (meta && "caches" in window) showCached(meta).catch(() => {});
  poll();
  setInterval(poll, config.photos.pollMinutes * 60_000);
}
