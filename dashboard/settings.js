// Settings: a small panel opened with the sliders button and closed by a tap outside it.
// What is chosen is kept on the Portal itself, so it survives restarts and reinstalls
// of the dashboard's files.
import config from "./config.js";
import { CYCLE_CHOICES, pictureCycle, setPictureCycle } from "./photo.js";

const button = document.getElementById("settings-button");
const panel = document.getElementById("settings");
const cycleChoices = document.getElementById("cycle-choices");

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

function open(show) {
  panel.hidden = !show;
  button.classList.toggle("on", show);
  if (show) renderCycle();
}

export function startSettings() {
  // Every setting so far is about the pictures.
  if (!config.server || !config.photos) return;
  document.getElementById("generate").hidden = false;
  button.hidden = false;
  button.addEventListener("click", (event) => {
    // Keep the tap from also toggling the picture-only view.
    event.stopPropagation();
    open(panel.hidden);
  });
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
