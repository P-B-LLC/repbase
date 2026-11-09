"""The parts of the load-test harness that must be right without a database."""

from django.test import SimpleTestCase

from loadtest import report
from loadtest.safety import UnsafeTarget, check_target

LOCAL_DB = "postgresql://postgres:x@127.0.0.1:5432/rytivo_loadtest_run"
LOCAL_REDIS = "redis://127.0.0.1:6379/0"


class TargetSafetyTests(SimpleTestCase):
    def test_a_loopback_load_test_database_is_allowed(self):
        check_target({"DATABASE_URL": LOCAL_DB, "REDIS_URL": LOCAL_REDIS})

    def test_a_database_not_named_for_load_testing_is_refused(self):
        with self.assertRaises(UnsafeTarget):
            check_target({"DATABASE_URL": "postgresql://u:p@127.0.0.1/rytivo", "REDIS_URL": LOCAL_REDIS})

    def test_a_remote_host_is_refused_even_with_the_right_name(self):
        for env in (
            {"DATABASE_URL": "postgresql://u:p@db.example.com/rytivo_loadtest_run", "REDIS_URL": LOCAL_REDIS},
            {"DATABASE_URL": LOCAL_DB, "REDIS_URL": "rediss://:p@cache.example.com:6379/0"},
        ):
            with self.subTest(env=env), self.assertRaises(UnsafeTarget):
                check_target(env)

    def test_a_staging_host_must_be_named_exactly(self):
        env = {"DATABASE_URL": "postgresql://u:p@db.example.com/rytivo_loadtest_run", "REDIS_URL": LOCAL_REDIS}
        check_target({**env, "LOADTEST_ALLOW_STAGING_HOSTS": "db.example.com"})
        with self.assertRaises(UnsafeTarget):
            check_target({**env, "LOADTEST_ALLOW_STAGING_HOSTS": "other.example.com"})

    def test_nothing_configured_is_refused(self):
        with self.assertRaises(UnsafeTarget):
            check_target({})


def sample(group, status=200, ms=100.0, sql=5, sql_ms=2.0, metric=0):
    return (group, status, ms, sql, sql_ms, metric)


class ReportingTests(SimpleTestCase):
    def mixed(self, first=400, later=200, core=110, error=0.1, cpu=50, reserve=30):
        groups = {name: {"p95_ms": core} for name in report.CORE_GROUPS}
        groups["first_page"] = {"p95_ms": first}
        groups["later_page"] = {"p95_ms": later}
        return {
            "groups": groups, "overall": {"error_rate_pct": error},
            "database": {"cpu_pct_of_host": cpu, "connection_reserve_pct": reserve},
        }

    def baseline(self):
        return {"groups": {name: {"p95_ms": 100} for name in report.CORE_GROUPS}}

    def verdicts(self, run, baseline=None):
        rows = report.evaluate(run, baseline if baseline is not None else self.baseline(), report.load_budgets())
        return {row["name"]: row["passed"] for row in rows}

    def test_within_every_budget_passes(self):
        self.assertTrue(all(self.verdicts(self.mixed(core=109)).values()))

    def test_each_budget_can_fail(self):
        for name, run in {
            "first_page_p95_ms": self.mixed(first=501),
            "later_page_p95_ms": self.mixed(later=251),
            "workout_p95_regression_pct": self.mixed(core=111),
            "error_rate_pct": self.mixed(error=0.6),
            "db_cpu_pct_of_host": self.mixed(cpu=61),
            "connection_reserve_pct": self.mixed(reserve=19),
        }.items():
            with self.subTest(name=name):
                self.assertFalse(self.verdicts(run)[name])

    def test_what_could_not_be_measured_does_not_pass(self):
        self.assertIsNone(self.verdicts(self.mixed(cpu=None))["db_cpu_pct_of_host"])
        verdicts = report.evaluate(self.mixed(), None, report.load_budgets())
        self.assertIsNone({row["name"]: row["passed"] for row in verdicts}["planner_p95_regression_pct"])

    def test_every_run_reports_the_same_metrics_with_or_without_traffic(self):
        busy = report.summarize([sample("first_page"), sample("workout", status=500)], 10)
        idle = report.summarize([], 10)
        self.assertEqual(report.metric_paths(busy), report.metric_paths(idle))

    def test_a_server_error_and_a_lost_response_both_count_against_the_rate(self):
        result = report.summarize([sample("food"), sample("food", status=500), sample("food", status=0), sample("food")], 4)
        self.assertEqual(result["overall"]["error_rate_pct"], 50.0)
        self.assertEqual(result["overall"]["errors_5xx"], 1)
        self.assertEqual(result["overall"]["failures"], 1)

    def test_metric_statements_are_averaged_per_request(self):
        result = report.summarize([sample("food", metric=5), sample("food", metric=0)], 2)
        self.assertEqual(result["overall"]["metric_sql_per_request"], 2.5)
