import config from "./config.js";
import { startAgenda } from "./agenda.js";
import { startControl } from "./control.js";
import { startPhotos } from "./photo.js";
import { startSettings } from "./settings.js";
import { startSnapshot } from "./snapshot.js";
import { startTasks } from "./tasks.js";
import { startVoice } from "./voice.js";
import { startYouTube } from "./youtube.js";

const $ = (id) => document.getElementById(id);
const metric = config.units === "metric";
const CACHE_KEY = "weather";

// ---------- Clock ----------

const timeFormat = new Intl.DateTimeFormat("en-US", {
  hour: "numeric",
  minute: "2-digit",
  hour12: config.hour12,
});
const dateFormat = new Intl.DateTimeFormat("en-US", {
  weekday: "long",
  month: "long",
  day: "numeric",
});

function renderClock() {
  const now = new Date();
  const parts = timeFormat.formatToParts(now);
  const part = (type) => parts.find((p) => p.type === type)?.value ?? "";
  $("time").textContent = `${part("hour")}:${part("minute")}`;
  $("ampm").textContent = part("dayPeriod");
  $("date").textContent = dateFormat.format(now);
}

// ---------- Weather (Open-Meteo, no API key) ----------

// WMO weather codes -> [label, icon]. Clear/partly icons switch to the moon at night.
const CODES = {
  0: ["Clear", "sun"],
  1: ["Mostly clear", "sun"],
  2: ["Partly cloudy", "partly"],
  3: ["Overcast", "cloud"],
  45: ["Fog", "fog"],
  48: ["Freezing fog", "fog"],
  51: ["Light drizzle", "rain"],
  53: ["Drizzle", "rain"],
  55: ["Heavy drizzle", "rain"],
  56: ["Freezing drizzle", "rain"],
  57: ["Freezing drizzle", "rain"],
  61: ["Light rain", "rain"],
  63: ["Rain", "rain"],
  65: ["Heavy rain", "rain"],
  66: ["Freezing rain", "rain"],
  67: ["Freezing rain", "rain"],
  71: ["Light snow", "snow"],
  73: ["Snow", "snow"],
  75: ["Heavy snow", "snow"],
  77: ["Snow grains", "snow"],
  80: ["Light showers", "rain"],
  81: ["Showers", "rain"],
  82: ["Heavy showers", "rain"],
  85: ["Snow showers", "snow"],
  86: ["Heavy snow showers", "snow"],
  95: ["Thunderstorm", "storm"],
  96: ["Thunderstorm, hail", "storm"],
  99: ["Thunderstorm, hail", "storm"],
};

function describe(code, isDay = true) {
  const [label, icon] = CODES[code] ?? ["—", "cloud"];
  return { label, icon: !isDay && icon === "sun" ? "moon" : icon };
}

function weatherUrl() {
  const params = new URLSearchParams({
    latitude: config.location.latitude,
    longitude: config.location.longitude,
    current: "temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,weather_code,is_day",
    hourly: "temperature_2m,weather_code,precipitation_probability,is_day",
    daily: "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset",
    timezone: "auto",
    forecast_days: 7,
    temperature_unit: metric ? "celsius" : "fahrenheit",
    wind_speed_unit: metric ? "kmh" : "mph",
  });
  return `https://api.open-meteo.com/v1/forecast?${params}`;
}

const deg = (value) => `${Math.round(value)}°`;

// Open-Meteo returns local wall-clock strings ("2026-11-25T14:00") for the location.
const hourLabel = (iso) => {
  const hour = Number(iso.slice(11, 13));
  if (!config.hour12) return `${String(hour).padStart(2, "0")}:00`;
  return `${hour % 12 || 12}${hour < 12 ? "am" : "pm"}`;
};
const clockLabel = (iso) => {
  const hour = Number(iso.slice(11, 13));
  const minute = iso.slice(14, 16);
  if (!config.hour12) return `${String(hour).padStart(2, "0")}:${minute}`;
  return `${hour % 12 || 12}:${minute} ${hour < 12 ? "am" : "pm"}`;
};
const dayLabel = (isoDate) =>
  new Date(`${isoDate}T12:00`).toLocaleDateString("en-US", { weekday: "short" });

function icon(name) {
  return `<svg class="icon"><use href="#i-${name}"/></svg>`;
}

function rainLabel(probability) {
  return probability >= 20 ? `${probability}%` : "";
}

