# Social Milestone 6: Original Sounds and Takes

**Status:** Done on `feature/social-m6-sounds-takes` (stacked on Milestone 5), awaiting review; not deployed
**Date:** 2026-11-09
**Area:** Backend / sounds, take totals, prompts

## Why

Milestone 2 gave posts a sound reference and take lineage. Nothing could yet create a
sound, take one down, count takes, or show that all of it stays consistent.

## What Was Changed

- **Sounds** (`core/sounds.py`, `core/sound_views.py`, migration 0099): an author
  publishes the audio of their own approved clip, on purpose, with a reuse permission.
  A worker extracts the audio once (`prepare_sounds`); posts reference the row.
- **Removal**: by the owner or a moderator, as a status. The credit, the audio file,
  new uses and For You eligibility all follow from the one row, and come back on
  restore. Only a moderator restores what a moderator removed.
- **`take_count`** on every post: takes anybody could see, the same for every reader.
- **Prompts**: a short fixed list to post under and browse by.
- `deploy/repbase-sounds.service` and `.timer`, with their own CPU and memory limits.

## Verification

31 tests in `core/test_sounds_and_takes.py`, including a table of the exit criterion:
source deletion, blocks, revoked reuse, sound removal, private accounts and saved
copies, each read from the feed, the post, the post list, the takes list and the
sound's posts. Audio extraction is tested with real ffmpeg on a fixture clip.

## Needs To Be Changed / Improved

- Extraction reads the clip from media storage. Clips processed by the object-storage
  video pipeline (`variants`, no `file`) are not handled yet and stay unprepared.
- The service file's `ReadWritePaths` names a placeholder media path.
- Shared-calendar invitations were not built here; the plan puts them after the feed
  is stable, and calendar sharing already exists on the `work/` branches.
- `take_count` is a subquery per card. It belongs in the cached engagement counts
  once takes are common.

---

# Social Milestone 5: Ranking Reads Measured Attention

**Status:** Done on `feature/social-m5-ranking` (stacked on Milestone 3), awaiting review; not deployed
**Date:** 2026-11-09
**Area:** Backend / For You ranking

## Why

The ranking already had the five stages the plan asks for. What it lacked was the new
evidence: measured watching and reading, hashtags, sounds, formats and take families.

## What Was Changed

- Measured viewing signals, tag and sound relevance, format baselines and a shared
  candidate cap, each behind a weight that is 0 by default. `MEASURED_VARIANT` turns
  them on together as one experiment arm. See `RECOMMENDATIONS.md`.
- A take counts as a strong signal; a page is not filled by one take family or one
  sound; a post on a removed sound is not eligible.
- No schema change.

## Verification

31 tests in `core/test_measured_ranking.py`. With every new weight at 0 the existing
ranking tests, including the simulated community, pass unchanged.

## Needs To Be Changed / Improved

- Nothing here has been run as an experiment. The weights are the plan's starting
  values and have not been evaluated on real readers.
- Shared quality figures (`watch`, completions) still come from the legacy columns;
  moving them needs new columns on `PostEngagementStats`.
- The learned model's features are unchanged, so it does not see the new parts.

---

# Social Milestone 3: Measured Attention, Safe to Send Twice

**Status:** Done on `feature/social-m3-attention` (stacked on Milestone 2), awaiting review; not deployed
**Date:** 2026-11-09
**Area:** Backend / impression ingestion; iOS / on-device measurement

## Why

The player reported the furthest position it reached, so seeking forward read as
watching. Batches were also dropped on any failure and could not be retried safely.

## What Was Changed

- **Schema version 2 on `POST /social/impressions/`** (`core/attention.py`): one
  summary per exposure, with active dwell, playback time, unique coverage, rewatch,
  attachment reading and playback failure. Every figure is bounded by something the
  server knows; a figure over its bound is lowered, not refused.
- **`ImpressionBatchReceipt`** (migration 0098), written in the transaction that adds
  the batch. A retried batch adds nothing. Receipts are per account and kept 7 days.
