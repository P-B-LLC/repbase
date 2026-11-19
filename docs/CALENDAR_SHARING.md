# Calendar plans and followers-only sharing

## Testing candidate — 4 November 2026

Work lives on the existing iOS `work/testflight-fixes-2026-10-27` and backend
`work/testflight-calendar-sync-2026-10-28` branches. Do not merge main, deploy,
change live secrets, or upload TestFlight without separate release approval.
The approved Figma month/day, item-color and sharing flows are the visual target:
https://www.figma.com/design/FbMNgG8cuZfrRFzQTtIVCy?node-id=392-15543
and https://www.figma.com/design/FbMNgG8cuZfrRFzQTtIVCy?node-id=383-14823.
The user's final instruction overrides the old Figma Following tab: invite
recipients are FOLLOWERS ONLY, enforced by the server as recipient -> owner.

## Privacy contract

- `/planner/` stays strictly owner-scoped. Accepting a share never makes a
  private planner row editable, completable, or readable through that route.
- Separate calendar grants invite one follower to one occurrence (default), or
  the addressed occurrence and future dates in the same stable series lineage.
  Pending invitations require explicit acceptance. Decline/revoke is not undone
  by retry; re-inviting requires a separate explicit confirmation.
- Shared responses contain only a limited plan projection: owner identity,
  title, category/appearance, scheduled date/time/zone, duration, recurrence and
  completion state. No notes, subtasks, workout-template IDs or training logs.
- Blocking in either direction and unfollowing revoke access durably. Every
  read also checks the current relationship. Refollowing does not restore grants.
- One occurrence can be private through a stable series/occurrence-date
  exclusion. Future edits preserve that identity even if displayed dates move.
  An earlier sharing boundary never silently expands an accepted invitation.
- Owner management includes all recipients in the lineage and tells the client
  whether a grant applies to the selected occurrence, including private dates.
- Calendar responses are private/no-store. The native app clears account state,
  fences stale reads, and dismisses stale shared-detail snapshots on refresh.

## Recurrence and storage

Tasks and events can repeat. Events still cannot be completed or have subtasks.
The occurrence is stored once. Grants connect recipients to an occurrence or
series; a private-date exception is separate. Twenty followers do not create
20 copies of every future workout. Shared reads project unmaterialized dates
without writing diary rows; a previously deleted materialized date stays gone.

Ordinary edits affect one occurrence. Explicit `scope=following` splits the
rule while preserving its stable lineage, earlier/completed history and private
exceptions. New repeats have a bounded 30-day owner-local lookback; old one-off
historical entries are still allowed. Existing ancient recurrence cursors are
not rewritten: legacy catch-up materialization needs separate load review.

Timed repeats retain a wall-clock time and IANA scheduling zone rather than
adding fixed UTC-day intervals. Native reminders resolve each occurrence in
that zone. Ambiguous DST times use the first occurrence; nonexistent times move
forward preserving smaller components. Shared calendars display the owner's
literal date/time with its explicit zone, not an unlabelled viewer-zone time.
Reference: https://www.rfc-editor.org/rfc/inline-errata/rfc5545.html.

## Reads and UI

The native month has two short title bars per date plus overflow; the selected
day shows full names/actions, all hours, anytime tasks and collapsed past due.
Completed items remain in place. Named item colors have adaptive light/dark
assets and checkmarked choices. Existing navigation and workout sync stay intact.

The shared month request is bounded to 42 days and two previews plus counts per
date. Owner filters are applied before counts/previews, and selected-day details,
accepted owner identities and followers are paginated. Follower search happens
on the server. Grants, projection rows/rules and invite rate are capped, with
constant-query regression tests. Existing own-calendar reads are preserved for
Home/workout/reminder synchronization; replacing that pipeline with a compact
own-month projection remains a separate optimization, not a new claim here.

Native shared models/state are separate from `PlannerEntry`/`PlannerStore`.
They are combined only for calendar display, never own task counts, Next up,
completion celebrations, reminders or workout editing. Visibility controls do
not change permissions. An invitation inbox supplies Show in my calendar and
Decline; existing social notification enum/push delivery is not changed.

## Validation and rollout checklist

1. Backend SQLite suite, schema validation and migration drift checks.
2. PostgreSQL suite for real row-lock/concurrency behavior (SQLite skips these).
3. Regenerated Swift client from the committed OAS; no handwritten generated code.
4. Mac Debug/Release builds, account-safety/transport tests and real native
   light/dark/month/day/large-text/RTL screenshots. Source checks are not rendering.
5. Two test accounts: followers-only search, invitation/accept/decline/reinvite,
   both scope choices, original-history privacy, edited/deleted/private dates,
   revocation/block/unfollow/refollow, stale network/account responses, overlapping
   items, DST and clock seconds. Shared items must never count as your own tasks.
6. Device barcode/camera and unrelated features are outside this change. No
   production user data is used for validation. Health checks alone do not prove
   permissions or the complete native workflow.

A test-branch push is not activation. Use a backend running these migrations
and endpoints to validate the real invitation workflow; old beta backends show
an update-needed message rather than inventing shared data.

