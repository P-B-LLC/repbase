#!/usr/bin/env bash
#
# Restart the dev server and prove it came back on the current code.
#
# The server belongs to the launchd agent com.repbase.devserver, which has
# KeepAlive set. This script used to kill the process and start its own with
# nohup, which starts a fight: launchd respawns, both race for port 5000, and
# the loser retries forever. Two things follow from that, and both were seen.
#
# The loser redirected stdout with `>`, so it truncated the shared log on
# every attempt -- which is how the traceback behind a 500 disappeared. And
# for as long as both were alive they were two writers on one SQLite file,
# which the app sees as a 500 from a locked database.
#
# A nohup'd server also outlives the shell and is reparented to pid 1, so it
# looks exactly like a launchd child in ps. The way to tell is `launchctl
# list`: a "-" in the PID column means the agent's own job is not running and
# something else is holding its port.
#
# So the sequence is: unload the agent, clear any stray, load it again, and
# refuse to report success while more than one server is alive.

set -uo pipefail

REPO="/Users/user299988/Documents/repbase"
PORT=5000
PROBE="/api/v1/step-counts/"
LABEL="com.repbase.devserver"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG="$HOME/Library/Logs/repbase-devserver.log"
DOMAIN="gui/$(id -u)"

cd "$REPO" || exit 1

running() { pgrep -f '[m]anage.py runserver' | wc -l | tr -d ' '; }
probe() {
    curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
        "http://127.0.0.1:${PORT}${PROBE}" 2>/dev/null
}

if [ -f "$PLIST" ]; then
    echo "handing the server back to launchd ($LABEL)"
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null
    # Anything still holding the port is a hand-started leftover, and it is
    # what stops the agent from ever coming up.
    pgrep -f '[m]anage.py runserver' >/dev/null && pkill -f '[m]anage.py runserver'
    sleep 2
    launchctl bootstrap "$DOMAIN" "$PLIST" || {
        echo "FAILED: could not load $LABEL"
        exit 1
    }
else
    echo "no launchd agent; starting one by hand"
    pgrep -f '[m]anage.py runserver' >/dev/null && pkill -f '[m]anage.py runserver'
    sleep 1
    nohup "$REPO/.venv/bin/python" manage.py runserver "127.0.0.1:$PORT" --noreload \
        >> "$LOG" 2>&1 < /dev/null &
    disown
fi

for _ in $(seq 1 25); do
    code="$(probe)"
    case "$code" in
        000|"") sleep 2 ;;
        404)
            echo "FAILED: $PROBE answered 404 - the server is up but not on this code"
            tail -5 "$LOG"
            exit 1
            ;;
        *)
            count="$(running)"
            pid="$(pgrep -f '[m]anage.py runserver' | head -1)"
            echo "up: pid $pid, $PROBE -> $code"
            ps -o lstart= -p "$pid"
            # More than one means the fight is on again, and whichever is
            # answering may not be the one carrying the newest code.
            if [ "${count:-0}" -gt 1 ]; then
                echo "FAILED: $count runserver processes are alive"
                pgrep -fl '[m]anage.py runserver'
                exit 1
            fi
            exit 0
            ;;
    esac
done

echo "FAILED: nothing answered on port $PORT"
tail -10 "$LOG"
exit 1
