#!/bin/sh
# Install launchd agents for the current user:
#   - a server that keeps out/ and the calendar available to the Portal
#   - a daily job that generates the day's picture at 05:00 (or when the Mac next wakes)
#   - once paired with Muse (muse/muse.py pair), the server for voice calls
# Remove them again with:  photoflow/install-schedule.sh --remove
set -eu
cd "$(dirname "$0")"

HERE="$(pwd)"
UV="$(command -v uv)"
AGENTS="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"

for job in photoflow.serve photoflow.daily muse.serve; do
  launchctl bootout "$DOMAIN/com.portaldash.$job" 2>/dev/null || true
  rm -f "$AGENTS/com.portaldash.$job.plist"
done
[ "${1:-}" = "--remove" ] && { echo "Removed."; exit 0; }

mkdir -p "$AGENTS" "$HERE/logs"

plist() { # job, script, its command, extra plist keys
  cat > "$AGENTS/com.portaldash.$1.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.portaldash.$1</string>
  <key>ProgramArguments</key>
  <array>
    <string>$UV</string><string>run</string><string>--script</string>
    <string>$2</string><string>$3</string>
  </array>
  <key>StandardOutPath</key><string>$HERE/logs/${1#photoflow.}.log</string>
  <key>StandardErrorPath</key><string>$HERE/logs/${1#photoflow.}.log</string>
  $4
</dict>
</plist>
EOF
  launchctl bootstrap "$DOMAIN" "$AGENTS/com.portaldash.$1.plist"
}

ALWAYS="<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>"
plist photoflow.serve "$HERE/flow.py" serve "$ALWAYS"
plist photoflow.daily "$HERE/flow.py" run "<key>StartCalendarInterval</key><dict><key>Hour</key><integer>5</integer><key>Minute</key><integer>0</integer></dict>"
if [ -f "$HOME/.config/portal-dash/muse/pairing.json" ]; then
  plist muse.serve "$(cd "$HERE/.." && pwd)/muse/muse.py" serve "$ALWAYS"
fi

echo "Installed. Logs are in $HERE/logs/"
