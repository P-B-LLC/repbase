"""Process-level release orchestration tests; all host commands are fakes.

The real helper's fixed production paths are rewritten only in a temporary test
copy. No subprocess can contact the database, network, root filesystem or host
systemd. The database support module has separate real PostgreSQL coverage.
"""

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest


BASE = "8323bb9ca402a5c0d25628855c5b23e36de64229"
TARGET = "a" * 40
SERVICE_NAMES = (
    "repbase.service", "repbase-feed-stats.service",
    "repbase-media-screening.service", "repbase-ranking-model.service",
    "repbase-video.service",
)
TIMER_NAMES = tuple(name.replace(".service", ".timer") for name in SERVICE_NAMES[1:])


FAKE_COMMAND = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

name = Path(sys.argv[0]).name
args = sys.argv[1:]
state_path = Path(os.environ["MOCK_SYNC_STATE"])
state = json.loads(state_path.read_text())
state["commands"].append({"name": name, "args": args})

def save():
    state_path.write_text(json.dumps(state))

def done(text="", code=0):
    save()
    if text:
        print(text)
    raise SystemExit(code)

if name == "id":
    done("0")
if name in ("flock", "sleep"):
    done()
if name == "sudo":
    while args and args[0].startswith("-"):
        flag = args.pop(0)
        if flag == "-u":
            args.pop(0)
    save()
    os.execvpe(args[0], args, os.environ)
if name == "git":
    if args[:1] == ["-C"]:
        args = args[2:]
    if args[0] == "rev-parse":
        done(state["head"] if args[-1] == "HEAD" else state["target"])
    if args[0] == "symbolic-ref":
        done(code=1)  # The beta starts from a detached release revision.
    if args[0] == "status":
        done()
    if args[0] in ("fetch", "merge-base"):
        done()
    if args[0] == "diff":
        done("" if "--diff-filter=DT" in args else "\n".join(state["paths"]))
    if args[0] == "show":
        save()
        sys.stdout.write(Path(os.environ["MOCK_HELPER_SOURCE"]).read_text())
        raise SystemExit(0)
    if args[0] == "checkout":
        state["head"] = args[-1]
        done()
if name == "systemctl":
    operation = args[0]
    if operation in ("list-unit-files", "list-units"):
        done("\n".join(name + " enabled" for name in state["units"]))
    if operation == "show":
        unit = args[-1]
        prop = next(arg.split("=", 1)[1] for arg in args if arg.startswith("--property="))
        data = state["units"].get(unit, {})
        def value(key):
            if key == "LoadState":
                return data.get("LoadState", "loaded") if data else "not-found"
            return data.get(key, "")
        if "--value" in args:
            done(value(prop))
        done("\n".join(key + "=" + value(key) for key in prop.split(",")))
    if operation == "stop":
        scenario = state["scenario"]
        failing = scenario == "quiesce_failure" and "repbase.service" in args
        worker_failure = scenario == "stop_worker_failure" and "repbase-media-screening.service" in args
        for unit in args[1:]:
            if failing and unit == "repbase.service":
                continue
            data = state["units"][unit]
            data.update(ActiveState="inactive", SubState="dead", Result="success",
                        MainPID="0", ControlPID="0", Job="", ControlGroup="")
            if unit == "repbase-media-screening.service":
                if scenario.startswith("stopped_signal") or scenario in (
                    "main_pid_remains", "control_pid_remains", "queued_job_remains",
                    "cgroup_populated", "cgroup_missing", "cgroup_empty",
                    "cgroup_removed", "cgroup_outside_root",
                ):
                    # systemd retains this outcome after intentionally stopping
                    # a running one-shot, even though no process can write.
                    data.update(ActiveState="failed", SubState="failed", Result="signal")
                if scenario == "main_pid_remains":
                    data["MainPID"] = "1001"
                if scenario == "control_pid_remains":
                    data["ControlPID"] = "1002"
                if scenario == "queued_job_remains":
                    data["Job"] = "42"
                if scenario in ("cgroup_populated", "cgroup_missing", "cgroup_empty", "cgroup_removed"):
                    data["ControlGroup"] = "/system.slice/repbase-media-screening.service"
                if scenario == "cgroup_outside_root":
                    data["ControlGroup"] = "/../outside-cgroup"
                if scenario == "unit_not_loaded_after_stop":
                    data["LoadState"] = "not-found"
                if worker_failure:
                    data.update(ActiveState="activating", SubState="start", MainPID="1001")
        done(code=int(failing or worker_failure))
    if operation == "reset-failed":
        for unit in args[1:]:
            data = state["units"][unit]
            if data["ActiveState"] == "failed":
                # Clearing exit bookkeeping does not terminate any process.
                # The helper must prove quiescence before issuing this call.
                data.update(ActiveState="inactive", SubState="dead", Result="success")
        done()
    if operation == "start":
        for unit in (arg for arg in args[1:] if not arg.startswith("--")):
            data = state["units"][unit]
            data.update(ActiveState="active", SubState="running", Result="success",
                        LoadState="loaded", MainPID="2001" if unit.endswith(".service") else "0",
                        ControlPID="0", Job="", ControlGroup="")
            if unit != "repbase.service" and unit.endswith(".service"):
                # Legitimate one-shot completion is not a restart failure.
                data.update(ActiveState="inactive", SubState="dead", MainPID="0")
            if state["head"] == state["target"]:
                if state["scenario"] == "links_then_health_failure" and unit == "repbase.service":
                    state["links"] = 1
                if state["scenario"] == "worker_error" and unit == "repbase-media-screening.service":
                    data.update(ActiveState="inactive", SubState="dead", Result="exit-code")
        done()
    if operation == "is-active":
        done(code=0 if state["units"][args[-1]]["ActiveState"] == "active" else 3)
