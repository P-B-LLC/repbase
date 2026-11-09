# Load-test harness

A repeatable measurement of what a request costs, under the mix the plan cares
about: one group scrolling Social while others save sets, complete planner tasks,
search foods and log meals.

Nothing here ships. `config.loadtest` is the only settings module that installs
the `loadtest` app, and it refuses any database without "loadtest" in its name and
any host that is not loopback or named in `LOADTEST_ALLOW_STAGING_HOSTS`.

## Running it

Needs a disposable PostgreSQL with `shared_preload_libraries=pg_stat_statements`
and a disposable Redis. **Both are wiped.**

```sh
export LOADTEST_ADMIN_URL=postgresql://postgres:x@127.0.0.1:55433/postgres
export LOADTEST_REDIS_URL=redis://127.0.0.1:56379/0
# Optional: restarting PostgreSQL is what empties its buffers for a cold run.
export LOADTEST_PG_RESTART='pg_ctl -D /path/to/data -w restart -m fast'

python -m loadtest.run seed                                        # once; about five minutes
python -m loadtest.run scenario --label core --mode core-only      # the regression baseline
python -m loadtest.run scenario --label mixed                      # expected beta load
python -m loadtest.run scenario --label mixed-cold --cache cold
python -m loadtest.run scenario --label mixed-2x --scale 2         # headroom
python -m loadtest.run plans --label today                         # EXPLAIN plans
python -m loadtest.run report results/core.json results/mixed.json --core-baseline results/core.json
```

Every scenario clones the seeded template, so runs start from identical data, and
virtual users use seeded random streams. `--stats late|missing` ages or removes the
feed statistics; `--redis refused|blackhole` makes Redis unreachable; `--buffered`
turns on `ANALYTICS_BUFFERED` and runs the flusher every 30 seconds.

For a staging machine, set `LOADTEST_ALLOW_STAGING_HOSTS` to the exact database and
Redis hostnames. Figures that cannot be read there come back as `null` rather than
disappearing, so every run reports the same set of metrics.

## What is measured

| Metric | Source |
|---|---|
| API latency by journey | Measured by the virtual users |
| SQL statements and SQL time per request | `loadtest.middleware`, on each response |
| Analytics SQL per request | The same, for statements touching the metric tables |
| Rows examined per request | `pg_stat_user_tables` deltas over requests. Includes background jobs |
| Rows returned, blocks | `pg_stat_statements` |
| Lock waits | `pg_stat_activity` sampled every second |
| Database CPU | `ps` CPU time of PostgreSQL processes; local databases only |
| Connections | `pg_stat_activity` peak against `max_connections` |
| Cache hit rate | Redis `INFO stats` deltas |
| Worker queue age | Age of feed statistics, the oldest queued push, video and screening job, and the last metric flush |

PostgreSQL does not report rows examined per statement, so that figure is an
average across a run. There is no job queue either: "queue age" is the age of the
oldest piece of work each scheduled job has yet to do.

## Reading the results

`budgets.json` holds the starting targets, as configuration. A local machine proves
before-and-after differences, statement counts and failure behaviour. It does not
prove absolute CPU, latency or connection budgets, which need hardware matching the
hosting plan.

Reviewed summaries are kept in `loadtest/baselines/`; raw output goes to
`loadtest/results/`, which git ignores.

Video startup is not measured here. It can only be observed on the device, and comes
with the client telemetry milestones.
