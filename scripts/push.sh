#!/bin/sh
# Send the dashboard's files to the Portal over Wi-Fi, no USB cable needed. Requires live
# updates to have been turned on once with scripts/live.sh.
#
#   scripts/push.sh ["a line to show on the Portal"]
#
# Only committed work is sent: the Portal reloads when the last commit to dashboard/
# changes (dashboard/reload.js checks every minute), and at once if it is connected to
# the Muse server. The scripts are checked first, since a page that does not load is
# hard to look at from here.
set -eu
cd "$(dirname "$0")/.."

for script in dashboard/*.js; do
  node --check "$script" || { echo "Not sent: $script does not parse." >&2; exit 1; }
done
if [ -n "$(git status --porcelain -- dashboard)" ]; then
  echo "Not sent: commit the changes in dashboard/ first." >&2
  exit 1
fi
if muse/muse.py reload "${1:-}" 2>/dev/null; then
  exit 0
fi
echo "The Portal will load the new files within a minute."