- **Ordering**: totals are sums and coverage a maximum; `skipped` belongs to the
  exposure observed last, by device time then session and sequence.
- **Legacy kept apart**: version 1 bodies write only the old columns, as before.
  Version 2 also feeds those columns their honest equivalents, so the ranking in use
  keeps working.
- A 64 kB request limit, and timestamps bounded to 48 hours back and 5 minutes ahead.

## Verification

37 tests in `core/test_attention.py` cover duplicate, reordered, interrupted and
malicious batches, including four simultaneous deliveries of one batch on PostgreSQL.
A batch of 38 posts runs the same number of statements as a batch of one.
1,757 backend tests pass on SQLite and PostgreSQL 16.

## Needs To Be Changed / Improved

- The ranker still reads only the legacy columns. Using the new ones is Milestone 5.
- Device measurements are tested with a simulated clock only; they have not been
  checked against known-duration playback on a simulator or an iPhone.

---

# Social Milestone 2: Publication Data and the Post Contract

**Status:** Done on `feature/social-m2-publication-contract` (stacked on Milestone 1), awaiting review; not deployed
**Date:** 2026-11-09
**Area:** Backend / post schema, API contract, feed query cost

## Why

The approved feed has four formats -- gym moment, workout, recipe, Your take -- and
credits for sources and sounds. All of that has to be in the schema and the contract
before the apps are built on it, without breaking the apps already installed.

## What Was Changed

- **Lineage on `Post`** (migration 0096): `is_take`, the direct `source_post`, the
  canonical `original_post`, and the attachment version taken. A take is an ordinary
  media, workout or meal post, not a new kind.
- **`Sound`**, referenced by posts and stored once. **`PostHashtag`** rows beside a
  `hashtags` list on the post, written together by `core.publication.write_hashtags`.
  `topics`, `language` and `reuse_permission` on the post.
- **Permissions** (`core/publication.py`): who may take a post or reuse a sound is
  decided at creation; whether a credit may still be shown is decided per reader at
  read time, by `visible_posts_for`.
- **Stored card figures** on `PostWorkout` and `PostMeal` (backfilled by 0097), and
  `?attachments=summary` on the post list, feed, For You and takes list.
- **Indexes**: three partial indexes on `Post` and one unique index on `PostHashtag`.

## Verification

Measured on PostgreSQL 16 with 200,000 posts, about 10,000 takes, 8,000 sound uses
and 140,000 hashtag rows. Buffers read to return one page of 21:

| Query | With the index | Without |
| ----- | ----- | ----- |
| Takes of a post, newest first | 6 | 2,266 |
| Posts using a sound, newest first | 3 | 204 |
| Posts carrying a hashtag, newest first | 4 | (unique index, always present) |
| Takes found when an original is deleted | 177 | 4,014 (sequential scan) |

The takes list as the API runs it, visibility rules included, walks `post_takes_newest`
with no sort. The three partial indexes total 1.3 MB beside a 31 MB table.

A summary page reads no exercise or food rows: up to four fewer queries on a page that has
workouts, recipes and reposts of them. A page with no takes and no sounds runs no
extra query for credits; a page with any number of takes runs one.

1,720 backend tests pass on SQLite and PostgreSQL 16. Responses are validated against
the committed contract and against the contract as it stood before this milestone.

## Needs To Be Changed / Improved

- Two existing `Post` indexes duplicate the leading column of a composite one
  (`author_id`, and `published_at`): 7.7 MB at this size, written on every post. Left
  alone here; worth dropping in a migration of their own.
- Nothing creates a `Sound` yet. Extraction and the removal workflow are Milestone 6.
- No take count on a card yet. It belongs with the cached engagement counts.
- Migrations 0095-0102 (Social) follow both `0094_health_workout_summaries` (work) and `0087_link_workout_calendar_tasks` (main); `0095_metric_flush_receipt` depends on both.

