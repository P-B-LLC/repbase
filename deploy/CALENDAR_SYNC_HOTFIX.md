# Beta Home / Calendar / Workouts sync rollout — 2026-10-28

## Scope and deployment gap

The iOS synchronization changes were pushed to the testing branch, but the
live backend was still at the like-response hotfix
`8323bb9ca402a5c0d25628855c5b23e36de64229`. That release does not include the
schedule-to-planner repair or migration `0081`. A pushed branch is not a
backend deployment, and an iOS rebuild alone cannot activate this repair.

`fix/beta-calendar-sync-2026-10-28` starts from that exact live revision and
backports the schedule repair from `944266d`. It preserves the like-response
fix, moderation-admin correction and Pillow 13 security update. It does not
include the pending own-post feed-ranking change, other dependencies, new
environment settings, or any changes to main or TestFlight.

The repair records the originating workout schedule for newly generated
planner tasks. Moving/replacing/clearing schedules and stopping a repeating
split can then reconcile the corresponding untouched, unfinished generated
tasks. Completed, customized, adopted/manual and unrelated tasks are retained.
The iOS testing build is still needed for the matching cross-screen refresh
behavior.

## Data preservation and legacy limitation

`0081_plannerentry_source_schedule` adds one nullable provenance relationship
and its index. There is no guessed backfill, broad task cleanup or data rewrite.
The helper checks row-count and migration baselines before restoring traffic.

Previously generated tasks that have no provenance cannot safely be identified
by their title, date or workout alone: older synchronization could adopt a
user-created task. They remain untouched. This update prevents future drift;
it does not establish that every currently displayed stale entry has been
repaired. Any obsolete existing entry needs separate review by exact ID and
an authorized UI/API action, never a bulk deletion by category/title.

## Root operator procedure

Only run a full commit SHA after its exact deployment tree has passed the
Linux, PostgreSQL, dependency and rollout tests recorded in the validation PR.
Do not substitute `main`, a short SHA or the moving branch name for that SHA.
Choose a quiet beta window with no concurrently running manual `manage.py`,
admin-maintenance or deployment jobs, and no cron/external writers outside the
inventoried systemd units. The observed host has the reviewed units, but this
inventory is not proof that every possible database writer has been stopped.

Use the separately supplied, pinned root-terminal command to fetch the branch,
materialize `deploy/deploy-calendar-sync-hotfix.sh` into a temporary file and
run it with the full SHA. Do not stream the helper through process substitution:
it copies itself before replacing the checkout. If pasted terminal text gains
`^[[200~` / `^[[201~` characters, cancel it and enter the supplied lines separately.

The helper:

1. Requires the exact currently deployed base, a clean checkout, reviewed
   changed paths, matching branch head/helper bytes, and the deployment lock.
2. Inventories the known model-loading systemd services/timers and records
   their original running states. Unreviewed app units fail preflight.
   Certificate renewal remains untouched. The web backend, screening/video
   workers and other application workers/timers are temporarily stopped.
3. Checks the migration graph, then creates a root-private custom PostgreSQL
   dump, verifies its archive listing, and records its SHA-256. The record
   directory is mode `0700`, generated files are private, and credentials are
   neither printed nor copied into it. Expect temporary app/API unavailability
   while backup and migration run; choose a quiet beta window.
4. Applies only `core.0081_plannerentry_source_schedule`, with bounded database
   lock/statement waits. No dependency installation or unrestricted migration
   command runs. A retry may retain an already-applied additive migration only
   when the support checks establish compatibility and no provenance links.
   The helper requires the existing direct PostgreSQL connection and refuses
   the project's transaction-pooling configuration; session lock timeouts must
   remain attached to the connection performing the migration.
5. Verifies preserved row counts/schema, runs the existing deployment checks
   without acknowledgements/bypasses, restores the original application
   service/timer states, and requires health `200` / unauthenticated API `401`.
   It records the full SHA in `/etc/repbase-release` only on success.

Backups and the release record are retained under
`/var/lib/repbase-calendar-sync.XXXXXXXX`; this helper does not remove them.
Protect them as production data and handle retention separately.

