# Save recovery — beta item 2

## Implemented

- Session end accepts optional `ended_at`, rejects future/before-start times,
  and preserves the original timestamp when completed sessions are replayed.
- Route samples deduplicate under a parent row lock, including overlapping
  retries. End locks the non-aggregated parent query.
- Food, recipe, planner, and workout-template POSTs accept UUID `Idempotency-Key`
  for JSON. Resource and successful response commit together. Same account/path/
  key and JSON replay the result; changed input returns 409. Validation failures
  and rolled-back transactions do not consume keys. Receipts cascade on account
  deletion and do not expire automatically.
- Workout create supports transactional `initial_exercises`/`initial_date`.
  Update-only `exercise_plan` atomically changes/reorders the plan, retains
  supplied relation IDs, and rejects relations/private exercises from another
  workout/account. Metadata rolls back with relation failures.
- Recipe create and ingredient replacement are transactional, including edits.
- Older unkeyed/multi-request clients remain compatible but do not gain the
  new guarantees simply by deploying this server.

## Integration and rollout

Apply migration `0049_save_receipts` with the backend release before distributing
the new iOS client. Both OpenAPI files match; Swift was regenerated on macOS.
iOS now sends original finish timestamps, persistent original create snapshots
and operation UUIDs, plus atomic workout create/edit requests.

## Verified 2026-09-12

- Full core suite: 278 tests ran, OK, two PostgreSQL-only skips (276 passed).
- Migration drift and schema validation pass; repository schemas match.
- iOS native harness: 19 passing account/navigation/recovery tests. Simulator
  Debug build passed. See iOS `SAVE_RECOVERY.md` for limitations.
- PostgreSQL tests in `core/test_save_concurrency.py` and the `recovery-postgres`
  CI job remain unexecuted here. SQLite does not prove row-lock behavior.

## Still open

Backend commit `eccc2c3` now applies receipt handling to session/set creates and
makes session start replay-safe. iOS still needs generated-header updates,
durable operation identities, and end-to-end integration for these writes.
Device tests must verify interrupted transport, restart, edited retries, account
changes, and protected local recovery files. This batch is not beta sign-off.
No push, deployment, or production migration was performed.