---

# Social Milestone 1: A Baseline, and Metrics Out of the Request

**Status:** Done on `feature/social-m1-baseline`, awaiting review; not deployed
**Date:** 2026-11-09
**Area:** Backend / load testing, API metrics, Redis failure handling, feed statistics

## Why

Before Social grows, it has to be able to grow without slowing workout logging, food
tracking or the planner. Two things stood in the way: nothing measured what a request
costs under mixed load, and each API request took row locks on shared counter rows.
An unreachable Redis also turned out to fail every save.

## What Was Changed

- **`loadtest/`**: a repeatable mixed-load harness with configurable budgets, kept out
  of the running application.
- **Buffered API metrics** (`core/metric_buffer.py`, `flush_api_metrics`): one Redis
  call per request, flushed every 30 to 60 seconds with a per-batch receipt so a retry
  adds nothing. Behind `ANALYTICS_BUFFERED`, off by default.
- **`core/resilient_cache.py`**: a failed Redis call is a cache miss, with 250 ms
  timeouts and a circuit breaker. Sign-in, registration and password reset refuse with
  a 503, and For You sheds itself.
- **Bounded feed statistics**: stale rows stand in for up to 6 hours, recounts are one
  request at a time and capped, and readers ask the next job run to repair what they
  found. Per-reader cache keys carry a ranking version.

## Verification

Full numbers are in `loadtest/baselines/2026-11-09/REPORT.md`. At the expected load,
analytics SQL went from 5 statements per request to 0 and all SQL from 12.9 to 7.9.
With Redis unreachable, saves carried on where they had previously all failed.
Measured on a laptop, so absolute CPU, latency and connection budgets are unproven.

## Needs To Be Changed / Improved

- Run the harness on hardware matching the hosting plan.
- The core p95 regression budget needs a longer run: at about 500 samples it is noise.
- `repbase-metrics-flush.timer` must be installed before `ANALYTICS_BUFFERED` is on.

---

# Performance Fix: Repost Feed N+1 Queries

**Status:** Done, committed
**Date:** 2026-10-09
**Area:** Backend / Feed & Post APIs

## Why

Every repost shown in a feed or post list triggered 5+ extra database queries
(one each for the reposted post's author, gym, disciplines, prompts, social
links). The more reposts in a feed, the slower the response — query count
grew with the number of reposts instead of staying flat.

## What Was Changed

- `core/views.py` — `posts_for_cards()`: added prefetching for the
  `repost_of` relation chain (author, gym, workout, meal, planner,
  disciplines, prompts, social links), matching what already existed for the
  top-level post.
- Also added `author__prompts` and `author__social_links` prefetch to the
  **top-level** author (it previously only prefetched `author__disciplines`).
  This was required, not optional: Django's prefetch batching only works if
  both the top-level and nested paths declare the same relation — without
  this, the repost fix silently didn't work.
- `core/test_review_regressions.py` — added 2 tests that confirm:
  1. Query count stays flat no matter how many reposts are in a feed.
  2. The reposted author's data (disciplines, prompts, social links, meal
     name) still renders correctly in the response.

## Verification

- Full test suite: 490/491 passing. The 1 failure is a pre-existing Postgres
  integration test that needs an env var not set in this environment —
  unrelated to this change.
- `manage.py check`: clean.
- Dev server boots and responds correctly on health endpoints.

## Needs To Be Changed / Improved

- Separate, known N+1: `get_repost_of` in `core/serializers.py` builds a
  fresh serializer per repost row, which defeats internal caching and causes
  a repeat `Follow`/`FollowRequest` lookup per row. Not fixed here — needs
  restructuring the serializer's caching, a bigger change.
  *Fixed 2026-10-10: see the last entry.*
- Production currently runs on `config.settings` instead of
  `config.production`, so Redis caching and compressed static file serving
  are not active. This is a known, intentional in-progress migration — needs
  Redis and a DB cert set up before the cutover.
  *Since then `deploy/configure.py` writes `DJANGO_SETTINGS_MODULE=config.production`;
  confirm the running service uses it.*
