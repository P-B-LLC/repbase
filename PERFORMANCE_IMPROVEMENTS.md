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
