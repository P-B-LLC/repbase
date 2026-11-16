"""Seed, run a scenario, or compare runs. See loadtest/README.md.

    python -m loadtest.run seed
    python -m loadtest.run scenario --label before-mixed-warm
    python -m loadtest.run plans --label before
    python -m loadtest.run report results/a.json results/b.json

Everything a run depends on is created here, from a database that never changes:
each scenario clones the seeded template, so runs start from identical data.
"""

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import psycopg

from . import report as reporting
from .collector import Collector
from .driver import apply_load
from .safety import check_target

ROOT = Path(__file__).resolve().parent.parent
RESULTS = Path(__file__).resolve().parent / "results"
SEED_DATABASE = "rytivo_loadtest_seed"
RUN_DATABASE = "rytivo_loadtest_run"
REFUSED_REDIS = "redis://127.0.0.1:1/0"


def admin_url():
    return os.environ["LOADTEST_ADMIN_URL"]


def database_url(name):
    head, _, _ = admin_url().rpartition("/")
    return f"{head}/{name}"


def base_env(database, redis_url, **extra):
    env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "config.loadtest",
        "DATABASE_URL": database_url(database),
        "DATABASE_SSLMODE": os.environ.get("LOADTEST_DATABASE_SSLMODE", "disable"),
        "REDIS_URL": redis_url,
        "CACHE_KEY_PREFIX": "loadtest",
        "ANALYTICS_ENABLED": "true",
        "PYTHONDONTWRITEBYTECODE": "1",
        "GUNICORN_BIND_HOST": "127.0.0.1",
        "PORT": os.environ.get("LOADTEST_PORT", "58001"),
        "WEB_CONCURRENCY": os.environ.get("WEB_CONCURRENCY", "2"),
        "WEB_THREADS": os.environ.get("WEB_THREADS", "4"),
    }
    env.update(extra)
    check_target(env)
    return env


def manage(env, *args, stdout=None):
    subprocess.run([sys.executable, "manage.py", *args], cwd=ROOT, env=env, check=True, stdout=stdout)


def recreate(name, template=None):
    with psycopg.connect(admin_url(), autocommit=True) as db:
        db.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s", (name,))
        db.execute(f'DROP DATABASE IF EXISTS "{name}"')
        source = f' TEMPLATE "{template}"' if template else ""
        db.execute(f'CREATE DATABASE "{name}"{source}')


def wait_until_up(port, process, seconds=60):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SystemExit("gunicorn exited before it was ready; see the run's server log.")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health/live/", timeout=2) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.25)
    raise SystemExit("gunicorn did not become ready.")


def tokens_and_exercise(url):
    with psycopg.connect(url) as db:
        tokens = [row[0] for row in db.execute(
            'SELECT key FROM authtoken_token ORDER BY user_id'
        ).fetchall()]
        exercise = db.execute("SELECT min(id) FROM core_exercise WHERE created_by_id IS NULL").fetchone()[0]
    return tokens, exercise


class SilentServer:
    """Accepts connections and never answers: a Redis that has hung.

    On loopback, so it behaves the same on any machine and needs no exception
    to the rule about where the harness may point."""

    def __init__(self):
        self.listener = socket.create_server(("127.0.0.1", 0), backlog=256)
        self.port = self.listener.getsockname()[1]
        self.held = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                self.held.append(self.listener.accept()[0])
            except OSError:
                return

    def close(self):
        self.listener.close()
        for connection in self.held:
            connection.close()


class Periodic:
    """Runs a management command on a fixed interval, as the systemd timer would."""

    def __init__(self, env, seconds, *command):
        self.env, self.seconds, self.command = env, seconds, command
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.failures = 0

    def _loop(self):
        while not self.stop_event.wait(self.seconds):
            self.once()

    def once(self):
        finished = subprocess.run(
            [sys.executable, "manage.py", *self.command], cwd=ROOT, env=self.env, capture_output=True, text=True,
        )
        if finished.returncode:
            self.failures += 1
            print(finished.stderr[-2000:], file=sys.stderr)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=120)