- Synchronous image processing (resize/re-encode) happens inside the request
  cycle, inside a DB transaction. No task queue (Celery, etc.) exists yet to
  move this to the background.

---

# Performance: For You Ranking, Uploads and View Tracking

**Status:** Done, committed
**Date:** 2026-10-10
**Area:** Backend / For You feed, video and photo uploads, social endpoints

## Why

Three additions had to stay fast as the community grows, and none of them
could be built the obvious way:

- **The For You page ranks posts from everyone.** A ranking is not an indexed
  `ORDER BY`, so the naive version scores every post on every request and
  gets slower with every post anyone makes.
- **Video uploads are ten to a hundred times larger than photos.** Photos
  travel as base64 inside JSON; a clip sent that way would be held in memory
  two or three times over, a third larger on the wire.
- **View tracking is written continuously** as people scroll, so its writes
  and its table both need a ceiling.

## What Was Changed

For You (`core/recommendations.py`, `core/for_you.py`):

- **Capped candidate retrieval.** Three sources, each with a limit: the
  newest 500 eligible posts, 150 from people followed, 150 on the reader's
  top topics. The work is set by those caps, not by how much has been posted.
- **Engagement counts** (likes, comments, shares, saves, views, long views,
  completions) are correlated subqueries in the candidate query itself: a
  column per count, never a joined row. The query plan confirms they run
  only for the 500 rows kept.
- **Interest profile reads** are bounded twice: 90 days, and 300 rows of each
  kind of interaction.
- **One ranking per first page.** It is cached for 30 minutes per reader and
  session; later pages read the cached order and fetch only their 20 posts.
- **Clips add no query per card**: `posts_for_cards()` joins `video` and
  `repost_of__video` with `select_related`.
- **New indexes**: `PostLike(user, -created_at)`, `PostComment(author,
  -created_at)`, `PostView(viewer, -last_seen_at)`, `PostView(surface,
  first_seen_at)`, `PostFeedback(viewer, -created_at)`, `PostVideo(status,
  created_at)` and `PostVideo.file`, plus unique `(viewer, post)` pairs on
  `PostView` and `PostFeedback`.
- **Impressions fold into one row per reader and post**, so the table grows
  with what there is to see rather than with how long people scroll. They
  arrive in batches of up to 50.
- **Measurement built in**: a `Server-Timing: rank;dur=…` header on every
  fresh ranking, a latency histogram kept in the cache, and
  `python manage.py feed_report` for latency and quality.

Uploads (`core/videos.py`, `core/uploads.py`, `core/media.py`):

- **Videos are sent as the raw request body** and streamed to a spooled
  temporary file: up to 4 MB in memory, then disk. The size limit is enforced
  from `Content-Length` before reading, and again while reading.
- **The video inspector seeks past media data** and reads only box headers
  and the `moov` box (capped at 16 MB). The metadata-free copy streams the
  media data in 1 MB chunks. Nothing is decoded.
- **Clips answer byte-range requests** when Django serves media (in
  development). In production nginx sends the file via `X-Accel-Redirect`
  and handles ranges itself.
- **Photo metadata is removed by walking JPEG segments, PNG chunks and WebP
  chunks.** Nothing is re-encoded.
- **The pixel ceiling is checked from the image header**, before any decode,
  so a decompression bomb is refused before memory is allocated for it.

Elsewhere:

- **Reads of `/social/posts/` no longer spend the 60-an-hour publishing
  limit.** Browsing profiles and liking posts could previously run a person
  into 429s.
- **The recommendation tests build their community once per class**
  (`setUpTestData`) and use a fast password hasher: 87 s → 2 s.

## Verification

Ranking benchmark on a scratch database: 200 authors, 3 likes and 5 views per
post, posts spread over 25 days, median of 7 runs on an Apple Silicon Mac.

