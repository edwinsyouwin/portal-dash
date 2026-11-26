// Agenda: upcoming Google Calendar events, fetched from the Mac's server (photoflow/gcal.py).
import config from "./config.js";

const CACHE_KEY = "agenda";
const VIEW_KEY = "agenda-view";
// "today" lists events for today plus calendar.daysAhead (config.js) following days. The
// others draw a grid of days with the important ones highlighted: the next seven days,
// or five weeks starting from this week's Sunday.
const VIEWS = {
  today: { label: "Today", days: (config.calendar?.daysAhead ?? 1) + 1 },
  week: { label: "Week", weeks: 1 },
  month: { label: "Month", weeks: 5 },
};
// Events repeating this often are routine and do not make a day stand out.
const ROUTINE = ["DAILY", "WEEKLY"];
// Neither do one-off events sharing a title this many times in the weeks fetched, which
// is how scheduling tools write a habit into a calendar.
const ROUTINE_COUNT = 4;
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
const weekdayFormat = new Intl.DateTimeFormat("en-US", { weekday: "narrow" });
const monthFormat = new Intl.DateTimeFormat("en-US", { month: "short" });

let data = null;
let view = "today";
// The day (as days from today) whose events show under the grid; null picks one itself.
let picked = null;

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

/** Titles of one-off events that come up so often they are really a routine. */
function habits() {
  const counts = new Map();
  for (const event of data.events) {
    if (!event.repeats) counts.set(event.title, (counts.get(event.title) ?? 0) + 1);
  }
  return new Set([...counts].filter(([, count]) => count >= ROUTINE_COUNT).map(([title]) => title));
}

function list(now) {
  const days = [];
  for (let offset = 0; offset < VIEWS.today.days; offset++) {
    const events = eventsOn(offset, now);
    const body = events.length
      ? events.map((event) => row(event, now)).join("")
      : `<div class="event none">${offset === 0 ? "Nothing more today" : "Nothing scheduled"}</div>`;
    days.push(`<section class="day"><h2>${dayLabel(offset, now)}</h2>${body}</section>`);
  }
  return `<div class="days">${days.join("")}</div>`;
}

function grid(now) {
  const { weeks } = VIEWS[view];
  // A single week starts today; several start on Sunday so the columns are weekdays.
  const first = weeks === 1 ? 0 : -now.getDay();
  const count = weeks * 7;
  const routine = habits();
  const important = (event) => !ROUTINE.includes(event.repeats) && !routine.has(event.title);
  const dateAt = (offset) => new Date(now.getFullYear(), now.getMonth(), now.getDate() + offset);

  const days = [];
  for (let offset = first; offset < first + count; offset++) {
    const events = offset < 0 ? [] : eventsOn(offset, now);
    days.push({ offset, date: dateAt(offset), events, important: events.some(important) });
  }
  // Until a day is tapped, show the next important one.
  const shown =
    days.find((day) => day.offset === picked) ?? days.find((day) => day.important) ?? null;

  const heads = days
    .slice(0, 7)
    .map((day) => `<span class="head">${weekdayFormat.format(day.date)}</span>`)
    .join("");
  const cells = days
    .map((day) => {
      const classes = ["gday"];
      if (day.offset < 0) classes.push("past");
      if (day.offset === 0) classes.push("today");
      if (day.important) classes.push("important");
      if (day === shown) classes.push("picked");
      const month = day.date.getDate() === 1 ? `<small>${monthFormat.format(day.date)}</small>` : "";
      return `<button type="button" class="${classes.join(" ")}" data-day="${day.offset}"${
        day.offset < 0 ? " disabled" : ""
      }>${month}${day.date.getDate()}</button>`;
    })
    .join("");

  let detail = `<span class="title none">No special days coming up</span>`;
  if (shown) {
    // Important events first; routine ones only fill in after them.
    const titles = [...shown.events.filter(important), ...shown.events.filter((e) => !important(e))]
      .map((event) => escapeHtml(event.title))
      .join(" · ");
    detail = `<span class="when">${dayLabel(shown.offset, now)}</span>
      <span class="title${titles ? "" : " none"}">${titles || "Nothing scheduled"}</span>`;
  }
  return `<div class="grid">${heads}${cells}</div><div class="detail">${detail}</div>`;
}

function render() {
  if (!data?.configured) {
    element.hidden = true;
    return;
  }
  const now = new Date();
  const failed = data.failed.length
    ? `<div class="more stale">Can't read: ${escapeHtml(data.failed.join(", "))}</div>`
    : "";
  // The list scrolls when it is longer than the card; keep its place across re-renders.
  const scrolled = element.querySelector(".days")?.scrollTop ?? 0;
  element.innerHTML = tabs() + (view === "today" ? list(now) : grid(now)) + failed;
  const days = element.querySelector(".days");
  if (days) days.scrollTop = scrolled;
  element.hidden = false;
}

// Today / Week / Month buttons (the choice is remembered) and taps on a day in the grid.
element.addEventListener("click", (event) => {
  // Taps on the card should not also toggle the picture-only view.
  event.stopPropagation();
  const day = event.target.closest("[data-day]");
  if (day) {
    picked = Number(day.dataset.day);
    render();
    return;
  }
  const button = event.target.closest("[data-view]");
  if (!button) return;
  view = button.dataset.view;
  picked = null;
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