def command_seed(args):
    env = base_env(SEED_DATABASE, os.environ["LOADTEST_REDIS_URL"])
    recreate(SEED_DATABASE)
    manage(env, "migrate", "--noinput", stdout=subprocess.DEVNULL)
    manage(env, "seed_load_data", *args.seed_args)


def prepare_database(env, args):
    recreate(RUN_DATABASE, SEED_DATABASE)
    manage(env, "migrate", "--noinput", stdout=subprocess.DEVNULL)
    with psycopg.connect(database_url(RUN_DATABASE), autocommit=True) as db:
        try:
            db.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
        except psycopg.Error as error:
            print(f"pg_stat_statements unavailable; statement figures will be null: {error}", file=sys.stderr)
        db.execute("UPDATE core_foodsearchcache SET fetched_at = now()")
    manage(env, "refresh_feed_stats", "--full", stdout=subprocess.DEVNULL)
    if args.stats == "late":
        with psycopg.connect(database_url(RUN_DATABASE), autocommit=True) as db:
            db.execute("UPDATE core_postengagementstats SET computed_at = now() - interval '2 hours'")
    elif args.stats == "missing":
        with psycopg.connect(database_url(RUN_DATABASE), autocommit=True) as db:
            db.execute("DELETE FROM core_postengagementstats")