| Eligible posts | SQLite ranking | PostgreSQL 16 ranking | PostgreSQL full first page (GET) | Queries per ranking |
| --- | --- | --- | --- | --- |
| 250 | 26 ms | 43 ms | 68 ms | 14 |
| 1,000 | 31 ms | 45 ms | 66 ms | 14 |
| 4,000 | 41 ms | 66 ms | 81 ms | 14 |
| 16,000 | 55 ms | 54 ms | 73 ms | 14 |

The query count is constant, and `ScaleTests` in `core/test_recommendations.py`
asserts it. On PostgreSQL the time is flat within noise from 250 to 16,000
posts. PostgreSQL ran with table statistics, as autovacuum keeps them; SQLite
ran without, as Django leaves it.

Uploads and view tracking, same machine:

- 20-second 720p H.264/AAC QuickTime clip (7.5 MB): inspection and the
  metadata-free rewrite take 3.5 ms, with 2.2 MB peak Python memory. The whole
  upload request -- stream, inspect, rewrite, store -- takes 36 ms.
- 12-megapixel JPEG (6.3 MB): metadata removal plus the decode check that
  confirms the result takes 76 ms.
- A batch of 50 impressions: 69 ms, 53 queries.

Also:

- 689 backend tests pass on SQLite (~3 min) and PostgreSQL 16 (~3.5 min).
- In Chrome against a running server: the For You page loaded, and clips were
  fetched with `206 Partial Content` and played.

## Needs To Be Changed / Improved

- **The candidate query depends on the planner choosing a newest-first index
  walk that stops early.** PostgreSQL does with table statistics. Without
  them -- right after a bulk load or restore, before autovacuum has analyzed
  -- it filters the whole 30-day window and sorts it: 185 ms at 16,000 posts
  instead of 54 ms. Run `ANALYZE` after bulk loads. SQLite with `ANALYZE`
  goes the same way (142 ms at 16,000 posts), which only affects
  development. The plan-independent fix is two steps: select the 500 newest
  eligible ids with a lean query, then compute counts for those ids alone.
- **Impressions update one row per post**: 53 queries for a batch of 50.
  A single `INSERT … ON CONFLICT DO UPDATE` adding to the counters would make
  it one statement. *Fixed 2026-10-10: see the last entry.*
- **The photo decode check is most of the 76 ms**, and `feed_variant()`
  decodes the same picture again a moment later. A reduced-size draft decode,
  or one decode shared by both, would cut it. It still runs synchronously in
  the request, as noted above.
- **Popularity comes from the newest 500 eligible posts.** An older post that
  is very popular is reached only through the following and interest sources.
  At larger scale, precompute per-post engagement into a table refreshed on a
  schedule.
- **The interest profile is rebuilt on every first page.** It is 10 to 12
  of a ranking's 14 to 16 queries, depending on the reader's history, while
  fetching candidates is only 3 or 4. Caching it for a few minutes would
  remove most of the remaining ranking time.
- **The latency histogram is per process in development**, where the cache is
  local memory. Production's Redis cache shares it across workers.
- **No transcoding or poster frames.** Clips are served as uploaded, minus
  their metadata, and players fetch the header to draw a first frame. An HEVC
  clip plays only in browsers that decode HEVC, which not all do; only Chrome
  was tested.

---

# Performance: Impressions in One Statement, Follow Lookups per Response, Week Ranges

**Status:** Done, committed
**Date:** 2026-10-10
**Area:** Backend / view tracking, feed serialization, the schedule list

## Why

Three costs listed above as still open, each growing with use:

- a batch of impressions was one query per post;
- each repost in a feed looked up the reader's follows and follow requests
  again;
- a client could read the schedule only all at once or one day at a time.

## What Was Changed

- `core/recommendations.py`: a batch of view events is written with one
  `INSERT … ON CONFLICT (viewer, post) DO UPDATE`, which adds to the
  counters. Written out because Django's upsert can only overwrite columns.
  Same merge rules as before.
