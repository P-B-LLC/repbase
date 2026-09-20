# APNs rollout — not enabled or deployed yet

Scope: community inbox activity only. Workout, meal and planner reminders stay
on-device to avoid duplicate schedules. Live Activity remote updates are not
part of this transport.

1. Deploy the reviewed commit through `deploy.sh` (including requirements and
   migration 0059). Leave `APNS_ENABLED=false` until a signed app is tested.
2. Set these non-secret identifiers and a **path**, not the private key contents,
   in `/etc/repbase.env`:

   ```ini
   APNS_ENABLED=false
   APNS_TEAM_ID=UGTPKHHJ5F
   APNS_KEY_ID=MKU4LR59JT
   APNS_KEY_PATH=/etc/repbase/apns/AuthKey_MKU4LR59JT.p8
   APNS_TOPIC=com.pbllc.rytivo
   APNS_ENVIRONMENT=production
   ```

   The existing topic-specific key is production-only. Do not configure it for
   sandbox. Use a separate sandbox deployment/key for development delivery.
   Key permissions should remain root:django 0640, directory 0750. Never commit
   keys, device tokens, the environment file, or JWTs.
3. Install the service/timer from this directory into `/etc/systemd/system/`,
   run `systemctl daemon-reload`; enable the timer only at rollout. The unit
   uses the existing deployment account and environment file. Confirm both
   settings match the running web service before enabling.
4. Restart `repbase` after changing environment. Enabling requires
   `APNS_ENABLED=true` in **both web and worker processes**. Only notifications
   created after enablement are queued; there is no historical inbox replay.
5. Install TestFlight on a physical iPhone, allow alerts, sign in, and wait for
   community preferences to sync. Have a second test account follow/comment.
   Test background delivery, cold-launch tap to inbox, opt-out, blocked actors,
   logout, switching accounts, and denied iOS permission. Do not use real users
   for smoke-test notification traffic.

## Operations

`python manage.py dispatch_push --limit 100` runs one batch. The timer runs
another batch one minute after completion. Attempts stop after five or 24 hours
of notification age. Terminal rows are retained for seven days and cleaned by
the worker. Check `PushDelivery` status/last_error counts and the unit's failed
state; these do not expose content or credentials. HTTP403 means signing/
permission configuration must be checked; HTTP400 commonly means token/topic/
environment mismatch. A failed job is not silently replayed after correcting
configuration: generate a new test event.

Disable web `APNS_ENABLED` and stop `repbase-push.timer` / its service to halt
delivery. Notifications already accepted by Apple cannot be recalled.

## Delivery guarantees and remaining gates

The inbox row and outbox enqueue commit together. Registration generation
guards prevent queued alerts from being reused across accounts or preference
changes. Auth-token revocation deletes registrations and pending jobs. Read,
blocked, moderated, deleted and expired notifications are suppressed at send
time. Network failures retry with bounded backoff; stable collapse IDs reduce
duplicates, but APNs delivery is not exactly once and is not guaranteed.

Offline logout cannot notify the server immediately; already submitted alerts
also cannot be recalled. Alerts therefore contain generic text only—never names,
meal/health data, messages or photos. Account-bound tap routing must refuse the
wrong account. Native client generation/build and physical TestFlight delivery
remain required; Python tests cannot validate signing, entitlements or iOS UI.

## Validation checkpoint (2026-09-20)

- Local Windows/Python 3.12 isolated environment; no production database used.
- `manage.py test core --noinput --parallel 4`: 511 tests, OK, 9 skipped
  PostgreSQL-only tests. Latest push-specific rerun: 23 passed, 2 PostgreSQL-only
  tests skipped. The later rerun includes bad-token cleanup and disabled-account
  suppression added during final review.
- OpenAPI validation, migration drift check and Django system checks pass.
- Disabled worker invocation confirms no alerts sent.
- Initial broad discovery incorrectly included the PostgreSQL settings module;
  the rerun uses CI's `core` label. A Linux-only absolute-path test fixture was
  made platform-neutral without relaxing production path validation.
- PostgreSQL race execution, native compilation and live APNs delivery are not
  verified. Keep this branch out of production until those gates pass.

## Follow-up verification

- Verified frontend generated-client commit `59ab18a` on GitHub. The user
  completed the Mac validation script and reported simulator build/launch success.
- Ran backend commit `3ddf4914b86c57c9bef8360580687c74547ab004` against an
  isolated PostgreSQL 16 cluster on loopback port 55439: **56 tests passed,
  zero skips**, including registration/logout and competing-worker concurrency.
  The run also covered account cleanup, inbox notifications and password resets.
- `deploy/verify-push-postgres.sh` reproduces this test setup without sourcing
  production environment values or connecting to the managed database. Its
  temporary database stops on exit. Verification artifacts were retained at
  `/tmp/rytivo-push-verification.eqYH6E`; no live records were used.
- Live server remains at `d37b457`, clean checkout, service active, health HTTP200.
  No production migration, configuration change, worker activation or APNs send
  was performed.
- Deployment is blocked by `core.E006`: moderation is disabled and no provider
  API key is configured. The user confirmed that they have not obtained a
  moderation provider key yet. Disclosure confirmation is also false; review it
  before activation. The existing Apple `.p8` key is unrelated to these gates.
- Next: securely configure moderation and verify disclosures, rerun deployment
  checks, deploy with push disabled, then perform controlled TestFlight delivery
  testing before enabling community alerts. Do not acknowledge/bypass E006/E007.
