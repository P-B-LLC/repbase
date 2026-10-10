# Testing

Three repositories, five suites. Every command below runs from a fresh clone
with nothing but the documented setup, and every one of them is what CI runs
or a superset of it.

| Suite | Where | What it proves | Command |
| --- | --- | --- | --- |
| Backend | `repbase` | The API, end to end: routing, auth, throttles, serializers, ORM, SQLite | `python manage.py test core` |
| Backend on PostgreSQL | `repbase` | The same suite on the database production runs | see below |
| Contract | `repbase` | `openapi.yaml` matches the serializers; every collection response matches the schema | `python manage.py spectacular --file /tmp/openapi.yaml` + diff |
| Web unit | `rytivo-web` | The client sends what it means and reads what it gets; impression batching | `npm test` |
| Web ↔ backend integration | `rytivo-web` | The real web client against a real server and database | `npm run test:integration` |
| iOS account safety | `IOS-Frontend` | Auth storage, save recovery, moderation consent, routing | `bash Scripts/test-account-safety.sh` |

## Backend

```sh
python3.14 -m venv .venv            # Python 3.14 is what CI uses; 3.12+ works
.venv/bin/pip install -r requirements-production.txt
.venv/bin/python manage.py test core
```

About three minutes for ~690 tests. Useful variations:

```sh
.venv/bin/python manage.py test core.test_recommendations      # one module
.venv/bin/python manage.py test core.test_security.MalformedInputTests
.venv/bin/python manage.py test core --parallel 4               # faster, same result
```

Seven tests skip on SQLite because they test PostgreSQL behaviour -- row
locks (`test_save_concurrency`) and the query planner (`test_identity_indexes`)
-- and run in the PostgreSQL job. One video test plays the cleaned files
through `ffmpeg` and skips where ffmpeg is not installed.

### On PostgreSQL

CI runs the whole suite a second time on PostgreSQL 17, because SQLite
forgives things PostgreSQL does not (see the comment in `.github/workflows/ci.yml`).
Locally, with any PostgreSQL 16+ listening on `127.0.0.1`:

```sh
DJANGO_SETTINGS_MODULE=config.test_postgres \
TEST_POSTGRES_PASSWORD=<password> TEST_POSTGRES_PORT=5432 \
.venv/bin/python manage.py test core
```

`config.test_postgres` only ever connects to loopback with its own database
name. Never point it, or any test setting, at a real database.

### Checks CI also runs

```sh
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python manage.py spectacular --file /tmp/openapi.yaml && diff openapi.yaml /tmp/openapi.yaml
```

After changing a serializer or a view's schema, regenerate the contract with
`python manage.py spectacular --file openapi.yaml`, then copy the change into
the clients (see "Contract changes" below).

## What the backend suite covers

The original suite (`core/tests.py` and the older `core/test_*.py` modules)
covers training, food, the planner, saves and their recovery, moderation, media
signing and deployment configuration. These modules were added with the For
You page, video uploads and the security audit:

| Module | Covers |
| --- | --- |
| `test_auth_flows` | Registration (validation, duplicates, weak and huge passwords, username and display-name rules), sign-in (no account oracle, suspended accounts), sign-out and token rotation, per-address and per-username limits, the spoofed `X-Forwarded-For` bypass, nothing secret in logs or responses |
| `test_content_creation` | Publishing each kind of post through the API, server-built snapshots, refusing other people's sources, every invalid field, editing and deleting only your own, photo uploads (type from bytes, location metadata removed, oversize, non-images, decompression bombs), comments and their limits |
| `test_video_uploads` | The MP4/QuickTime inspector against real encoder output and against forged durations, oversize pictures, box bombs, truncation and random damage; the upload endpoint (415/413/400/404/401/503), metadata removal, replacing and deleting files, Range requests, takedowns, review before publication, the moderator queue and its permissions |
| `test_recommendations` | Topic, scoring, ranking and cursor units; different people get different pages; the page responds to likes, follows, views and "not interested"; new accounts; every eligibility rule; paging; constant query count as content grows; impressions; the quality report; the populated page against the schema |
| `test_reporting` | Reporting posts end to end, privacy of report detail, limits, a moderator's takedown reaching every feed and the photo itself, suspensions hiding content |
| `test_security` | The staff-only member list, closed profiles' sub-lists, IDOR across every owner-scoped resource, malformed filters (no 500s), injection-shaped search text, security headers and CORS, CSRF for cookie sessions, error-report redaction, production-only settings |

Shared builders live in `core/fixtures_for_tests.py` (named so the test runner
skips it). Real video fixtures and how to regenerate them are in
`core/test_fixtures/video/`.

## Web

```sh
cd ../rytivo-web
npm ci
npm run lint && npm run build && npm test
```

The tests import the TypeScript sources directly and need Node 22.18 or newer
(type stripping on by default).

### Web ↔ backend integration

```sh
cd ../rytivo-web
npm run test:integration
```

Starts the backend from `../repbase` (or `RYTIVO_BACKEND_DIR`) with its
`.venv` (or `RYTIVO_BACKEND_PYTHON`) under `config.test_e2e`, on a throwaway
SQLite file and media directory in the system temp folder, then drives it with
the real web client: sign-up and sign-in, posting, cross-account refusals, a
video upload with byte-range playback, For You with dismissal and impressions,
paging, reporting and sign-out. It skips itself, saying why, when the backend
is not there. `config.test_e2e` refuses to start without its two environment
variables, so it cannot be pointed at the development database.

## iOS

```sh
cd ../IOS-Frontend
bash Scripts/test-account-safety.sh
```

Needs a Swift toolchain. Building the app itself needs Xcode 26.

## Contract changes

`openapi.yaml` is the source of truth for both clients:

1. Regenerate it here: `python manage.py spectacular --file openapi.yaml`.
2. Web: apply the same change to `rytivo-web/api/openapi.yaml` and run
   `npm run api:types`. The web copy can carry endpoints from other branches,
   so apply the change rather than copying the file over it — a unified diff
   of the old and new backend contract applies cleanly with `patch`.
3. iOS: copy it to `IOS-Frontend/API/openapi.yaml` and run
   `bash Scripts/generate-api-client.sh` on a Mac with Xcode, then build.

Additive changes -- a new endpoint, a new nullable response field -- do not
break a client generated from the previous contract: both clients ignore keys
they do not know. Adding a value to an enum that appears in a *response* does
break the generated Swift client, which decodes enums strictly; send new
values as plain strings instead, as `Post.kind` does.
