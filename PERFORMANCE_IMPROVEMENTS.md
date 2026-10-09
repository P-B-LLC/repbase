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
