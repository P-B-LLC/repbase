# Durable per-set planning — testing candidate

Branch: `work/testflight-calendar-sync-2026-10-28`. No main merge, hosted
migration, deployment, worker change or live account mutation is included.

## Contract

- `WorkoutExercise.planned_sets` stores at most 50 ordered target rows. Each
  row has a UUID unique within that exercise, set type, optional reps 1–1000,
  and optional decimal-string kilogram weight 0–99999.99 (two places).
- Nonempty rows define count/types and clear aggregate targets. Empty rows
  retain legacy semantics; omission preserves existing explicit rows. Older
  clients cannot contradict/delete explicit rows without an intentional
  `planned_sets_version: 1` template replacement.
- Authenticated `GET /api/v1/workouts/capabilities/` advertises version 1.
  This is additive and does not grant access to another owner's templates.
- Session creation snapshots target rows, source relation ID, rest and superset
  membership under the same parent lock used by plan mutations. Snapshots are
  read-only and survive later template edits/deletion. No actual set is logged
  by planning or starting. Historical unknown snapshots remain null.
- Additive migration `0093_workout_planned_sets` changes no old logged result.
  `openapi.yaml` is generated from the backend and copied into the iOS branch.

## Tests

Focused suite: `core.test_workout_planned_sets core.test_workout_structure
core.test_session_recovery core.test_save_recovery core.test_activity_tracking
core.test_schedule_planner_sync` initially passed 242 tests, five PostgreSQL-only
skips locally. Run the full CI on the exact testing head for PostgreSQL locking
and complete regression coverage. Schema validation, migration drift and Django
checks passed locally. No production accuracy/load claim follows from SQLite.

Coverage includes malformed/duplicate/oversized rows, ownership, atomic rejection,
old-client protection, stable IDs, explicit blank targets, independent actual
logs, immutable snapshots, idempotent start, reordered/sparse relation orders,
and concurrent session snapshot vs edit on PostgreSQL.

## Real local testing

`Scripts/run-local-workout-testing.py` uses a new marked private
`.local-workout-testing` directory and a loopback-only API. It inherits no hosted
database, provider, Redis, SMTP or APNs credentials; it does not read `.env`.
It prompts for a separate `local_workout_tester` password on first run and keeps
that hashed account and saved workouts on subsequent runs. Never run destructive
automated fixture seeding against this durable local database.

See the iOS branch's `WORKOUT_LOCAL_TESTING.md` for paired Mac terminal commands.
The simulator launcher verifies this isolated API and uses its own simulator,
without preview flags or an existing live-account simulator.