function renderWeather({ data, fetchedAt }) {
  const { current, hourly, daily } = data;

  const now = describe(current.weather_code, current.is_day);
  $("now-icon").setAttribute("href", `#i-${now.icon}`);
  $("now-temp").textContent = deg(current.temperature_2m);
  $("now-label").textContent = `${now.label} · ${config.location.name}`;
  $("now-detail").textContent =
    `Feels ${deg(current.apparent_temperature)} · ` +
    `Humidity ${current.relative_humidity_2m}% · ` +
    `Wind ${Math.round(current.wind_speed_10m)} ${metric ? "km/h" : "mph"}`;
  $("now-sun").textContent =
    `Sunrise ${clockLabel(daily.sunrise[0])} · Sunset ${clockLabel(daily.sunset[0])}`;
  $("now").hidden = false;

  // Next 12 hours, starting from the current hour.
  const start = Math.max(0, hourly.time.findIndex((t) => t >= current.time.slice(0, 13)));
  $("hourly").innerHTML = hourly.time
    .slice(start, start + 12)
    .map((time, offset) => {
      const i = start + offset;
      const { icon: name } = describe(hourly.weather_code[i], hourly.is_day[i]);
      return `<div class="cell">
        <span class="when">${offset === 0 ? "Now" : hourLabel(time)}</span>
        ${icon(name)}
        <span>${deg(hourly.temperature_2m[i])}</span>
        <span class="rain">${rainLabel(hourly.precipitation_probability[i])}</span>
      </div>`;
    })
    .join("");

  $("daily").innerHTML = daily.time
    .map((date, i) => {
      const { icon: name } = describe(daily.weather_code[i]);
      return `<div class="cell">
        <span class="when">${i === 0 ? "Today" : dayLabel(date)}</span>
        ${icon(name)}
        <span class="range">
          <span>${deg(daily.temperature_2m_max[i])}</span>
          <span class="lo">${deg(daily.temperature_2m_min[i])}</span>
        </span>
      </div>`;
    })
    .join("");

  renderStatus(fetchedAt);
}

function renderStatus(fetchedAt, failed = false) {
  const status = $("status");
  const at = timeFormat.format(new Date(fetchedAt));
  status.textContent = failed ? `Weather offline · last updated ${at}` : `Weather updated ${at}`;
  status.classList.toggle("stale", failed);
}

function cachedWeather() {
  try {
    return JSON.parse(localStorage.getItem(CACHE_KEY));
  } catch {
    return null;
  }
}

const RETRY_MS = 30_000;

async function refreshWeather() {
  let nextIn = config.weatherRefreshMinutes * 60_000;
  try {
    const response = await fetch(weatherUrl());
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const snapshot = { data: await response.json(), fetchedAt: Date.now() };
    try {
      localStorage.setItem(CACHE_KEY, JSON.stringify(snapshot));
    } catch {
      // Storage unavailable; the dashboard still works, it just starts blank offline.
    }
    renderWeather(snapshot);
  } catch (error) {
    console.warn("Weather refresh failed:", error);
    const cached = cachedWeather();
    if (cached) renderStatus(cached.fetchedAt, true);
    else $("status").textContent = "Weather unavailable";
    // The network is often not ready right after boot or launch; try again soon.
    nextIn = RETRY_MS;
  }
  setTimeout(refreshWeather, nextIn);
}

// ---------- Night dimming (via the shell's JS bridge) ----------

const minutesOf = (hhmm) => {
  const [h, m] = hhmm.split(":").map(Number);
  return h * 60 + m;
};

let dimmed = null;

function applyDimming() {
  const shell = window.PortalShell;
  if (!shell || !config.dim) return;
  const now = new Date();
  const minute = now.getHours() * 60 + now.getMinutes();
  const from = minutesOf(config.dim.from);
  const to = minutesOf(config.dim.to);
  // The window usually wraps past midnight (e.g. 22:00 -> 06:30).
  const shouldDim = from <= to ? minute >= from && minute < to : minute >= from || minute < to;
  if (shouldDim === dimmed) return;
  dimmed = shouldDim;
  if (shouldDim) shell.setBrightness(config.dim.level);
  else shell.resetBrightness();
}

// ---------- Start ----------

function tick() {
  renderClock();
  applyDimming();
}

const cached = cachedWeather();
if (cached) renderWeather(cached);

tick();
setInterval(tick, 1000);
refreshWeather();
startPhotos();
startAgenda();
startTasks();
startVoice();
startControl();
startSettings();
startYouTube();
startSnapshot();
