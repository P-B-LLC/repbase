# RepBase

RepBase is a Django REST Framework backend for a fitness application. It supports user profiles, workout planning, workout-session logging, body-weight history, and exercise progress tracking.

The native iOS application is maintained as a separate project. This repository contains the API, database models, authentication, administration tools, and API documentation used by that client.

## Current features

- Account registration and token authentication
- Private profile management
- Limited member profiles for authenticated users
- Shared and user-created exercises
- Reusable workout templates
- Planned exercises, sets, repetitions, and target weights
- Workout scheduling by date
- Planned, active, and completed workout sessions
- Individual set logging
- Computed session duration
- Exercise progress history and training volume
- Body-weight history
- Owner-scoped data access
- A social feed: posts built from workouts, meals and plans, with photos and short video clips
- A personalized For You page (see [docs/RECOMMENDATIONS.md](docs/RECOMMENDATIONS.md))
- Reporting, moderation and human review of video (see [docs/MODERATION_OPERATIONS.md](docs/MODERATION_OPERATIONS.md))
- Interactive OpenAPI documentation

## Technology

- Python 3.12+ (CI and production use 3.14)
- Django 6.1
- Django REST Framework 3.18
- SQLite for local development
- drf-spectacular for OpenAPI generation

## Repository layout

```text
config/        Settings: settings.py (development), production.py, test_postgres.py,
               test_e2e.py (the iOS end-to-end suite), loadtest.py, local_workout_testing.py
core/          The Django app: models, serializers, views, feature modules
               (calendar_sharing*, health_*, cookbook*, sound*, recommendations, ...),
               management/commands, migrations, templates, static
core/test_*.py The test suite (one module per feature)
deploy/        deploy.sh, systemd units and timers, nginx snippets, operator notes
loadtest/      The mixed Social load-test harness and reviewed baselines
Scripts/       Contract validation and the local workout-testing launcher
docs/          Guides, runbooks and feature notes; docs/archive/ holds old PDF exports
openapi.yaml   The committed API contract (CI fails if it drifts from the serializers)
```

## Local setup

On macOS or Linux:

```sh
python3.14 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python manage.py migrate
.venv/bin/python manage.py runserver 5000
```

From PowerShell in the project directory:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py runserver 5000
```

The service will be available at [http://localhost:5000](http://localhost:5000).

If PowerShell prevents virtual-environment activation, allow it for the current terminal session:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.venv\Scripts\Activate.ps1
```

## API documentation

After starting the server:

- Interactive documentation: [http://localhost:5000/api/docs/](http://localhost:5000/api/docs/)
- OpenAPI schema: [http://localhost:5000/api/schema/](http://localhost:5000/api/schema/)
- API root: [http://localhost:5000/api/v1/](http://localhost:5000/api/v1/)
- Health check: [http://localhost:5000/health/](http://localhost:5000/health/)
- Django Admin: [http://localhost:5000/admin/](http://localhost:5000/admin/)

The validated schema is also committed as [`openapi.yaml`](openapi.yaml).

## Authentication

Register or sign in through the API to receive an opaque token. Authenticated requests use the following header:

```http
Authorization: Token <token>
```

Store this token in the iOS Keychain and send it only over HTTPS outside local development.

Authentication endpoints:

```text
POST /api/v1/auth/register/
POST /api/v1/auth/login/
POST /api/v1/auth/rotate-token/
POST /api/v1/auth/logout/
GET  /api/v1/me/
PATCH /api/v1/me/
```

## Main API resources

```text
/api/v1/users/
/api/v1/exercises/
/api/v1/workouts/
/api/v1/workout-exercises/
/api/v1/schedules/
/api/v1/sessions/
/api/v1/session-exercises/
/api/v1/set-entries/
/api/v1/body-weight/
/api/v1/progress/exercises/{exercise_id}/
```

Records are scoped to the authenticated account. A user cannot access another user's workouts, sessions, set entries, schedules, or weight history.

## Measurement units

RepBase uses metric values as its canonical storage format:

- Weight is stored in kilograms using fields ending in `_kg`.
- Height is stored in centimeters using fields ending in `_cm`.
- `unit_preference` records whether the user prefers metric or imperial display.

The API does not currently convert measurements. The iOS application should convert values for display and convert user input back to kilograms or centimeters before sending it to the API.

```text
pounds = kilograms x 2.20462
kilograms = pounds / 2.20462

inches = centimeters / 2.54
centimeters = inches x 2.54
```

Keeping one canonical storage unit prevents rounding drift in progress calculations.

## Workout-session lifecycle

A workout session follows this sequence:

```text
planned -> active -> completed
```

Create the session through `/api/v1/sessions/`, then use the dedicated actions:

```text
POST /api/v1/sessions/{id}/start/
POST /api/v1/sessions/{id}/end/
```

Starting and ending a session records its timestamps. `duration_seconds` is calculated from those timestamps and is not stored separately.

## Testing and validation

Run the automated API tests (about 2,200 tests; nine minutes serially, two
to three with `--parallel 4`):

```sh
.venv/bin/python manage.py test core --parallel 4
```

[docs/TESTING.md](docs/TESTING.md) covers every suite across the three repositories --
the backend on SQLite and PostgreSQL, the contract checks, the web client's
unit tests, the web-to-backend integration suite, and the iOS tests -- and what
each one proves.

Check Django configuration and pending model changes:

```powershell
python manage.py check
python manage.py makemigrations --check --dry-run
```

Regenerate and validate the OpenAPI schema:

```powershell
python manage.py spectacular --file openapi.yaml --validate
```

## Configuration

Development defaults are included for local use. Production settings should be provided through environment variables. Copy [`.env.example`](.env.example) as a reference, but do not commit real secrets.

Important production values include:

- `DJANGO_SECRET_KEY`
- `DJANGO_DEBUG`
- `DJANGO_ALLOWED_HOSTS`
- `DJANGO_SECURE_SSL_REDIRECT`
- SMTP configuration

SQLite is appropriate for local development. A production deployment should use a managed database and HTTPS.

## Project documentation

- [User Guide](docs/USER_GUIDE.md) - plain-language walkthrough
- [API Guide](docs/API.md) - API integration details
- [Testing](docs/TESTING.md) - how to run every suite, and what each covers
- [Security](SECURITY.md) - audit findings, controls in place, remaining risks
- [Recommendations](docs/RECOMMENDATIONS.md) - how the For You page ranks, and how to tune and monitor it
- [Product Feature Summary](docs/PRODUCT_FEATURE_SUMMARY.md) - product scope and roadmap
- [Social release](docs/SOCIAL_RELEASE.md) and [privacy](docs/SOCIAL_PRIVACY.md) - staged rollout and data handling
- [Hosted configuration](docs/HOSTED_CONFIGURATION.md) and [deploy/README.md](deploy/README.md) - running it
- [OpenAPI specification](openapi.yaml) - machine-readable contract for the iOS and web clients

Everything else in [docs/](docs/) is a feature note or a testing handoff. The
PDFs in [docs/archive/](docs/archive/) are exports from the first version and
are older than the Markdown guides.
