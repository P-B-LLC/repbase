"""Narrow, secret-safe database operations for the calendar-sync beta rollout.

Run only through deploy-calendar-sync-hotfix.sh, with app writers stopped. This
module never restores a backup, reverses a migration, or removes planner rows.
"""

import argparse
import json
import os
import subprocess
import sys

from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder


PREVIOUS = ("core", "0080_signup_agreement_acceptance")
TARGET = ("core", "0081_plannerentry_source_schedule")
TABLES = (
    "core_plannerentry", "core_workoutschedule", "core_workoutsession",
    "core_workoutcycle", "core_workoutrecurrence", "core_repbaseuser",
)
COLUMN = "source_schedule_id"


class ReleaseError(Exception):
    """Operator-facing messages must never contain connection credentials."""


def require_postgres():
    if connection.vendor != "postgresql":
        raise ReleaseError("This release helper requires the existing PostgreSQL database.")
    if connection.settings_dict.get("DISABLE_SERVER_SIDE_CURSORS", False):
        # This project's environment parser sets this for transaction pooling.
        # The release's session-level timeouts require a direct connection.
        raise ReleaseError("This release helper requires a direct, non-transaction-pooled database connection.")


def column_exists():
    with connection.cursor() as cursor:
        columns = connection.introspection.get_table_description(cursor, TABLES[0])
    return COLUMN in {column.name for column in columns}


def linked_count():
    if not column_exists():
        return 0
    with connection.cursor() as cursor:
        cursor.execute('SELECT COUNT(*) FROM "core_plannerentry" '
                       'WHERE "source_schedule_id" IS NOT NULL')
        return cursor.fetchone()[0]


def status():
    require_postgres()
    counts = {}
    with connection.cursor() as cursor:
        for table in TABLES:
            cursor.execute(f"SELECT COUNT(*) FROM {connection.ops.quote_name(table)}")
            counts[table] = cursor.fetchone()[0]
    return {
        "format": 1,
        "row_counts": counts,
        "applied_migrations": [list(key) for key in sorted(
            MigrationRecorder(connection).applied_migrations()
        )],
        "source_column_exists": column_exists(),
        "linked_planner_rows": linked_count(),
    }


def checked_plan():
    require_postgres()
    executor = MigrationExecutor(connection)
    executor.loader.check_consistent_history(connection)
    applied = set(executor.loader.applied_migrations)
    if PREVIOUS not in applied or TARGET not in executor.loader.graph.nodes:
        raise ReleaseError("Expected core 0080 and the candidate 0081 migration were not found.")
    # Refuse schema drift: this narrow rollout is not permission to apply any
    # other pending migration, nor to run against a newer unknown database.
    if applied - set(executor.loader.graph.nodes):
        raise ReleaseError("Database contains migrations unknown to this release; re-plan deployment.")
    plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    keys = [(migration.app_label, migration.name, backwards)
            for migration, backwards in plan]
    expected = [] if TARGET in applied else [(*TARGET, False)]
    if keys != expected:
        raise ReleaseError("Migration plan is not exactly the calendar-sync migration; deployment stopped.")
    if column_exists() != (TARGET in applied):
        raise ReleaseError("Migration record and source-schedule column disagree; inspect the schema.")
    return executor, plan


def preflight():
    checked_plan()
    result = status()
    # A retry after a pre-traffic failure can leave the additive schema in
    # place. Once populated, this is no longer the reviewed starting point.
    if result["linked_planner_rows"]:
        raise ReleaseError("Schedule links already exist; re-plan this rollout without deleting them.")
    return result


