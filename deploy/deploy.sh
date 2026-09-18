#!/usr/bin/env bash
#
# Deploy a commit of this repository to the droplet.
#
# The point of this script is that the answer to "what is running?" is a fact
# on disk rather than a memory: the application directory is a git checkout,
# so `git -C /home/django/app rev-parse HEAD` always answers it. Code used to
# be uploaded rather than cloned, which left nothing to ask.
#
# Run as root on the droplet:
#     deploy/deploy.sh            # deploy origin/main
#     deploy/deploy.sh <ref>      # deploy a tag, branch or commit
#
# Safe to re-run. It stops before restarting if anything fails, so a broken
# deploy leaves the previous release serving.

set -euo pipefail

# Run from a copy, because this script is inside the tree it checks out.
#
# bash reads a script incrementally, so replacing the file mid-run makes it
# resume at a byte offset into different text. That is exactly what happened
# the first time this ran: the checkout succeeded, and bash then executed a
# line from the version it had already buffered.
#
# The copy fixes the text for the duration. The version you invoke is the
# version that runs to completion; the one it checks out takes effect next
# time, which is the same contract every other file in the tree has.
if [ -z "${REPBASE_DEPLOY_PINNED:-}" ]; then
    pinned="$(mktemp)"
    cat "$0" > "$pinned"
    chmod +x "$pinned"
    export REPBASE_DEPLOY_PINNED=1
    trap 'rm -f "$pinned"' EXIT
    "$pinned" "$@"
    exit $?
fi

APP=/home/django/app
USER=django
REF="${1:-origin/main}"

[ "$(id -u)" -eq 0 ] || { echo "Run as root." >&2; exit 1; }
[ -d "$APP/.git" ] || { echo "$APP is not a git checkout. Clone it first." >&2; exit 1; }

# Sourcing parses the quoting the way systemd's EnvironmentFile does. The
# previous `xargs` form split every value on whitespace, so the first setting
# to contain a space — DEFAULT_FROM_EMAIL, "Rytivo <noreply@rytivo.app>" —
# broke the deploy. `sudo -E` then carries the environment across, because the
# file is root-only and the django user cannot read it itself.
run() {
    set -a
    # shellcheck disable=SC1091
    . /etc/repbase.env
    set +a
    # -H as well as -E: preserving root's environment wholesale also preserved
    # its HOME, and psycopg then looked for the database certificate under
    # /root, which the django user cannot read.
    sudo -H -E -u "$USER" "$@"
}

was="$(git -C "$APP" rev-parse --short HEAD)"

echo "==> fetching"
sudo -u "$USER" git -C "$APP" fetch --quiet --tags origin

# A dirty tree means somebody edited the server by hand. Overwriting that
# silently would destroy the only copy of whatever they were doing.
if ! sudo -u "$USER" git -C "$APP" diff --quiet || ! sudo -u "$USER" git -C "$APP" diff --cached --quiet; then
    echo "Working tree at $APP has uncommitted changes. Resolve them first." >&2
    sudo -u "$USER" git -C "$APP" status --short >&2
    exit 1
fi


# Put the checkout back if anything after this point fails.
#
# The promise this script makes is that `git rev-parse HEAD` says what is
# running. A failed deploy that leaves the tree on the new commit while the
# old process still serves traffic breaks exactly that promise, and quietly:
# the service is healthy, so nothing looks wrong.
restore() {
    local code=$?
    if [ "$code" -ne 0 ] && [ "$(git -C "$APP" rev-parse --short HEAD)" != "$was" ]; then
        echo "==> deploy failed; putting the checkout back to $was" >&2
        sudo -u "$USER" git -C "$APP" checkout --quiet --detach "$was" || true
    fi
    return $code
}
trap restore EXIT
echo "==> checking out $REF"
sudo -u "$USER" git -C "$APP" checkout --quiet --detach "$REF"
now="$(git -C "$APP" rev-parse --short HEAD)"

echo "==> dependencies"
run "$APP/.venv/bin/pip" install --quiet --upgrade -r "$APP/requirements.txt"
run "$APP/.venv/bin/pip" install --quiet --upgrade -r "$APP/requirements-production.txt"

echo "==> migrations"
run "$APP/.venv/bin/python" "$APP/manage.py" migrate --noinput

# STATIC_ROOT is defined by config.production. While the service still runs on
# config.settings there is nowhere to collect to, and saying so beats a stack
# trace that looks like the deploy broke.
echo "==> static files"
if run "$APP/.venv/bin/python" -c 'import django;django.setup();from django.conf import settings;raise SystemExit(0 if settings.STATIC_ROOT else 1)' 2>/dev/null; then
    run "$APP/.venv/bin/python" "$APP/manage.py" collectstatic --noinput >/dev/null
else
    echo "    skipped: STATIC_ROOT is unset under the configured settings module"
fi

# `check --deploy` decides whether this deploy is allowed to restart anything.
#
# ACKNOWLEDGE_CHECKS names errors that are known, accepted and deliberately
# deferred — nothing else. Any error not on that list still stops the deploy,
# so this cannot quietly become "skip the checks": a new problem introduced by
# the commit being deployed fails exactly as before.
#
# Naming one here is a decision to run in production with that protection
# missing. core.E006 says in its own hint not to bypass it to ship, and that
# remains true; putting it on this list defers it, it does not answer it.
echo "==> checks"
if ! check_output="$(run "$APP/.venv/bin/python" "$APP/manage.py" check --deploy 2>&1)"; then
    printf '%s\n' "$check_output"
    failed="$(printf '%s\n' "$check_output" | grep -oE '\(([a-z_]+\.E[0-9]+)\)' | tr -d '()' | sort -u)"
    unexpected=""
    for id in $failed; do
        case " ${ACKNOWLEDGE_CHECKS:-} " in
            *" $id "*) ;;
            *) unexpected="$unexpected $id" ;;
        esac
    done
    if [ -z "$failed" ] || [ -n "$unexpected" ]; then
        echo >&2
        echo "Deploy stopped by checks:${unexpected:- (no error id could be read)}" >&2
        echo "Fix them, or name an accepted one in ACKNOWLEDGE_CHECKS." >&2
        exit 1
    fi
    echo
    echo "!! DEPLOYING WITH KNOWN CHECKS UNRESOLVED:$(printf ' %s' $failed)"
    echo "!! These are production protections that are not in place."
    printf '%s deployed %s with unresolved:%s\n' \
        "$(date -Is)" "$REF" "$(printf ' %s' $failed)" >> /var/log/repbase-deploy.log
fi

echo "==> restarting"
systemctl restart repbase
sleep 4
systemctl is-active --quiet repbase || { echo "Service failed to start." >&2; journalctl -u repbase -n 30 --no-pager >&2; exit 1; }

for path in /health/ /api/v1/; do
    code="$(curl -s -o /dev/null -w '%{http_code}' "https://rytivo.app$path")"
    echo "    https://rytivo.app$path -> $code"
done

printf '%s\n' "$now" > /etc/repbase-release
echo "==> deployed $was -> $now"
