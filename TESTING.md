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

About three and a half minutes for ~960 tests (under a minute and a half with `--parallel 4`). Useful variations:

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
pip install pip-audit && pip-audit -r requirements-production.txt   # known vulnerabilities
```

After changing a serializer or a view's schema, regenerate the contract with
`python manage.py spectacular --file openapi.yaml`, then copy the change into
the clients (see "Contract changes" below).

## What the backend suite covers

### Friend-post like response regression (2026-10-28)

`core.test_post_likes` covers the HTTP-500 regression where `_card` readback
incorrectly required the viewer to own the liked post. The live beta was
still on `de27d1b`; the visibility-scoped correction from `42281fd` was already
in this branch/main but not deployed. Replacing the correction in memory with
the deployed query reproduces `Post.DoesNotExist` after the like is saved.
The new ten-test suite verifies actual 200 responses and saved counts,
idempotent likes/unlikes, follower permission, blocks, private/unpublished/
moderated/deleted content, anonymous requests, and privacy changing before
response readback. Keep these tests even after the isolated beta hotfix.

Do not test likes against real members' posts as an automated deployment
probe. Health 200 alone does not establish that an authenticated like works.

The original suite (`core/tests.py` and the older `core/test_*.py` modules)
covers training, food, the planner, saves and their recovery, moderation, media
signing and deployment configuration. These modules were added with the For
You page, video uploads and the security audit:

| Module | Covers |
| --- | --- |
| `test_auth_flows` | Registration (validation, duplicates, weak and huge passwords, username and display-name rules), sign-in (no account oracle, suspended accounts), sign-out and token rotation, per-address and per-username limits, the spoofed `X-Forwarded-For` bypass, nothing secret in logs or responses |
| `test_content_creation` | Publishing each kind of post through the API, server-built snapshots, refusing other people's sources, every invalid field, editing and deleting only your own, photo uploads (type from bytes, location metadata removed, oversize, non-images, decompression bombs), comments and their limits |
| `test_video_uploads` | The MP4/QuickTime inspector against real encoder output and against forged durations, oversize pictures, box bombs, truncation and random damage; the upload endpoint (415/413/400/404/401/503), metadata removal, replacing and deleting files, Range requests, takedowns, review before publication, the moderator queue and its permissions |
| `test_recommendations` | Topic, scoring, exploration, ranking, reason, experiment and cursor units; different people get different pages, and interest beats exploration across seeds; momentum, similar taste (never on one person's likes) and new creators; the next page follows what was just liked or dismissed; nothing already acted on comes back; new accounts; every eligibility rule and hidden topics; paging, retries and expired sessions; the reader's controls (interests, hiding topics, personalisation off, starting fresh); viewing-history retention and its deploy check; constant query count as content grows, for first and later pages; impressions and their recorded reasons; the quality report per reason, action, clip, experiment arm and return rate; the populated page and preferences against the schema |
| `test_suggestions` | People to follow: each source and its wording, the most personal reason winning, taste neighbours never suggested themselves, "followed by" counting only open profiles; everyone who must be left out (self, followed, requested, blocked, closed, suspended, quiet, dismissed); dismissing and undoing; personalisation off; bounded work; the response against the schema |
| `test_feed_integrity` | Whose engagement counts: suspended, brand-new, bursting and single-minded accounts and the author's own; comments, reposts and saves once per person, never hidden or from someone the author blocked; a like ring buying neither the page, the trend nor anybody's taste (and leading the page with trust off); the moderation cooldown for strangers but not followers, its end and its undoing, rejected clips; every moderation path (admin actions and change form, report queue, clip review, access portal) writing the same audit row and its undo, the record outliving the post, the cached list dropped only once a decision commits; bait and pitch captions and ordinary ones left alone |
| `test_recommendation_signals` | Value per view with saves and shares ahead of likes; other people's skip and not-interested rates, and nothing for a post with no record; rewatching; watching recorded only for real clips and never past their end; exercise interests from a known vocabulary, their reason and their place in the interests list; fatigue and who it never applies to; followed authors' share of the page; a wider spread for readers who said nothing yet; old cached rank states; the diversity section of `feed_report`; "more like this" and "fewer like this" (one choice per post, the reader's own posts refused, reposts meaning the original, fewer counted with the crowd protections, more never counted for anybody else); added interests (the offer, one chip per name, declared and shaping the page, the newest choice winning over hidden topics, kept after leaving the offer, surviving a fresh start) |
| `test_feed_stats` | The per-post counts job: it writes what the ranking would count, the ranking reads it in one query, later runs catch what changed, a run stopped half way leaves nothing behind, a lost record of the last run looks back further, a post deleted mid-count is left out (also with real commits), every row comes round in time whatever the window, stale and old-shaped rows are never read, the page is the same without the job, and `core.W009` |
| `test_ranking_model` | The learned part: AUC and a logistic fit that finds the signal; what each For You impression records (latest serving wins, the database keeps the first impression, real NULLs), deleted with the history; the gate (too little data, no better than the rules, a model that learned only the slot); one active model and pruning; a model names nobody; the training command and `feed_report` |
| `test_feed_evaluation` | A simulated community with known tastes, a like ring, bait, a moderated author and new creators, run through the real ranking before and after this ranking: integrity exposure falls, nothing manipulated reaches anybody's top three, utility (overall and cold-start) does not fall. `FEED_EVAL_REPORT=1` prints every figure; `FEED_EVAL_ABLATION=1` measures each new part; `FEED_EVAL_LEARNED=1` trains a model on the community's own record and measures the page with it |
| `test_reporting` | Reporting posts end to end, privacy of report detail, limits, a moderator's takedown reaching every feed and the photo itself, suspensions hiding content |
| `test_security` | The staff-only member list, closed profiles' sub-lists, IDOR across every owner-scoped resource, malformed filters (no 500s), injection-shaped search text, security headers and CORS, CSRF for cookie sessions, error-report redaction, production-only settings |
| `test_security_audit` | The October 2026 audit, one class per finding: planning horizons that one request could fill the database through, sign-in limits on Django's own pages (admin, browsable API, insights) and behind the proxy, per-account password-reset limits, no superuser bypass in the app API, ownerless gyms, gym members hiding closed profiles, the password needed to change email, bounded batches and numbers, long food searches, gunicorn listening on loopback |

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
more and fewer like this, added interests, paging, reporting and sign-out. It skips itself, saying why, when the backend
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
   `bash Scripts/generate-api-client.sh` on a Mac with Xcode, then build. Without
   a Mac, the iOS repository's "Generate API client artifact" workflow can be run
   on the branch; its artifact is the regenerated `API/GeneratedSources`.

Additive changes -- a new endpoint, a new nullable response field -- do not
break a client generated from the previous contract: both clients ignore keys
they do not know. Adding a value to an enum that appears in a *response* does
break the generated Swift client, which decodes enums strictly; send new
values as plain strings instead, as `Post.kind` does.

## Workout schedule and planner synchronization (2026-10-28)

The testing branch `work/testflight-calendar-sync-2026-10-28` adds migration
`0081_plannerentry_source_schedule`. It records private provenance only for
planner tasks newly created by `schedules/sync-planner/`; a manually authored
task adopted for the same workout/date is never claimed as generated data.
No API request/response fields change and no Swift client regeneration is needed.

Removing a schedule now removes its untouched, unfinished generated task in
the same transaction, including bulk clear, weekly-repeat cleanup, rotation
changes, and workout deletion. Completed records, manual/adopted tasks, and
generated tasks customized with details/steps are retained and detached.
Ending a rotation closes its rule and removes only its own future untrained
dates. Recorded/active sessions, past dates, other rotations, and manually
scheduled workouts remain intact. Training dates use the account's time zone.
Moving or replacing a scheduled workout updates its untouched generated task
in place. Renaming a workout updates untouched generated titles. Manual,
customized, and completed task details are never rewritten by those updates;
moving their former schedule detaches them and can offer a new task at the new
date. A task already authored at the destination is adopted without ownership.

Focused validation:

```sh
python manage.py test core.test_schedule_planner_sync
python manage.py makemigrations --check --dry-run
python manage.py check
```

Run the focused tests again with `config.test_postgres` for real concurrent
sync/removal validation; SQLite deliberately skips PostgreSQL row-lock tests.
The full `core` suite and schema comparison remain release requirements.

### Existing stale planner tasks need conservative review

This migration deliberately does not guess ownership for legacy tasks. Older
syncs created some tasks and adopted others, but stored no way to distinguish
them. Already orphaned tasks may therefore remain after upgrading, as may
customized reminders that the user explicitly changed. Do not bulk-delete all
workout-category tasks or infer generated ownership from a matching title.

For an affected account, review the exact unfinished stale task IDs with the
user, then remove only the confirmed obsolete tasks through the authenticated
planner UI/API. Preserve completed records and unrelated/custom reminders.
No production data repair, deployment, or merge is performed by this change.

## Pillow security update (2026-10-28)

The same testing branch pins Pillow 13.0.0 instead of 12.3.0. Production inherits
this pin through `requirements-production.txt`; no audit exceptions, API changes,
or changes to image-validation limits are included. This upgrade addresses the
dependency-audit findings against the old pin. See the
[upstream release notes](https://pillow.readthedocs.io/en/stable/releasenotes/13.0.0.html).

Validate the installed version and dependency graph, then run the existing
image-path regressions (synthetic media and mocked providers, no live screening):

```sh
python -c "import PIL; print(PIL.__version__)"
pip check
pip-audit -r requirements-production.txt
python manage.py test core.test_content_creation.PostPhotoUploadTests core.tests.FeedSizedPhotoTests core.tests.PostsCarryBothPhotoSizesTests core.test_moderation_hardening core.test_moderation_safety core.test_openai_media_moderation core.test_moderation_frames core.test_media_real_frames core.test_media_provider_command core.test_beta_publication --noinput
```

The real frame tests need both FFmpeg and ffprobe; report their skips explicitly
if unavailable. Full SQLite/PostgreSQL CI must pass on the new head before
promotion. The testing-branch update does not upgrade a deployed server.

Local validation on Python 3.14.3 / Pillow 13.0.0: the focused modules above
discovered 99 tests across two runs (96 passed, three real-FFmpeg tests skipped
because FFmpeg/ffprobe were not installed). Dependency compatibility, Django
system checks, and the production requirements security audit passed with no
known vulnerabilities reported. Full CI evidence belongs to the exact PR head.

## Own publications in For You (2026-10-28)

The testing branch includes the reader's eligible published posts in For You.
The newest eligible own post from the last 24 hours leads a fresh first page;
it occupies a normal ranked slot and remains de-duplicated through cursor
paging. The additional own-post candidate source is capped at 20. Private,
unpublished, rejected, hidden, child-held and unapproved-video content remain
excluded; no moderator or visibility rule is bypassed. Own impressions still
do not affect recommendation learning. No migrations or API fields change.

```sh
python manage.py test core.test_recommendations core.test_recommendation_signals core.test_feed_integrity core.test_feed_stats core.test_ranking_model
```

`OwnPublicationTests` covers first-page confirmation and refresh, pending to
approved transitions, privacy/publication gates, takedowns after ranking,
pagination without duplicates or mid-session insertions, bounded candidates,
and expiry of the reserved slot with normal author spacing. Client QA must
also verify immediate insertion after a confirmed publish and that an older
in-flight feed response cannot erase the new card. Full CI on SQLite and
PostgreSQL remains required before promotion; this does not deploy the change.

Local Windows validation: 243 recommendation/integrity/stats/model tests passed,
then 31 own-publication/eligibility/paging tests passed after the final cached-page
visibility checks. Schema generation is semantically unchanged; system checks,
migration checks and diff hygiene passed. The full 1,268-test attempt did not
produce a passing result: the existing production-settings test uses the POSIX
path `/persistent/media`, which fails `Path.is_absolute()` on Windows, and the
parallel runner then failed to pickle that traceback. Linux SQLite/PostgreSQL CI
must supply the full-suite result. Temporary test databases were removed by the
runner.
