#!/usr/bin/env bash
# Disposable database and synthetic media only. Never source production secrets.
set -euo pipefail
repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
case "$repo_dir" in /tmp/rytivo-media-check.*/repo) ;; *) echo 'Requires isolated temporary checkout'; exit 1;; esac
run_dir="${repo_dir%/repo}"
pg_bin=/usr/lib/postgresql/16/bin
isolated() { env -i PATH=/usr/bin:/bin LANG=C.UTF-8 "$@"; }
stop_database() {
    if [[ -f "$run_dir/pgdata/postmaster.pid" ]]; then
        isolated "$pg_bin/pg_ctl" -D "$run_dir/pgdata" -m fast -w stop
    fi
}
trap stop_database EXIT
test ! -e "$run_dir/pgdata"
isolated "$pg_bin/initdb" -D "$run_dir/pgdata" -U postgres --auth=trust --no-instructions >/dev/null
isolated "$pg_bin/pg_ctl" -D "$run_dir/pgdata" -l "$run_dir/postgres.log" \
    -o "-h 127.0.0.1 -p 55449 -k $run_dir -c shared_buffers=32MB -c max_connections=12" -w start
cd "$repo_dir"
isolated env DJANGO_SETTINGS_MODULE=config.test_postgres TEST_POSTGRES_PORT=55449 \
    TEST_POSTGRES_PASSWORD=disposable-loopback-test \
    nice -n 15 /home/django/app/.venv/bin/python manage.py test \
    core.test_media_real_frames core.test_moderation_frames core.test_openai_media_moderation \
    core.test_media_publication core.test_media_screening_jobs core.test_media_dashboard \
    core.test_media_provider_command core.test_safety_consent core.test_reporting core.test_access \
    core.test_media core.test_media_transfer core.test_push \
    core.test_video_pipeline core.test_video_delivery core.test_video_uploads \
    core.test_media_screening_concurrency core.test_video_concurrency \
    core.test_access_concurrency core.test_push_concurrency --noinput --failfast
