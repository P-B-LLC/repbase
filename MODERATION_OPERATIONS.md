# Social moderation release gate

## Main promotion — 17 October 2026

- Full PR validation exposed and fixed the post-card response scope for likes/reposts
  and a publication timestamp incorrectly editable/required in the admin change form.
  Required CI must pass on the corrected head before promotion is complete.

- User approved promotion after reporting the updated agreement flow works.
- Production read-only verification: de27d1b deployed; repbase and the screening
  timer active. This promotion does not redeploy production or alter settings.
- Fresh merge checks: 51 agreement/screening/publication/dashboard tests passed;
  migration drift check passed. Earlier broader validation remains documented below.
- Includes migration 0080, signup/sign-in agreement records, protected moderator
  dashboard, screening workers and publication controls. Historical WIP notes
  below describe development checkpoints, not current branch availability.

## Latest WIP — signup/sign-in agreements (17 October 2026)

- User chose agreement acceptance at signup, and once after sign-in for existing
  accounts, instead of a separate pre-post OpenAI dialog. Disclosure remains in
  the Privacy Policy. No existing account is backfilled as having accepted.
- Migration 0080 stores immutable per-user/version acceptance with a timestamp.
  Registration accepts optional `agreement_version` for legacy compatibility;
  the new clients require a checkbox and send the current version. Acceptance
  and the initial screening preference are saved in the signup transaction.
- GET/PUT `/api/v1/me/agreements/` lets authenticated clients read/accept the
  current version. Anonymous writes, stale versions, and session writes without
  CSRF fail. Repeated acceptance does not reverse a subsequent withdrawal.
- Tests: 65 selected consent/moderation tests passed; after two extra isolation/
  rollback tests, the final 51-test agreement/publication/dashboard run passed.
  Migration drift check passed. Web build and 51 tests passed. These are local
  tests, not new production or Xcode validation.
- Deploy backend and migration 0080 BEFORE the new clients. New clients fail
  closed at the agreement screen if the endpoint is missing/unreachable. iOS
  must regenerate Swift and run `Scripts/validate-moderation-ios.sh` on Mac.
  The previous Mac success does not validate this new contract. Public web
  `/legal/*.html` files must be deployed with the bundle.
- Screening remains enabled in production from the prior release; this WIP
  does not disable checks or change the provider. This revision is not deployed.

## Prepared beta deployment — 17 October 2026

User selected the existing beta backend. `deploy/deploy-moderation-beta.sh`
requires root and an exact full SHA matching the current WIP branch head, stops
on dirty checkout or enabled screening, creates a database backup using the
verified server backup utility (version-matched pg_dump), then invokes the
candidate's deploy script and checks health plus anonymous dashboard redirect.
It does not install the screening worker or change moderation configuration.
No check acknowledgement is supplied automatically. If core.E006 blocks this
release, obtain explicit acknowledgement for this deployment before retrying.
The server was read-only checked at e819ef6, clean and active. No deployment
has been executed by this preparation. Migrations 0076–0078 are additive except
index replacement; review traffic/lock impact during the deployment window.
Screening-disabled means the new media queue can be empty; do not seed fake
flagged content into real users' feeds. The older report workflow remains usable.
Consent version 2026-10-16 requires compatible clients; inspect any server
MODERATION_CONSENT_VERSION override before later enabling the provider flow.

## Dashboard acceptance continuation — 17 October 2026

- Mac evidence: the user completed generation, account-safety tests and the
  simulator build, then pushed generated `Types.swift` as iOS commit `cbf6328`.
  That commit has been fetched and fast-forwarded into the local iOS checkout.
- Added `core/test_media_dashboard.py`: anonymous/disabled access, CSRF, invalid
  decisions/reasons, duplicate rejection/audit/notification behavior, conflicting
  decisions, own-post review prevention, private revision-bound image preview,
  safety holds, queue filtering/pagination and caption escaping.
- Broader validation found an actual takedown regression: the author-only status
  exception allowed hidden posts in detail/profile results. `core/views.py` now
  requires `is_hidden=False` for that exception. The 88-test local report/access/
  publication/dashboard/push run passes with the fix. Web suite: 49 passing tests.
- Isolated runner now includes 219 selected tests with real PostgreSQL/FFmpeg.
  Final completed run: 218 passed, 1 skipped (explicit nginx delivery drill),
  zero failures. Test database destroyed and isolated PostgreSQL stopped.
  Prior attempts stopped for a streaming-test double-close, missing schema in
  the transfer bundle and an SSH disconnect; those are not passing full runs.
  Test fixtures use a fast hasher only in `config.test_postgres`, never production.

### Manual iOS/web staging walkthrough still required

1. Deploy the matching WIP backend to an explicitly approved test environment,
   with synthetic accounts/media, no private user uploads. Keep live screening
   disabled unless credentials/disclosure and provider checks are ready.
