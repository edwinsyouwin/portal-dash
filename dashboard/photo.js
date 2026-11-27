// Daily picture: fetched from the Mac's photoflow server and kept in the browser cache,
// so it stays on screen while the Mac is asleep or off. The arrow buttons, or a swipe
// across the screen, go back through the earlier pictures the Mac still has.
import config from "./config.js";

const CACHE_NAME = "daily-photo";
const META_KEY = "photo"; // the newest picture
// An earlier picture chosen to look at; it stays until a new one is made.
const SHOWN_KEY = "photo-shown";
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

let holdButtonsUntil = 0;

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
  // While someone is going through the pictures the buttons stay under their finger,
  // unless the clock or the cards now need that corner.
  const taken = [cards.rect, time.rect];
  const held = Date.now() < holdButtonsUntil && generate.dataset.corner;
  if (held && !taken.some((other) => overlap(cornerRect(sizeOf(generate), held), other))) return;
  place(generate, ["bl", "tr", "tl", "br"], rects, taken);
}

// The cards grow when their data arrives and the clock changes width with the time.
const resizes = new ResizeObserver(layout);
resizes.observe(clock);
resizes.observe(weather);
resizes.observe(agenda);
img.addEventListener("load", layout);
addEventListener("resize", layout);

// ---------- Fetching and caching ----------

function readMeta(key = META_KEY) {
  try {
    return JSON.parse(localStorage.getItem(key));
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

async function fetchManifest() {
  const response = await fetch(`${serverUrl()}/manifest.json`, { cache: "no-store" });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

/** What the dashboard keeps about a picture, from its entry in the Mac's manifest. */
const metaOf = (entry) => ({
  id: entry.id,
  url: `${serverUrl()}/${entry.file}?v=${entry.id}`,
  date: entry.date,
  scene: entry.scene,
  style: entry.style,
  subjects: entry.subjects,
});

// The Cache API only exists on secure origins; without it, pictures are shown uncached
// and nothing is remembered.
const canCache = "caches" in window;

/** Shows a picture, downloading it into the cache first if it is not there. */
async function show(meta) {
  if (!canCache) {
    display(meta.url, meta);
    return;
  }
  const cache = await caches.open(CACHE_NAME);
  if (!(await cache.match(meta.url))) {
    const response = await fetch(meta.url);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    await cache.put(meta.url, response);
  }
  await showCached(meta);
}

/** Remembers the newest picture and the one chosen (if any); drops the rest from the cache. */
async function remember(newest, chosen) {
  if (!canCache) return;
  if (newest) localStorage.setItem(META_KEY, JSON.stringify(newest));
  if (chosen) localStorage.setItem(SHOWN_KEY, JSON.stringify(chosen));
  else localStorage.removeItem(SHOWN_KEY);
  const keep = [readMeta()?.url, chosen?.url];
  const cache = await caches.open(CACHE_NAME);
  for (const key of await cache.keys()) {
    if (!keep.includes(key.url)) await cache.delete(key);
  }
}

async function poll() {
  try {
    const { current } = await fetchManifest();
    if (!current || current.id === readMeta()?.id) return;
    // A new picture takes over from whichever earlier one was being looked at.
    const meta = metaOf(current);
    await show(meta);
    await remember(meta, null);
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

// ---------- Earlier pictures ----------

const BROWSE_NOTE_MS = 4_000;
const HOLD_BUTTONS_MS = 8_000;
// A touch that travels this far sideways, and mostly sideways, is a swipe.
const SWIPE_PX = 120;
let browsing = false;

const dayOf = (date) =>
  new Date(`${date}T12:00`).toLocaleDateString(undefined, { month: "short", day: "numeric" });

/**
 * Shows another of the pictures the Mac has kept: "previous" (the one made before the
 * one on screen), "next" or "latest". Going past either end comes round to the other.
 */
export async function browsePictures(to) {
  if (browsing || !config.server || !config.photos) return;
  browsing = true;
  try {
    const { history = [] } = await fetchManifest(); // newest first
    if (!history.length) return;
    const onScreen = (readMeta(SHOWN_KEY) ?? readMeta())?.id;
    const at = Math.max(0, history.findIndex((entry) => entry.id === onScreen));
    const step = to === "next" ? -1 : 1;
    const index = to === "latest" ? 0 : (at + step + history.length) % history.length;
    const meta = metaOf(history[index]);
    holdButtonsUntil = Date.now() + HOLD_BUTTONS_MS;
    setTimeout(layout, HOLD_BUTTONS_MS + 100);
    await show(meta);
    await remember(index ? null : meta, index ? meta : null);
    const which = index ? dayOf(meta.date) : "newest";
    note(`Picture ${history.length - index} of ${history.length} · ${which}`, BROWSE_NOTE_MS);
  } catch (error) {
    console.warn("Could not show another picture:", error);
    note("Can't reach the Mac", NOTE_MS);
  } finally {
    browsing = false;
  }
}

for (const [id, to] of [["previous-button", "previous"], ["next-button", "next"]]) {
  document.getElementById(id).addEventListener("click", (event) => {
    event.stopPropagation();
    browsePictures(to);
  });
}

// Swipe right for the picture before, left for the one after, as in a photo album.
// Not from the calendar (it scrolls) or a video call's picture.
let swipeFrom = null;
document.addEventListener(
  "touchstart",
  ({ target, touches }) => {
    const free = touches.length === 1 && !target.closest("#agenda, #call-video, #timer");
    swipeFrom = free ? { x: touches[0].clientX, y: touches[0].clientY } : null;
  },
  { passive: true },
);
document.addEventListener(
  "touchend",
  ({ changedTouches }) => {
    const from = swipeFrom;
    swipeFrom = null;
    if (!from || !document.body.classList.contains("has-photo")) return;
    const across = changedTouches[0].clientX - from.x;
    const down = changedTouches[0].clientY - from.y;
    if (Math.abs(across) < SWIPE_PX || Math.abs(across) < 2 * Math.abs(down)) return;
    browsePictures(across > 0 ? "previous" : "next");
  },
  { passive: true },
);

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
  document.getElementById("generate-button").hidden = false;
  document.getElementById("pager").hidden = false;
  const meta = readMeta(SHOWN_KEY) ?? readMeta();
  if (meta && canCache) showCached(meta).catch(() => {});
  poll();
  setInterval(poll, config.photos.pollMinutes * 60_000);
}
