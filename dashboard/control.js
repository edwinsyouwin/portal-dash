// What Muse may do to the screen during a conversation (muse/muse.py, COMMANDS): enlarge
// the calendar or the weather, show the picture alone, go through the earlier pictures,
// play a YouTube video, and run a countdown timer. The
// Mac has already checked each command; this checks again and ignores anything else.
// The Mac also says when the dashboard's own files have changed, to load them afresh.
import { setAgendaView } from "./agenda.js";
import { browsePictures } from "./photo.js";
import { refreshTasks, typing } from "./tasks.js";
import { pauseVideo, playVideo, resumeVideo, stopVideo } from "./youtube.js";

const FOCUS_MS = 3 * 60_000; // enlarged views go back to normal by themselves
const DONE_MS = 2 * 60_000; // how long "Time's up" stays if nobody taps it
const CHIME_EVERY_MS = 2_500;
const CHIMES = 8;
const TIMER_KEY = "timer";
const NOTICE_KEY = "notice";
const NOTICE_MS = 12_000;
const FOCUSED = { calendar: "agenda", weather: "weather" };

const body = document.body;
const timer = document.getElementById("timer");
const timerLabel = document.getElementById("timer-label");
const timerTime = document.getElementById("timer-time");
const timerFill = document.getElementById("timer-fill");
const noticeBox = document.getElementById("notice");

// ---------- Notices ----------

let noticeTimer = null;

function notice(text, stay = false) {
  clearTimeout(noticeTimer);
  noticeBox.textContent = text;
  noticeBox.hidden = false;
  if (!stay) noticeTimer = setTimeout(() => (noticeBox.hidden = true), NOTICE_MS);
}

noticeBox.addEventListener("click", (event) => {
  event.stopPropagation();
  noticeBox.hidden = true;
});

/** Loads the dashboard's files again, carrying a notice across to show afterwards. */
function reload(text) {
  try {
    if (text) sessionStorage.setItem(NOTICE_KEY, text);
  } catch {
    // The notice is lost; the reload matters more.
  }
  location.reload();
}

// ---------- Views ----------

let focusTimer = null;

function show(view, range) {
  clearTimeout(focusTimer);
  delete body.dataset.focus;
  body.classList.remove("photo-mode");
  if (view === "home") return;
  if (view === "picture") body.classList.add("photo-mode");
  else if (view === "tasks") body.dataset.focus = "calendar"; // the tasks are a tab of that card
  else if (view in FOCUSED) body.dataset.focus = view;
  else return;
  if (view === "tasks") setAgendaView("tasks");
  if (view === "calendar" && range) setAgendaView(range);
  focusTimer = setTimeout(() => show("home"), FOCUS_MS);
}

// A tap outside an enlarged card puts things back; taps on the card itself still work.
document.addEventListener(
  "click",
  (event) => {
    const card = document.getElementById(FOCUSED[body.dataset.focus]);
    if (!card || card.contains(event.target)) return;
    event.stopPropagation();
    if (!typing()) show("home");
  },
  true,
);

// ---------- Timer ----------

let running = null; // { end, total, label }, times in ms
let ticker = null;
let chimer = null;

const two = (n) => String(n).padStart(2, "0");

function clock(ms) {
  const seconds = Math.ceil(ms / 1000);
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return hours ? `${hours}:${two(minutes)}:${two(seconds % 60)}` : `${minutes}:${two(seconds % 60)}`;
}

function remember(state) {
  try {
    if (state) localStorage.setItem(TIMER_KEY, JSON.stringify(state));
    else localStorage.removeItem(TIMER_KEY);
  } catch {
    // Storage unavailable; the timer just does not survive a restart.
  }
}

function stopTimer() {
  clearInterval(ticker);
  clearInterval(chimer);
  running = null;
  remember(null);
  timer.hidden = true;
  timer.classList.remove("done");
}

function chime() {
  const context = new AudioContext();
  [0, 0.25, 0.5].forEach((delay) => {
    const tone = new OscillatorNode(context, { frequency: 880 });
    const level = new GainNode(context, { gain: 0.2 });
    level.gain.setValueAtTime(0.2, context.currentTime + delay);
    level.gain.linearRampToValueAtTime(0, context.currentTime + delay + 0.18);
    tone.connect(level).connect(context.destination);
    tone.start(context.currentTime + delay);
    tone.stop(context.currentTime + delay + 0.2);
  });
  setTimeout(() => context.close(), 1_000);
}

function finish() {
  clearInterval(ticker);
  timer.classList.add("done");
  timerTime.textContent = "Time's up";
  timerFill.style.width = "100%";
  let chimes = 0;
  chime();
  chimer = setInterval(() => (++chimes < CHIMES ? chime() : clearInterval(chimer)), CHIME_EVERY_MS);
  ticker = setTimeout(stopTimer, DONE_MS);
}

function tick() {
  const left = running.end - Date.now();
  if (left <= 0) {
    finish();
    return;
  }
  timerTime.textContent = clock(left);
  timerFill.style.width = `${100 - (left / running.total) * 100}%`;
}

function startTimer(state) {
  stopTimer();
  running = state;
  remember(state);
  timerLabel.textContent = state.label;
  timer.hidden = false;
  tick();
  if (running) ticker = setInterval(tick, 250);
}

// Tap the timer to dismiss it.
timer.addEventListener("click", (event) => {
  event.stopPropagation();
  stopTimer();
});

// ---------- Commands ----------

export function handleCommand(command) {
  if (command.name === "show" && typeof command.view === "string") {
    show(command.view, command.range);
  } else if (command.name === "picture" && ["previous", "next", "latest"].includes(command.to)) {
    browsePictures(command.to);
  } else if (command.name === "video") {
    if (command.action === "play" && typeof command.id === "string") {
      playVideo(command.id, typeof command.title === "string" ? command.title.slice(0, 120) : "");
    } else if (command.action === "pause") pauseVideo();
    else if (command.action === "resume") resumeVideo();
    else if (command.action === "stop") stopVideo();
  } else if (command.name === "tasks") {
    refreshTasks(); // Muse changed the list on the Mac
  } else if (command.name === "notice" && typeof command.text === "string") {
    notice(command.text.slice(0, 300), true);
  } else if (command.name === "reload") {
    reload(typeof command.notice === "string" ? command.notice.slice(0, 300) : "");
  } else if (command.name === "timer") {
    const seconds = Number(command.seconds);
    if (!(seconds > 0)) stopTimer();
    else if (seconds <= 86_400) {
      const label = typeof command.label === "string" ? command.label.slice(0, 40) : "";
      startTimer({ end: Date.now() + seconds * 1000, total: seconds * 1000, label });
    }
  }
}

export function startControl() {
  try {
    const carried = sessionStorage.getItem(NOTICE_KEY);
    sessionStorage.removeItem(NOTICE_KEY);
    if (carried) notice(carried);
  } catch {
    // No notice to show.
  }
  // A timer that was running when the dashboard restarted carries on.
  try {
    const saved = JSON.parse(localStorage.getItem(TIMER_KEY));
    if (saved && saved.end > Date.now() && saved.total > 0) startTimer(saved);
    else remember(null);
  } catch {
    remember(null);
  }
}
