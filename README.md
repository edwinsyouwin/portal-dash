# portal-dash

An always-on home dashboard for a Meta Portal+ (Gen 2, Android 10), installed over ADB:
clock, weather, a Google Calendar agenda, voice and video calls to your Muse, and a daily
AI-generated picture of your household behind it all.

- `dashboard/` — the dashboard itself: plain HTML/CSS/JS, no build step.
- `shell/` — a small Android app that shows it full-screen in a WebView.
- `photoflow/` — optional Mac-side server: makes the daily picture and reads the calendars.
- `muse/` — optional Mac-side server: voice and video calls to Muse, with wake words.

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
scripts/live.sh      # once, over USB: let the Portal take dashboard updates over Wi-Fi
scripts/push.sh      # after that: send committed dashboard changes, no cable needed
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

The two arrow buttons, or a swipe across the screen, go back and forth through the
pictures made earlier; the one you stop on stays until a new picture is made.

**Snapshots.** With `snapshot: true` in `dashboard/config.js`, a camera button opens the
Portal's camera: take a photo, and the Mac has it redrawn in one of your styles, keeping
whoever is in it as they are (`photoflow/flow.py snapshot PHOTO` does the same for any
photo). The photo is sent to the image provider when you press "Make the picture", so ask
your guests first; it is not kept on the Portal or the Mac. Snapshots count towards
`button_daily_limit`. The Portal's camera switch must be on, and the app needs the camera
permission, which `scripts/install.sh` grants.

**Memories.** With `memories = true` in `profile.toml` (off by default), each picture is set
in one of your own outings: a place and time picked at random from the Photos library,
shown to the model through two of its photos, with your `interests` to draw on. The model
invents a new moment there; it is told not to copy a photo. The reference photos in
`refs/` are still what the faces come from. Photos showing anyone but the subjects are
never sent, nor selfies, screenshots or documents; `memory_skip` lists places to leave
out. With memories off, the `scenes` list is used.

The sliders button opens Settings, where the picture can be set to change by itself
every 2, 5, 15 or 60 minutes, going round the pictures already made (`cycleMinutes` under
`photos` in `config.js` sets the starting choice).

The sparkle button on the dashboard asks the Mac for another picture straight away
(about 30 seconds). It only works while `flow.py serve` is running, and is capped by
`button_daily_limit` in `profile.toml`.

Setup:

1. Copy `photoflow/profile.example.toml` to `photoflow/profile.toml` and edit it: who is
   in the pictures, your own scenes (interests and memories) and styles.
   `photoflow/flow.py gather` fills `photoflow/refs/` for you: ten close-ups of each
   subject's face, cropped from the best-rated recent photos of them in Photos (no
   selfies or sunglasses, eyes open, nobody else in the crop), and six photos of everyone together.
   Look through them and delete any that are not a good likeness; `gather --replace`
   starts again, and `gather --day 2025-06-01` adds close-ups from one occasion's photos
   (a wedding, a party), fetching that day's full-size originals from iCloud to crop from. Each picture is then made from one group photo and two close-ups of
   each subject (`reference_portraits` in `profile.toml`), which gives a much better
   likeness than a single photo and costs a few cents more per picture.
   Reference photos come from `photoflow/refs/` when it has any: group photos in
   `refs/together/` and one folder per subject (`refs/alex/`, ...). When `refs/` is
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

The dashboard shows Google Calendar events in a card under the weather. Buttons switch
between a list for today and tomorrow, and a grid of the next week or five weeks in which
days with something out of the ordinary are highlighted. Events that repeat daily or
weekly do not count, nor do one-off events with the same title four or more times in
those weeks. Tap a day to see what is on it. For each calendar, copy
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

## Tasks

The calendar card has a Tasks tab: a shared to-do list. Tap the circle to finish a task,
its words to change them, × to remove it, and type in the box to add one. The list lives
on the Mac (`~/.config/portal-dash/tasks.json`), so it survives restarts and reinstalls
and Muse can read and change it too ("add milk to the list", "what's left to do?"). The
Portal shows its last copy while the Mac is away, but changes need the Mac. Like the
pictures, the list is served unauthenticated on the home network. `tasks: false` in
`dashboard/config.js` hides it.