if name == "python":
    if args[:2] == ["-m", "deploy.calendar_sync_release"]:
        action = args[2]
        state["support"].append(action)
        if action == "preflight":
            done(json.dumps({"baseline": True}), code=int(state["scenario"].endswith("preflight_failure")))
        if action == "backup":
            done("PGDMP mock private database", code=int(state["scenario"].endswith("backup_failure")))
        if action == "migrate":
            state["migrated"] = True
            done()
        if action == "verify":
            assert json.load(sys.stdin) == {"baseline": True}
            done(json.dumps({"verified": True}))
        if action == "rollback-safe":
            done(code=int(bool(state["links"])))
        if action == "status":
            done(json.dumps({"links": state["links"]}))
    if args[-2:] == ["check", "--deploy"]:
        done()
if name == "pg_restore":
    assert Path(args[-1]).read_bytes().startswith(b"PGDMP")
    done("; Mock custom archive\n1; 0 0 TABLE public core_plannerentry")
if name == "pg_dump":
    done(code=2)  # Only the mocked support backup may produce a dump.
if name == "curl":
    if state["scenario"] == "links_then_health_failure" and state["head"] == state["target"]:
        done("500")
    if state["scenario"] == "api_bad_gateway" and state["head"] == state["target"]:
        done("200" if args[-1].endswith("/health/") else "502")
    done("200" if args[-1].endswith("/health/") else "401")
