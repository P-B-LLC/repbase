# Private Rytivo insights

Backend-rendered dashboard: `https://rytivo.app/insights/`, on the web app's
existing origin. The web Account page links here for the designated email;
that link is discoverability only, never authorization. A separate short-lived
administrator session is required even if the web app's API token is signed in.
The dashboard links back to `/account`. No iOS release is required.
This is an initial aggregate usage/reliability dashboard, not crash
reporting or a complete product-analytics platform.

## Access and verification

Every request requires an active staff account whose email is exactly
`admin@rytivo.app` (case-insensitive), plus an enabled AnalyticsAccess grant.
Even other superusers are denied. Public registration does NOT create a grant.
Login uses the account's username/password and expires after 30 minutes.
Logout is POST-only and CSRF protected. Do not share this account.

The application does not yet verify mailbox ownership automatically. A trusted
operator must independently verify ownership and the intended account ID before
running this command in the deployed application environment:

```sh
python manage.py grant_analytics_access --user-id ACCOUNT_ID --confirm-email-ownership
```

The confirmation flag records an operator attestation; it is not email delivery
or verification. The command refuses ambiguous duplicate email accounts and
does not create accounts, passwords, or superuser permissions. It sets staff
status; review any other application capabilities associated with staff first.

Revoke immediately (existing dashboard sessions are checked on every request):

```sh
python manage.py grant_analytics_access --user-id ACCOUNT_ID --revoke
```

Revocation intentionally does not remove staff status or unrelated permissions.

## Rollout checklist (not yet performed)

1. Review and deploy this branch through the existing release checks. Do not
   bypass existing moderation or other deployment gates. Back up the database;
   apply migrations including 0060 and its dependencies with `manage.py migrate`.
2. Route `/insights/` and its children to Django, not the public site's SPA
   fallback. Require HTTPS. Preserve the host and trusted proxy scheme headers;
   never trust arbitrary client-supplied forwarded headers. Disable proxy/CDN
   caching for this path. Verify secure session/CSRF cookies and CSRF origin
   configuration. The dashboard fails closed with insecure production cookies.
   The web repository's `deploy/rytivo.app.nginx.conf` includes these locations
   and a shared login POST limiter. Review it against the installed config,
   validate with `nginx -t`, and reload only after successful validation. Build
   and deploy the web client to expose its Account-page link. Do not overwrite
   unrelated live nginx configuration blindly. Test login POSTs across workers
   and direct deep links before confirming the shared limiter flag.
3. **Before public exposure, enforce a shared login rate limit at the reverse
   proxy or configure a shared atomic cache backend.** The built-in default
   local-memory cache limits only one worker. The application adds a five-attempt
   /15-minute per-source limit, but this alone is not a distributed defense.
   `REMOTE_ADDR` behind a proxy may group all users together; do not fix this by
   blindly trusting X-Forwarded-For. Prefer restricting this admin route to a VPN
   or operator IP as defense in depth. MFA is not implemented here.
   Only after verifying the shared limit across workers, set
   `ANALYTICS_LOGIN_SHARED_LIMIT_CONFIRMED=true`. Production insights returns
   503 until this operator confirmation is configured; it does not configure
   a proxy or cache automatically.
4. Verify the mailbox/account and explicitly grant access as above. Check an
   ordinary account receives 403, anonymous visitors are redirected to login,
   and revoking the grant takes effect without waiting for logout.
5. Review the privacy disclosure and internal purpose/retention before enabling
   `ANALYTICS_ENABLED=true` in the backend environment and restarting workers.
   It defaults to false. No third-party analytics vendor receives data.
6. Schedule `python manage.py prune_expired_rows` daily in the established job
   runner (it now invokes analytics cleanup), or run `prune_analytics` separately.
   Both support `--dry-run`. Alert on cleanup failures. Retention is 30 UTC days
   for account presence, 90 for route aggregates, and 3 for the five-minute
   live buckets; backups need their own retention policy. No operational
   meals/tasks/workouts are deleted.
7. Schedule `python manage.py check_api_health` every five minutes to get an
   email when a route starts failing or turns slow, and another when it
   recovers. Without it the dashboard only reports a problem while somebody
   happens to be looking at it, which at 3am is nobody.

   ```cron
   */5 * * * * cd /home/django/app && set -a && . /etc/repbase.env && set +a && PYTHONDONTWRITEBYTECODE=1 .venv/bin/python manage.py check_api_health >> /var/log/repbase-alerts.log 2>&1
   ```

   Sourcing the environment is not optional. cron runs with almost none of
   it, and without `DATABASE_URL` the settings fall back to a local SQLite
   file -- so the check would run, find an empty database, report everything
   healthy, and never mention that it was looking at the wrong one.
   `PYTHONDONTWRITEBYTECODE=1` keeps a root-run job from leaving `.pyc` files
   the application user cannot rewrite.

   It looks at the last 15 minutes, wider than its own schedule so a blip
   between two runs is still caught by one of them. It is edge-triggered: one
   email when an alert opens and one when it closes, never a repeat every run,
   because a repeating alert is one people filter. `--dry-run` reports what it
   would do and writes nothing.

   Recipients default to the approved insights accounts. Set
   `ANALYTICS_ALERT_EMAILS` to a comma-separated list for a pager or shared
   address instead. With no recipients the alert is still recorded, so a
   missing address loses the email and not the incident.

   It reports nothing while `ANALYTICS_ENABLED` is false: silence from a
   system recording nothing would read as an all-clear.
8. Smoke-test known successful/rejected requests in staging and compare counters.
   Measure API latency under expected concurrency before enabling production
   collection: recording currently performs synchronous, atomic database writes
   and popular routes share a daily counter row. Database recording failures
   preserve the application response but may lose observations. Monitor the
   generic analytics-write warning. Collection is not billing-grade telemetry.

## Metric definitions

- API-active accounts: distinct non-staff accounts with a successful API request
  in the chosen window. Includes background sync; NOT app opens or true DAU.
- Requests/4xx/5xx: observations of `/api/v1/` requests, grouped by fixed route
  name and method. Includes anonymous traffic. No raw URLs, IDs, query strings,
  bodies, tokens, IPs, user agents, meal contents, or health measurements are
  stored in these route aggregates. Login throttling stores a short-lived keyed
  hash of the connection source in the cache.
- Response time: weighted mean application processing milliseconds before
  analytics writes. Excludes network transit and is not p95/p99.
- Signups/workout completions/task completions/meals with entries: aggregate
  queries over current operational records, excluding staff; workout totals
  exclude HealthKit imports. These are mutable snapshots, not immutable events.
- Account-day presence is linked to the account and cascades on account deletion.
  Treat it as personal data even though the dashboard only shows counts.
- No historical API data is invented. No-data periods are not proof of no users.
  Screen funnels, client save failures, crash-free sessions, notification delivery,
  and cohort retention need further instrumentation and privacy review.

## Local verification

```sh
python manage.py test core.test_analytics --noinput
python manage.py makemigrations --check --dry-run
python manage.py check
```

`render_insights_preview --output NEW_FILE.html` generates clearly labelled,
synthetic data without reading customer data. It is a static design preview,
not an authenticated dashboard; its navigation/login controls are not functional.
