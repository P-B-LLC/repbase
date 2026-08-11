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
- Interactive OpenAPI documentation

## Technology

- Python 3.12+
- Django 6.1
- Django REST Framework 3.18
- SQLite for local development
- drf-spectacular for OpenAPI generation

## Local setup

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

Run the automated API tests:

```powershell
python manage.py test
```

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

- [User Guide](USER_GUIDE.md) - plain-language walkthrough
- [API Guide](API.md) - API integration details
- [Product Feature Summary](PRODUCT_FEATURE_SUMMARY.md) - product scope and roadmap
- [OpenAPI specification](openapi.yaml) - machine-readable contract for the iOS client

PDF versions of the user, API, and product guides are included for convenient sharing.
