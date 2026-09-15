# Hosted backend preparation (item 3)

This prepares the code; it does not provision hosting, migrate existing data,
or certify beta readiness. The current SQLite file and media are unchanged.

## Configuration boundaries

- Ordinary `python manage.py ...` retains local SQLite when DATABASE_URL is
  absent. Setting DATABASE_URL opts into PostgreSQL. Never set a real hosted
  database URL for automated tests.
- Hosted commands below always select `config.production`. Set DJANGO_DEBUG=false
  explicitly. Missing database, weak secret, wildcard hosts, insecure cache,
  and missing absolute media path fail at settings load.
- `.env.example` lists variable names and placeholders, not usable credentials.
  The app does not load .env automatically. Configure the host's secret manager.
- PostgreSQL URLs support percent-encoded credentials and only sslmode and
  sslrootcert query parameters. Other options are explicit environment variables.
  Production requires verify-full (encryption + hostname verification); obtain
  the provider CA or use its supported system trust configuration. Do not
  bypass certificate validation to make a deployment connect.
- Redis is shared across workers for throttles and cache, with authenticated
  TLS, finite socket timeouts and a per-environment key prefix. DRF throttles
  still are not a strict atomic security perimeter: add ingress abuse controls.
- Database connection reuse defaults to 60 seconds. Transaction pooling disables
  persistent connections, server-side cursors and prepared statements. Confirm
  the provider supports connection startup options/timeouts; for incompatible
  poolers use its direct endpoint or a provider-specific reviewed adapter.
- Budget connections for workers, deployments overlapping, commands and admin
  access. Start with two synchronous web workers, then measure before scaling.

## Reproducible deployment (Linux host, Python matching CI)

1. Install `pip install -r requirements-production.txt`.
2. Inject production secrets/settings and mount persistent media at DJANGO_MEDIA_ROOT.
   An absolute path is not proof of persistence; verify the host retains the
   mounted volume across deploys. Object storage needs the exact-name adapter
   contract in core/media_storage.py, not an arbitrary storage substitution.
3. Run `python -m config.deploy check`. This checks Django deployment settings
   and database/cache connectivity without migrating or importing user data.
4. Run `python -m config.deploy release` ONCE as a serialized release job after
   a backup. It migrates and collects admin static assets. Use a direct database
   endpoint and a migration-role credential here if the runtime role lacks DDL.
   Large migrations may require a separately approved larger statement timeout.
5. Start `python -m config.deploy serve`. It refuses unapplied migrations and
   starts Gunicorn; web workers never run migrations automatically.

The release command changes the configured database. Do not run it on existing
user data before the migration/backup plan is approved. No commands in this
implementation have targeted the existing Mac database.

WhiteNoise serves collected admin static assets. Uploaded media is never put
in the public static tree; signed media handling remains unchanged. Retain
STATIC_ROOT output in the deployed image/artifact (or run collectstatic during
the image build with the required build-time configuration).

## HTTPS / health probes

- TLS terminates at the provider ingress. Block direct public access to the
  application port. Only then set DJANGO_TRUST_PROXY_HTTPS=true and configure
  ingress to replace, not forward, untrusted X-Forwarded-Proto.
- `/health/live/` is process liveness only; no dependency access.
- `/health/ready/` checks SELECT 1 and a short-lived cache round trip. It returns
  generic 503 on failure, never a DSN or exception text. GET and HEAD only;
  responses are not cacheable. Use a reasonable probe interval (e.g. 30s).
- HTTPS redirection and ALLOWED_HOSTS still apply to probes. Configure probes
  through HTTPS ingress or with the explicitly trusted headers/host; don't
  disable HTTPS globally to accommodate a probe.
- Gunicorn bounds worker lifetime and request/graceful timeouts. Access logs
  omit query strings, authorization headers and bodies. Review host-level logs
  too, especially signed photo URLs. Add provider alerts before beta.

## Verification and remaining provider work

- `python manage.py test core.test_hosted_configuration`: parser rejection,
  secret redaction, pooling settings, health dependency failures and startup flow.
