#!/bin/sh
# Live-edit loop: serve dashboard/ from this Mac and point the Portal at it over USB.
# Edit files, press Enter to reload the Portal. Ctrl-C stops the server and switches
# the Portal back to its bundled dashboard.
set -eu
cd "$(dirname "$0")/.."
. scripts/env.sh
[ -f dashboard/config.js ] || cp dashboard/config.example.js dashboard/config.js

PORT="${PORT:-8765}"
URL="http://localhost:$PORT/"

show() {
  "$ADB" shell am start -n com.portaldash.shell/.MainActivity --es url "$1" >/dev/null 2>&1
}

# Static server that forbids caching, so a reload always picks up the latest edit.
python3 -c '
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()
    def log_message(self, *args):
        pass

ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), partial(Handler, directory="dashboard")).serve_forever()
' "$PORT" &
SERVER=$!

restore() {
  kill "$SERVER" 2>/dev/null || true
  show default || true
  "$ADB" reverse --remove "tcp:$PORT" >/dev/null 2>&1 || true
}
trap restore EXIT
trap 'exit 130' INT TERM

until curl -s -o /dev/null "$URL"; do
  kill -0 "$SERVER" 2>/dev/null || { echo "Could not start a server on port $PORT" >&2; exit 1; }
  sleep 0.2
done

# Makes the Portal's localhost:$PORT reach this Mac through the USB cable.
"$ADB" reverse "tcp:$PORT" "tcp:$PORT" >/dev/null
show "$URL"

echo "Serving dashboard/ at $URL (works in a desktop browser too)"
echo "Press Enter to reload the Portal, Ctrl-C to stop."
while read -r _; do
  show "$URL"
done
