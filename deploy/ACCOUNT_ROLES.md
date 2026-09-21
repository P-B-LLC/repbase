# Account roles

Implementation branch: `feature/account-roles`, based on `insights-cache`
(`464dc61`). The existing analytics/push migrations on that branch are prerequisites;
do not cherry-pick migrations 0061/0062 onto a database missing 0059/0060.

## Authority

| Role | Assign roles | Aggregate insights | Review reported posts/comments |
| --- | --- | --- | --- |
| Standard account | No | No | No |
| Analytics | No | Yes | No |
| Moderator | No | No | Yes |
| Owner | Analytics/Moderator on lower-level accounts | Yes | Yes |
| Superowner (one protected account) | All lower-level roles, including Owner | Yes | Yes |
| Active Django superuser | Owner bootstrap only before a Superowner exists; lower-level roles afterwards | Yes | Yes |

Multiple roles may be selected. These app roles never set `is_staff`,
`is_superuser`, Django groups, or model permissions. Ordinary staff status alone
does not grant any app role. No role grants access to another person's private
workout, food, weight, route, or calendar records.

Owner can change another active lower-level account, not an Owner, Superowner,
server superuser, or themself. Superowner can appoint and demote Owners but cannot
change its own protected role. Removing all assignable roles restores standard
access. No API, profile edit, or portal form can assign Superowner; even a Django
superuser cannot alter it through these endpoints. The database permits only one
Superowner, including inactive accounts. Recovery/transfer requires deliberate
server-operator work, not automatic fallback to a lower role.

The Superowner is app-level authority, not priority scheduling, unrestricted
access to private health records, or exemption from content-safety rules. A
server/database operator remains technically capable of out-of-band recovery.

The existing explicitly verified admin analytics grant remains valid until its
account first receives a versioned role record. From then on that role record is
authoritative: removing Analytics cannot silently fall back to the old grant.
Owner still inherits Analytics. The older analytics command updates both systems.

## Interfaces

- API: `GET /api/v1/me/access/`, paginated/search-required
  `GET /api/v1/administration/users/`, and
  `GET/PUT /api/v1/administration/users/{profile_id}/access/`.
- Report queue: `GET /api/v1/administration/reports/?kind=post|comment`.
- Decisions: `POST /api/v1/administration/reports/{kind}/{report_id}/decision/`.
  Only `hide` and `no_action`, with a required reason. No account deletion or
  general content-edit capability. Hiding settles open reports about that content.
- Web app: Account → Administration. It uses the authenticated API token.
- iOS: Settings → Roles & permissions opens `/access/` inside SFSafariViewController.
  This deliberately uses a separate 30-minute server session, not token injection
  or a token in a URL. Users explicitly sign out of administration separately.
- `/access/` is the mobile-friendly Django portal; `/insights/` remains the
  existing aggregate dashboard. Both independently authorize each request.

Role changes use a database transaction and the seeded `AccessPolicyLock` row.
The actor is re-read after locking. Expected `version` is required; a stale write
returns 409, not last-write-wins. Every effective change and moderation decision
is recorded in `AccessAudit`, readable by server superusers in Django admin but
not editable/deletable there. This is an application audit, not tamper-proof
external storage; a database operator can still change it.

## Deploy deliberately

1. Back up the database and deploy the backend including prerequisite analytics
   changes. Apply `python manage.py migrate` on PostgreSQL.
2. Keep secure cookies and the existing shared login-rate-limit configuration.
   `/access/` and `/insights/` fail closed in production without it. Apply the
   updated `rytivo-web/deploy/rytivo.app.nginx.conf`: `/access/` must proxy to
   Django rather than the React SPA; `/access/login/` must be rate limited.
   Check `nginx -t` before reloading.
3. The requested initial Superowner is `Rytivo_Official`, with email
   `admin@rytivo.app`. This is a deployment instruction, **not a default grant**
   based on a username/email. Confirm its auth-user ID against the live database
   and verify ownership independently. Then dry-run and execute:

   ```sh
   python manage.py grant_app_superowner --username Rytivo_Official --email admin@rytivo.app --user-id VERIFIED_ID --dry-run
   python manage.py grant_app_superowner --username Rytivo_Official --email admin@rytivo.app --user-id VERIFIED_ID --confirm-account-ownership
   ```

   Both require migrations 0061/0062 first. The command checks exact username,
   email, immutable ID, active status, app profile, and ambiguous identity
   matches. It never creates an account, changes credentials, sets Django
   staff/superuser flags, or replaces an existing different Superowner. Repeat
   runs for the same account are no-ops. The grant is audited.
   The legacy `grant_app_owner` command refuses to run once a Superowner exists.
4. Deploy the web production build, then verify Owner grant/revoke, Analytics
   dashboard access, Moderator queue, standard-account denials, and CSRF on the
   hosted HTTPS origin. Revocation must work in an already-open session.
5. Build the iOS branch on macOS and test the in-app portal, separate sign-in,
   light/dark appearance, and Done dismissal. Mac SSH was unavailable during
   implementation, so the iOS build/simulator check remains outstanding.

No production migrations, deployments, account promotions, or pushes were run.
The live server was inspected read-only: deployed commit `464dc61`, with no role
models yet. SSH as `django` works, but `/etc/repbase.env` is root-only and sudo
requires operator authorization. The live account ID/email match has therefore
**not** been verified and Superowner has **not** been assigned.

`core.test_access_concurrency` verifies PostgreSQL write/version conflicts,
revocation ordering, and simultaneous Superowner bootstraps. Run with
`deploy/verify-access-postgres.sh` from a disposable `/tmp` checkout; the script
clears inherited environment, never sources production secrets, starts its own
loopback database on port 55447, and stops only that test database afterwards.
