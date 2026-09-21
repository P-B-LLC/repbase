# Account roles

Implementation branch: `feature/account-roles`, based on `insights-cache`
(`464dc61`). The existing analytics/push migrations on that branch are prerequisites;
do not cherry-pick migration 0061 onto a database missing 0059/0060.

## Authority

| Role | Assign roles | Aggregate insights | Review reported posts/comments |
| --- | --- | --- | --- |
| Standard account | No | No | No |
| Analytics | No | Yes | No |
| Moderator | No | No | Yes |
| Owner | Yes | Yes | Yes |
| Active Django superuser | Yes | Yes | Yes |

Multiple roles may be selected. These app roles never set `is_staff`,
`is_superuser`, Django groups, or model permissions. Ordinary staff status alone
does not grant any app role. No role grants access to another person's private
workout, food, weight, route, or calendar records.

Owner can change another active, non-superuser account, not their own access.
Removing all roles restores standard access. Server superusers are changed only
by the operator. Because an Owner cannot demote themself, removing another Owner
cannot remove the last active Owner through this workflow.

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
3. Existing active server superusers can assign the first Owner. Alternatively,
   after independently verifying the exact account, the operator can run:

   ```sh
   python manage.py grant_app_owner --username EXACT_USERNAME --confirm-account-ownership
   ```

   This command never creates a user or grants Django staff/superuser rights.
   It records the bootstrap in the audit. Do not promote someone just because
   they registered an admin-looking email address.
4. Deploy the web production build, then verify Owner grant/revoke, Analytics
   dashboard access, Moderator queue, standard-account denials, and CSRF on the
   hosted HTTPS origin. Revocation must work in an already-open session.
5. Build the iOS branch on macOS and test the in-app portal, separate sign-in,
   light/dark appearance, and Done dismissal. Mac SSH was unavailable during
   implementation, so the iOS build/simulator check remains outstanding.

No production migrations, deployments, account promotions, or pushes were run
as part of this implementation. Backend tests use isolated SQLite test databases;
PostgreSQL concurrent-write/row-lock verification is still required before beta.
