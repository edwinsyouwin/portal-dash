// Dashboard settings: copy to config.js, edit and redeploy (or just refresh when using the dev server).
export default {
  // Weather location.
  location: { name: "San Francisco", latitude: 37.7749, longitude: -122.4194 },

  units: "imperial", // "imperial" (°F, mph) or "metric" (°C, km/h)
  hour12: true,

  // Minutes between weather refreshes.
  weatherRefreshMinutes: 15,

  // The Mac's photoflow server (its LAN address; keep it fixed with a DHCP reservation).
  // It supplies the daily picture and the calendar. Set to null to do without both.
  server: "http://192.168.1.10:8766",

  // Daily AI picture. Set to null to show only the dashboard.
  photos: { pollMinutes: 10 },
  // A shared to-do list, as a tab of the calendar card; kept on the Mac.
  tasks: true,
  // A camera button: take a photo and have it redrawn in one of the picture styles. The
  // photo is sent to the image provider. Needs `photos` above.
  snapshot: true,
  // A YouTube button: search (needs YOUTUBE_API_KEY in the Mac's .env) and play here.
  youtube: true,

  // Google Calendar agenda: today plus this many following days. Set to null to hide it.
  calendar: { pollMinutes: 5, daysAhead: 1 },

  // Voice calls to Muse through the Mac (muse/muse.py serve); `key` is what
  // `muse/muse.py key` prints. wakeWord keeps the microphone open so that saying
  // "Hey Muse" starts a call (needs muse/build-wakeword.sh). Set to null to hide the button.
  muse: null, // { port: 8768, key: "…", wakeWord: false }

  // Dim the screen overnight (only has an effect inside the Portal shell app).
  // Set to null to leave brightness alone.
  dim: { from: "22:00", to: "06:30", level: 0.05 },
};
