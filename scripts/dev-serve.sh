#!/usr/bin/env bash
# dev-serve.sh — start the local server cleanly on a fixed port.
# Kills any lingering gunicorn, waits for the port to actually free,
# then binds. Prevents the zombie-gunicorn / random-port mess.
#
# Usage:
#   scripts/dev-serve.sh              # port 3050, foreground
#   scripts/dev-serve.sh --bg         # background (for scripted smoke tests)
#   scripts/dev-serve.sh --port 3060  # custom port
#   scripts/dev-serve.sh --stop       # kill gunicorn only, no restart
#   scripts/dev-serve.sh --status     # show what's running

set -uo pipefail

PORT=3050
BG=0
STOP=0
STATUS=0
LSOF=$(command -v lsof || echo "/usr/sbin/lsof")

while [ $# -gt 0 ]; do
  case "$1" in
    --bg)     BG=1; shift ;;
    --stop)   STOP=1; shift ;;
    --status) STATUS=1; shift ;;
    --port)   PORT="$2"; shift 2 ;;
    -h|--help)
      /usr/bin/sed -n '2,11p' "$0" | /usr/bin/sed 's/^# //;s/^#//'; exit 0 ;;
    *) echo "unknown arg: $1"; exit 1 ;;
  esac
done

status() {
  echo "── gunicorn processes ──"
  "$LSOF" -i 2>/dev/null | /usr/bin/grep -i gunicorn || echo "  none"
  echo "── port $PORT ──"
  "$LSOF" -i ":$PORT" 2>/dev/null || echo "  free"
}

if [ "$STATUS" = "1" ]; then
  status; exit 0
fi

# kill anything bound by our app (matches by command line), then wait up to 5s
# for the port to actually release
echo "stopping any running gunicorn app.server..."
pkill -f "gunicorn app.server" 2>/dev/null || true
for i in 1 2 3 4 5 6 7 8 9 10; do
  if ! "$LSOF" -i ":$PORT" >/dev/null 2>&1; then break; fi
  sleep 0.5
done

if "$LSOF" -i ":$PORT" >/dev/null 2>&1; then
  echo "✗ port $PORT still in use after kill — something else is holding it:"
  "$LSOF" -i ":$PORT"
  exit 1
fi

if [ "$STOP" = "1" ]; then
  echo "✓ stopped"; exit 0
fi

# ensure output/web is fresh so asset serving (manifest.json, icons) works
python3 -m app.publish > /dev/null 2>&1

if [ "$BG" = "1" ]; then
  gunicorn app.server:app --bind 127.0.0.1:$PORT --workers 1 > /tmp/gu-dev.log 2>&1 &
  GPID=$!
  sleep 2
  if "$LSOF" -i ":$PORT" >/dev/null 2>&1; then
    echo "✓ running in background on http://127.0.0.1:$PORT (pid $GPID, log /tmp/gu-dev.log)"
  else
    echo "✗ failed to bind — check /tmp/gu-dev.log"
    /usr/bin/tail -20 /tmp/gu-dev.log
    exit 1
  fi
else
  echo "starting on http://127.0.0.1:$PORT (Ctrl-C to stop)..."
  exec gunicorn app.server:app --bind 127.0.0.1:$PORT --workers 1
fi
