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

## Data conventions

- Timestamps use ISO 8601 and UTC.
- Weight is stored in kilograms.
- Decimal weights are represented as JSON strings to avoid floating-point precision loss.
- The user's `unit_preference` controls client display, not stored values.
- Resource ownership is derived from the authentication token and cannot be assigned in request bodies.

The committed [OpenAPI schema](openapi.yaml) is the source of truth for request and response shapes.