def command_scenario(args):
    real_redis = os.environ["LOADTEST_REDIS_URL"]
    silent = SilentServer() if args.redis == "blackhole" else None
    app_redis = {
        "up": real_redis, "refused": REFUSED_REDIS,
        "blackhole": f"redis://127.0.0.1:{silent.port}/0" if silent else "",
    }[args.redis]
    extra = {"ANALYTICS_BUFFERED": "true"} if args.buffered else {}
    env = base_env(RUN_DATABASE, app_redis, **extra)
    run_url = database_url(RUN_DATABASE)
    print(f"[{args.label}] preparing database", flush=True)
    # Only the application under test loses Redis. Preparing the data is not
    # part of the scenario, and the statistics job needs it on older code.
    prepare_database(base_env(RUN_DATABASE, real_redis, **extra), args)

    import redis

    redis_client = redis.Redis.from_url(real_redis)
    redis_client.flushall()
    if args.cache == "cold" and os.environ.get("LOADTEST_PG_RESTART"):
        subprocess.run(os.environ["LOADTEST_PG_RESTART"], shell=True, check=True, stdout=subprocess.DEVNULL)

    RESULTS.mkdir(parents=True, exist_ok=True)
    port = int(env["PORT"])
    feed = 0 if args.mode == "core-only" else round(args.feed_users * args.scale)
    core = 0 if args.mode == "social-only" else round(args.core_users * args.scale)
    tokens, exercise = tokens_and_exercise(run_url)
    log_path = RESULTS / f"{args.label}.server.log"
    with open(log_path, "w") as log:
        server = subprocess.Popen(
            [sys.executable, "-m", "gunicorn", "-c", "gunicorn.conf.py", "config.wsgi:application"],
            cwd=ROOT, env=env, stdout=log, stderr=log, start_new_session=True,
        )
        flusher = job = None
        try:
            wait_until_up(port, server)
            if args.cache == "warm" and args.redis == "up":
                print(f"[{args.label}] warming", flush=True)
                apply_load("127.0.0.1", port, tokens, feed_users=round(feed * 0.3), core_users=round(core * 0.3),
                           seconds=args.warmup, ramp=5, seed=args.seed, exercise_id=exercise)
            if args.buffered:
                flusher = Periodic(env, 30, "flush_api_metrics")
                flusher.start()
            collector = Collector(run_url, None if args.redis != "up" else redis_client,
                                  local_database="127.0.0.1" in run_url or "localhost" in run_url)
            collector.reset_counters()
            collector.start()
            if args.stats == "fresh":
                # The five-minute job lands inside the run, as it would.
                job = threading.Timer(args.ramp + args.duration / 3, Periodic(
                    env, 0, "refresh_feed_stats").once)
                job.start()
            print(f"[{args.label}] applying load: {feed} feed + {core} core users for {args.duration}s", flush=True)
            samples, seconds = apply_load(
                "127.0.0.1", port, tokens, feed_users=feed, core_users=core, seconds=args.duration,
                ramp=args.ramp, seed=args.seed, exercise_id=exercise,
            )
            collector.stop()
            if flusher:
                flusher.stop()
                flusher.once()
            summary = reporting.summarize(samples, seconds)
            summary.update(collector.result(len(samples)))
            summary["scenario"] = {
                "label": args.label, "mode": args.mode, "cache": args.cache, "stats": args.stats, "redis": args.redis,
                "buffered": bool(args.buffered), "scale": args.scale, "feed_users": feed, "core_users": core,
                "duration_s": args.duration, "seed": args.seed,
                "web_workers": int(env["WEB_CONCURRENCY"]), "web_threads": int(env["WEB_THREADS"]),
                "git_commit": os.environ.get("LOADTEST_GIT_COMMIT") or subprocess.run(
                    ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                ).stdout.strip(),
            }
            output = RESULTS / f"{args.label}.json"
            output.write_text(json.dumps(summary, indent=1, sort_keys=True))
            print(reporting.markdown([(args.label, summary)]), flush=True)
            print(f"wrote {output}", flush=True)
        finally:
            if job:
                job.cancel()
            if flusher:
                flusher.stop()
            try:
                os.killpg(server.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            server.wait(timeout=45)
            if silent:
                silent.close()


def command_plans(args):
    real_redis = os.environ["LOADTEST_REDIS_URL"]
    env = base_env(RUN_DATABASE, real_redis)
    args.stats = "fresh"
    prepare_database(env, args)
    manage(env, "capture_explain_plans", "--output", str(RESULTS / f"plans-{args.label}"))


def command_report(args):
    runs = []
    for name in args.files:
        path = Path(name)
        runs.append((path.stem, json.loads(path.read_text())))
    print(reporting.markdown(runs))
    budgets = reporting.load_budgets(args.budgets)
    baseline = json.loads(Path(args.core_baseline).read_text()) if args.core_baseline else None
    for label, run in runs:
        if run["scenario"]["mode"] == "mixed":
            print(f"\nBudgets for {label}:")
            for row in reporting.evaluate(run, baseline, budgets):
                verdict = {True: "pass", False: "FAIL", None: "not measured"}[row["passed"]]
                print(f"  {row['name']:32s} {row['value']!s:>10} (limit {row['limit']})  {verdict}")


def main():
    parser = argparse.ArgumentParser(prog="loadtest.run")
    commands = parser.add_subparsers(dest="command", required=True)
    seed = commands.add_parser("seed")
    seed.add_argument("seed_args", nargs="*", help="passed to seed_load_data, after --")
    seed.set_defaults(run=command_seed)
    scenario = commands.add_parser("scenario")
    scenario.add_argument("--label", required=True)
    scenario.add_argument("--mode", choices=("mixed", "core-only", "social-only"), default="mixed")
    scenario.add_argument("--cache", choices=("warm", "cold"), default="warm")
    scenario.add_argument("--stats", choices=("fresh", "late", "missing"), default="fresh")
    scenario.add_argument("--redis", choices=("up", "refused", "blackhole"), default="up")
    scenario.add_argument("--buffered", action="store_true", help="turn on ANALYTICS_BUFFERED and run the flusher")
    scenario.add_argument("--feed-users", type=int, default=100)
    scenario.add_argument("--core-users", type=int, default=50)
    scenario.add_argument("--scale", type=float, default=1.0, help="2 tests the headroom")
    scenario.add_argument("--duration", type=int, default=150)
    scenario.add_argument("--ramp", type=int, default=20)
    scenario.add_argument("--warmup", type=int, default=45)
    scenario.add_argument("--seed", type=int, default=20261109)
    scenario.set_defaults(run=command_scenario)
    plans = commands.add_parser("plans")
    plans.add_argument("--label", required=True)
    plans.set_defaults(run=command_plans)
    compare = commands.add_parser("report")
    compare.add_argument("files", nargs="+")
    compare.add_argument("--budgets")
    compare.add_argument("--core-baseline", help="the core-only run the regression budget compares against")
    compare.set_defaults(run=command_report)
    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