## Failure / recovery boundaries

The new relationship uses Django-side deletion behavior. Once new code writes
non-null provenance links, the old models do not know how to handle them.
Blindly checking out the old code can make later schedule/workout deletions
fail database constraints.

If stopping workers fails **before any candidate checkout is attempted**, the
helper verifies that the original base, clean checkout and release stamp are
unchanged, then restores the original web service independently of worker
state. It attempts each originally active worker/timer separately. A worker
problem must not strand the unchanged web backend offline.

After candidate checkout is attempted, failure recovery first stops all app
workers/timers. It automatically
restores the old code only if the schema is absent or it can prove there are
zero provenance links. The additive nullable schema is retained; it is not
reversed. Existing dependencies and security fixes remain unchanged.

If compatibility cannot be proved, it leaves application units stopped and
prints `RECOVERY NEEDS ATTENTION` with the private record location. Do not
restart old code, reverse `0081`, detach all links, or restore a whole database
without reviewing current data first. Prefer a forward fix. A database restore
could erase writes made since the backup; this helper never does one
automatically. Review the saved unit-state record when restoring services.

### Stopped-but-failed worker incident

The first operator attempt with `fb95aee` stopped the existing oneshot media
workers. Systemd retained `ActiveState=failed` / `Result=signal`, despite zero
main/control PIDs, no queued job and no remaining control group. The old helper
accepted only `inactive`, then repeated that mistaken check during recovery.
It stopped before checkout, backup or migration, but left the old API offline.
The operator restored the original web service and worker timers; authenticated
API access again requires sign-in (anonymous response `401`).

The corrected helper records stop evidence and accepts `inactive` **or**
`failed` services only after proving zero main/control PIDs, no queued job and
an empty or removed cgroup. For an existing cgroup, the cgroup-v2 `populated=0`
flag covers all descendants; missing or unreadable evidence fails closed.
Historical `Result=signal` is not evidence of a running process. Timers must
still be inactive, and restarted workers must still report successful state.
Do not rerun the old SHA; use the newly validated pinned candidate.

## Validation and acceptance

The rollout tests must cover exact migration selection, no data loss,
zero-link rollback compatibility and populated-link rollback refusal against
a disposable PostgreSQL database, plus the runtime schedule regressions.
Mocked shell checks alone do not establish database migration safety. Record
the final candidate's CI links/results in its draft validation PR.
The PostgreSQL CI job explicitly installs matching version-17 client tools on
the disposable runner for the real backup rehearsal. This does not install or
change any software on the live beta server.
`core/test_calendar_sync_shell.py` exercises the helper as a Bash subprocess
with temporary paths and mocked host commands: success, pre-migration failure,
backup failure, quiescence failure, unsafe linked-data rollback refusal,
unexpected paths, release-stamp mismatch and immediate worker failure. These
tests skip on Windows and run under both Linux CI suites.
The expanded cases cover retained failed/signal states, remaining main/control
processes, queued jobs, populated/unreadable cgroup evidence, unchanged-web
recovery and misleading `health=200` with `API=502`.
An explicit `RYTIVO_SYSTEMD_REHEARSAL=1` CI step runs
`core.test_calendar_sync_systemd` against a unique, disposable transient sleep
service on the isolated runner. It reproduces the actual oneshot stop state
and exercises the helper's exact read-only predicate; no real application
unit or production server is touched. Ordinary/local test runs skip this
privileged rehearsal unless explicitly opted in.

After a successful root deployment, use an authorized beta test account and
the iOS testing build to verify:

- Create a dated workout; revisit Home, Workouts and Calendar and confirm the
  same selection without restarting the app.
- Change or clear that newly created schedule and confirm the linked unfinished
  generated task changes/disappears across all three views.
- End a repeating split; confirm affected future, untrained dates clear while
  completed/active/past and unrelated/manual items are preserved.
- Complete/undo a workout and revisit Home and Calendar to verify refresh.
- Confirm manually created, customized and completed tasks remain untouched.

The health probes establish process/API reachability, not this end-to-end
account behavior. The original report was not reproduced against the user's
private data; the missing backend rollout was independently confirmed.
