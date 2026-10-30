# Repbase API

The mobile API is versioned under `/api/v1/`. Interactive documentation is available at `/api/docs/`, and the OpenAPI document is served from `/api/schema/`.

## Authentication

Register or log in to receive an opaque API token. Store the token in the iOS Keychain and send it over HTTPS with every authenticated request:

```http
Authorization: Token <token>
```

Authentication endpoints:

- `POST /api/v1/auth/register/`
- `POST /api/v1/auth/login/`
- `POST /api/v1/auth/rotate-token/`
- `POST /api/v1/auth/logout/`
- `GET /api/v1/me/`
- `PATCH /api/v1/me/`

Token rotation invalidates the token used for the request and returns a replacement. After logout, the token can no longer be used.

## Main resources

- `/api/v1/users/` — limited public profile fields for authenticated users
- `/api/v1/exercises/` — shared and user-created exercises
- `/api/v1/workouts/` — reusable workout templates
- `/api/v1/workout-exercises/` — planned exercises and targets
- `/api/v1/schedules/` — workouts assigned to dates
- `/api/v1/sessions/` — workout sessions owned by the authenticated user
- `/api/v1/session-exercises/` — exercises performed during sessions
- `/api/v1/set-entries/` — actual weight and repetitions
- `/api/v1/body-weight/` — historical body-weight measurements
- `/api/v1/progress/exercises/{exercise_id}/` — exercise progress points

List responses are paginated with `count`, `next`, `previous`, and `results` fields.

## Social

- `/api/v1/social/posts/` — posts the reader may see; create one from your own session, meal or planner entry
- `/api/v1/social/feed/` — the people you follow, newest first (cursor paging)
- `/api/v1/social/for-you/` — posts picked for you from everyone, each with a `recommendation` saying why (cursor paging)
- `POST /api/v1/social/impressions/` — what was on screen and for how long, in batches of up to 50
- `POST`/`DELETE /api/v1/social/posts/{id}/not-interested/` — dismiss a post from For You, or undo it
- `GET`/`PATCH /api/v1/social/for-you/preferences/` — what For You has picked up about you, the topics you hid, and whether it is personalised at all
- `DELETE /api/v1/social/for-you/history/` — start For You fresh: past activity stops counting and viewing history is deleted
- `GET /api/v1/social/suggested-people/?limit=` — people you might want to follow, each with a `reason` and a `label`
- `POST`/`DELETE /api/v1/social/suggested-people/{id}/dismiss/` — stop suggesting somebody for six months, or undo it
- `POST /api/v1/social/posts/{id}/report/` — report a post to the moderators
- `POST`/`DELETE /api/v1/social/posts/{id}/video/` — attach or remove a clip on your own post

A clip is sent as the request body itself with a video `Content-Type`, not as
base64 or multipart. The server reads the file to decide what it is: MP4 or
QuickTime, H.264 or HEVC, at most 60 seconds and 25 MB by default. It answers
413 when the file is too large, 415 for a non-video `Content-Type`, and 400
with a sentence under `video` for anything else. Where moderation is on, a new
clip has `status: "pending"` and is shown only to its author until approved.
See [RECOMMENDATIONS.md](RECOMMENDATIONS.md) for how For You ranks.

## Session lifecycle

Create a planned session:

```http
POST /api/v1/sessions/
Content-Type: application/json

{"workout": 1}
```

When a workout is supplied, its exercises are copied into the session. Start and end timestamps cannot be edited directly:

```http
POST /api/v1/sessions/{id}/start/
POST /api/v1/sessions/{id}/end/
```

Valid transitions are `planned → active → completed`. Repeating or skipping a transition returns HTTP `409 Conflict`.

During an active session, `POST /api/v1/sessions/{id}/reorder/` with `{"session_exercises": [<id>, ...]}` renumbers the session's exercises 1..n in that order in one transaction and returns them as a list. The list must hold every SessionExercise id of the session exactly once; anything else, or a session that is not active, is a `400` and changes nothing. Exercises are added and removed mid-session through `/api/v1/session-exercises/`.

