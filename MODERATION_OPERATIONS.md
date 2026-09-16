# Social moderation release gate

Apple guideline 1.2 requires filtering, reporting with timely action, user
blocking, and published contact information. This implementation supports those
requirements; neither the classifier nor passing tests guarantees App Review approval.

## Automated pre-publication checks

- OpenAI moderation endpoint, pinned omni-moderation-2024-09-26, text + images.
- Checked: registration display names/usernames, profile text/prompts/photos,
  post captions/photos/recipe instructions, copied snapshot names/titles, and
  comment/reply creation/edits, public gym details and normalized social handles.
  Report details intentionally bypass classification:
  users must be able to quote abuse to moderators.
- Sends an explicit text allowlist, never account email/password/token or numeric
  measurements. Unshared workout/food records are not sent. Photos are checked
  using a still, EXIF-stripped review copy; animated uploads are rejected when
  moderation is enabled. The moderation copy does not change existing stored-photo
  metadata policy. Review storage privacy separately.
- Flagged results reject publication with an appeal contact. Timeout, missing key,
  malformed response and rate-limit failures return generic 503, not approval.
  No request bodies, credentials or provider responses are logged by this adapter.
- All classifier-flagged categories are blocked in this initial beta policy. This
  can produce false positives, including discussion of recovery/self-harm. Review
  appeals compassionately; do not promise the classifier understands every context.
- The model has modality/category limitations. It is not a reliable detector of
  every image-text slur, dangerous health claim, spam, impersonation or illegal
  image. Human review, monitoring and evaluation remain necessary.

## Consent and secrets

- Updated iOS submits only after an explicit per-submission OpenAI permission
  prompt. Cancel stops the request. This is not persisted across accounts.
- MODERATION_API_KEY belongs in backend secret storage, never the app or Git.
- Production forces MODERATION_ENABLED=true and deployment checks reject missing
  credentials. MODERATION_DISCLOSURE_CONFIRMED defaults false and blocks transfer.
  Set it true ONLY after the updated app and public privacy pages are released
  together and permission/cancel behavior is verified on device. It is an operator
  gate, NOT recorded per-user consent or proof that old clients have consented.
- Do not enable the provider for legacy clients without the new permission flow.
  Before any external beta, enforce distribution of the updated build; if older
  live clients exist, implement a server-enforced minimum-version/consent protocol
  before activation. No personal data was sent to OpenAI during implementation.
- Review OpenAI data controls/retention and complete relevant Apple privacy
  declarations. Publish the regenerated Legal pages; local HTML is not a public URL.

## Human queues and response process

The safety contact is **support@rytivo.app**, which is what the app, the backend
default and the published legal documents all say. It has no mailbox behind it
yet: create one and confirm somebody reads it before beta, because an appeal
address nobody answers is worse than none. The owner must
confirm it is monitored and assign a primary moderator plus backup. No account,
mailbox or staff credential has been created by this implementation.

1. Use Django admin Post reports and Comment reports. Queues default to unresolved,
   oldest first. Each report is deduplicated per reporter and target. Reporter
   identity/detail is not returned publicly. Replies can be reported too.
2. For a genuine violation, hide the post/comment, review related content and
   repeated conduct, and suspend an abusive account through auth User.is_active
   when warranted. Token authentication must reject inactive users; verify this
   on the deployed service. Never give all moderators unrestricted superuser access.
3. Hidden comment threads and their notifications disappear; hidden/blocked original
   posts cannot resurface through reposts. Counts exclude hidden/blocked comments.
4. Report details are private. Do not copy harmful photos into email or logs.
   Suspected child exploitation or imminent threats need a qualified escalation
   process and jurisdiction-appropriate legal advice, not routine email forwarding.
5. Triage urgent safety reports as soon as possible. Target review of other reports
   within 24 hours. This is our operational target, not a quoted Apple deadline.
6. Run `python manage.py check_moderation_queue` hourly and alert an actual person
   on nonzero exit. It emits counts only and checks both report queues. Configure
   the scheduler/alert destination; writing a command does not run it automatically.
7. Monitor the support inbox for profile complaints and appeals. Comment/post
   reports can be submitted inside the updated app without an email application.
8. Reports linked to deleted content/accounts may cascade with those records.
   This is not an immutable legal evidence store. Establish an appropriate retention
   policy before promising one, and reconcile legal copy with that policy.

## Required before external beta

- Configure a restricted provider key, test actual allowed/flagged/timeout cases
  using controlled fixtures, check latency/rate limits and add availability alerts.
- Deploy migration 0054 after backup through the normal release job; deploy the
  matching regenerated iOS API client. No live database migration was run here.
- Validate permission decline/accept and retry UX on a physical device, including
  registration and profile-photo replacement. Inspect larger text and dark mode.
- Validate report submission/failure/retry, moderator takedown, block/unblock,
  notifications and repost visibility with separate accounts.
- Review existing unmoderated development content manually or start the beta with
  a clean, approved dataset. This adapter does not retroactively clear old content.
- Review future UGC entry points before exposing them broadly; this is not a
  universal content scanner for arbitrary database rows or linked external pages.
- Confirm a named human, monitored contact, queue schedule and urgent escalation
  process. These operational gates cannot be closed by code alone.

Sources checked during implementation:
[Apple review guidelines 1.2 and 5.1](https://developer.apple.com/app-store/review/guidelines/),
[OpenAI moderation](https://developers.openai.com/api/docs/guides/moderation),
[OpenAI data controls](https://developers.openai.com/api/docs/guides/your-data).

## Verification recorded 15 September 2026

- Final isolated SQLite backend suite: 398 tests, 391 passed, 7 skipped. Skips
  require PostgreSQL planner/row-lock behavior; they are not passes and must run
  against the hosted database configuration before release.
- Includes 18 moderation regression tests: rejected publication rollback,
  provider timeout/malformed results, no transfer without configured disclosure,
  text allowlisting, reporting deduplication, moderator permissions/takedown,
  blocked/hidden comment threads and counts, PATCH reply visibility and reposts.
- OpenAPI validated and matches the iOS contract; generated client compiled.
  Migration drift check reported no changes. The new migration was exercised by
  the test database, not applied to a live database.
- Updated iOS unsigned Simulator Release build succeeded on the validation Mac.
- No live provider request, production deployment, physical-device verification,
  mailbox monitoring or scheduled queue alert was performed by these tests.
