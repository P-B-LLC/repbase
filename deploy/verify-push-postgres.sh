#!/usr/bin/env bash
# Disposable local PostgreSQL verification; never sources the production env.
set -euo pipefail
ref="${1:?Pass the full backend commit SHA to verify}"
[[ "$ref" =~ ^[0-9a-f]{40}$ ]] || { echo 'Expected full commit SHA.' >&2; exit 1; }
[[ "$(id -u)" == 0 ]] || { echo 'Run as root on the droplet.' >&2; exit 1; }
pg_bin=/usr/lib/postgresql/16/bin
test -x "$pg_bin/initdb"
run_dir=$(mktemp -d /tmp/rytivo-push-verification.XXXXXX)
chown django:django "$run_dir"
chmod 750 "$run_dir"
echo "Verification artifacts: $run_dir"
as_test() { runuser -u django -- env -i HOME="$run_dir" PATH=/usr/bin:/bin LANG=C.UTF-8 "$@"; }
stop_database() {
    if [[ -f "$run_dir/pgdata/postmaster.pid" ]]; then
        as_test "$pg_bin/pg_ctl" -D "$run_dir/pgdata" -m fast -w stop
    fi
}
trap stop_database EXIT

sudo -u django git -C /home/django/app fetch --quiet origin feat/community-push-notifications
as_test git clone --quiet --shared --no-checkout /home/django/app "$run_dir/repo"
as_test git -C "$run_dir/repo" checkout --quiet --detach "$ref"
as_test /usr/bin/python3 -m venv "$run_dir/venv"
as_test "$run_dir/venv/bin/pip" install --quiet -r "$run_dir/repo/requirements-production.txt"
as_test "$pg_bin/initdb" -D "$run_dir/pgdata" -U postgres --auth=trust --no-instructions >/dev/null
# Only this disposable instance listens on this loopback port. Startup fails
# safely if the port is already occupied; no existing service is stopped.
as_test "$pg_bin/pg_ctl" -D "$run_dir/pgdata" -l "$run_dir/postgres.log" \
    -o "-h 127.0.0.1 -p 55439 -k $run_dir -c shared_buffers=32MB -c max_connections=20" -w start
cd "$run_dir/repo"
as_test env DJANGO_SETTINGS_MODULE=config.test_postgres TEST_POSTGRES_PORT=55439 \
    TEST_POSTGRES_PASSWORD=disposable-loopback-test \
    "$run_dir/venv/bin/python" manage.py test \
    core.test_push core.test_push_concurrency core.test_account_safety \
    core.tests.NotificationTests core.tests.PasswordResetTests --noinput
