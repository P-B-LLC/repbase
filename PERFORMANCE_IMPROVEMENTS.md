# Performance Fix: Repost Feed N+1 Queries

**Status:** Done, not yet committed
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
- Production currently runs on `config.settings` instead of
  `config.production`, so Redis caching and compressed static file serving
  are not active. This is a known, intentional in-progress migration — needs
  Redis and a DB cert set up before the cutover.
- Synchronous image processing (resize/re-encode) happens inside the request
  cycle, inside a DB transaction. No task queue (Celery, etc.) exists yet to
  move this to the background.

---

# Performance: For You Ranking, Uploads and View Tracking

**Status:** Done, not yet committed
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
  it one statement.
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
