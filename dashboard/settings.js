// Settings: a small panel opened with the sliders button and closed by a tap outside it.
// What is chosen is kept on the Portal itself, so it survives restarts and reinstalls
// of the dashboard's files.
import config from "./config.js";
import { CYCLE_CHOICES, pictureCycle, setPictureCycle } from "./photo.js";
import { serverUrl, setServer } from "./server.js";

const button = document.getElementById("settings-button");
const panel = document.getElementById("settings");
const cycleChoices = document.getElementById("cycle-choices");
const macStatus = document.getElementById("mac-status");
const macScan = document.getElementById("mac-scan");

// ---------- Changing the picture by itself ----------

const cycleLabel = (minutes) =>
  !minutes ? "Off" : minutes % 60 ? `${minutes} min` : `${minutes / 60} hr`;

function renderCycle() {
  const current = pictureCycle();
  // A value from config.js that is not one of the usual choices still gets a button.
  const choices = [...new Set([...CYCLE_CHOICES, current])].sort((a, b) => a - b);
  cycleChoices.replaceChildren(
    ...choices.map((minutes) => {
      const choice = document.createElement("button");
      choice.type = "button";
      choice.textContent = cycleLabel(minutes);
      choice.classList.toggle("on", minutes === current);
      choice.addEventListener("click", () => {
        setPictureCycle(minutes);
        renderCycle();
      });
      return choice;
    }),
  );
}

// ---------- Finding the Mac ----------

// What the Mac's server calls itself in /status.json (photoflow/flow.py).
const SERVICE = "portal-dash";
const PROBE_MS = 1_500;
// Addresses tried at a time; a home network's 254 take a few seconds this way.
const PROBE_BATCH = 64;
const RELOAD_MS = 1_500;
const ADDRESS = /^\d+\.\d+\.\d+\.\d+$/;

/** True if the Mac's server answers at this address. */
async function answers(base) {
  const stop = new AbortController();
  const timer = setTimeout(() => stop.abort(), PROBE_MS);
  try {
    const response = await fetch(`${base}/status.json`, { cache: "no-store", signal: stop.signal });
    return response.ok && (await response.json()).service === SERVICE;
  } catch {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

/**
 * The networks worth searching, as the first three numbers of their addresses: the
 * Portal's own (from the shell app) and the ones the Mac was last on.
 */
function networks(current) {
  const own = window.PortalShell?.localAddresses?.() ?? "";
  const known = [...own.split(","), current.hostname, new URL(config.server).hostname];
  return [...new Set(known.filter((host) => ADDRESS.test(host)).map((host) => host.replace(/\.\d+$/, "")))];
}

async function findMac() {
  const current = new URL(serverUrl());
  const at = (host) => `${current.protocol}//${host}:${current.port}`;
  if (await answers(at(current.hostname))) return current.hostname;
  for (const network of networks(current)) {
    macStatus.textContent = `Looking on ${network}.x…`;
    for (let first = 1; first < 255; first += PROBE_BATCH) {
      const hosts = [];
      for (let last = first; last < Math.min(first + PROBE_BATCH, 255); last++) hosts.push(`${network}.${last}`);
      const found = await Promise.all(hosts.map((host) => answers(at(host))));
      if (found.includes(true)) return hosts[found.indexOf(true)];
    }
  }
  return null;
}

async function scan() {
  macScan.disabled = true;
  const current = new URL(serverUrl());
  const host = await findMac();
  macScan.disabled = false;
  if (!host) {
    macStatus.textContent = "Not found. Is the Mac awake and on this Wi-Fi?";
  } else if (host === current.hostname) {
    macStatus.textContent = `The Mac is answering at ${host}`;
  } else {
    macStatus.textContent = `Found at ${host}. Reconnecting…`;
    setServer(`${current.protocol}//${host}:${current.port}`);
    // The shell app fetches updated dashboard files from the Mac too.
    window.PortalShell?.serverMoved?.(host);
    setTimeout(() => location.reload(), RELOAD_MS);
  }
}

async function renderMac() {
  const base = serverUrl();
  const host = new URL(base).hostname;
  macStatus.textContent = `Checking ${host}…`;
  const up = await answers(base);
  if (!macScan.disabled) macStatus.textContent = up ? `Connected at ${host}` : `No answer from ${host}`;
}

// ---------- USB debugging ----------

const debugSetting = document.getElementById("debug-setting");
const debugStatus = document.getElementById("debug-status");
const debugButton = document.getElementById("debug-button");

/**
 * Only the shell app can do this. If it has been given the right (scripts/install.sh
 * does that) it turns debugging on itself; otherwise the button opens the Portal's own
 * Settings, where it is under Debug > ADB Enabled.
 */
function renderDebug() {
  const shell = window.PortalShell;
  if (!shell?.openSettings) return;
  debugSetting.hidden = false;
  const direct = Boolean(shell.canSetDebugging?.());
  const on = Boolean(shell.debuggingOn?.());
  if (direct) {
    debugStatus.textContent = on ? "On" : "Off";
    debugButton.textContent = on ? "Turn off" : "Turn on";
    debugButton.onclick = () => {
      shell.setDebugging(!on);
      setTimeout(renderDebug, 500);
    };
  } else {
    debugStatus.textContent = "In the Portal\u2019s Settings: Debug, then ADB Enabled";
    debugButton.textContent = "Open Settings";
    debugButton.onclick = () => shell.openSettings();
  }
}

// ---------- The panel ----------

function open(show) {
  panel.hidden = !show;
  button.classList.toggle("on", show);
  if (!show) return;
  renderCycle();
  renderMac();
  renderDebug();
}

export function startSettings() {
  // Every setting so far is about what comes from the Mac.
  if (!config.server) return;
  document.getElementById("generate").hidden = false;
  button.hidden = false;
  button.addEventListener("click", (event) => {
    // Keep the tap from also toggling the picture-only view.
    event.stopPropagation();
    open(panel.hidden);
  });
  macScan.addEventListener("click", scan);
  panel.addEventListener("click", (event) => event.stopPropagation());
  // A tap anywhere else closes the panel and does nothing more.
  document.addEventListener(
    "click",
    (event) => {
      if (panel.hidden || panel.contains(event.target) || button.contains(event.target)) return;
      event.stopPropagation();
      open(false);
    },
    true,
  );
}