## YouTube

With `youtube: true` in `dashboard/config.js`, a play button opens a search box; pick a
result and it plays in the middle of the screen through the Portal's speakers, in
YouTube's own embedded player (so with YouTube's ads and controls). A pasted YouTube link
plays directly, and the arrows button on the player makes it fill the screen. Searching needs `YOUTUBE_API_KEY` in `.env`: a free YouTube Data API v3
key, good for about 100 searches a day; the Mac does the searching, so the key never
reaches the Portal. Muse can play something by name and pause, resume or stop it
(`dashboard.video.play`, `dashboard.video.control`). While you are talking to Muse the
video is turned down, not stopped. Some videos do not allow embedding and will not play.

## If the Mac changes network

Settings (the sliders button) shows whether the Mac is answering and has a "Look for the
Mac" button, which searches the Portal's own network and the one the Mac was last on,
then reconnects everything to the address it finds. A fixed address (a DHCP reservation
on your router) avoids the need.

## Muse calls (`muse/`)

Talk to your [Muse](https://gadgets.muse.ai/) through the Portal's microphone, speakers
and screen. It is built on Meta's Muse Gadget SDK, which is "not a supported product" and
may stop working; read its [terms](https://gadgets.muse.ai/sdk-terms). The SDK is made
for Linux gadgets: here the Mac runs it and holds the call, and the Portal lends its
Bluetooth for pairing and its microphone, speakers and screen for calls. The only commands
registered with Muse change what the dashboard shows (below); the SDK's own commands,
which run programs and read files, are never registered, so Muse cannot run anything on
the Mac.

Setup, with the Portal plugged in:

1. Get an SDK token at gadgets.muse.ai (Account > SDK tokens) and put it in `.env` as
   `MUSE_SDK_TOKEN=mgst_...`.
2. `muse/muse.py pair`, then in the Muse app: Settings > Devices > Developer mode on >
   Add Device > the `MuseGadget...` name it prints. The Portal's Bluetooth carries the
   handshake; the pairing (device tokens) is saved in `~/.config/portal-dash/muse`.
3. Set `muse` in `dashboard/config.js` to `{ port: 8768, key: "...", wakeWord: true }`
   with the key `muse/muse.py key` prints, and run `scripts/install.sh`, which also
   grants the app the microphone.
4. Optional wake words: `muse/build-wakeword.sh` builds the SDK's "Hey Muse" detector
   for macOS; "Hey Jarvis" (openWakeWord) needs nothing. Video calls need libopus
   (`brew install opus`).
5. `muse/muse.py serve`, or `photoflow/install-schedule.sh` to keep it running.

Using it:

- The microphone button starts a voice call and the camera button a video call, which
  shows Muse's picture full-screen. Tap again (or the picture) to hang up.
- During a call, the other button switches between voice and video, and so does saying
  the other wake phrase. The switch ends one call and starts the other, which takes a
  couple of seconds; Muse keeps the conversation.
- With `wakeWord` on, "Hey Muse" starts a voice call and "Hey Jarvis" a video call
  (`MUSE_VIDEO_PHRASE` in `.env` changes which). A green ring on the microphone button
  means the microphone is open.
- Say "goodbye" and the call ends; otherwise it ends after 30 seconds of silence. Muse
  hangs up a voice call itself. It cannot end a video call, so there the Mac listens for
  "goodbye" (or "stop listening", "hang up") with a local speech model (Whisper, about
  150 MB, fetched on first use) and hangs up; the sound and words stay on the Mac.
- Wake words are sometimes "heard" in other talk or a television. A voice call started
  that way ends if nothing is said in five seconds, a video call if nothing is said in
  twenty. `JARVIS_THRESHOLD` and `JARVIS_RUN` in `muse/muse.py` set how sure the
  "Hey Jarvis" detector must be.
- The Portal's own microphone switch must be on.

Muse and the screen:

- Muse is given these commands and no others: `dashboard.show` (enlarge the calendar or
  the weather, show the picture alone, or go back), `dashboard.timer.start` and
  `dashboard.timer.cancel` (a countdown on screen that chimes), and
  `dashboard.picture.new` and `dashboard.picture.browse` (earlier pictures), and
  `dashboard.tasks.list`, `.add`, `.update` and `.remove` (the task list), and
  `dashboard.video.play` and `.control` (YouTube). The Mac
  checks every parameter, and the dashboard checks again.
  Enlarged views go back by themselves after three minutes, or on a tap outside them.
- Asked in text (the Muse app), Muse runs them within seconds. In a call it currently
  says it will and never does, so `muse.py` also picks the plainest requests out of what
  the caller said in a voice call ("set a ten minute timer for the pasta", "show the
  calendar for the week", "how's the weather", "go back") and carries them out itself.
  Video calls report no text, so there the screen cannot be changed by voice.

Changing the dashboard by voice:

- Tell Muse what you would like different ("make the clock smaller", "show the humidity
  in the weather card"). Muse passes your words on with `dashboard.change.request`; a
  coding agent on the Mac ([Claude Code](https://claude.com/claude-code), which must be
  installed and signed in) makes the change, and the Portal reloads with it in a few
  minutes. "Undo that" takes the last change back (`dashboard.change.undo`).
- For this the Portal has to take its dashboard from the Mac: run `scripts/live.sh` once
  with the cable in (`scripts/live.sh off` undoes it). The shell app then asks
  photoflow's server for each dashboard file, accepts it only if it is signed with the key
  in `~/.config/portal-dash/deploy_key`, and otherwise uses the copy in the app.
  `dashboard/config.js` is never sent this way.
- Guard rails, since the request is only someone's words relayed by an assistant: the
  coding agent has file tools only (no shell, no connectors) and works inside
  `dashboard/`; its change is thrown away if git shows anything touched outside
  `dashboard/`, a script no longer parses, a new host is named, or it is very large.
  Kept changes are committed to this repository on the Mac (subject `Voice change: ...`)
  and never pushed, so `git log` shows each one. Ten a day, one at a time, and none while
  the repository has uncommitted work.
- Anyone who can talk to your Muse can ask for a change. The checks limit what a change
  can do; they do not judge whether it is a good idea.

What to know:

- With `wakeWord` on, the Portal sends its microphone to the Mac all the time, unencrypted
  on the home network. Only the detectors hear it, on the Mac, until a wake phrase; then
  the call's audio goes to Muse. Both detectors make mistakes, and a false wake opens a
  call ("Hey Jarvis" also answers to similar phrases).
- The dashboard must present `key` before the Mac will start a call, so other devices on
  the network cannot talk to your Muse through it. Anyone in the room can.
- Calls only work while the Mac is awake and at the address in `config.js`.
- Muse cannot be interrupted: the microphone is muted while it speaks.
- Muse answers when it hears a pause. In a room with constant talk, music or TV it may
  never answer; sound quieter than `MIC_THRESHOLD` in `muse.py` is treated as silence.
- `MUSE_DEBUG=1 muse/muse.py serve` logs microphone levels and call events (never what
  was said).

## Shell app

- **Other dashboards:** point the shell at any URL, and it remembers it across restarts:

  ```sh
  adb shell am start -n com.portaldash.shell/.MainActivity --es url http://host:port/
  adb shell am start -n com.portaldash.shell/.MainActivity --es url default   # back to bundled
  ```

  If the URL is unreachable it retries every 15 seconds.
- **Getting out:** tap the top-left corner five times quickly to open the Portal's Settings.
- **USB debugging** is turned off by the Portal whenever it restarts. The dashboard's
  Settings panel has a switch for it (or, on an app installed before that existed, a
  button that opens the Portal's Settings, where it is under Debug > ADB Enabled).
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
