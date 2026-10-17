#!/usr/bin/env bash
# Dashboard-only beta rollout. Run as root; never enables automated screening.
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
run_configured "$app/.venv/bin/python" -c 'import os; assert os.getenv("MEDIA_SCREENING_ENABLED", "false").lower() != "true", "Disable automated screening before this dashboard-only rollout"'
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
echo 'No screening worker was installed or enabled. Complete authorized iOS review tests next.'
