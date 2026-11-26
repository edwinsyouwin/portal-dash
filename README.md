# portal-dash

An always-on home dashboard for a Meta Portal+ (Gen 2, Android 10), installed over ADB:
clock, weather, a Google Calendar agenda, and a daily AI-generated picture of your
household behind it all.

- `dashboard/` — the dashboard itself: plain HTML/CSS/JS, no build step.
- `shell/` — a small Android app that shows it full-screen in a WebView.
- `photoflow/` — optional Mac-side server: makes the daily picture and reads the calendars.

The dashboard files are bundled into the app, so the clock and weather work without the
Mac. Tested on one Portal+ Gen 2 only; this is a personal project, not affiliated with Meta.

## Requirements

- JDK 17 (`brew install openjdk@17`) and the Android SDK (platform 36, build-tools 36).
- ADB enabled on the Portal (Settings > Debug > ADB Enabled) and a USB-C data cable.
  The cable is only needed to install and update; the dashboard runs over Wi-Fi.
- For `photoflow/`: macOS with the Photos app, and [uv](https://docs.astral.sh/uv/).

`scripts/env.sh` holds the paths to the JDK, SDK and adb; override them with
`JAVA_HOME`, `ANDROID_HOME` or `ADB` if yours differ.

## Quick start

```sh
cp dashboard/config.example.js dashboard/config.js   # then set your location
scripts/install.sh
```

`dashboard/config.js`, `photoflow/profile.toml` and `.env` hold your own settings and are
not committed; each has an `.example` to copy.

## Commands

```sh
scripts/install.sh   # build the app with the current dashboard and install it
scripts/dev.sh       # live-edit: serve dashboard/ from the Mac, Enter reloads the Portal
```

`dev.sh` works over the USB cable (`adb reverse`), so the Portal and Mac do not need to
see each other on the network. When it exits, the Portal goes back to the bundled copy;
run `install.sh` to make your edits permanent.

## Configuring

Edit `dashboard/config.js`: weather location, units, 12/24-hour clock, and the overnight
dimming window. Weather comes from Open-Meteo and needs no API key.

## Daily AI picture (`photoflow/`)

Once a day the Mac picks reference photos of the people in `photoflow/profile.toml` from
the Photos library, asks an image model to draw them in one of the listed scenes and
styles, and publishes the result. The dashboard shows it full-screen with a small clock and
a compact weather card on top. The flow records where the faces and animals are, and the
dashboard puts the clock and card in the corners that avoid them, drawing the clock as an
outline if it cannot. Tap the screen to see the picture on its own for a minute.

The round arrow button on the dashboard asks the Mac for another picture straight away
(about 30 seconds). It only works while `flow.py serve` is running, and is capped by
`button_daily_limit` in `profile.toml`.

Setup:

1. Copy `photoflow/profile.example.toml` to `photoflow/profile.toml` and edit it: who is
   in the pictures, your own scenes (interests and memories) and styles.
   Reference photos come from `photoflow/refs/` when it has any: group photos in
   `refs/together/`, or one folder per subject (`refs/alex/`, ...). When `refs/` is
   empty, photos are picked from the Photos library by the `photos_name` of each subject,
   which must match Photos > People & Pets (`photoflow/flow.py people` lists the names).
2. Copy `.env.example` to `.env` and add `OPENAI_API_KEY` (or `GEMINI_API_KEY`, with
   `provider = "gemini"` in `profile.toml`; that provider is untested).
3. Set `server` in `dashboard/config.js` to this Mac's address on your network, and
   reinstall. Reserve that address in your router so it does not change.
4. Try it:

   ```sh
   photoflow/flow.py run --dry-run   # show the picked photos and the prompt only
   photoflow/flow.py run             # generate and publish today's picture
   photoflow/flow.py serve           # make it available to the Portal
   ```

5. `photoflow/install-schedule.sh` keeps the server running and generates a new picture
   every day at 05:00 (`--remove` undoes it).

Each picture is kept in `photoflow/out/` (the newest 60) and copied into the "Portal Dash"
album in Photos (`photos_album` in `profile.toml`). Token usage and cost per picture are
appended to `photoflow/logs/usage.csv`.

The Portal downloads the picture from the address in `dashboard/config.js` (`server`)
and keeps its own copy, so it stays up while the Mac is asleep. Reference photos are sent
to the image provider; generated pictures are served unauthenticated on the home network.

To add another image model, write a function like `generate_openai` in `flow.py`, add it
to `PROVIDERS`, and set `provider` in `profile.toml`.

## Calendar

The dashboard shows today's and tomorrow's Google Calendar events, in a card under the weather. For each calendar, copy
its private address from Google Calendar (Settings > the calendar under "Settings for my
calendars" > "Secret address in iCal format") into `.env`:

```
CALENDAR_ALEX=https://calendar.google.com/calendar/ical/.../basic.ics
CALENDAR_SAM=https://calendar.google.com/calendar/ical/.../basic.ics
```

The part after `CALENDAR_` is the label on that person's events. The Mac's server
(`photoflow/gcal.py`) reads the feeds and the dashboard asks it every few minutes; new
lines in `.env` are picked up without a restart. Anyone with a secret address can read
that calendar, and event titles are served unauthenticated on the home network.

## Shell app

- **Other dashboards:** point the shell at any URL, and it remembers it across restarts:

  ```sh
  adb shell am start -n com.portaldash.shell/.MainActivity --es url http://host:port/
  adb shell am start -n com.portaldash.shell/.MainActivity --es url default   # back to bundled
  ```

  If the URL is unreachable it retries every 15 seconds.
- **Getting out:** tap the top-left corner five times quickly to open the Portal's Settings.
- **Page API:** pages get `window.PortalShell` with `setBrightness(0..1)`,
  `resetBrightness()`, `openSettings()` and `reload()`.
- **Debugging:** with the Portal plugged in, open `chrome://inspect` in Chrome on the Mac.
- **Home screen:** installing does not replace the Portal's launcher. To make the
  dashboard the home screen:

  ```sh
  adb shell am start -n com.portaldash.shell/.MainActivity --ez home true
  adb shell cmd package set-home-activity com.portaldash.shell/.Home
  ```

  and to undo it:

  ```sh
  adb shell cmd package set-home-activity com.facebook.alohaapps.launcher/com.facebook.aloha.app.home.touch.HomeActivity
  adb shell am start -n com.portaldash.shell/.MainActivity --ez home false
  ```

## License

MIT; see `LICENSE`.
