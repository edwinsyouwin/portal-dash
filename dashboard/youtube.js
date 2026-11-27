// YouTube on the Portal: a search box (answered by the Mac, which holds the API key) and
// YouTube's own embedded player in the middle of the screen. Muse can start, pause and
// stop it too (control.js).
import config from "./config.js";
import { serverUrl } from "./server.js";

// The ordinary embed address sends this device to YouTube's mobile site, which refuses
// to be embedded; the no-cookie one does not.
const EMBED = "https://www.youtube-nocookie.com";
const VIDEO_ID = /^[\w-]{11}$/;
// A pasted link works as well as a search.
const LINK = /(?:youtu\.be\/|[?&]v=|\/embed\/|\/shorts\/)([\w-]{11})/;
// How loud the video stays while someone is talking to Muse, of 100.
const DUCKED = 12;

const button = document.getElementById("tube-button");
const panel = document.getElementById("tube-panel");
const form = document.getElementById("tube-search");
const box = form.querySelector("input");
const results = document.getElementById("tube-results");
const player = document.getElementById("tube-player");
const frame = document.getElementById("tube-frame");
const title = document.getElementById("tube-title");

let ducked = false;

// ---------- The player ----------

/** Tells YouTube's player to do something (its IFrame API, spoken directly). */
function tell(func, ...args) {
  frame.contentWindow?.postMessage(JSON.stringify({ event: "command", func, args }), EMBED);
}

export function playVideo(id, name = "") {
  if (!VIDEO_ID.test(id)) return;
  const options = new URLSearchParams({ autoplay: 1, playsinline: 1, enablejsapi: 1, rel: 0, origin: location.origin });
  title.textContent = name;
  frame.src = `${EMBED}/embed/${id}?${options}`;
  player.hidden = false;
  button.classList.add("on");
  openPanel(false);
}

export function stopVideo() {
  frame.src = "about:blank";
  player.hidden = true;
  button.classList.remove("on");
}

export const pauseVideo = () => tell("pauseVideo");
export const resumeVideo = () => tell("playVideo");

/** Turns the video down while a call to Muse is up, and back up after. */
export function duckVideo(down) {
  ducked = down;
  if (!player.hidden) tell("setVolume", down ? DUCKED : 100);
}

// A video started during a call starts quiet too.
frame.addEventListener("load", () => ducked && !player.hidden && setTimeout(() => tell("setVolume", DUCKED), 1_500));

// ---------- Search ----------

function show(videos) {
  results.replaceChildren(
    ...videos.map((video) => {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "tube-result";
      const picture = document.createElement("img");
      picture.src = video.thumbnail;
      picture.alt = "";
      const words = document.createElement("span");
      const name = document.createElement("b");
      name.textContent = video.title;
      const channel = document.createElement("small");
      channel.textContent = video.channel;
      words.append(name, channel);
      row.append(picture, words);
      row.addEventListener("click", () => playVideo(video.id, video.title));
      return row;
    }),
  );
}

function say(text) {
  const line = document.createElement("p");
  line.className = "setting-note";
  line.textContent = text;
  results.replaceChildren(line);
}

async function search(query) {
  const link = query.match(LINK);
  if (link) {
    playVideo(link[1]);
    return;
  }
  say("Searching…");
  try {
    const response = await fetch(`${serverUrl()}/youtube/search?q=${encodeURIComponent(query)}`);
    const answer = await response.json();
    if (!response.ok) throw new Error(answer.error);
    if (answer.videos.length) show(answer.videos);
    else say("Nothing found");
  } catch (problem) {
    // fetch() reports an unreachable server as a TypeError.
    say(problem instanceof TypeError ? "Can't reach the Mac" : problem.message);
  }
}

// ---------- The panel ----------

function openPanel(open) {
  panel.hidden = !open;
  if (!open) box.blur();
}

export function startYouTube() {
  if (!config.server || !config.youtube) return;
  document.getElementById("generate").hidden = false;
  button.hidden = false;
  button.addEventListener("click", (event) => {
    // Keep the tap from also toggling the picture-only view.
    event.stopPropagation();
    openPanel(panel.hidden);
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const query = box.value.trim();
    box.blur(); // puts the keyboard away, so the results can be seen
    if (query) search(query);
  });
  document.getElementById("tube-close").addEventListener("click", stopVideo);
  for (const layer of [panel, player]) layer.addEventListener("click", (event) => event.stopPropagation());
  // A tap anywhere else closes the search panel and does nothing more.
  document.addEventListener(
    "click",
    (event) => {
      if (panel.hidden || panel.contains(event.target) || button.contains(event.target)) return;
      event.stopPropagation();
      openPanel(false);
    },
    true,
  );
}
