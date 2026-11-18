# Apple Health and wearable workout summaries — testing branch

This work is on `work/testflight-calendar-sync-2026-10-28`. Migration
`0094_health_workout_summaries` has not been applied to the live backend.
The matching iOS work is on `work/testflight-fixes-2026-10-27`.

## What is available

Apple Watch workouts are read by the iPhone app from Apple Health, then sent
to the authenticated Rytivo API after an explicit connection agreement. This
is not a watchOS recording app and is not a real-time watch telemetry stream.

Garmin direct access is **not enabled**. Garmin developer approval/access has
not been obtained. There are no invented OAuth endpoints, stored Garmin
passwords, direct-Garmin ingestion routes, or claims of two-way Garmin editing.
The dashboard can show Garmin-origin workouts already available to the app
through Apple Health. That source is identified by the exact reported source
bundle `com.garmin.connect.mobile`, never by a display name alone. This remains
device-reported provenance, not server-verified Garmin API data.

## API contract

All routes require the signed-in account. Account identity is server-derived.

- `GET /api/v1/integrations/apple-health/`: `enabled`, nullable
  `consent_version`/`last_sync_at`, last imported/updated counts,
  `schema_version: 1`, and `garmin_direct_available: false`.
- `PATCH /api/v1/integrations/apple-health/`: enable with
  `{"enabled": true, "consent_version": "apple-health-v1"}`; disconnect with
  `{"enabled": false}`. Enabling a connection records agreement but cannot prove
  which HealthKit read permissions the person granted on an iPhone.
- `POST /api/v1/sessions/import-health/`: keeps the original `workouts` batch
  shape and original result counts. Optional fields add `workout_activity`,
  active `duration_seconds`, source name/bundle/device, energy and average/max
  heart rate. `updated` and `linked` result counts are additive.
- `GET /api/v1/integrations/workouts/`: 50-row pages with optional
  `source=apple_health|garmin` and inclusive `since=YYYY-MM-DD`. There is no
  client-controlled unlimited page size.
- Owner session responses include optional, nullable, read-only
  `health_summary`, using the same summary contract as the integration list.

Decimal metric values use JSON strings. Missing readings stay null; zero
energy/distance is distinct from missing data. Duration uses HealthKit's active
duration when supplied, rather than counting paused time. Older import clients
fall back to elapsed time without erasing an already-known active duration.

## Consent, privacy and safety

- Migration creates no enabled connections and does not infer consent from
  old records. Both workout imports and step writes require the current
  enabled connection. Old clients therefore need the updated connection flow.
- Disconnect blocks future writes, including imports already waiting on the
  shared account lock. It retains previously imported history. It is not a
  delete-history action and does not revoke Apple's device permissions.
- Private health summaries are not added to social post snapshots, shared
  calendar projections, or public profiles. Only existing owner-scoped
  session/integration routes expose them.
- Import/disconnect serialize on the account row. Per-account source IDs are
  also database-unique. Retrying a completed batch updates or skips the same
  source record instead of duplicating workouts.
- A native session is linked only if it is the sole same-activity candidate,
  starts/ends within 90 seconds, and has at least 90% actual interval overlap.
  Ambiguous overlaps are counted as `skipped_overlapping`, not guessed or
  duplicated. A zero/short adjacent workout cannot pass on boundary tolerance
  alone.
- Linking does not rewrite sets, notes, manual energy, route data, or the
  native session's timeline. Native `health_external_id` remains unchanged.
- Exact historical imported IDs gain a summary without copying their session.
  New importer templates have a private activity identity, not name matching;
  user-created similarly named workouts are not claimed.
- Sample identities cannot be reused for another activity or time. Omitted or
  null metrics cannot erase an existing reading. Merged heart-rate bounds are
  checked before writes.
- Source metrics are bounded and validated, batches are limited to 500, repeat
  records are loaded together, and importer templates are reused per activity
  within a batch. Imports have a separate configurable limit, default 120/hour.
  Dashboard/session read queries do not grow one query per summary.
- Wearable strength sessions can count as performed from their valid active
  duration without inventing strength-set logs.

## Validation

Run from this repository using the existing isolated test configuration:

```sh
python manage.py test core.test_health_integration core.test_activity_tracking core.test_security_audit
python manage.py test core
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py spectacular --file /tmp/rytivo-health-openapi.yaml
diff openapi.yaml /tmp/rytivo-health-openapi.yaml
```

`core.test_health_integration` covers explicit consent, authenticated/private
reads, missing data, retry/update semantics, native matching, collisions,
activity/source provenance, input bounds, training counts, pagination and
query cost. The PostgreSQL CI job also exercises simultaneous retries and
disconnect-before-import lock ordering. SQLite deliberately skips row-lock
tests; passing SQLite alone is not concurrency proof.

For actual local API testing, follow `WORKOUT_PER_SET_TESTING.md` and the iOS
`WORKOUT_LOCAL_TESTING.md`. The local backend runner applies current migrations
only to its marked, private local test database. Do not deploy this branch to
`rytivo.app` simply to test a simulator.

A physical iPhone paired with Apple Watch is still required to validate real
Health permission sheets, delayed watch synchronization, workout-source
coverage and heart-rate availability. Simulator/test fixtures cannot establish
device approval or production Garmin access. No physical-device validation or
live Garmin synchronization is claimed by this change.
