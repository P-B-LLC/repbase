#!/usr/bin/env bash
#
# Restart the development server and prove it came back on the current code.
#
# The server runs with --noreload, so every edit to this repo needs a restart
# or the old code keeps being served. That failure is silent and looks like a
# bug in the app: an endpoint added minutes ago answers 404, and the obvious
# suspects are the route, the router, and the client.
#
# It has already happened once. The restart was written as
#
#     launchctl kickstart -k gui/$(id -u)/com.repbase.devserver | head -2 || pkill ...
#
# and a pipeline's exit status is the *last* command's, so `head` succeeding
# hid `kickstart` failing and the `||` fallback never ran. This script checks
# what happened instead of assuming.

set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT=5000
# A path the router cannot mistake for something else. `sessions/import-health/`
# is a poor probe: DRF matches it as `sessions/{pk}/` and answers 401 whether
# or not the route exists.
PROBE="/api/v1/step-counts/"

cd "$REPO" || exit 1

old="$(pgrep -f '[m]anage.py runserver' | head -1)"
if [ -n "$old" ]; then
    echo "stopping $old"
    pkill -f 'manage.py runserver'
    for _ in $(seq 1 10); do
        pgrep -f '[m]anage.py runserver' >/dev/null || break
        sleep 1
    done
    pgrep -f '[m]anage.py runserver' >/dev/null && pkill -9 -f 'manage.py runserver'
fi

nohup "$REPO/.venv/bin/python" manage.py runserver "127.0.0.1:$PORT" --noreload \
    > "$HOME/Library/Logs/repbase-devserver.log" 2>&1 < /dev/null &
disown

for _ in $(seq 1 25); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
        "http://127.0.0.1:$PORT$PROBE" 2>/dev/null)"
    case "$code" in
        000|"") sleep 2 ;;
        404)
            echo "FAILED: $PROBE answered 404 — the server is up but not on this code"
            tail -5 "$HOME/Library/Logs/repbase-devserver.log"
            exit 1
            ;;
        *)
            pid="$(pgrep -f '[m]anage.py runserver' | head -1)"
            echo "up: pid $pid, $PROBE -> $code"
            ps -o lstart= -p "$pid"
            exit 0
            ;;
    esac
done

echo "FAILED: nothing answered on port $PORT"
tail -10 "$HOME/Library/Logs/repbase-devserver.log"
exit 1
