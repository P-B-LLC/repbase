# Releasing the new Social in stages

How the release is controlled, what has to be true before each widening, and
where the evidence for each row of the plan's test table is — including the
rows that have none yet. Passing a test here is evidence about the code. It
is not evidence that a deployment is configured to match; the gates below
read the running system for that reason.

Nothing in this file authorises a deployment or a release.

## The switches

Seven, each moved by itself (`core/rollout.py`):

| Flag | Off means | Enforced by |
| --- | --- | --- |
| `new_feed` | The app shows the feed it always had | The app, told by `GET social/features/` |
| `autoplay` | Clips in the new feed wait for a tap | The app |
| `measurement_v2` | The app sends plain view events (what was on screen, how long, how far a clip got); summaries that still arrive are set aside | App and server |
| `measured_ranking` | For You uses the weights as they stand (the control) | Server |
| `sounds` | No new sounds, no new posts set to one. Existing credits stay | Server |
| `takes` | No new takes. Existing takes stay | Server |
| `ranked_for_you` | For You is the newest public posts in order: no ranking, cache or statistics | Server |

Stages: `off`, `staff`, `cohort` (staff plus invited accounts), `everyone`.
With no stage set a flag is at `SOCIAL_ROLLOUT_DEFAULT`: `off` in production,
`everyone` where `DEBUG` is on. Two exceptions: `ranked_for_you` is on unless
turned off, and `measured_ranking` is off everywhere until released.

```sh
python manage.py social_rollout show
python manage.py social_rollout invite USERNAME --note "beta"
python manage.py social_rollout gates
python manage.py social_rollout set takes cohort --backend-revision REV --client-build "1.2 (580)" --by NAME
python manage.py social_rollout set takes off          # always allowed
```

A change reaches each worker on its next request. If Redis is unreachable
when the change is made, workers catch up within ten minutes.

## What pauses expansion

`set` to a wider stage runs these first and refuses if any fails. `--force`
widens anyway and the record says it was forced.

| Gate | Fails when |
| --- | --- |
| `no_open_api_alerts` | Any route has an open failing or slow alert (`check_api_health`) |
| `api_health_is_recorded` | `ANALYTICS_ENABLED` is off, so no alert could have opened |
| `first_page_latency` | Ranking p95 over 1000 ms in the last day, or no rankings recorded |
| `next_page_latency` | Later-page p95 over 500 ms |
| `feed_statistics_fresh` | `refresh_feed_stats` has not written within `FEED_STATS_STALE_SECONDS` |
| `queues_moving` | Video, screening, sound or deletion work has waited over 30 minutes |
| `retention_running` | Rows are past their retention (the privacy gate) |
| `daily_maintenance_running` | No insight snapshot in two days |

Database saturation is not one of them: CPU and connection counts are the
database host's to report. Check them by hand before widening.

Each change is written to `SocialRolloutEvent` with the backend revision,
client build and every gate's result at that moment. That table is the
record of tested revisions the plan asks for.

## Order

1. Deploy with every flag at its default (off). Confirm `social_rollout show`.
2. Install the timers if they are not already: feed stats, metrics flush,
   sounds, and the daily `config.deploy maintenance`. Set `ANALYTICS_ENABLED`.
3. `measurement_v2` to `staff`, then `new_feed`, then `autoplay`. Use it for a
   few days. Read `/insights/social/`.
4. The same three to `cohort` after inviting accounts.
5. `takes`, then `sounds`, to `staff` and then `cohort`.
6. `measured_ranking` only as a deliberate experiment, and only once
   `/insights/social/` shows measured exposures arriving.
7. `everyone`, one flag at a time, with the gates passing.

If the ranking misbehaves: `social_rollout set ranked_for_you off`.

## Before a public rollout

The plan requires these to be working in production, and none can be shown
from a test run:

- Reporting and blocking — built and tested (`test_reporting`, `tests.py`).
- Moderation operations — `MODERATION_OPERATIONS.md`; needs a staffed queue.
- Audio-rights controls — a sound's owner or a moderator can remove it and
  every use loses it (`test_sounds_and_takes.RemovingSoundTests`). There is no
  in-app screen for it yet, and no process for a rights holder who is not a
  member to ask.

## Evidence for each test area

| Area | Evidence | Not covered |
| --- | --- | --- |
| Measurement correctness | Server: `test_attention` (bounds, coverage, rewatch, failures). App: `AttentionMeterTests` (seeks, rate changes, loops, buffering, backgrounding, covered cards) with a fake clock | Against real clips of known length on a device. The meter has never observed a real `AVPlayer` |
| Retry and concurrency | `test_attention` (duplicate and reordered batches, concurrent batches on PostgreSQL), `test_post_likes`, `test_release.ConcurrentActionTests` (simultaneous likes and "I tried this", PostgreSQL), `test_save_concurrency` | — |
| Visibility and source credit | `test_feed_cache_rules.VisibilityBeatsTheCacheTests`, `test_publication_contract`, `test_sounds_and_takes.ConsistencyTests` and `RemovingSoundTests`, `test_release.RankingSwitchTests` for the plain page | — |
| Ranking behaviour | `test_recommendations` (new accounts, eligibility, paging, simulated community), `test_measured_ranking` (formats, clip lengths, tags, families), `test_recommendation_signals` | Whether results are *sensible* to people. That is a judgement for the beta |
| Abuse resistance | `test_feed_integrity` (like rings, bursts, new-account trust, bait), `test_measured_ranking.TagAndSoundScoreTests` (tag stuffing), `test_reporting`, throttles in `test_security` | Coordinated reporting is rate-limited per account and reaches a human queue; there is no automatic detection of a ring of reporters |
| Database scaling | `test_recommendations.ScaleTests` and query-count assertions in `test_publication_contract` and `test_release`; `test_identity_indexes` (PostgreSQL plans); `loadtest/` harness with budgets | The harness has not been run against production-sized hardware. Warm and cold results from Milestone 1 are from a laptop |
| Failure recovery | `test_resilient_cache` (Redis outage: saves and sign-in carry on), `test_feed_stats` (stale and missing statistics), `test_metric_buffer` (lost and repeated flushes), `test_attention` (oversize and burst bounds), `test_release` (ranking off needs no cache) | Stopped workers are detected by the gates, not simulated. Slow storage and connection pressure are untested |
| Native experience | The app builds for the simulator (Debug and Release). `Scripts/test-account-safety.sh` | Everything else in the row: signed device testing, light and dark, large text, VoiceOver, low bandwidth, memory pressure. None has been done |
| Regression | The whole backend suite (planner, workouts, meals, reminders) passes on SQLite and PostgreSQL with the Social changes in | "Under Social load" needs the mixed load test run on real hardware. Widgets and Home are not exercised by any automated test here |
| Contract compatibility | `core/test_fixtures/contract/posts_before_m2.json` (old payloads still valid), `ForYouContractTests`, `test_release.MeasurementSwitchTests` (old events with summaries off), CI's `openapi.yaml` diff; Swift client regenerated and built | The web client's contract copy is behind the backend |

## Recorded revisions

Fill in from `social_rollout show` at each widening; the table of record is
`SocialRolloutEvent`.

| Date | Flag | Stage | Backend | Client build | Gates |
| --- | --- | --- | --- | --- | --- |
| | | | | | |

Required configuration: `ANALYTICS_ENABLED=true`, `ANALYTICS_BUFFERED=true`
with its timer, Redis, the feed-stats, sounds and daily maintenance timers,
`SOCIAL_ROLLOUT_DEFAULT` unset (off).
