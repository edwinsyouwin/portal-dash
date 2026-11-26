// Agenda: upcoming Google Calendar events, fetched from the Mac's server (photoflow/gcal.py).
import config from "./config.js";

const CACHE_KEY = "agenda";
const VIEW_KEY = "agenda-view";
// How far ahead each view looks, in days; "today" uses calendar.daysAhead from config.js.
const VIEWS = {
  today: { label: "Today", days: (config.calendar?.daysAhead ?? 1) + 1 },
  week: { label: "Week", days: 7 },
  month: { label: "Month", days: 30 },
};
// One colour per calendar, in the order the server lists them.
const COLORS = ["#f2b45a", "#6fb1ff", "#7fd1a0", "#e58fb1"];

const element = document.getElementById("agenda");

const timeFormat = new Intl.DateTimeFormat("en-US", {
  hour: "numeric",
  minute: "2-digit",
  hour12: config.hour12,
});
const dayFormat = new Intl.DateTimeFormat("en-US", { weekday: "long" });
const dateFormat = new Intl.DateTimeFormat("en-US", { weekday: "short", month: "short", day: "numeric" });

let data = null;
let view = "today";

/** YYYY-MM-DD for a local date, matching how all-day events are dated. */
function isoDay(date) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function escapeHtml(text) {
  return text.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
}

/** Events still to come (or under way) on the day `offset` days from now. */
function eventsOn(offset, now) {
  const dayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate() + offset);
  const dayEnd = new Date(now.getFullYear(), now.getMonth(), now.getDate() + offset + 1);
  const day = isoDay(dayStart);
  return data.events.filter((event) => {
    if (event.all_day) return event.start <= day && day < event.end;
    const start = new Date(event.start);
    const end = new Date(event.end);
    return start < dayEnd && end > dayStart && end > now;
  });
}

function when(event, now) {
  if (event.all_day) return "All day";
  return new Date(event.start) <= now ? "Now" : timeFormat.format(new Date(event.start));
}

function row(event, now) {
  const dots = event.who
    .map((name) => {
      const color = COLORS[data.calendars.indexOf(name) % COLORS.length];
      return `<span class="who" style="background:${color}">${escapeHtml(name[0])}</span>`;
    })
    .join("");
  return `<div class="event">
    <span class="when">${when(event, now)}</span>
    <span class="title">${escapeHtml(event.title)}</span>
    <span class="whos">${dots}</span>
  </div>`;
}

function dayLabel(offset, now) {
  if (offset === 0) return "Today";
  if (offset === 1) return "Tomorrow";
  const day = new Date(now.getFullYear(), now.getMonth(), now.getDate() + offset);
  return (offset < 7 ? dayFormat : dateFormat).format(day);
}

function tabs() {
  const buttons = Object.entries(VIEWS)
    .map(
      ([key, { label }]) =>
        `<button type="button" data-view="${key}"${key === view ? ' class="on"' : ""}>${label}</button>`,
    )
    .join("");
  return `<div class="tabs">${buttons}</div>`;
}

function render() {
  if (!data?.configured) {
    element.hidden = true;
    return;
  }
  const now = new Date();
  const today = view === "today";
  const seen = new Set();
  const days = [];
  for (let offset = 0; offset < VIEWS[view].days; offset++) {
    let events = eventsOn(offset, now);
    if (!today) {
      // Longer views skip empty days and list an event spanning several days only once.
      events = events.filter((event) => {
        const key = `${event.title}|${event.start}`;
        return !seen.has(key) && seen.add(key);
      });
      if (!events.length) continue;
    }
    const body = events.length
      ? events.map((event) => row(event, now)).join("")
      : `<div class="event none">${offset === 0 ? "Nothing more today" : "Nothing scheduled"}</div>`;
    days.push(`<section class="day"><h2>${dayLabel(offset, now)}</h2>${body}</section>`);
  }
  if (!days.length) days.push(`<section class="day"><div class="event none">Nothing scheduled</div></section>`);
  const failed = data.failed.length
    ? `<div class="more stale">Can't read: ${escapeHtml(data.failed.join(", "))}</div>`
    : "";
  // The list scrolls when it is longer than the card; keep its place across re-renders.
  const scrolled = element.querySelector(".days")?.scrollTop ?? 0;
  element.innerHTML = `${tabs()}<div class="days">${days.join("")}</div>${failed}`;
  element.querySelector(".days").scrollTop = scrolled;
  element.hidden = false;
}

// Today / Week / Month buttons; the choice is remembered.
element.addEventListener("click", (event) => {
  const button = event.target.closest("[data-view]");
  if (!button) return;
  // Keep the tap from also toggling the picture-only view.
  event.stopPropagation();
  view = button.dataset.view;
  element.querySelector(".days").scrollTop = 0;
  try {
    localStorage.setItem(VIEW_KEY, view);
  } catch {
    // Storage unavailable; the choice just lasts until the next restart.
  }
  render();
});

async function refresh() {
  try {
    const base = config.server.replace(/\/$/, "");
    const response = await fetch(`${base}/calendar.json`, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    data = await response.json();
    try {
      localStorage.setItem(CACHE_KEY, JSON.stringify(data));
    } catch {
      // Storage unavailable; the agenda just starts empty next time.
    }
  } catch (error) {
    // Mac asleep or unreachable: keep showing what we last had.
    console.warn("Calendar refresh failed:", error);
  }
  render();
}

export function startAgenda() {
  if (!config.server || !config.calendar) return;
  try {
    data = JSON.parse(localStorage.getItem(CACHE_KEY));
    const saved = localStorage.getItem(VIEW_KEY);
    if (saved in VIEWS) view = saved;
  } catch {
    data = null;
  }
  render();
  refresh();
  setInterval(refresh, config.calendar.pollMinutes * 60_000);
  // Events drop off as they end, and "Now" appears as they start.
  setInterval(render, 60_000);
}