2. Launch the validated iOS build, sign in as an authorized moderator or owner,
   then Profile → Settings → Moderation dashboard. This opens a mobile web
   dashboard in SFSafariViewController, with its own secure session sign-in.
   Web entry: Account → Moderation → Open moderation dashboard.
3. Confirm ordinary and analytics-only users cannot enter; revoke a moderator
   while their page is open and verify the next decision is refused.
4. Review synthetic image/video, caption and shared workout/meal/task details;
   test all four queue tabs, empty states, pagination and mobile scrolling.
5. Approve and verify publication once; reject and verify author inbox wording
   without reviewer identity/notes. Check stale tabs, repeat submissions,
   own-post review, safety holds and hidden-content protections.
6. Confirm simulator/device video playback and real push delivery separately.
   Automated database/outbox tests do not prove APNs delivery. Live OpenAI
   connectivity/accuracy and installed reverse-proxy delivery remain separate gates.

No staging URL is currently recorded and no production deployment was performed.

## WIP branch handoff — 16 October 2026

The user authorized committing and pushing the current work branches for Mac
validation, not merging to main or deploying. Backend branch:
`work/moderation-fresh-start-2026-10-15`; iOS and web branches:
`work/openai-moderation-ui-2026-10-16`. The earlier local-only statements below
describe the validation checkpoints before this handoff.

On a clean Mac iOS checkout, fetch origin, switch to the iOS work branch, pull
with `--ff-only`, then run `bash Scripts/validate-moderation-ios.sh`.
Generated Swift files intentionally remain outstanding in this WIP snapshot;
the Mac must regenerate them before building. Review and commit the generated
diff and any build fixes on the same work branch. Do not reset local Mac edits.
No iOS build success or live OpenAI accuracy validation is claimed. Keep
`MEDIA_SCREENING_ENABLED=false` until the remaining release gates pass.

## OpenAI-only asynchronous media replacement — WIP, 16 October 2026

Branch `work/moderation-fresh-start-2026-10-15`. Not deploy-ready. New upload
integration is behind `MEDIA_SCREENING_ENABLED=false` by default. No production configuration
changed, no external API tests or user-content transmissions performed.

Implemented in this iteration:

- `core/openai_media_moderation.py`: exact `omni-moderation-latest`, private image
  bytes, existing disclosure/key/timeout settings, explicit consent and server
  child-safety hold guard; decisions use only sexual score with a caller-supplied
  threshold. Strict finite-score/image-coverage validation; bounded responses;
  redirects prohibited; 429 retry hints retained; network failures never approve.
- `core/moderation_frames.py`: FFmpeg first-frame/interval/scene-change sampling,
  one-second default interval, 768-pixel bounding box, single-thread filters,
  private temporary directory and frame-count/size/runtime bounds. Too many frames
  fail rather than silently accepting partial coverage. Sampling cannot guarantee
  detection of brief content between samples; audio is not moderated.
- `core/media_publication.py` and migration `0076_media_publication_status`:
  pending/flagged/approved/rejected status, generation tokens, `published_at`,
  `reviewed_at`, explicit child-safety hold; locked shared approval/rejection path
  and audit entries. Idempotent approval preserves its first publication timestamp.
  Existing timestamps backfilled from `created_at`; no old posts made fresh.
- `core/media_screening_jobs.py`, management command `process_media_screening`,
  and migration 0078: durable jobs, leased claims, five-attempt bounded retry with
  backoff/Retry-After, generation checks, persisted consent version and threshold,
  safety recheck before each provider call. Only aggregate score/model evidence is
  stored. Provider calls and frame extraction are outside database transactions.
- Feature-gated integration: image creation, caption edits on media posts, legacy
  video attach/replace, local/direct pipeline begin and transcode completion.
  Videos wait for transcoding before screening. Beta video bypass cannot approve
  this flow. Video removal invalidates stale jobs/review forms without approval;
  a removed-media post held for review remains private for a moderator's decision.
- `core/media_review_portal.py`, `access/media_review.html`: existing reports
  dashboard gains a Media screening tab, full shared snapshot details, private
  session-authorized previews, generation-bound Approve/Reject, confidential notes.
  Child-held media has no preview/release here. Rejection writes one inbox notice
  and uses the existing APNs outbox; reviewer identity and notes are not disclosed.
- `published_at` now drives Post ordering/indexes (0077), feed pagination,
  recommendation windows/age, repost history and feed-stat refresh. User and
  interaction dates remain unchanged. Candidate.created_at is an internal legacy
  name now populated from published_at. API schema includes publication/status
  fields and a notification message; created_at remains the upload date.
