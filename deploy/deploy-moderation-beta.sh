#!/usr/bin/env bash
# Beta rollout. Screening activation is explicit in /etc/repbase.env.
set -euo pipefail
candidate="${1:?Pass the exact validated commit SHA}"
[[ "$candidate" =~ ^[0-9a-f]{40}$ ]] || { echo 'A full commit SHA is required.' >&2; exit 1; }
[[ "$(id -u)" == 0 ]] || { echo 'Run in the root terminal.' >&2; exit 1; }
app=/home/django/app
backup_tool=/home/django/rytivo-release.0UCSDN/backup_db.py
test -r "$backup_tool"
sudo -u django git -C "$app" fetch origin work/moderation-fresh-start-2026-10-15
test "$(git -C "$app" rev-parse FETCH_HEAD)" = "$candidate" || {
    echo 'Remote branch changed; revalidate the intended commit first.' >&2; exit 1;
}
test -z "$(git -C "$app" status --porcelain)" || {
    echo 'Server checkout is not clean; stopping.' >&2; exit 1;
}
run_configured() (
    set -a
    . /etc/repbase.env
    set +a
    sudo -H -E -u django "$@"
)
run_configured "$app/.venv/bin/python" -c '
import os
for name in ("MODERATION_ENABLED", "MODERATION_DISCLOSURE_CONFIRMED"):
    if os.getenv(name, "false").lower() != "true":
        raise SystemExit(name + " must be true in /etc/repbase.env before deploying")
if not os.getenv("MODERATION_API_KEY", "").strip():
    raise SystemExit("MODERATION_API_KEY is missing")
'
release_dir="$(mktemp -d /home/django/rytivo-dashboard-beta.XXXXXXXX)"
chown django:django "$release_dir"
chmod 700 "$release_dir"
git -C "$app" rev-parse HEAD > "$release_dir/previous-commit.txt"
echo "Backup and release record: $release_dir"
run_configured "$app/.venv/bin/python" "$backup_tool" "$release_dir/database.backup"
test -s "$release_dir/database.backup"
# Use the validated deploy script without changing the running checkout first.
git -C "$app" show "$candidate:deploy/deploy.sh" > "$release_dir/deploy.sh"
bash "$release_dir/deploy.sh" "$candidate"
test "$(git -C "$app" rev-parse HEAD)" = "$candidate"
test "$(curl --silent --show-error -o /dev/null -w '%{http_code}' https://rytivo.app/health/)" = 200
test "$(curl --silent --show-error -o /dev/null -w '%{http_code}' 'https://rytivo.app/access/reports/?kind=media')" = 302
echo 'Backend dashboard deployed; anonymous access redirects to sign-in.'
if run_configured "$app/.venv/bin/python" -c 'import os, sys; sys.exit(0 if os.getenv("MEDIA_SCREENING_ENABLED", "false").lower() == "true" else 1)'; then
    bash "$app/deploy/activate-media-screening.sh"
else
    echo 'MEDIA_SCREENING_ENABLED is false. Worker activation has not run.'
fi
