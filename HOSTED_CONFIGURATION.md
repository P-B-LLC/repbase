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