- `core/serializers.py`: the reader's followed and requested ids are kept in
  the serializer context, which the whole response shares, instead of on
  each serializer instance.
- `core/views.py`: `GET /api/v1/schedules/` takes `start` and `end`.

## Verification

- A batch of 50 impressions: 2 queries (visibility, then the write), down
  from 53. Asserted in `ImpressionTests`, on SQLite and PostgreSQL 16.
- A feed of 5 reposts makes as many queries as a feed of 1, counting every
  query, not only the prefetched tables (`RepostFeedQueryCostTests`).
- 814 backend tests pass on SQLite and PostgreSQL 16.

## Needs To Be Changed / Improved

- Still open from above: the For You candidate query's dependence on table
  statistics, the double photo decode, synchronous image work in the request,
  and the interest profile rebuilt on every first page. Caching that profile
  would trade away the page reacting at once to a like or a follow, which the
  tests and RECOMMENDATIONS.md promise, unless every write that feeds it also
  invalidates it.


---

# Performance: For You Shares Its Counts, Re-ranks Each Page, and Was Timed at Scale

**Status:** Done, not yet committed
**Date:** 2026-10-12
**Area:** Backend / For You ranking and its sessions

## Why

The recommendation upgrade (similar taste, trending, momentum, new creators,
a re-rank on every page) adds work to every ranking, and the costs listed
above were still open: per-post counts were recomputed for every reader as
correlated subqueries, and nothing had been measured at a realistic size.

Measured first, on PostgreSQL 16 with 20,000 accounts, 90,000 posts (30,000
inside the window), 900,000 likes, 3.2 million view rows and 400,000 follows:
the previous version took 149 ms p50 per first page, 134 ms of it in
candidate retrieval, and of that about 50 ms per 500 posts was counting
likes, comments and views -- the same counts for every reader.

## What Was Changed

- `core/recommendations.py`:
  - per-post engagement counts are computed once, as six grouped queries over
    the posts that lack them, and shared through the cache for ten minutes
    (`engagement_for`); the reader's own actions are still read live;
  - this week's trending posts are computed once for everybody every five
    minutes, and follower counts of recently joined authors (for the newcomer
    part) every fifteen;
  - a later page no longer re-reads the whole ranking: the session keeps up to
    400 unserved candidates by id, re-checks them against eligibility, and
    re-scores them against a fresh profile;
  - similar taste asks for at most 100 neighbours with two or more shared
    likes before weighing them, which keeps its slowest query bounded.
- `config/settings.py`: without Redis, the local cache now holds 20,000
  entries instead of Django's 300. At 300 a single ranking evicted its own
  counts, so development measured a page that recomputed everything.

## Verification

Same database, 30 readers, Django's local-memory cache standing in for Redis:

| | p50 | p95 | queries |
| --- | --- | --- | --- |
| first page, counts cached (normal) | 91 ms | 98 ms | 22 |
| first page, cache empty (after a restart) | 275 ms | 476 ms | 30 |
| later page, re-ranked | 28 ms | 30 ms | 15 |
| people suggestions | 57 ms | 67 ms | 30 |
| previous version, first page | 149 ms | 162 ms | 16 |

`ScaleTests` asserts that the query count is constant in the amount of
content for first pages, later pages and suggestions, and that a warm cache
saves the counting queries. 960 backend tests pass on SQLite and PostgreSQL 16.

## Needs To Be Changed / Improved

- **A restart starts cold.** The first rankings after a deploy pay for the
  counts (about 275 ms, up to 480). A scheduled job writing per-post counts
  to a table would remove that and the cache entries both, at the cost of a
  job to run.
- **Later pages rebuild the profile.** About 11 ms of a later page's 28 is
  the profile, read fresh on purpose so the page follows what the reader just
  did. Reading only what changed since the session began would save most of
  that.
- **Not load-tested under concurrency.** The figures above are one request at
  a time.