def migrate():
    _, plan = checked_plan()
    if linked_count():
        raise ReleaseError("Schedule links appeared during maintenance; do not migrate with writers active.")
    if not plan:
        return
    # PostgreSQL makes this AddField migration atomic. Bounded waits avoid
    # trapping the app indefinitely behind an unexpected long transaction.
    with connection.cursor() as cursor:
        cursor.execute("SHOW lock_timeout")
        old_lock = cursor.fetchone()[0]
        cursor.execute("SHOW statement_timeout")
        old_statement = cursor.fetchone()[0]
        cursor.execute("SELECT set_config('lock_timeout', '5s', false)")
        cursor.execute("SELECT set_config('statement_timeout', '120s', false)")
    try:
        call_command("migrate", TARGET[0], TARGET[1], interactive=False, verbosity=1)
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('lock_timeout', %s, false)", [old_lock])
            cursor.execute("SELECT set_config('statement_timeout', %s, false)", [old_statement])


def verify(baseline):
    checked_plan()
    current = status()
    try:
        expected_migrations = {tuple(key) for key in baseline["applied_migrations"]} | {TARGET}
        valid = (
            baseline["format"] == 1
            and baseline["linked_planner_rows"] == 0
            and set(baseline["row_counts"]) == set(TABLES)
            and current["row_counts"] == baseline["row_counts"]
            and current["source_column_exists"]
            and current["linked_planner_rows"] == 0
            and {tuple(key) for key in current["applied_migrations"]} == expected_migrations
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ReleaseError("Post-migration verification failed; keep writers stopped and review the release record.")
    return current


def rollback_safe():
    require_postgres()
    # Called only AFTER web and workers have been stopped. Old Django code
    # ignores the nullable column but cannot correctly delete populated FKs.
    if linked_count():
        raise ReleaseError("Old-code rollback refused: schedule links exist. Keep maintenance active; use a reviewed forward fix.")


def backup_environment():
    require_postgres()
    config = connection.settings_dict
    options = config.get("OPTIONS", {})
    supported = {"sslmode", "sslrootcert", "connect_timeout", "options", "prepare_threshold"}
    if set(options) - supported or not all(config.get(key) for key in ("NAME", "USER", "HOST")):
        raise ReleaseError("Database connection options need a reviewed backup configuration.")
    # No URL or password in argv, and no unrelated application/API secrets in
    # the pg_dump child. Ignore inherited PG* variables that could redirect it.
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "TMP", "TEMP", "TMPDIR")
           if key in os.environ}
    env.update({
        "PGDATABASE": str(config["NAME"]), "PGUSER": str(config["USER"]),
        "PGHOST": str(config["HOST"]), "PGPORT": str(config.get("PORT") or 5432),
        "PGPASSWORD": str(config.get("PASSWORD") or ""),
        "PGSSLMODE": options.get("sslmode", "prefer"),
        "PGCONNECT_TIMEOUT": "10",
        "PGOPTIONS": "-c statement_timeout=240000 -c lock_timeout=5000",
    })
    if options.get("sslrootcert"):
        env["PGSSLROOTCERT"] = str(options["sslrootcert"])
    return env


def backup(output):
    try:
        result = subprocess.run(
            ["pg_dump", "--format=custom", "--no-owner", "--no-acl",
             "--no-password", "--lock-wait-timeout=5s"],
            env=backup_environment(), stdout=output, stderr=subprocess.PIPE,
            timeout=300, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ReleaseError("Database backup could not complete; no migration permitted.") from None
    if result.returncode:
        # libpq/pg_dump errors can include connection details; do not echo.
        raise ReleaseError("Database backup failed; check installed PostgreSQL tools and database access privately.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight", "backup", "migrate", "verify", "rollback-safe", "status"))
    args = parser.parse_args(argv)
    try:
        import django
        django.setup()
        if args.action == "backup":
            backup(sys.stdout.buffer)
        elif args.action == "migrate":
            migrate()
        elif args.action == "rollback-safe":
            rollback_safe()
        else:
            result = {"preflight": preflight, "status": status,
                      "verify": lambda: verify(json.load(sys.stdin))}[args.action]()
            print(json.dumps(result, sort_keys=True))
        return 0
    except ReleaseError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        print("Calendar-sync database check failed; inspect configuration privately. No credentials were printed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