- Shared visibility and signed-media guards exclude nonapproved/child-held posts.
  This does NOT establish that all production delivery paths have been audited.
  Tests use mocked provider results; no live model accuracy claim is supported.

Latest continuation checkpoint (16 October 2026):

Dashboard continuation:

- Shared responsive `/access/reports/?kind=media` workspace now provides flagged,
  pending, approved and rejected queues with counts, oldest-first pagination,
  private previews, screening evidence, confidential decision reasons and read-only
  completed decisions. Own-post review controls are hidden; server checks remain authoritative.
- iOS Settings uses the generated `myAccessRetrieve` contract to show a moderation
  entry only when `capabilities.moderate` is true. It opens SFSafariViewController;
  this is an embedded web dashboard, not a separate native review implementation.
  Web Account uses the same capability to link to the dashboard. Both require
  dashboard session sign-in; no app token is handed to the browser.
- Authorization is checked on every dashboard/preview/decision request. Standard
  and analytics-only accounts are denied; moderators, owners and superowners have
  moderation access, as do active Django superusers under the existing policy.
  Revocation applies immediately at the server, even if a client button is stale.
- Fourteen publication/dashboard tests pass, including populated queue rendering,
  the role matrix, approval/history and revocation. Web build and 47 tests pass.
- New Mac runner: `bash Scripts/validate-moderation-ios.sh` in the iOS repository.
  It regenerates the client, runs account-safety tests and builds for the simulator.
  NOT executed here: Windows has no Xcode and the Mac connection is unavailable.
  Generated bindings are still outstanding; do not describe the iOS build as passing.

- Preserved all backend changes on `work/moderation-fresh-start-2026-10-15`.
  iOS and web UI changes are on `work/openai-moderation-ui-2026-10-16`
  in their respective repositories. No commits, pushes or production deployment.
- Web API bindings regenerated; production build and all 47 web tests passed.
  Pending/rejected status, publication dates and explicit media disclosure are wired.
- iOS source integration includes pending/rejected cards, feed suppression,
  publication dates, video consent and rejection notification routing. Generated
  Swift bindings and Xcode compilation remain outstanding; Mac SSH is unavailable.
  From the updated iOS repository on macOS run:
  `bash Scripts/generate-api-client.sh`, `bash Scripts/test-account-safety.sh`, then
  `xcodebuild -project "IOS Frontend/IOS Frontend.xcodeproj" -scheme "IOS Frontend" -destination 'generic/platform=iOS Simulator' CODE_SIGNING_ALLOWED=NO build`.
  These local changes must first be transferred/pushed before another checkout can test them.
- 39 tests passed with real FFmpeg and disposable PostgreSQL, including generated
  video extraction/cleanup and concurrent job claims/conflicting decisions.
  Runner: `deploy/verify-media-isolated.sh`; the temporary database was stopped
  and destroyed by test teardown. Production code/database/services were untouched.
- Subsequent 36-test local run passed, including two author-only status/privacy
  regressions and four opt-in provider-command tests. Provider responses remain
  mocked; these runs are not an accuracy evaluation or capacity benchmark.
- `python manage.py check_media_provider --confirm-external-transfer` performs
  ONE real request using a generated geometric JPEG only, with no database writes.
  Requires securely configured `MODERATION_API_KEY` and confirmed disclosure.
  It has NOT been run live: local credentials are absent and production secrets
  are not accessible to the current connection. Never paste credentials in chat.
- Consent default/client version is now `2026-10-16` for video-frame disclosure.
  Coordinate backend/client rollout and any environment override; older clients
  may need an update before submitting. Deploy backend contract additions before
  clients that require the new response fields. Keep screening disabled until
  client generation/build, live check, threshold evaluation and delivery audit pass.

Required next before activation:

Validation checkpoint (16 October 2026): 189-test regression run completed with
186 passing and 3 skipped; the separately rerun 10-test publication/review suite
also passed, including the newly added preview authorization and configuration
gate cases. OpenAPI generation/validation, Django check, migration drift and
diff whitespace checks passed. Provider calls and new video-frame evaluation
are mocked; this does not validate model accuracy, PostgreSQL lock behavior,
production storage, or a simulator build. All changes remain local on the branch;
nothing has been pushed, installed, enabled, or deployed by this implementation.

1. Install a SINGLE bounded worker process/timer initially; do not launch unbounded
   parallel workers. Command: `python manage.py process_media_screening --limit 10`.
   It refuses to run while the feature is disabled. Monitor failed/waiting jobs,
   queue age, provider quotas, disk usage, and review turnaround before scaling.
   Uninstalled examples: `deploy/repbase-media-screening.service` and `.timer`.
   Validate the memory/CPU limits with the existing video encoder on the target
   host first; they are starting bounds, not a capacity measurement. No database
   writes to local SQLite are allowed by this service sandbox; use PostgreSQL.
