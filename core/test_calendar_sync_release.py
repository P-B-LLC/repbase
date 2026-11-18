"""The narrow sync rollout must preserve data and refuse unsafe rollback.

PostgreSQL rehearsals use Django's disposable test database only. They never
restore a dump or start application services.
"""

from contextlib import contextmanager, nullcontext, redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import timedelta
import io
import os
import re
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
from unittest import mock, skipUnless

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TransactionTestCase
from django.utils import timezone

from deploy import calendar_sync_release as release

from .fixtures_for_tests import FAST_PASSWORDS, make_account
from .models import PlannerEntry, WorkoutSchedule, WorkoutSession, WorkoutTemplate


def snapshot(*, applied=False, links=0):
    migrations = [list(release.PREVIOUS)]
    if applied:
        migrations.append(list(release.TARGET))
    return {
        "format": 1,
        "row_counts": {name: 3 for name in release.TABLES},
        "applied_migrations": migrations,
        "source_column_exists": applied,
        "linked_planner_rows": links,
    }


class CalendarSyncReleaseUnitTests(SimpleTestCase):
    def test_release_requires_a_direct_postgres_connection(self):
        for vendor, config in (
            ("sqlite", {}),
            ("postgresql", {"DISABLE_SERVER_SIDE_CURSORS": True}),
        ):
            with self.subTest(vendor=vendor), mock.patch.object(
                release, "connection", SimpleNamespace(vendor=vendor, settings_dict=config)
            ), self.assertRaises(release.ReleaseError):
                release.require_postgres()

    def fake_executor(self, *, applied=False, plan=None, unknown=False):
        migration = SimpleNamespace(app_label=release.TARGET[0], name=release.TARGET[1])
        executor = mock.Mock()
        known = {release.PREVIOUS, release.TARGET}
        done = {release.PREVIOUS}
        if applied:
            done.add(release.TARGET)
        if unknown:
            done.add(("core", "0999_unreviewed"))
        executor.loader.applied_migrations = done
        executor.loader.graph.nodes = dict.fromkeys(known)
        executor.loader.graph.leaf_nodes.return_value = [release.TARGET]
        executor.migration_plan.return_value = ([] if applied else [(migration, False)]) if plan is None else plan
        return executor

    def run_checked_plan(self, executor, *, column=False):
        with mock.patch.object(release, "require_postgres"), \
                mock.patch.object(release, "MigrationExecutor", return_value=executor), \
                mock.patch.object(release, "column_exists", return_value=column):
            return release.checked_plan()

    def test_only_exact_forward_migration_is_accepted(self):
        executor = self.fake_executor()
        actual, plan = self.run_checked_plan(executor)
        self.assertIs(actual, executor)
        self.assertEqual(len(plan), 1)
        executor.loader.check_consistent_history.assert_called_once_with(release.connection)

    def test_already_applied_schema_is_an_idempotent_plan(self):
        _, plan = self.run_checked_plan(self.fake_executor(applied=True), column=True)
        self.assertEqual(plan, [])

    def test_plan_drift_unknown_history_and_column_mismatch_are_refused(self):
        extra = SimpleNamespace(app_label="core", name="0082_not_approved")
        candidates = [
            (self.fake_executor(plan=[(extra, False)]), False),
            (self.fake_executor(plan=[(extra, True)]), False),
            (self.fake_executor(plan=[]), False),
            (self.fake_executor(unknown=True), False),
            (self.fake_executor(), True),
            (self.fake_executor(applied=True), False),
        ]
        missing_previous = self.fake_executor()
        missing_previous.loader.applied_migrations = set()
        candidates.append((missing_previous, False))
        missing_target = self.fake_executor()
        missing_target.loader.graph.nodes = {release.PREVIOUS: None}
        candidates.append((missing_target, False))
        for executor, column in candidates:
            with self.subTest(column=column, plan=executor.migration_plan.return_value):
                with self.assertRaises(release.ReleaseError):
                    self.run_checked_plan(executor, column=column)

    def test_preflight_retry_requires_no_existing_links(self):
        with mock.patch.object(release, "checked_plan"), \
                mock.patch.object(release, "status", return_value=snapshot(applied=True)):
            self.assertTrue(release.preflight()["source_column_exists"])
        with mock.patch.object(release, "checked_plan"), \
                mock.patch.object(release, "status", return_value=snapshot(applied=True, links=1)):
            with self.assertRaisesRegex(release.ReleaseError, "already exist"):
                release.preflight()

    def test_verify_requires_only_additive_migration_and_unchanged_rows(self):
        before = snapshot()
        current = snapshot(applied=True)
        with mock.patch.object(release, "checked_plan"), \
                mock.patch.object(release, "status", return_value=current):
            self.assertEqual(release.verify(before), current)
            self.assertEqual(release.verify(current), current)
        changes = []
        drift = deepcopy(current)
        drift["row_counts"]["core_plannerentry"] -= 1
        changes.append(drift)
        drift = deepcopy(current)
        drift["applied_migrations"].append(["core", "0082_unapproved"])
        changes.append(drift)
        changes.extend([snapshot(applied=True, links=1), snapshot()])
        for drift in changes:
            with self.subTest(drift=drift), mock.patch.object(release, "checked_plan"), \
                    mock.patch.object(release, "status", return_value=drift):
                with self.assertRaises(release.ReleaseError):
                    release.verify(before)
        with mock.patch.object(release, "checked_plan"), \
                mock.patch.object(release, "status", return_value=current):
            for malformed in ({}, None, {**before, "format": 2}, snapshot(links=1)):
                with self.subTest(malformed=malformed), self.assertRaises(release.ReleaseError):
                    release.verify(malformed)

    def test_rollback_refuses_links_and_fails_closed_on_database_error(self):
        with mock.patch.object(release, "require_postgres"), \
                mock.patch.object(release, "linked_count", return_value=0):
            release.rollback_safe()
        with mock.patch.object(release, "require_postgres"), \
                mock.patch.object(release, "linked_count", return_value=1):
            with self.assertRaisesRegex(release.ReleaseError, "rollback refused"):
                release.rollback_safe()
        with mock.patch.object(release, "require_postgres"), \
                mock.patch.object(release, "linked_count", side_effect=RuntimeError("database unavailable")):
            with self.assertRaises(RuntimeError):
                release.rollback_safe()

    def test_migrate_restores_timeout_settings_even_after_failure(self):
        fake_connection = mock.MagicMock()
        cursor = fake_connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [("5s",), ("30s",)]
        with mock.patch.object(release, "connection", fake_connection), \
                mock.patch.object(release, "checked_plan", return_value=(None, [object()])), \
                mock.patch.object(release, "linked_count", return_value=0), \
                mock.patch.object(release, "call_command", side_effect=RuntimeError("migration failed")) as command:
            with self.assertRaises(RuntimeError):
                release.migrate()
        command.assert_called_once_with("migrate", *release.TARGET, interactive=False, verbosity=1)
        self.assertIn(mock.call("SELECT set_config('lock_timeout', '5s', false)"), cursor.execute.call_args_list)
        self.assertIn(mock.call("SELECT set_config('statement_timeout', '120s', false)"), cursor.execute.call_args_list)
        self.assertEqual(cursor.execute.call_args_list[-2:], [
            mock.call("SELECT set_config('lock_timeout', %s, false)", ["5s"]),
            mock.call("SELECT set_config('statement_timeout', %s, false)", ["30s"]),
        ])

    def test_migrate_never_writes_on_drift_or_existing_links(self):
        with mock.patch.object(release, "checked_plan", side_effect=release.ReleaseError("drift")), \
                mock.patch.object(release, "call_command") as command:
            with self.assertRaises(release.ReleaseError):
                release.migrate()
            command.assert_not_called()
        with mock.patch.object(release, "checked_plan", return_value=(None, [object()])), \
                mock.patch.object(release, "linked_count", return_value=1), \
                mock.patch.object(release, "call_command") as command:
            with self.assertRaises(release.ReleaseError):
                release.migrate()
            command.assert_not_called()

    def test_backup_credentials_are_not_in_argv_or_inherited_environment(self):
        config = {
            "NAME": "disposable-db", "HOST": "db.invalid", "USER": "fixture-user",
            "PASSWORD": "fixture-secret-not-real", "PORT": "25060",
            "OPTIONS": {"sslmode": "verify-full", "sslrootcert": "/fixture/ca.crt"},
        }
        fake_connection = SimpleNamespace(vendor="postgresql", settings_dict=config)
        injected = {"PATH": "/fixture/bin", "PGHOST": "wrong.invalid", "PGSERVICE": "wrong-service",
                    "DATABASE_URL": "do-not-copy", "MODERATION_API_KEY": "do-not-copy"}
        with mock.patch.object(release, "connection", fake_connection), \
                mock.patch.dict(os.environ, injected, clear=True), \
                mock.patch.object(release.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            release.backup(io.BytesIO())
        argv = run.call_args.args[0]
        env = run.call_args.kwargs["env"]
        self.assertNotIn(config["PASSWORD"], " ".join(argv))
        self.assertNotIn(config["HOST"], " ".join(argv))
        self.assertIn("--no-password", argv)
        self.assertIn("--format=custom", argv)
        self.assertEqual(env["PGPASSWORD"], config["PASSWORD"])
        self.assertEqual(env["PGHOST"], config["HOST"])
        self.assertEqual(env["PGDATABASE"], config["NAME"])
        self.assertEqual(env["PGSSLROOTCERT"], "/fixture/ca.crt")
        for name in ("PGSERVICE", "DATABASE_URL", "MODERATION_API_KEY"):
            self.assertNotIn(name, env)
        self.assertEqual(run.call_args.kwargs["stderr"], subprocess.PIPE)

    def test_backup_rejects_unreviewed_connection_options(self):
        config = {"NAME": "fixture", "HOST": "fixture", "USER": "fixture", "OPTIONS": {"service": "secret"}}
        with mock.patch.object(release, "connection", SimpleNamespace(vendor="postgresql", settings_dict=config)), \
                self.assertRaises(release.ReleaseError):
            release.backup_environment()

    def test_backup_failures_do_not_echo_credentials(self):
        secret = "private-connection-fixture"
        outcomes = [
            {"return_value": SimpleNamespace(returncode=1, stderr=secret.encode())},
            {"side_effect": OSError(secret)},
            {"side_effect": subprocess.TimeoutExpired([secret], 300)},
        ]
        for outcome in outcomes:
            with self.subTest(outcome=outcome), mock.patch.object(release, "backup_environment", return_value={}), \
                    mock.patch.object(release.subprocess, "run", **outcome):
                with self.assertRaises(release.ReleaseError) as raised:
                    release.backup(io.BytesIO())
                self.assertNotIn(secret, str(raised.exception))

    def test_cli_hides_unexpected_exceptions_and_malformed_baseline(self):
        for action, patches in (
            ("preflight", {"preflight": mock.Mock(side_effect=RuntimeError("secret-fixture"))}),
            ("verify", {}),
        ):
            output, errors = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(errors), mock.patch("django.setup"), \
                    mock.patch.object(release.sys, "stdin", io.StringIO("not-json")), \
                    (mock.patch.multiple(release, **patches) if patches else nullcontext()):
                self.assertEqual(release.main([action]), 1)
            self.assertEqual(output.getvalue(), "")
            self.assertNotIn("secret-fixture", errors.getvalue())
            self.assertIn("No credentials were printed", errors.getvalue())


@skipUnless(connection.vendor == "postgresql", "Migration/backup rehearsal requires disposable PostgreSQL")
@FAST_PASSWORDS
class CalendarSyncReleasePostgresTests(TransactionTestCase):
    def setUp(self):
        _, self.profile, _ = make_account("sync-release-rehearsal")
        self.today = timezone.now().date()
        self.workout = WorkoutTemplate.objects.create(owner=self.profile, name="My leg day", workout_type="lifting")
        self.schedule = WorkoutSchedule.objects.create(owner=self.profile, workout=self.workout, scheduled_date=self.today)
        self.manual = PlannerEntry.objects.create(
            owner=self.profile, title="Personal reminder", category="workout",
            workout=self.workout, scheduled_date=self.today, notes="Keep my own details",
        )
        self.completed = PlannerEntry.objects.create(
            owner=self.profile, title="Earlier workout", category="workout",
            workout=self.workout, scheduled_date=self.today - timedelta(days=1), completed_at=timezone.now(),
        )
        self.session = WorkoutSession.objects.create(
            repbase_user=self.profile, workout=self.workout, status="completed",
            started_at=timezone.now() - timedelta(hours=1), ended_at=timezone.now(),
        )

    @contextmanager
    def legacy_schema(self):
        # Keep the test runner's expected latest schema even when assertions
        # fail, so Django can safely flush its disposable database afterward.
        try:
            MigrationExecutor(connection).migrate([release.PREVIOUS])
            yield
        finally:
            MigrationExecutor(connection).migrate([release.TARGET])

    def test_forward_migration_preserves_tasks_history_and_zero_link_rollback(self):
        original = {row.pk: (row.title, row.notes, row.completed_at, row.workout_id)
                    for row in PlannerEntry.objects.all()}
        with self.legacy_schema():
            self.assertFalse(release.column_exists())
            release.rollback_safe()
            before = release.preflight()
            self.assertNotIn(list(release.TARGET), before["applied_migrations"])
            with redirect_stdout(io.StringIO()):
                release.migrate()
            after = release.verify(before)
            self.assertTrue(after["source_column_exists"])
            self.assertEqual(after["linked_planner_rows"], 0)
            self.assertEqual(after["row_counts"], before["row_counts"])
            release.rollback_safe()
            self.assertEqual({row.pk: (row.title, row.notes, row.completed_at, row.workout_id)
                              for row in PlannerEntry.objects.all()}, original)
            self.assertTrue(WorkoutSession.objects.filter(pk=self.session.pk, status="completed").exists())
            # A previous pre-traffic failure may have left the nullable column.
            retry_before = release.preflight()
            with redirect_stdout(io.StringIO()):
                release.migrate()
            self.assertEqual(release.verify(retry_before), retry_before)
            # Exercise the actual old model's delete path while the additive
            # nullable FK remains, not just the helper's zero-link decision.
            old_apps = MigrationExecutor(connection).loader.project_state([release.PREVIOUS]).apps
            old_schedule = old_apps.get_model("core", "WorkoutSchedule")
            old_schedule.objects.get(pk=self.schedule.pk).delete()
            self.assertFalse(WorkoutSchedule.objects.filter(pk=self.schedule.pk).exists())
            self.assertEqual(PlannerEntry.objects.count(), len(original))
            self.assertTrue(WorkoutSession.objects.filter(pk=self.session.pk).exists())

    def test_populated_links_prevent_old_code_rollback_without_mutating_data(self):
        PlannerEntry.objects.filter(pk=self.manual.pk).update(source_schedule=self.schedule)
        before = release.status()
        with self.assertRaisesRegex(release.ReleaseError, "rollback refused"):
            release.rollback_safe()
        with self.assertRaisesRegex(release.ReleaseError, "already exist"):
            release.preflight()
        with self.assertRaisesRegex(release.ReleaseError, "writers active"):
            release.migrate()
        self.assertEqual(release.status(), before)
        self.manual.refresh_from_db()
        self.assertEqual(self.manual.source_schedule_id, self.schedule.pk)
        self.assertEqual(self.manual.notes, "Keep my own details")

    def test_row_change_during_maintenance_is_detected(self):
        before = release.preflight()
        PlannerEntry.objects.create(owner=self.profile, title="Concurrent task", scheduled_date=self.today)
        with self.assertRaisesRegex(release.ReleaseError, "verification failed"):
            release.verify(before)

    def test_real_custom_backup_can_be_listed_without_restoring_database(self):
        versions = {}
        for tool in ("pg_dump", "pg_restore"):
            self.assertIsNotNone(shutil.which(tool), f"PostgreSQL release rehearsal requires {tool} on PATH")
            version = subprocess.run([tool, "--version"], capture_output=True, text=True, timeout=10, check=False)
            self.assertEqual(version.returncode, 0, f"Could not read {tool} version; stderr withheld")
            match = re.match(rf"^{tool} \(PostgreSQL\) (\d+)(?:\.(\d+))?", version.stdout.strip())
            self.assertIsNotNone(match, f"Could not identify {tool} version; output withheld")
            versions[tool] = (int(match[1]), int(match[2] or 0))
        server_version = connection.pg_version
        server_major = server_version // 10000
        diagnostics = ", ".join([
            f"server={server_major}.{server_version % 10000}",
            *(f"{tool}={major}.{minor}" for tool, (major, minor) in versions.items()),
        ])
        # Versions are safe diagnostics; never include the DB URL, hostname,
        # credentials, or raw pg_dump stderr when this rehearsal fails.
        print(f"Calendar-sync backup toolchain: {diagnostics}")
        self.assertGreaterEqual(versions["pg_dump"][0], server_major,
                                f"pg_dump cannot back up a newer PostgreSQL major version: {diagnostics}")
        self.assertGreaterEqual(versions["pg_restore"][0], versions["pg_dump"][0],
                                f"pg_restore is older than the archive-producing client: {diagnostics}")
        # NAME is the active test database, not the configured non-test name.
        self.assertTrue(str(connection.settings_dict["NAME"]).startswith("test_"))
        before = release.status()
        with tempfile.TemporaryDirectory(prefix="rytivo-sync-backup-rehearsal-") as temporary:
            archive_path = os.path.join(temporary, "database.dump")
            try:
                with open(archive_path, "wb") as archive:
                    release.backup(archive)
            except release.ReleaseError:
                self.fail(f"Disposable database backup failed ({diagnostics}); connection details withheld")
            with open(archive_path, "rb") as archive:
                self.assertEqual(archive.read(5), b"PGDMP")
            # Match the deployment helper: let pg_restore open the archive.
            # Buffered Python reads can leave an inherited stdin descriptor
            # at a different offset even after the Python stream seeks to 0.
            result = subprocess.run(["pg_restore", "--list", archive_path],
                                    capture_output=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0,
                         f"pg_restore could not validate the disposable backup ({diagnostics}); stderr withheld")
        listing = result.stdout.decode("utf-8", errors="replace")
        self.assertIn("core_plannerentry", listing)
        self.assertIn("core_workoutschedule", listing)
        self.assertEqual(release.status(), before)
