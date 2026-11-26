#!/bin/sh
# Let the Portal take dashboard updates from this Mac over Wi-Fi (needs the USB cable once).
#
#   scripts/live.sh        turn it on: the Portal asks photoflow's server for each
#                          dashboard file and falls back to the copy in the app
#   scripts/live.sh off    use only the copy in the app
#
# Files are only accepted if signed with the key in ~/.config/portal-dash/deploy_key, which
# this hands to the app. dashboard/config.js is never sent: it changes with install.sh.
set -eu
cd "$(dirname "$0")/.."
. scripts/env.sh

if [ "${1:-}" = off ]; then
  "$ADB" shell am start -n com.portaldash.shell/.MainActivity --es live off >/dev/null 2>&1
  echo "The Portal uses the dashboard in the app."
  exit 0
fi

# The server address the dashboard already uses, from config.js.
SERVER=$(sed -n 's/^ *server: *"\(http:[^"]*\)".*/\1/p' dashboard/config.js | head -1)
[ -n "$SERVER" ] || { echo "Set server in dashboard/config.js first." >&2; exit 1; }
KEY_FILE="$HOME/.config/portal-dash/deploy_key"
if [ ! -f "$KEY_FILE" ]; then
  mkdir -p "$(dirname "$KEY_FILE")"
  (umask 077; od -An -N32 -tx1 /dev/urandom | tr -d ' \n' > "$KEY_FILE")
fi
"$ADB" shell am start -n com.portaldash.shell/.MainActivity \
  --es live "${SERVER%/}/dashboard/" --es livekey "$(cat "$KEY_FILE")" >/dev/null 2>&1
echo "The Portal takes dashboard updates from ${SERVER%/}/dashboard/."
