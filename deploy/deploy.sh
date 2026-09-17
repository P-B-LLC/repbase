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

APP=/home/django/app
USER=django
REF="${1:-origin/main}"

[ "$(id -u)" -eq 0 ] || { echo "Run as root." >&2; exit 1; }
[ -d "$APP/.git" ] || { echo "$APP is not a git checkout. Clone it first." >&2; exit 1; }

run() { sudo -u "$USER" env $(grep -v '^#' /etc/repbase.env | xargs -d'\n') "$@"; }

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
    echo "    skipped: STATIC_ROOT unset under $DJANGO_SETTINGS_MODULE"
fi

echo "==> checks"
run "$APP/.venv/bin/python" "$APP/manage.py" check --deploy

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
