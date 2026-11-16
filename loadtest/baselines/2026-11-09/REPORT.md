# Milestone 1 baseline: before and after

Measured 2026-11-09 on a 12-core, 32 GB Mac with local PostgreSQL 16 and Redis, and
gunicorn at the production default of 2 workers × 4 threads. The expected beta load
is an assumption agreed for this milestone: 100 people scrolling For You and 50 using
workouts, the planner and food. Headroom is twice that.

**A laptop proves differences, statement counts and failure behaviour. It does not
prove absolute CPU, latency or connection budgets**, which need hardware matching the
hosting plan. The first exit criterion is therefore not yet met.

Seed: 20,000 accounts, 300,000 follows, 60,000 posts, about 3 million views, 600,000
likes, with one creator followed by 15,000 accounts and one post seen by 18,000.
"Before" is commit `11a3544`, the harness on unchanged code. "After" is the final
code with `ANALYTICS_BUFFERED` on and the flusher running every 30 seconds. Every run
is in `runs/`, the full tables are in `all-runs.md`, and the plans are in
`plans-before/` and `plans-after/`.

## Expected load, warm caches

Ranges are two identical runs.

| | Before | After |
|---|---|---|
| Analytics SQL statements per request | 5.0 | **0** |
| All SQL statements per request | 12.9 | **7.9** |
| First page p95 | 265–271 ms | 238–256 ms |
| Later page p95 | 128–129 ms | 117–120 ms |
| Workout p50 / p95 | 9.9 / 34–54 ms | 7.9 / 47–73 ms |
| Planner p50 / p95 | 12 / 28–30 ms | 10 / 23 ms |
| Food p50 / p95 | 9 / 30 ms | 7 / 30–33 ms |
| Database CPU (cores) | 0.395 | 0.37–0.38 |
| Lock waiters, peak | 0–1 | 0 |
| 5xx | 0 | 0 |
| Cache hit rate | 96.8% | 97.0% |

Cold caches give first page 258 → 243 ms and later pages 137 → 118 ms. At twice the
load, first page is 292 → 283 ms, later pages 165 → 158 ms, database CPU 0.657 → 0.652
cores and 5xx is 0 both ways.

Database CPU barely moved. At this scale feed queries dominate it, and the removed
statements were cheap on their own. What went away is the shared-row locking in the
request path, and every core request is about 2 ms faster at the median.

## Budgets (`budgets.json`) at the expected load and at 2×

| Budget | Result |
|---|---|
| First page p95 ≤ 500 ms | Pass: 238–256 ms, 283 ms at 2× |
| Later pages p95 ≤ 250 ms | Pass: 117–120 ms, 158 ms at 2× |
| 5xx below 0.5% | Pass: 0 at both |
| Database CPU ≤ 60% | Pass locally only: 3% of this machine; not meaningful for a hosted plan |
| Connections, 20% in reserve | Pass locally only: peak 8 of 50 |
| Core p95 ≤ 10% over baseline | **Inconclusive** |

The core budget cannot be judged at this sample size. Workout p95 ranged from 26 to
154 ms across comparable runs at the expected load, before and after, on about 500
samples, while its median held at 8–10 ms. At 1× the run fails the budget for workouts,
and at 2× it passes by 25%. Paired against "before", workout p95 is lower in five of
six comparisons, so there is no sign of a regression, but this is not a pass.

## Redis unreachable

| | Before | After, Redis hung | After, connection refused | After, healthy control |
|---|---|---|---|---|
| Workout saves p50 / p95 | no response (30 s timeout) | 10 / 125 ms | 11 / 116 ms | 9 / 658 ms |
| Planner p50 / p95 | no response | 14 / 133 ms | 14 / 77 ms | 11 / 257 ms |
| Food p50 / p95 | no response | 9 / 92 ms | 9 / 71 ms | 8 / 336 ms |
| Failed saves | every request | 0 | 0 | 0 |
| For You | no response | 503 in 3 ms | 503 in 3 ms | served |
| Following feed | no response | served | served | served |

Before, the whole server stopped: each thread waited three seconds per cache call, and
requests timed out at 30 seconds. The control is a cold-start run with healthy Redis.
A hung Redis and a refused connection cost the same, which is the circuit breaker
working. Medians are 2–3 ms above the control and tails are lower, because For You is
being shed. The statistics job crashed during an outage before and completes now.

## Statistics job late, caches cold

First page p95 was 274 → 260 ms, and the bounded recount never appeared among the
twelve costliest statements. **The old recount did not visibly hurt at this load
either**, because each result is shared for ten minutes. This change bounds the worst
case rather than fixing an observed slowdown. It is covered by
`test_only_one_request_counts_at_a_time` and the capped-count tests. The cache hit rate
in this run is 51%, because each ranking looks for per-post counts that do not exist
while stale rows stand in.

## Metrics accuracy and repeatability

- In a run with 1,970 API requests in the access log, the flushed daily rows, five-minute
  rows and receipts each summed to exactly 1,970.
- Flush lag stayed at about 24 seconds.
- All 18 saved runs report the identical set of 113 metrics, warm or cold.
- Identical runs repeat closely: first page p95 265 and 271 ms before, later pages 128 and 129 ms.

## Corrections made during measurement

- CPU was undercounted in the two earliest "before" runs (`before-core-only` and
  `before-mixed-warm`), so the CPU figure above comes from `before-mixed-warm-repeat`.
- The first 2× runs showed 1.5% failed requests. All of them fell in the last second of
  the run, when every virtual user woke at the deadline and overflowed the operating
  system's accept queue. It was a defect in the load driver, now fixed, and both 2×
  runs were repeated.