## Workout structure

- `SetEntry.set_type` is `normal` (default), `warmup`, `drop` or `failure`. Warm-ups are excluded from personal records, exercise progress and profile highlights; they still count in `logged_set_count`. `previous-sets` rows carry `set_type` too.
- A workout's exercise relation (`WorkoutExercise`, `initial_exercises`, `exercise_plan`) carries `rest_seconds` (null = the person's default, 0 = no timer, max 3600), `superset_group` (1–100; the same number in one workout is one superset) and `set_types` (one SetType per planned set, never longer than `target_sets`). A plan item that leaves a field out keeps the relation's current value; lowering `target_sets` without sending `set_types` trims the list.
- `WorkoutTemplate.last_performed_at` is when the workout was last finished and trained (see "Activities" below for what counts), or null.
- `PublicRepbaseUser.follows_viewer` says whether that person follows the reader.
- `GET /api/v1/exercises/` also takes `search` (name or alias contains, ignoring case, first 100 characters) and `mine=true`.

All of these response fields are optional in the contract so an app built against them still reads a server deployed before them.

## Activities and logged sessions

- `WorkoutTemplate.activity` is one of 51 slugs (`core/activities.py`: `weightlifting`, `running`, `tennis`, `yoga`, `other`, ...). It is a plain string, not an enum, so the list can grow; clients should treat an unknown slug as `other`. Responses always carry one. Writing `activity` also sets `workout_type` to its family; writing only `workout_type` (older apps) sets that family's default activity unless the workout is already an activity of that family. `workout_type` keeps its four values.
- Sessions carry `activity` (the workout's, null if it was deleted) and what the person said: `perceived_effort` (0–100), `manual_calories_kcal` (1–20000, typed in, never measured), `manual_distance_km` (0.01–9999.999), `notes` (≤2000) and `activity_details` (only the keys the activity allows, e.g. `rounds` for boxing, `session_type` for tennis; unknown keys and wrong types are a 400 naming the key). All five are writable with `PATCH /api/v1/sessions/{id}/` in any status. A typed distance becomes `recorded_distance_km` when the session has no GPS or Health distance; clearing it clears that.
- `POST /api/v1/sessions/log/` (`ActivityLogRequest`, 201 `WorkoutSession`) logs a finished session after the fact: `activity`, `started_at`, `duration_seconds` (60–86400, ending at most 5 minutes from now), and `workout` (yours, same activity) or `workout_name` (found ignoring case, or created). Optional `scheduled_date` schedules it (reusing that day's entry), plus the five fields above. It sets `is_manual_entry`, ticks the day's planner task like `end`, and replays with the `Idempotency-Key` header like every other create.
- What counts as training (stats, `last_performed_at`): a `strength` activity (weightlifting, calisthenics) needs a logged set; every other activity needs a duration or a distance above zero. A session whose workout was deleted counts by sets, as before. `GET /api/v1/sessions/?activity=` filters by activity.
- Exercises carry the shared library's `slug`, `aliases`, `category`, `equipment`, `movement_pattern`, `level`, `mechanic`, `force`, `family`, `primary_muscles`, `secondary_muscles`, `disciplines` and `instructions` (read-only; empty on custom exercises). The list also filters by `equipment`, `category`, `pattern` and `muscle` (a primary muscle), and `name=` returns your own exercise before a shared one of the same name. `python manage.py sync_exercise_library [--file PATH]` loads the library; migration 0085 does it once if `core/data/exercise_library.json` exists.

Every new response field is optional in the contract, and no response enum gained a value, so older app builds keep decoding.

## Data conventions

- Timestamps use ISO 8601 and UTC.
- Weight is stored in kilograms.
- Decimal weights are represented as JSON strings to avoid floating-point precision loss.
- The user's `unit_preference` controls client display, not stored values.
- Resource ownership is derived from the authentication token and cannot be assigned in request bodies.

The committed [OpenAPI schema](openapi.yaml) is the source of truth for request and response shapes.
