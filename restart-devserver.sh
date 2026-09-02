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
    launchctl bootout "user/$(id -u)/$LABEL" 2>/dev/null
    # Anything still holding the port is a hand-started leftover, and it is
    # what stops the agent from ever coming up.
    pgrep -f '[m]anage.py runserver' >/dev/null && pkill -f '[m]anage.py runserver'
    sleep 2

    # Three tries, because the first one can fail for a reason that has
    # nothing to do with this project. The gui domain needs *this* user to
    # hold the console session, and on a shared machine somebody else may
    # hold it -- seen 2026-09-01, when the console belonged to another
    # account entirely and bootstrap answered "Domain does not support
    # specified action".
    #
    # The last tier matters most. A script that fails and exits leaves no
    # server at all, and the next thing anybody does is start one by hand;
    # that hand-started process is what fights the agent for port 5000 when
    # launchd recovers, which is the mess this whole file exists to prevent.
    # Better to start it here, and say plainly what was started.
    if launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null; then
        echo "loaded into $DOMAIN"
    elif launchctl bootstrap "user/$(id -u)" "$PLIST" 2>/dev/null; then
        echo "loaded into user/$(id -u) -- no gui session for this user"
    else
        echo "launchd refused the job, so this is a plain process instead."
        echo "  It will not survive a reboot, and the agent will fight it for"
        echo "  the port if launchd recovers. Re-run this script then."
        # The agent carries an environment; a bare nohup would not, and the
        # food search would quietly fall back to a rate-limited demo key.
        KEY="$(/usr/libexec/PlistBuddy -c \
            'Print :EnvironmentVariables:USDA_FDC_API_KEY' "$PLIST" 2>/dev/null)"
        USDA_FDC_API_KEY="$KEY" nohup "$REPO/.venv/bin/python" \
            manage.py runserver "127.0.0.1:$PORT" --noreload \
            >> "$LOG" 2>&1 < /dev/null &
        disown
    fi
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
