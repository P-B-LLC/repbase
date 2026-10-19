#!/usr/bin/env bash
# Run AFTER deploying this backend, with the settings below in /etc/repbase.env.
# No acknowledgements, no secret output, and no automatic unmoderated fallback.
set -euo pipefail
[[ "$(id -u)" == 0 ]] || { echo 'Run in the root terminal.' >&2; exit 1; }
app=/home/django/app
cd "$app"
run_configured() (
    set -a
    . /etc/repbase.env
    set +a
    sudo -H -E -u django "$@"
)
run_configured "$app/.venv/bin/python" -c '
import os
for name in ("MODERATION_ENABLED", "MEDIA_SCREENING_ENABLED", "MODERATION_DISCLOSURE_CONFIRMED"):
    if os.getenv(name, "false").lower() != "true":
        raise SystemExit(name + " must be true in /etc/repbase.env")
if not os.getenv("MODERATION_API_KEY", "").strip():
    raise SystemExit("MODERATION_API_KEY is missing")
if os.getenv("VIDEO_MODERATION_BYPASS", "false").lower() == "true":
    raise SystemExit("Set VIDEO_MODERATION_BYPASS=false before activation")
'
getent group rytivo-media > /dev/null
command -v ffmpeg > /dev/null
command -v ffprobe > /dev/null
run_configured "$app/.venv/bin/python" manage.py migrate --check
run_configured "$app/.venv/bin/python" manage.py check --deploy --fail-level ERROR
# This sends only a generated geometric image, never an existing upload.
run_configured "$app/.venv/bin/python" manage.py check_media_provider --confirm-external-transfer
install -m 644 deploy/repbase-media-screening.service /etc/systemd/system/repbase-media-screening.service
install -m 644 deploy/repbase-media-screening.timer /etc/systemd/system/repbase-media-screening.timer
systemctl daemon-reload
systemctl start repbase-media-screening.service
systemctl enable --now repbase-media-screening.timer
systemctl restart repbase
systemctl is-active --quiet repbase
systemctl is-active --quiet repbase-media-screening.timer
test "$(curl --silent --show-error --max-time 20 -o /dev/null -w '%{http_code}' https://rytivo.app/health/)" = 200
echo 'Screening worker and backend are active; live synthetic provider check passed.'
echo 'Now test one permitted photo and short video from iOS and inspect their publication status.'
