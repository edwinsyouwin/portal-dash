// Where the Mac is. config.js has the address the dashboard was installed with; when the
// Mac moves to another network, Settings can find it again (settings.js) and the new
// address is remembered here on the Portal until config.js itself changes.
import config from "./config.js";

const KEY = "server";

/** The Mac's server, without a trailing slash; "" when none is configured. */
export function serverUrl() {
  let found = null;
  try {
    const moved = JSON.parse(localStorage.getItem(KEY));
    if (moved?.from === config.server) found = moved.to;
  } catch {
    // Nothing remembered.
  }
  return (found || config.server || "").replace(/\/$/, "");
}

export function setServer(url) {
  try {
    localStorage.setItem(KEY, JSON.stringify({ from: config.server, to: url }));
  } catch {
    // The new address lasts until the dashboard restarts.
  }
}
