// Keeps the dashboard's files current without the USB cable. With live updates on
// (scripts/live.sh) the shell app takes the files from the Mac each time the page loads;
// this asks the Mac now and then which version it has (the last commit to dashboard/)
// and loads the page afresh when that has changed. (scripts/push.sh does not wait for
// this: it has the Muse server tell the page to reload at once.)
//
// Loaded by its own script tag and kept free of the rest of the dashboard, so that a
// change which breaks the page can still be replaced by one that mends it.
import config from "./config.js";
import { serverUrl } from "./server.js";

const CHECK_MS = 60_000;
// A reload waits for these: a call to Muse, a video, someone typing.
const BUSY = "#voice-button.on, #video-button.on, #tube-player:not([hidden]), input:focus";

let loaded = null; // the version this page was loaded with

async function check() {
  try {
    const response = await fetch(`${serverUrl()}/dashboard/version`, { cache: "no-store" });
    if (!response.ok) return;
    const { version } = await response.json();
    if (!version) return;
    if (loaded === null) loaded = version;
    else if (version !== loaded && !document.querySelector(BUSY)) location.reload();
  } catch {
    // The Mac is away; the files in hand will do.
  }
}

if (config.server) {
  check();
  setInterval(check, CHECK_MS);
}
