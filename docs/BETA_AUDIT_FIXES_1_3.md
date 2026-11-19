# Beta audit fixes 1–3 — 2026-09-13

Scope: default gear switching, partial photo-post publication, and social-like
request races. These changes do not declare the entire app beta-ready.

## Backend implementation

- Gear writes lock the owner row, refresh an existing instance after acquiring
  the lock, clear the old default before saving, and roll back both on failure.
  The uniqueness constraint remains the final invariant. Different owners do
  not serialize against each other.
- Post media gets durable PendingMediaDeletion records before storage writes.
  Publication and snapshot creation commit together only after required media
  succeeds. Cleanup workers lock the same staging rows, preventing cleanup
  during publication. Success consumes the staging records; errors roll back
  publication and attempt cleanup, retaining failed cleanup for operational retry.
- No schema migration or public API contract change is needed. Existing post
  images remain compatible. The cleanup command must still be scheduled in
  production: `python manage.py retry_media_deletions`.
- An interrupted request is tested by bypassing normal Exception handlers after
  writing a file. No post is published and the durable cleanup record remains.
- Existing response-loss duplication of social creates is audit item 5, not
  solved by this publication fix. Do not describe publication as idempotent.

## Regression commands

Final full-suite result on the Mac: 308 tests ran, 305 passed, 3 PostgreSQL-only
tests skipped. No migration drift; regenerated schema matches openapi.yaml.

`python manage.py test core.test_beta_publication`

`python manage.py test core`

PostgreSQL: existing CI runs `core.test_save_concurrency`, now including
simultaneous default switches. This Mac has no PostgreSQL runtime; SQLite
results do not prove PostgreSQL row-lock behavior.

The shared test mixin's `_pre_setup` now uses Django's classmethod signature,
allowing transaction-based tests to start instead of failing during setup.

## Matching iOS change

IOS-Frontend adds SocialLikeCoordinator, patches only confirmed like fields,
shares pending state by original post ID across reposts, and invalidates late
responses when the account changes. See that repository's matching report.
