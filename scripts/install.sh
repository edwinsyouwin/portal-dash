#!/bin/sh
# Build the shell app (with the dashboard bundled in) and install it on the connected Portal.
set -eu
cd "$(dirname "$0")/.."
. scripts/env.sh
[ -f dashboard/config.js ] || cp dashboard/config.example.js dashboard/config.js

(cd shell && ./gradlew --quiet assembleDebug)
"$ADB" install -r shell/build/outputs/apk/debug/portal-dash-shell-debug.apk
# Show the bundled dashboard, dropping any dev-server URL left over from scripts/dev.sh.
"$ADB" shell am start -n com.portaldash.shell/.MainActivity --es url default
