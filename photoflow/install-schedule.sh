#!/bin/sh
# Install two launchd agents for the current user:
#   - a server that keeps out/ available to the Portal
#   - a daily job that generates the day's picture at 05:00 (or when the Mac next wakes)
# Remove them again with:  photoflow/install-schedule.sh --remove
set -eu
cd "$(dirname "$0")"

HERE="$(pwd)"
UV="$(command -v uv)"
AGENTS="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"

for job in serve daily; do
  launchctl bootout "$DOMAIN/com.portaldash.photoflow.$job" 2>/dev/null || true
  rm -f "$AGENTS/com.portaldash.photoflow.$job.plist"
done
[ "${1:-}" = "--remove" ] && { echo "Removed."; exit 0; }

mkdir -p "$AGENTS" "$HERE/logs"

plist() { # name, flow.py command, extra plist keys
  cat > "$AGENTS/com.portaldash.photoflow.$1.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.portaldash.photoflow.$1</string>
  <key>ProgramArguments</key>
  <array>
    <string>$UV</string><string>run</string><string>--script</string>
    <string>$HERE/flow.py</string><string>$2</string>
  </array>
  <key>StandardOutPath</key><string>$HERE/logs/$1.log</string>
  <key>StandardErrorPath</key><string>$HERE/logs/$1.log</string>
  $3
</dict>
</plist>
EOF
  launchctl bootstrap "$DOMAIN" "$AGENTS/com.portaldash.photoflow.$1.plist"
}

plist serve serve "<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>"
plist daily run "<key>StartCalendarInterval</key><dict><key>Hour</key><integer>5</integer><key>Minute</key><integer>0</integer></dict>"

echo "Installed. Logs are in $HERE/logs/"
