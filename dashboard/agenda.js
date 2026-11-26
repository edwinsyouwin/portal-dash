// Agenda: upcoming Google Calendar events, fetched from the Mac's server (photoflow/gcal.py).
import config from "./config.js";

const CACHE_KEY = "agenda";
const MAX_ROWS = 7;
// One colour per calendar, in the order the server lists them.
const COLORS = ["#f2b45a", "#6fb1ff", "#7fd1a0", "#e58fb1"];

const element = document.getElementById("agenda");

const timeFormat = new Intl.DateTimeFormat("en-US", {
  hour: "numeric",
  minute: "2-digit",
  hour12: config.hour12,
});
const dayFormat = new Intl.DateTimeFormat("en-US", { weekday: "long" });

let data = null;

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

function render() {
  if (!data?.configured) {
    element.hidden = true;
    return;
  }
  const now = new Date();
  let rowsLeft = MAX_ROWS;
  let hidden = 0;
  const days = [];
  for (let offset = 0; offset <= config.calendar.daysAhead; offset++) {
    const events = eventsOn(offset, now);
    const shown = events.slice(0, rowsLeft);
    rowsLeft -= shown.length;
    hidden += events.length - shown.length;
    const label =
      offset === 0
        ? "Today"
        : offset === 1
          ? "Tomorrow"
          : dayFormat.format(new Date(now.getFullYear(), now.getMonth(), now.getDate() + offset));
    const body = shown.length
      ? shown.map((event) => row(event, now)).join("")
      : `<div class="event none">${offset === 0 ? "Nothing more today" : "Nothing scheduled"}</div>`;
    days.push(`<section class="day"><h2>${label}</h2>${body}</section>`);
  }
  const more = hidden ? `<div class="more">+${hidden} more</div>` : "";
  const failed = data.failed.length
    ? `<div class="more stale">Can't read: ${escapeHtml(data.failed.join(", "))}</div>`
    : "";
  element.innerHTML = days.join("") + more + failed;
  element.hidden = false;
}

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
  } catch {
    data = null;
  }
  render();
  refresh();
  setInterval(refresh, config.calendar.pollMinutes * 60_000);
  // Events drop off as they end, and "Now" appears as they start.
  setInterval(render, 60_000);
}
