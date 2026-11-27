// Tasks: a short household to-do list in a card of its own, kept on the Mac
// (photoflow/flow.py) so that it outlives restarts and Muse can change it too.
import config from "./config.js";
import { serverUrl } from "./server.js";

const CACHE_KEY = "tasks";
const POLL_MS = 60_000;
const ERROR_MS = 6_000;
const MAX_CHARS = 120; // as the Mac enforces

let tasks = [];
let editing = null; // id of the task whose words are being changed
let error = "";
let errorTimer = null;

const card = document.getElementById("tasks");

// The card moves clear of the on-screen keyboard while a box in it has the cursor
// (style.css), so the tap that starts or ends typing can land on whatever is behind the
// card. Code that acts on taps elsewhere asks here first.
const TYPING_TAP_MS = 500;
let typedAt = 0;
for (const kind of ["focusin", "focusout"]) {
  document.addEventListener(kind, (event) => {
    if (event.target.tagName === "INPUT") typedAt = Date.now();
  });
}

export const typing = () =>
  document.activeElement?.tagName === "INPUT" || Date.now() - typedAt < TYPING_TAP_MS;

export const tasksOn = () => Boolean(config.server) && config.tasks !== false;

function escapeHtml(text) {
  return text.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
}

function keep(list) {
  tasks = list;
  try {
    localStorage.setItem(CACHE_KEY, JSON.stringify(tasks));
  } catch {
    // Storage unavailable; the list just starts empty until the Mac answers.
  }
}

/** The latest list from the Mac; on failure, what was last seen stays up. */
export async function refreshTasks() {
  if (!tasksOn()) return;
  try {
    const response = await fetch(`${serverUrl()}/tasks.json`, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    keep((await response.json()).tasks);
  } catch (problem) {
    console.warn("Task refresh failed:", problem);
  }
  redraw();
}

async function change(request) {
  try {
    // Plain text, so the browser sends it without asking the Mac's permission first.
    const response = await fetch(`${serverUrl()}/tasks`, { method: "POST", body: JSON.stringify(request) });
    const answer = await response.json();
    if (!response.ok) throw new Error(answer.error);
    keep(answer.tasks);
    error = "";
  } catch (problem) {
    // fetch() reports an unreachable server as a TypeError.
    error = problem instanceof TypeError ? "Can't reach the Mac" : problem.message;
    clearTimeout(errorTimer);
    errorTimer = setTimeout(() => {
      error = "";
      redraw();
    }, ERROR_MS);
  }
  redraw(true);
}

/** The card's contents: tasks still to do, then finished ones, then a box to add one. */
function tasksHtml() {
  const rows = [...tasks.filter((task) => !task.done), ...tasks.filter((task) => task.done)]
    .map((task) => {
      const words =
        task.id === editing
          ? `<input class="task-words" data-task="words" maxlength="${MAX_CHARS}" value="${escapeHtml(task.text)}">`
          : `<span class="title" data-task="edit">${escapeHtml(task.text)}</span>`;
      return `<div class="task${task.done ? " done" : ""}" data-id="${task.id}">
        <button type="button" class="check" data-task="toggle" aria-label="${task.done ? "Not done" : "Done"}">${task.done ? "✓" : ""}</button>
        ${words}
        <button type="button" class="remove" data-task="delete" aria-label="Remove">×</button>
      </div>`;
    })
    .join("");
  const finished = tasks.some((task) => task.done)
    ? `<button type="button" class="clear" data-task="clear">Clear finished</button>`
    : "";
  return `<h2>Tasks</h2><div class="days tasks">${rows || `<div class="event none">Nothing to do</div>`}</div>
    <form class="task-add" data-task="add">
      <input maxlength="${MAX_CHARS}" placeholder="Add a task" enterkeyhint="done">
      <button type="submit">Add</button>
    </form>
    <div class="more${error ? " stale" : ""}">${escapeHtml(error)}${error ? "" : finished}</div>`;
}

/**
 * Draws the card. Routine redraws wait while someone is typing in it, so the words do
 * not vanish under their fingers; ones that follow a tap (`force`) do not.
 */
function redraw(force = false) {
  if (!force && card.contains(document.activeElement) && document.activeElement.tagName === "INPUT") return;
  // The list scrolls when it is longer than the card; keep its place across redraws.
  const scrolled = card.querySelector(".days")?.scrollTop ?? 0;
  card.innerHTML = tasksHtml();
  card.querySelector(".days").scrollTop = scrolled;
  card.hidden = false;
}

/** A tap inside the card. */
function taskClicked(event) {
  // Taps on the card should not also toggle the picture-only view.
  event.stopPropagation();
  const target = event.target.closest("[data-task]");
  if (!target || target.tagName === "FORM") return;
  const id = Number(target.closest("[data-id]")?.dataset.id);
  const task = tasks.find((candidate) => candidate.id === id);
  const action = target.dataset.task;
  if (action === "clear") change({ action: "clear_done" });
  else if (!task || action === "words") return;
  else if (action === "toggle") change({ action: "update", id, done: !task.done });
  else if (action === "delete") change({ action: "delete", id });
  else if (action === "edit") {
    editing = id;
    redraw(true);
    const box = document.querySelector(".task-words");
    box?.focus();
    box?.setSelectionRange(box.value.length, box.value.length);
  }
}

/** Finishes changing a task's words: saved if they changed, dropped if emptied. */
function finishEdit(box) {
  const id = editing;
  if (id === null) return;
  editing = null;
  const task = tasks.find((candidate) => candidate.id === id);
  const text = box.value.trim();
  if (task && text && text !== task.text) change({ action: "update", id, text });
  else redraw(true);
}

export function startTasks() {
  if (!tasksOn()) return;
  try {
    tasks = JSON.parse(localStorage.getItem(CACHE_KEY)) ?? [];
  } catch {
    tasks = [];
  }
  card.addEventListener("click", taskClicked);
  card.addEventListener("submit", (event) => {
    event.preventDefault();
    const box = event.target.querySelector("input");
    const text = box.value.trim();
    box.value = "";
    box.blur();
    if (text) change({ action: "add", text });
  });
  // Enter, or leaving the box, saves the new words.
  card.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && event.target.matches(".task-words")) finishEdit(event.target);
  });
  card.addEventListener("focusout", (event) => {
    if (event.target.matches(".task-words")) finishEdit(event.target);
  });
  redraw();
  refreshTasks();
  setInterval(refreshTasks, POLL_MS);
}