2. Finish iOS generated bindings and Mac build validation; manually verify both
   clients against a staging backend, including pending-to-approved refresh.
3. Audit signed media, processed video variants, thumbnails, notifications,
   recommendations and private storage/nginx settings. Update API/client status
   and timestamp contracts and consent for video frames, without exposing keys.
4. Extend the passing PostgreSQL/FFmpeg checks with the full regression suite,
   production-like storage validation and consented API accuracy evaluation.
   Configure and validate thresholds before rollout; do not claim
   ordinary swimwear will always pass a probabilistic model.

No PhotoDNA implementation was found in this branch. No provider was added for
child-safety screening. The explicit hold prevents this client from submitting
known/suspected material when that hold is set; it is not a CSAM detector.
Official reference: https://developers.openai.com/api/docs/guides/moderation .
Existing synchronous text/profile controls below remain; the new sexual-score-only
policy applies to queued post media when enabled. The default threshold 0.5 is
an uncalibrated starting configuration, NOT a measured safety guarantee. Deployment
check core.E013 validates prerequisites but cannot establish accuracy or worker health.

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

## Video clips: human review

The classifier reads text and still images, not video, so where moderation is
on (always, in production) a clip is reviewed by a person before anyone but
its author sees it.

1. Django admin → **Post videos** opens on the clips waiting, oldest first.
   The change page plays the clip through a signed link; nothing has to be
   downloaded.
2. **Approve** shows it to everyone who can see the post. **Reject** keeps it
   visible to its author only, marked rejected, and the media server refuses
   to serve it. Both record who decided and when. Both need change permission
   on videos and posts; a view-only account is not offered them.
3. `check_moderation_queue` counts waiting clips and treats one older than the
   deadline as overdue, exactly as it does reports.
4. While a clip waits, its post stays in its author's followers' feeds without
   the clip, and is kept out of For You.
5. Clips are refused before review unless they are MP4 or QuickTime, H.264 or
   HEVC, no longer than 60 seconds and no larger than 25 MB. Location and
   device metadata are removed before storage.

## What a takedown reaches

- **Hiding a post** removes it from every feed, For You, comments and
  notifications, and its photo and clip stop being served even to anyone
  holding a signed link.
- **Suspending an account** (`User.is_active = False`) now hides everything it
  posted and every comment it made, as well as stopping its token. Setting the
  flag back restores it all.
- **Three or more open reports** from different people take a post out of For
  You until the reports are reviewed. It stays in its author's followers'
  feeds. Closing the reports, whatever the decision, returns it.
- A person's own report takes the post off their own For You at once.

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

## 17 October 2026 continuation: saved permission and activation

- Migration 0079 adds an account-scoped, versioned safety-review preference.
  GET/PUT `/api/v1/me/safety-consent/` is authenticated, private/no-store, and
  session writes require CSRF. Legacy request headers never silently grant
  persistent consent. Saved withdrawal overrides legacy headers.
- Withdrawal cancels waiting/queued/claimed jobs and invalidates leases; an
  already-sent provider request cannot be recalled, but cannot publish afterward.
  Regrant does not resurrect cancelled jobs. New submission is required.
- Local selected backend tests: 95 passed before the final in-flight-withdrawal
  test was added. Final isolated PostgreSQL/real FFmpeg run: 229 tests, 228 passed,
  one existing opt-in nginx delivery drill skipped. Provider responses mocked in
  these tests; no live key or user media was used.
- Remote checkout was read-only verified at e819ef6, repbase active, screening
  timer absent. This is NOT a record of activation. Root access is still required.
- Before root deployment, replace (do not duplicate) these entries in
  `/etc/repbase.env`: MODERATION_ENABLED=true, MEDIA_SCREENING_ENABLED=true,
  MODERATION_DISCLOSURE_CONFIRMED=true, VIDEO_MODERATION_BYPASS=false. Retain the
  existing private MODERATION_API_KEY. Disclosure confirmation refers to the
  implemented explicit permission and privacy wording, not a hidden terms clause.
- `deploy/deploy-moderation-beta.sh <full validated SHA>` now supports explicit
  screening activation after backup/deployment. It calls
  `deploy/activate-media-screening.sh`, which checks migrations/config, sends one
  synthetic geometric image to OpenAI, installs the bounded systemd worker/timer,
  and restarts/checks the backend. It does not acknowledge E006 or print secrets.
  Failure after code deployment leaves the new code deployed and pending media
  private; fix the reported problem and rerun activation. Never turn screening
  off as a worker-error workaround.
- New iOS source requires Mac client generation/tests/build. Web build and 50
  tests pass. No claim of live end-to-end moderation, calibrated accuracy, or
  specialist child-safety detection. Rotate previously exposed infrastructure
  credentials before wider beta access.
