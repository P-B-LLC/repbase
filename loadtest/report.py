"""Turn run files into numbers that can be compared and judged.

`summarize` and `evaluate` are plain functions over plain data, so what counts
as passing can be tested without a database.
"""

import json
from pathlib import Path

from .driver import CORE_GROUPS, GROUPS

GROUP_FIELDS = ("count", "errors_5xx", "failures", "p50_ms", "p95_ms", "p99_ms", "sql_count_mean", "sql_ms_mean")


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower), 1)


def _group(samples):
    elapsed = [row[2] for row in samples]
    count = len(samples)
    return {
        "count": count,
        "errors_5xx": sum(1 for row in samples if row[1] >= 500),
        "failures": sum(1 for row in samples if row[1] == 0),
        "p50_ms": percentile(elapsed, 0.5),
        "p95_ms": percentile(elapsed, 0.95),
        "p99_ms": percentile(elapsed, 0.99),
        "sql_count_mean": round(sum(row[3] for row in samples) / count, 2) if count else None,
        "sql_ms_mean": round(sum(row[4] for row in samples) / count, 2) if count else None,
    }


def summarize(samples, seconds):
    """Latency and per-request SQL, by journey group and overall."""
    groups = {name: _group([row for row in samples if row[0] == name]) for name in GROUPS}
    overall = _group(samples)
    total = len(samples)
    bad = sum(1 for row in samples if row[1] >= 500 or row[1] == 0)
    overall.update({
        "requests_per_second": round(total / max(seconds, 1), 2),
        "error_rate_pct": round(bad / total * 100, 3) if total else None,
        "metric_sql_per_request": round(sum(row[5] for row in samples) / total, 3) if total else None,
    })
    return {"groups": groups, "overall": overall}


def evaluate(mixed, core_baseline, budgets):
    """[{name, limit, value, passed}] for one mixed run. `passed` is None when
    the figure could not be measured, which is not the same as passing."""
    rows = []

    def check(name, value, limit):
        rows.append({"name": name, "limit": limit, "value": value, "passed": None if value is None else value <= limit})

    check("first_page_p95_ms", mixed["groups"]["first_page"]["p95_ms"], budgets["first_page_p95_ms"])
    check("later_page_p95_ms", mixed["groups"]["later_page"]["p95_ms"], budgets["later_page_p95_ms"])
    for group in CORE_GROUPS:
        base = core_baseline["groups"][group]["p95_ms"] if core_baseline else None
        now = mixed["groups"][group]["p95_ms"]
        regression = round((now - base) / base * 100, 1) if base and now is not None else None
        check(f"{group}_p95_regression_pct", regression, budgets["core_p95_regression_pct"])
    check("error_rate_pct", mixed["overall"]["error_rate_pct"], budgets["error_rate_pct"])
    check("db_cpu_pct_of_host", mixed["database"]["cpu_pct_of_host"], budgets["db_cpu_pct"])
    reserve = mixed["database"]["connection_reserve_pct"]
    rows.append({
        "name": "connection_reserve_pct", "limit": budgets["connection_reserve_pct"], "value": reserve,
        "passed": None if reserve is None else reserve >= budgets["connection_reserve_pct"],
    })
    return rows


def load_budgets(path=None):
    return json.loads(Path(path or Path(__file__).with_name("budgets.json")).read_text())


def metric_paths(value, prefix=""):
    """Every leaf path in a run file, so two runs can be shown to report the same set."""
    if isinstance(value, dict):
        found = set()
        for key, child in value.items():
            found |= metric_paths(child, f"{prefix}.{key}" if prefix else key)
        return found
    return {prefix}


def markdown(runs, budgets=None, core_baseline=None):
    """A table per concern for [(label, run)]."""
    lines = ["| Run | Requests/s | 5xx+fail % | First page p95 | Later page p95 | Workout p95 | Planner p95 | Food p95 |",
             "|---|---|---|---|---|---|---|---|"]
    for label, run in runs:
        groups, overall = run["groups"], run["overall"]
        lines.append(
            f"| {label} | {overall['requests_per_second']} | {overall['error_rate_pct']} | "
            f"{groups['first_page']['p95_ms']} | {groups['later_page']['p95_ms']} | {groups['workout']['p95_ms']} | "
            f"{groups['planner']['p95_ms']} | {groups['food']['p95_ms']} |"
        )
    lines += ["", "| Run | SQL statements/request | Metric SQL/request | Rows examined/request | DB CPU cores | "
                  "Peak connections | Lock waiters peak | Cache hit % | Stats age s | Flush lag s |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for label, run in runs:
        database, cache, workers, overall = run["database"], run["cache"], run["workers"], run["overall"]
        lines.append(
            f"| {label} | {overall['sql_count_mean']} | {overall['metric_sql_per_request']} | "
            f"{database['rows_examined_per_request']} | {database['cpu_cores_mean']} | "
            f"{database['connections_peak']}/{database['max_connections']} | {database['lock_waiters_peak']} | "
            f"{cache['hit_rate_pct']} | {workers['feed_stats_age_s']} | {workers['metric_flush_lag_s']} |"
        )
    return "\n".join(lines)
