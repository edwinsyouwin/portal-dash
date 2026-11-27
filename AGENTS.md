# Working on portal-dash

Notes for coding agents and anyone new to the code. The README says how to set it up
and use it; this says how the parts fit together and what has to stay true.

## The parts

| Part | Runs on | What it is |
| --- | --- | --- |
| `dashboard/` | Portal (WebView) | The page: plain HTML, CSS and ES modules. No build step, no packages. |
| `shell/` | Portal | Android app (Kotlin) that shows the page full screen and is the home screen. |
| `photoflow/` | Mac | `flow.py serve` (port 8766): daily picture, calendar, signed dashboard files. |
| `muse/` | Mac | `muse.py serve` (port 8768): calls to Muse, wake words, Muse's commands. |
| `scripts/` | Mac | Build and install over USB (`install.sh`), live editing (`dev.sh`, `live.sh`). |

The Mac services run under launchd (`photoflow/install-schedule.sh`); their logs are in
`photoflow/logs/`. After editing `muse/muse.py` or `photoflow/flow.py`, restart the
service: `launchctl kickstart -k gui/$(id -u)/com.portaldash.muse.serve` (or
`…photoflow.serve`). Restarting the Muse service cuts off a call in progress.

## How the page reaches the Portal

1. `scripts/install.sh` bundles `dashboard/` into the app. This copy always works, with
   the Mac off.
2. With `scripts/live.sh` on, the app first asks the Mac for each file
   (`/dashboard/<file>` on port 8766) and accepts it only with a valid signature
   (`liveFile` in `MainActivity.kt`, `send_dashboard_file` in `flow.py`). `config.js` is
   never served this way.
3. `scripts/dev.sh` points the app at a plain server over the USB cable, for editing.

So an edit to `dashboard/` shows on the Portal after a reload if live delivery is on,
and is only permanent after `install.sh`.

## How a call works

- `voice.js` opens a WebSocket to `muse.py serve`, presents the key from `config.js` and
  sends the microphone as 24 kHz 16-bit mono PCM in 960-sample frames
  (`voice-worklet.js`).
- The Mac sends back binary messages whose first byte says what they are: `A` sound,
  `K`/`D` H.264 key and delta frames (video calls); and JSON messages for state, Muse's
  words, and `command` (handled by `control.js`).
- `muse.py` holds the call with the Muse Gadget SDK (`VoiceCall` or `VideoCall`). The SDK
  is pinned to one commit at the top of `muse.py` and in `build-wakeword.sh`; keep the
  two in step.
- Wake words are detected on the Mac: "Hey Muse" (the SDK's detector, built by
  `build-wakeword.sh`) and "Hey Jarvis" (openWakeWord).
- Calls are half duplex. While Muse's speech is playing, the page sends silence in place
  of the microphone so Muse does not hear itself.
- A voice call reports what was said as text and Muse ends it on "goodbye". A video call
  reports no text and Muse cannot end it, so `Farewell` in `muse.py` listens for the
  goodbye with a local speech model.

Choices made on the Portal itself (the Settings panel, the picture being shown, a running
timer) live in the WebView's `localStorage`, not in `config.js`.

## What Muse can do

Muse is registered with the `dashboard.*` commands in `COMMANDS` (`muse.py`) and nothing
else. A command becomes a JSON `command` message to every connected dashboard, checked
on the Mac (`dashboard_command`) and again in the page (`handleCommand`). To add one:
add it to `COMMANDS` and `GUIDE`, validate it in `dashboard_command`, handle it in
`control.js`.

`dashboard.change.request` passes the owner's words to `ChangeDesk`, which runs Claude
Code with file tools only, in `dashboard/`, and keeps the result only if it passes the
checks there. Kept changes are local commits titled `Voice change: …`.

## Rules that must hold

- **Secrets stay out of git and out of logs.** `.env`, `dashboard/config.js` and
  `photoflow/profile.toml` are ignored; each has an `.example`. Pairing tokens and keys
  live in `~/.config/portal-dash/`. Log the kind of an error from the network
  libraries, not its text, and never log what was said in a call.
- **Never register the SDK's own commands** (they run programs and read files).
- **Keep the working tree clean.** `ChangeDesk` refuses to work while there are
  uncommitted changes, so commit finished work. Nothing pushes by itself.
- **`dashboard/` stays dependency-free** and names no hosts beyond `KNOWN_HOSTS` in
  `muse.py`; a voice change that adds one is refused.
- **The Portal's microphone switch belongs to the owner.** If the microphone delivers
  silence, say so; do not work around it.
- This is a public repository: no home addresses, device serials, local IP addresses or
  personal names in committed files.

## Checking a change

There is no test suite. What exists:

- `node --check dashboard/<file>.js` for syntax.
- The Portal itself: reload the page and look. The screen is 1920×1280 CSS pixels, a
  WebView on Chromium 131, Android 10. Layout is tuned for exactly that.
- For calls, a stand-in dashboard that plays a WAV into the WebSocket as if it were the
  microphone tests the Mac side without anyone speaking. Calls to Muse are real calls
  on the owner's account, so keep them few and short.
- Muse runs commands a few seconds late in a call, and may run an earlier request at
  the caller's next turn. Do not leave test requests pending with it.

## Style

Comments say why, in plain words, at about the density already there. Python is
standard library plus the dependencies listed at the top of each script (run with
`uv run --script`). Match the code around the change.
