"""What the database, cache and workers were doing during a run.

Sampled from outside the application, so measuring cannot change what is
measured. Each figure is `None` when it cannot be read -- no Redis, a managed
database whose processes are not visible -- rather than absent, so every run
produces the same set of metrics and two runs can always be compared.
"""

import os
import subprocess
import threading
import time

import psycopg

QUEUE_AGES = {
    "feed_stats_age_s": "SELECT EXTRACT(EPOCH FROM now() - max(computed_at)) FROM core_postengagementstats",
    "push_queue_age_s": "SELECT EXTRACT(EPOCH FROM now() - min(created_at)) FROM core_pushdelivery WHERE status = 'queued'",
    "video_queue_age_s": (
        "SELECT EXTRACT(EPOCH FROM now() - min(created_at)) FROM core_postvideo "
        "WHERE processing_state NOT IN ('ready', 'failed')"
    ),
    "screening_queue_age_s": (
        "SELECT EXTRACT(EPOCH FROM now() - min(created_at)) FROM core_mediascreeningjob WHERE state = 'queued'"
    ),
    "metric_flush_lag_s": "SELECT EXTRACT(EPOCH FROM now() - max(applied_at)) FROM core_metricflushreceipt",
}


def _seconds(text):
    """CPU time as `ps` prints it: [[dd-]hh:]mm:ss[.ss]."""
    days = 0
    if "-" in text:
        day_text, text = text.split("-", 1)
        days = int(day_text)
    total = 0.0
    for part in text.split(":"):
        total = total * 60 + float(part)
    return days * 86400 + total


def postgres_cpu_seconds():
    """{pid: CPU seconds} for local PostgreSQL processes, or None."""
    try:
        output = subprocess.run(["ps", "-axo", "pid=,comm=,time="], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    found = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1].rsplit("/", 1)[-1].rstrip(":") == "postgres":
            found[int(parts[0])] = _seconds(parts[-1])
    return found or None