- `python manage.py test core`: local regression suite.
- CI's disposable PostgreSQL service uses the same database parser and runs
  the entire backend suite. It does not connect to an actual hosted database.
- After selecting hosting: verify PostgreSQL/Redis TLS, role permissions,
  actual connectivity, concurrency, connection budget, static assets, and an
  upload across a restart. Run a signed iOS build against the deployed HTTPS API.
- Configure SMTP and verify delivery; schedule prune_expired_rows and
  retry_media_deletions; implement backups and prove a restore. These are
  separate deployment gates, not completed by adding settings.
- Rehearse the SQLite-to-PostgreSQL data move, preserve IDs and reset sequences,
  validate constraints/counts, plan write downtime and rollback. `migrate`
  creates schema; it does NOT import SQLite data.

References: [Django database pooling](https://docs.djangoproject.com/en/dev/ref/databases/),
[Gunicorn settings](https://gunicorn.org/reference/settings/).

## Verification recorded during implementation

Isolated Mac validation, September 15, 2026 (no existing database or media used):

- Full SQLite suite: 355 tests ran, 352 passed, 3 PostgreSQL-only skipped.
- Final focused configuration suite: 18 tests passed (includes two additional
  production-module subprocess checks added after the full-suite run).
- Production dependencies installed; Gunicorn configuration validated.
- Production deployment checks passed at ERROR level using test-only settings.
  HSTS include-subdomains/preload warnings are intentional until domain ownership
  and all subdomains are verified; neither is enabled blindly.
- Static collection succeeded: 158 assets, 456 post-processed outputs.
- No migration drift; generated OpenAPI matches the committed schema.
- PostgreSQL CI configuration is updated, but no PostgreSQL server is available
  in this validation environment. Its actual CI run and hosted TLS/connectivity
  checks remain open. No signed app, deployment, or database cutover is claimed.

## Photos

Media is the only thing here a deployment cannot recreate. The database can be
migrated and the code redeployed; a lost photo is gone.

**Where it lives.** A mounted volume at `DJANGO_MEDIA_ROOT`, today. That is a
real answer and it has a real limit worth naming rather than discovering: a
volume attaches to one machine, so it survives a deploy only while the
provider keeps giving you the same machine, and `WEB_CONCURRENCY` spanning two
hosts would split it. Object storage is the endpoint. What stands between here
and there is not a rewrite — everything already reads and writes through
`default_storage` — but an adapter satisfying the reserved-name contract in
`core/media_storage.py`, because `django-storages` renames on collision and a
renamed file is one no row can find.

**Privacy does not change with storage.** Photos are served by `core.media`
behind an HMAC signature in every configuration. The bucket or directory stays
private and no public URL is ever handed out. A storage swap that made objects
publicly readable would defeat the signature entirely, so it is the one thing
to check on any adapter.

**Moving and backing up are the same command**, which is deliberate: a backup
nobody can restore is not a backup, and restoring is this pointed the other
way.

```bash
python manage.py transfer_media --audit                 # compare rows to storage
python manage.py transfer_media --to-path /backups/media
python manage.py transfer_media --to-alias objectstore  # once an alias exists
```

It copies what rows point at rather than what the directory holds, writes
through the destination's reserved-name contract so every file arrives under
the name its row refers to, verifies size, and is idempotent — an interrupted
run is resumed by running it again. It refuses to copy while a referenced file
is missing, because carrying that gap forward makes the old storage, which
still holds the evidence, the thing you delete.

Verified against the live media on 2026-09-15: 15 referenced files, 21 MB,
copied and re-verified, and a signed URL generated before the move serves
correctly from the copy with `DEBUG=false` — same name, same signature,
different storage. Three orphans were reported and deliberately not copied.

**Orphans are reported, never deleted.** Those three are files no row points
at, left by uploads that failed before the cleanup machinery existed. The
audit names them; removing them is a human decision, because a tool that
deletes files it believes unreferenced is one bad query away from deleting
photos.

**Cleanup runs on a clock.** `python -m config.deploy maintenance`, daily: it
prunes expired rows, retries media deletions that failed in storage, and
audits media against the database. One entry rather than a list a cron can get
half right.
