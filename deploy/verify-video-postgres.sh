#!/usr/bin/env bash
# An ephemeral loopback database. Never read the production environment.
set -euo pipefail
repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
case "$repo_dir" in /tmp/rytivo-video-check.*/repo) ;; *) echo 'Requires isolated /tmp/rytivo-video-check.*/repo.' >&2; exit 1;; esac
run_dir="${repo_dir%/repo}"
pg_bin=/usr/lib/postgresql/16/bin
test -x "$pg_bin/initdb"
test ! -e "$run_dir/pgdata"
isolated() { env -i HOME="$run_dir" PATH=/usr/bin:/bin LANG=C.UTF-8 "$@"; }
stop_database() {
    if [[ -f "$run_dir/pgdata/postmaster.pid" ]]; then
        isolated "$pg_bin/pg_ctl" -D "$run_dir/pgdata" -m fast -w stop
    fi
}
trap stop_database EXIT
isolated "$pg_bin/initdb" -D "$run_dir/pgdata" -U postgres --auth=trust --no-instructions >/dev/null
isolated "$pg_bin/pg_ctl" -D "$run_dir/pgdata" -l "$run_dir/postgres.log" \
    -o "-h 127.0.0.1 -p 55448 -k $run_dir -c shared_buffers=32MB -c max_connections=12" -w start
cd "$repo_dir"
isolated env DJANGO_SETTINGS_MODULE=config.test_postgres TEST_POSTGRES_PORT=55448 \
    TEST_POSTGRES_PASSWORD=disposable-loopback-test \
    /home/django/app/.venv/bin/python manage.py test core.test_video_concurrency --noinput