class Collector:
    def __init__(self, dsn, redis_client=None, local_database=True, interval=1.0):
        self.dsn = dsn
        self.redis = redis_client
        self.local = local_database
        self.interval = interval
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.connections, self.lock_waiters, self.active = [], [], []
        self.queue_samples = {name: [] for name in QUEUE_AGES}
        self.cpu_totals = {}
        self.cpu_started = self.cpu_stopped = None
        self.statements_available = False

    def reset_counters(self):
        with psycopg.connect(self.dsn, autocommit=True) as db:
            db.execute("SELECT pg_stat_reset()")
            try:
                db.execute("SELECT pg_stat_statements_reset()")
                self.statements_available = True
            except psycopg.Error:
                self.statements_available = False
        if self.redis is not None:
            try:
                self.redis.config_resetstat()
            except Exception:  # noqa: BLE001 - a provider may refuse CONFIG; the stat is then None
                self.redis = None

    def start(self):
        self.cpu_before = postgres_cpu_seconds() if self.local else None
        self.cpu_started = time.monotonic()
        self.thread.start()

    def _queues(self, db):
        for name, sql in QUEUE_AGES.items():
            try:
                value = db.execute(sql).fetchone()[0]
                self.queue_samples[name].append(None if value is None else float(value))
            except psycopg.Error:
                self.queue_samples[name].append(None)

    def _loop(self):
        with psycopg.connect(self.dsn, autocommit=True) as db:
            tick = 0
            while not self.stop_event.is_set():
                row = db.execute(
                    "SELECT count(*), count(*) FILTER (WHERE wait_event_type = 'Lock'), "
                    "count(*) FILTER (WHERE state = 'active') FROM pg_stat_activity "
                    "WHERE datname = current_database() AND pid <> pg_backend_pid()"
                ).fetchone()
                self.connections.append(row[0])
                self.lock_waiters.append(row[1])
                self.active.append(row[2])
                if tick % 10 == 0:
                    self._queues(db)
                tick += 1
                self.stop_event.wait(self.interval)

    def stop(self):
        self.cpu_after = postgres_cpu_seconds() if self.local else None
        self.cpu_stopped = time.monotonic()
        self.stop_event.set()
        self.thread.join(timeout=10)

    def _cpu(self):
        if not self.cpu_before or not self.cpu_after:
            return None, None
        used = sum(
            max(0.0, seconds - self.cpu_before.get(pid, 0.0)) for pid, seconds in self.cpu_after.items()
        )
        cores = used / max(self.cpu_stopped - self.cpu_started, 0.001)
        return round(cores, 3), round(cores / (os.cpu_count() or 1) * 100, 2)

    def _cache(self):
        if self.redis is None:
            return {"hit_rate_pct": None, "hits": None, "misses": None, "commands": None}
        try:
            info = self.redis.info("stats")
        except Exception:  # noqa: BLE001
            return {"hit_rate_pct": None, "hits": None, "misses": None, "commands": None}
        hits, misses = info.get("keyspace_hits", 0), info.get("keyspace_misses", 0)
        return {
            "hit_rate_pct": round(hits / (hits + misses) * 100, 2) if hits + misses else None,
            "hits": hits, "misses": misses, "commands": info.get("total_commands_processed"),
        }

    def result(self, requests):
        """The finished figures. `requests` divides the per-request ones."""
        with psycopg.connect(self.dsn, autocommit=True) as db:
            time.sleep(1.5)  # backends publish their table counters about once a second
            statements = (None, None, None, None)
            top = []
            if self.statements_available:
                statements = db.execute(
                    "SELECT coalesce(sum(calls), 0), coalesce(sum(total_exec_time), 0), coalesce(sum(rows), 0), "
                    "coalesce(sum(shared_blks_hit + shared_blks_read), 0) FROM pg_stat_statements s "
                    "JOIN pg_database d ON d.oid = s.dbid WHERE d.datname = current_database()"
                ).fetchone()
                top = db.execute(
                    "SELECT left(regexp_replace(query, '\\s+', ' ', 'g'), 160), calls, "
                    "round(total_exec_time::numeric, 1), round(mean_exec_time::numeric, 2), rows "
                    "FROM pg_stat_statements s JOIN pg_database d ON d.oid = s.dbid "
                    "WHERE d.datname = current_database() ORDER BY total_exec_time DESC LIMIT 12"
                ).fetchall()
            examined = db.execute(
                "SELECT coalesce(sum(coalesce(seq_tup_read, 0) + coalesce(idx_tup_fetch, 0)), 0) FROM pg_stat_user_tables"
            ).fetchone()[0]
            database = db.execute(
                "SELECT deadlocks FROM pg_stat_database WHERE datname = current_database()"
            ).fetchone()
            maximum = int(db.execute("SHOW max_connections").fetchone()[0])
            self._queues(db)
        cores, percent = self._cpu()
        peak = max(self.connections, default=0)
        per_request = max(requests, 1)
        queues = {}
        for name, values in self.queue_samples.items():
            known = [value for value in values if value is not None]
            queues[name] = round(max(known), 1) if known else None
        return {
            "database": {
                "cpu_cores_mean": cores,
                "cpu_pct_of_host": percent,
                "connections_peak": peak,
                "max_connections": maximum,
                "connection_reserve_pct": round((1 - peak / maximum) * 100, 1),
                "active_queries_mean": round(sum(self.active) / max(len(self.active), 1), 2),
                "lock_waiters_mean": round(sum(self.lock_waiters) / max(len(self.lock_waiters), 1), 3),
                "lock_waiters_peak": max(self.lock_waiters, default=0),
                "deadlocks": database[0] if database else None,
                "statement_calls": None if statements[0] is None else int(statements[0]),
                "statement_time_ms": None if statements[1] is None else round(float(statements[1]), 1),
                "statement_calls_per_request": None if statements[0] is None else round(int(statements[0]) / per_request, 2),
                "rows_examined_per_request": round(int(examined) / per_request, 1),
                "rows_returned_per_request": None if statements[2] is None else round(int(statements[2]) / per_request, 1),
                "blocks_per_request": None if statements[3] is None else round(int(statements[3]) / per_request, 1),
            },
            "cache": self._cache(),
            "workers": queues,
            "top_statements": [
                {"query": row[0], "calls": row[1], "total_ms": float(row[2]), "mean_ms": float(row[3]), "rows": row[4]}
                for row in top
            ],
        }
