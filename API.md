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
