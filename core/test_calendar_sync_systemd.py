"""Opt-in, real systemd rehearsal using only a disposable transient service.

Run on an isolated CI VM with RYTIVO_SYSTEMD_REHEARSAL=1. No database, repbase
units, deployment, or production host is accessed. Default test runs skip it.
"""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest
import uuid


@unittest.skipUnless(os.environ.get("RYTIVO_SYSTEMD_REHEARSAL") == "1",
                     "Real systemd rehearsal is explicitly opt-in on an isolated CI VM")
class CalendarSyncSystemdRehearsal(unittest.TestCase):
    PROPERTIES = (
        "LoadState", "ActiveState", "SubState", "Result",
        "MainPID", "ControlPID", "ControlGroup",
    )

    def command(self, args, *, timeout=15):
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)

    def unit_state(self, unit):
        result = self.command([
            "systemctl", "show", "--property=" + ",".join(self.PROPERTIES), unit,
        ], timeout=5)
        self.assertEqual(result.returncode, 0, f"Could not inspect isolated unit: {result.stderr}")
        return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)

    def await_state(self, unit, predicate):
        deadline = time.monotonic() + 15
        while True:
            state = self.unit_state(unit)
            if predicate(state):
                return state
            if time.monotonic() >= deadline:
                self.fail(f"Isolated service did not reach the expected state: {state}")
            time.sleep(0.1)

    def helper_predicate(self, block, unit):
        # Execute only the production helper's marked, read-only predicate.
        # Its unit argument is a UUID name created by this test, never repbase.
        script = 'CGROUP_ROOT=/sys/fs/cgroup\n' + block + '\nunit_is_quiescent "$1"\n'
        return self.command(["sudo", "-n", "bash", "-c", script, "sync-rehearsal", unit])

    def test_intentionally_stopped_oneshot_is_quiescent_even_when_failed(self):
        self.assertTrue(sys.platform.startswith("linux"), "Opt-in rehearsal requires a Linux CI VM")
        self.assertEqual(Path("/proc/1/comm").read_text().strip(), "systemd",
                         "Opt-in rehearsal requires real systemd as PID 1, not a mock/container shim")
        for executable in ("sudo", "systemd-run", "systemctl", "bash", "sleep"):
            self.assertIsNotNone(shutil.which(executable), f"Missing rehearsal command: {executable}")
        self.assertTrue(Path("/sys/fs/cgroup/cgroup.controllers").is_file(),
                        "Opt-in rehearsal requires unified cgroup v2")
        privilege = self.command(["sudo", "-n", "true"])
        self.assertEqual(privilege.returncode, 0, "Isolated CI rehearsal needs noninteractive sudo")

        helper = Path(__file__).resolve().parents[1] / "deploy" / "deploy-calendar-sync-hotfix.sh"
        source = helper.read_text()
        start, end = "# BEGIN QUIESCENCE CHECK", "# END QUIESCENCE CHECK"
        self.assertEqual(source.count(start), 1)
        self.assertEqual(source.count(end), 1)
        block = source.split(start, 1)[1].split(end, 1)[0]
        self.assertIn("unit_is_quiescent()", block)

        unit = f"rytivo-sync-rehearsal-{uuid.uuid4().hex}.service"
        self.assertRegex(unit, r"^rytivo-sync-rehearsal-[0-9a-f]{32}\.service$")
        try:
            created = self.command([
                "sudo", "-n", "systemd-run", "--quiet", "--no-block", f"--unit={unit}",
                "--property=Type=oneshot", "--property=KillMode=control-group",
                "--property=TimeoutStopSec=5s", shutil.which("sleep"), "60",
            ])
            self.assertEqual(created.returncode, 0, f"Could not start isolated service: {created.stderr}")
            running = self.await_state(unit, lambda state: int(state.get("MainPID") or "0") > 0)
            self.assertIn(running["ActiveState"], ("active", "activating"))
            self.assertNotEqual(self.helper_predicate(block, unit).returncode, 0,
                                "Production quiescence predicate accepted a live writer process")

            stopped = self.command(["sudo", "-n", "systemctl", "stop", unit])
            self.assertEqual(stopped.returncode, 0, f"Could not stop isolated service: {stopped.stderr}")
            state = self.await_state(unit, lambda row: (
                row.get("ActiveState") in ("inactive", "failed")
                and row.get("MainPID") == "0" and row.get("ControlPID") == "0"
            ))
            # systemd can preserve failed/signal after intentional termination
            # of a oneshot. It is evidence of how it exited, not a live writer.
            print("Native systemd stopped-oneshot evidence: " + ", ".join(
                f"{key}={state.get(key, '')}" for key in
                ("ActiveState", "SubState", "Result", "MainPID", "ControlPID")
            ))
            self.assertEqual(state["ActiveState"], "failed",
                             "Rehearsal must reproduce the retained failed state from the beta incident")
            self.assertEqual(state["Result"], "signal")
            quiescent = self.helper_predicate(block, unit)
            self.assertEqual(quiescent.returncode, 0,
                             f"Stopped oneshot was incorrectly rejected: {state}; {quiescent.stderr}")
        finally:
            # Exact test-created unit only. No files, timers, or other units
            # are removed or modified, even when an assertion fails.
            try:
                self.command(["sudo", "-n", "systemctl", "stop", unit])
            finally:
                self.command(["sudo", "-n", "systemctl", "reset-failed", unit])


if __name__ == "__main__":
    unittest.main()