save()
print("Unexpected mocked command: " + name + " " + repr(args), file=sys.stderr)
raise SystemExit(2)
'''


@unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "Requires POSIX Bash; runs in Linux CI")
class CalendarSyncShellTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="calendar-sync-shell-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.app = self.root / "app"
        (self.app / ".git").mkdir(parents=True)
        (self.app / ".venv/bin").mkdir(parents=True)
        (self.root / "records").mkdir()
        self.cgroup_root = self.root / "cgroup"
        self.cgroup_root.mkdir()
        (self.cgroup_root / "cgroup.controllers").write_text("cpu memory pids\n")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for command in ("id", "flock", "sleep", "sudo", "git", "systemctl", "pg_dump", "pg_restore", "curl"):
            self.executable(self.bin / command, FAKE_COMMAND)
        self.executable(self.app / ".venv/bin/python", FAKE_COMMAND)
        self.env_file = self.root / "app.env"
        self.env_file.write_text("DJANGO_SETTINGS_MODULE=config.test_postgres\n")
        self.stamp = self.root / "release-stamp"
        self.stamp.write_text(BASE + "\n")
        source = Path(__file__).resolve().parents[1] / "deploy/deploy-calendar-sync-hotfix.sh"
        helper = source.read_text()
        substitutions = {
            "APP=/home/django/app": f"APP={self.app}",
            "CGROUP_ROOT=/sys/fs/cgroup": f"CGROUP_ROOT={self.cgroup_root}",
            "/etc/repbase.env": str(self.env_file),
            "/etc/repbase-release": str(self.stamp),
            "/run/lock/repbase-deploy.lock": str(self.root / "deployment.lock"),
            "/var/lib/repbase-calendar-sync.": str(self.root / "records/release."),
        }
        for original, replacement in substitutions.items():
            self.assertIn(original, helper)
            helper = helper.replace(original, replacement)
        self.helper = self.root / "helper.sh"
        self.helper.write_text(helper)
        self.state_file = self.root / "state.json"
        self.state = {
            "head": BASE, "target": TARGET, "scenario": "success",
            "links": 0, "migrated": False, "commands": [], "support": [],
            "paths": [
                "core/models.py", "core/views.py",
                "core/migrations/0081_plannerentry_source_schedule.py",
                "core/schedule_sync.py", "core/test_schedule_planner_sync.py",
                "deploy/deploy-calendar-sync-hotfix.sh", "deploy/CALENDAR_SYNC_HOTFIX.md",
                "deploy/calendar_sync_release.py", "core/test_calendar_sync_release.py",
                "core/test_calendar_sync_shell.py", "TESTING.md",
                "core/test_calendar_sync_systemd.py",
                ".github/workflows/ci.yml",
            ],
            "units": {
                unit: {
                    "LoadState": "loaded", "ActiveState": "active", "SubState": "running",
                    "Result": "success", "MainPID": "1001" if unit.endswith(".service") else "0",
                    "ControlPID": "0", "Job": "", "ControlGroup": "",
                }
                for unit in SERVICE_NAMES + TIMER_NAMES
            },
        }
        # Exercise a running one-shot and originally idle service/timer, too.
        self.state["units"]["repbase-media-screening.service"]["ActiveState"] = "activating"
        self.state["units"]["repbase-ranking-model.service"]["ActiveState"] = "inactive"
        self.state["units"]["repbase-ranking-model.service"]["MainPID"] = "0"
        self.state["units"]["repbase-ranking-model.timer"]["ActiveState"] = "inactive"

    def executable(self, path, text):
        path.write_text(text)
        path.chmod(0o700)

    def run_helper(self, scenario="success"):
        self.state["scenario"] = scenario
        if scenario == "stopped_signal_inactive_worker":
            self.state["units"]["repbase-media-screening.service"].update(
                ActiveState="inactive", SubState="dead", MainPID="0",
            )
        if scenario in ("cgroup_populated", "cgroup_empty", "cgroup_missing"):
            group = self.cgroup_root / "system.slice/repbase-media-screening.service"
            group.mkdir(parents=True)
            if scenario != "cgroup_missing":
                populated = "1" if scenario == "cgroup_populated" else "0"
                (group / "cgroup.events").write_text(f"populated {populated}\nfrozen 0\n")
        self.state_file.write_text(json.dumps(self.state))
        environment = os.environ.copy()
        environment.update({
            "PATH": str(self.bin) + os.pathsep + environment.get("PATH", ""),
            "REPBASE_CALENDAR_SYNC_PINNED": "1",
            "MOCK_SYNC_STATE": str(self.state_file),
            "MOCK_HELPER_SOURCE": str(self.helper),
        })
        result = subprocess.run(
            [shutil.which("bash"), str(self.helper), TARGET],
            env=environment, text=True, capture_output=True, timeout=30,
        )
        self.state = json.loads(self.state_file.read_text())
        return result

    def commands(self, name, operation=None):
        commands = [entry["args"] for entry in self.state["commands"] if entry["name"] == name]
        if operation:
            commands = [args for args in commands if operation in args]
        return commands

    def test_success_validates_backup_before_migration_and_restores_original_units(self):
        result = self.run_helper()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.state["head"], TARGET)
        self.assertEqual(self.stamp.read_text().strip(), TARGET)
        self.assertEqual(self.state["support"], ["preflight", "backup", "migrate", "verify"])
        operations = self.state["commands"]
        archive_check = next(i for i, item in enumerate(operations) if item["name"] == "pg_restore")
        migration = next(i for i, item in enumerate(operations) if item["name"] == "python" and "migrate" in item["args"])
        self.assertLess(archive_check, migration)
        starts = self.commands("systemctl", "start")
        self.assertFalse(any("repbase-ranking-model.service" in args for args in starts))
        self.assertFalse(any("repbase-ranking-model.timer" in args for args in starts))
        self.assertEqual(self.state["units"]["repbase-media-screening.timer"]["ActiveState"], "active")
        record, = (self.root / "records").iterdir()
        self.assertEqual(stat.S_IMODE(record.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((record / "database.dump").stat().st_mode), 0o600)
        self.assertTrue((record / "database.dump.sha256").is_file())

    def test_pre_migration_failure_restores_clean_base_and_services(self):
        result = self.run_helper("preflight_failure")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state["head"], BASE)
        self.assertFalse(self.state["migrated"])
        self.assertEqual(self.stamp.read_text().strip(), BASE)
        self.assertEqual(self.state["units"]["repbase.service"]["ActiveState"], "active")
        self.assertIn("Original backend restored", result.stderr)

    def test_links_after_resume_refuse_old_code_rollback_and_leave_writers_stopped(self):
        result = self.run_helper("links_then_health_failure")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state["head"], TARGET)
        self.assertEqual(self.state["links"], 1)
        self.assertEqual(self.stamp.read_text().strip(), BASE)
        self.assertTrue(all(unit["ActiveState"] == "inactive" for unit in self.state["units"].values()))
        self.assertIn("old-code compatibility could not be proved", result.stderr)
        self.assertFalse(any(args[-1] == BASE for args in self.commands("git", "checkout")))

    def test_quiescence_failure_prevents_candidate_checkout(self):
        result = self.run_helper("quiesce_failure")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state["head"], BASE)
        self.assertFalse(self.commands("git", "checkout"))
        self.assertEqual(self.state["support"], [])

    def test_intentionally_stopped_oneshot_signal_with_no_processes_can_deploy(self):
        result = self.run_helper("stopped_signal")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.state["head"], TARGET)
        self.assertEqual(self.stamp.read_text().strip(), TARGET)
        self.assertTrue(self.state["migrated"])
        self.assertEqual(self.state["units"]["repbase.service"]["ActiveState"], "active")
        record, = (self.root / "records").iterdir()
        evidence = (record / "unit-stop-evidence").read_text()
        worker = evidence.split("unit=repbase-media-screening.service\n", 1)[1].split("\nunit=", 1)[0]
        for expected in ("ActiveState=failed", "Result=signal", "MainPID=0", "ControlPID=0"):
            self.assertIn(expected, worker.splitlines())

    def test_idle_worker_started_by_timer_before_stop_is_reset_without_forced_start(self):
        result = self.run_helper("stopped_signal_inactive_worker")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.state["head"], TARGET)
        worker = self.state["units"]["repbase-media-screening.service"]
        self.assertEqual(worker["ActiveState"], "inactive")
        self.assertEqual(worker["Result"], "success")
        self.assertEqual(self.state["units"]["repbase-media-screening.timer"]["ActiveState"], "active")
        self.assertTrue(any("repbase-media-screening.service" in args
                            for args in self.commands("systemctl", "reset-failed")))
        self.assertFalse(any("repbase-media-screening.service" in args
                             for args in self.commands("systemctl", "start")))
        record, = (self.root / "records").iterdir()
        evidence = (record / "unit-stop-evidence").read_text()
        saved_worker = evidence.split("unit=repbase-media-screening.service\n", 1)[1].split("\nunit=", 1)[0]
        self.assertIn("ActiveState=failed", saved_worker.splitlines())
        self.assertIn("Result=signal", saved_worker.splitlines())

    def test_stopped_signal_and_preflight_failure_restore_unchanged_base(self):
        result = self.run_helper("stopped_signal_preflight_failure")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state["head"], BASE)
        self.assertFalse(self.state["migrated"])
        self.assertEqual(self.state["units"]["repbase.service"]["ActiveState"], "active")
        self.assertEqual(self.stamp.read_text().strip(), BASE)

    def test_stopped_signal_and_backup_failure_restore_unchanged_base(self):
        result = self.run_helper("stopped_signal_backup_failure")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state["head"], BASE)
        self.assertNotIn("migrate", self.state["support"])
        self.assertEqual(self.state["units"]["repbase.service"]["ActiveState"], "active")
        self.assertEqual(self.stamp.read_text().strip(), BASE)

    def assert_writer_evidence_blocks_checkout(self, scenario):
        result = self.run_helper(scenario)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.state["head"], BASE)
        self.assertFalse(self.commands("git", "checkout"))
        self.assertEqual(self.state["support"], [])
        self.assertEqual(self.stamp.read_text().strip(), BASE)
        self.assertFalse(any("repbase-media-screening.service" in args
                             for args in self.commands("systemctl", "reset-failed")),
                         "An unsafe/unknown worker must retain its failure evidence")
        if scenario in (
            "main_pid_remains", "control_pid_remains", "queued_job_remains",
            "cgroup_populated", "cgroup_missing", "cgroup_outside_root",
        ):
            self.assertEqual(self.state["units"]["repbase-media-screening.service"]["ActiveState"], "failed")
            self.assertEqual(self.state["units"]["repbase-media-screening.timer"]["ActiveState"], "inactive")
            self.assertFalse(any("repbase-media-screening.service" in args
                                 for args in self.commands("systemctl", "start")))
        # No candidate/schema work started, so a worker problem must not keep
        # the unchanged web backend down during failure recovery.
        self.assertEqual(self.state["units"]["repbase.service"]["ActiveState"], "active")

    def test_nonzero_main_pid_blocks_candidate_checkout(self):
        self.assert_writer_evidence_blocks_checkout("main_pid_remains")

    def test_nonzero_control_pid_blocks_candidate_checkout(self):
        self.assert_writer_evidence_blocks_checkout("control_pid_remains")

    def test_queued_job_blocks_candidate_checkout(self):
        self.assert_writer_evidence_blocks_checkout("queued_job_remains")

    def test_populated_cgroup_blocks_candidate_checkout(self):
        self.assert_writer_evidence_blocks_checkout("cgroup_populated")

    def test_missing_cgroup_evidence_blocks_candidate_checkout(self):
        self.assert_writer_evidence_blocks_checkout("cgroup_missing")

    def test_cgroup_traversal_blocks_candidate_checkout(self):
        self.assert_writer_evidence_blocks_checkout("cgroup_outside_root")

    def test_not_loaded_unit_blocks_candidate_checkout(self):
        self.assert_writer_evidence_blocks_checkout("unit_not_loaded_after_stop")

    def test_proven_empty_cgroup_allows_candidate_checkout(self):
        result = self.run_helper("cgroup_empty")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.state["head"], TARGET)
        self.assertTrue(self.state["migrated"])

    def test_removed_empty_cgroup_allows_candidate_checkout(self):
        result = self.run_helper("cgroup_removed")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.state["head"], TARGET)
        self.assertTrue(self.state["migrated"])

    def test_pre_switch_worker_stop_error_restores_base_web_independently(self):
        self.assert_writer_evidence_blocks_checkout("stop_worker_failure")
        starts = self.commands("systemctl", "start")
        self.assertTrue(starts)
        self.assertEqual(starts[0][-1], "repbase.service")

    def test_health_200_does_not_accept_api_502(self):
        result = self.run_helper("api_bad_gateway")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Required health checks did not pass", result.stderr)
        self.assertEqual(self.state["head"], BASE)
        self.assertEqual(self.stamp.read_text().strip(), BASE)
        self.assertEqual(self.state["units"]["repbase.service"]["ActiveState"], "active")

    def test_backup_failure_cannot_run_migration(self):
        result = self.run_helper("backup_failure")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state["head"], BASE)
        self.assertNotIn("migrate", self.state["support"])
        self.assertFalse(self.commands("pg_restore"))

    def test_unexpected_code_path_is_rejected_before_any_service_stop(self):
        self.state["paths"].append("core/ranking.py")
        result = self.run_helper()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unexpected changed path", result.stderr)
        self.assertFalse(self.commands("systemctl", "stop"))
        self.assertFalse(self.commands("git", "checkout"))

    def test_mismatched_release_stamp_is_rejected_before_any_service_stop(self):
        self.stamp.write_text("b" * 40 + "\n")
        result = self.run_helper()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Release stamp differs", result.stderr)
        self.assertFalse(self.commands("systemctl", "stop"))
        self.assertFalse(self.commands("git", "checkout"))

    def test_immediate_worker_error_is_not_reported_as_success(self):
        result = self.run_helper("worker_error")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state["head"], BASE)
        self.assertIn("Original backend restored", result.stderr)
        self.assertEqual(self.stamp.read_text().strip(), BASE)
