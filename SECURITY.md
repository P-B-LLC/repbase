# Security

What was audited, what was found and fixed, what protects the application
now, and what is still open. Written for whoever runs or changes this backend
and its two clients.

## Scope of the audit

The backend (authentication, authorization, sessions, every API endpoint's
queryset scoping, query parameters, file uploads, media serving, settings and
environment handling, logging, admin moderation tooling), the web client (token
handling, request construction, the nginx configuration that serves it, its
dependencies), and the iOS client's storage and upload paths, read but not
modified.

## Findings and fixes

Ordered by severity. Each fix has a regression test, named in the last column.

| # | Severity | Finding | Fix | Test |
| --- | --- | --- | --- | --- |
| 1 | Critical | `/api/repbase/` was a plain Django view outside DRF's authentication defaults, and listed **every member's name, username, email, height and weight** to anonymous visitors, ignoring `shows_height`, `shows_weight` and `is_profile_public`. | Staff only (`staff_member_required`); others are sent to the admin sign-in. | `test_security.MemberListTests` |
| 2 | High | Every anonymous rate limit -- sign-in, registration, password reset -- could be bypassed by sending a different made-up `X-Forwarded-For` on each request. DRF's default `NUM_PROXIES=None` uses the whole header as the client identity, and nginx appends to whatever the client sent. Reproduced: fifteen failed sign-ins, no 429. | Production sets `NUM_PROXIES=1` (`REPBASE_NUM_PROXIES` to change it), so only the address nginx wrote counts. | `test_auth_flows.SignInTests.test_a_spoofed_forwarded_address_does_not_reset_the_limit` |
| 3 | High | Uploaded photos were stored and served **with their full EXIF/XMP/IPTC metadata** -- GPS location, camera serial number, owner name, captions. The web client uploads the original file, so a photo taken at home published a home address. | The stored original keeps only its pixels, colour profile and orientation. Nothing is re-encoded: JPEG segments, PNG chunks and WebP chunks are filtered and the compressed image data copied byte for byte. JFIF thumbnails (which can show an uncropped original) and multi-picture extras are dropped. | `test_content_creation.PostPhotoUploadTests` |
| 4 | Medium | A closed profile's featured lifts, followers and following were readable by any signed-in stranger who knew its id, although the profile itself was emptied for them. | Those lists are empty, in their usual shape, for anyone who may not read the profile, and for either side of a block. | `test_security.ClosedProfileTests` |
| 5 | Medium | Suspending an account (`User.is_active = False`, the documented moderation step) stopped its token and left everything it had posted and commented visible to everyone. | Posts and comments by inactive accounts are hidden everywhere they are served, including counts; reinstating restores them. | `test_reporting.SuspensionTests` |
| 6 | Medium | The admin's "Hide"/"Unhide" post actions declared no permission, so any staff account that could *view* posts could take them down or restore them. | Both require change permission, like the report actions already did. | `test_video_uploads.VideoReviewTests.test_a_read_only_moderator_is_not_offered_the_decision` |
| 7 | Medium | No limit on failed sign-ins per account, only per address -- no defence against guessing spread across many addresses. | Failed sign-ins are counted per username (default 20 an hour, from any address); success does not count; a password reset clears it. Counted whether or not the account exists, so it is not an oracle. | `test_auth_flows.SignInTests` |
| 8 | Medium | Comments, follows and reports had no limits of their own; the single `post` limit was spent by every read and like as well as by publishing. | Limits by action: publishing 60/h, likes and saves 600/h, comments 120/h, follows 200/h, reports 30/h, video uploads 20/h, impressions 600/h. All configurable by environment variable. Reads fall under the per-user ceiling. | Rate-limit tests in each suite |
| 9 | Low | Usernames accepted any characters -- spaces, slashes, markup, right-to-left overrides, Cyrillic look-alikes -- and display names accepted invisible formatting characters. Service names like `admin` and `support` could be taken. | New and changed usernames: ASCII letters, digits and `@.+-_`; a short list of service names is reserved. Display names may not contain bidi overrides, zero-width spaces or control characters (emoji joiners stay allowed). Existing values are never re-validated, so no account is locked out of editing its profile. | `test_auth_flows` |
| 10 | Low | Passwords had no maximum length; a megabyte password is compared to the username with a quadratic similarity check. | 256 characters for new passwords, 4096 accepted at sign-in. | `test_auth_flows.RegistrationTests` |
| 11 | Low | Eight list filters (`?author=`, `?post=`, `?session=`, `?meal=`, planner dates, ...) passed raw text to the database and answered **500** to anything that was not a number or date. | Parsed in `core/params.py`; a bad value is a 400 naming the parameter. | `test_security.MalformedInputTests` |
| 12 | Low | `kind=repost` on post creation built a repost of nothing and answered 500. | Refused with a 400. | `test_content_creation` |
| 13 | Low | The pixel-count ceiling on image uploads applied only when moderation was on; a few-kilobyte file can declare a canvas that takes gigabytes to decode. | Always checked, from the header, before decoding. | `test_a_decompression_bomb_is_refused_from_its_header` |
| 14 | Low | Error reports (the development 500 page, admin error email) printed the `Authorization` header -- the caller's token -- because Django's redaction pattern does not match it. | `DEFAULT_EXCEPTION_REPORTER_FILTER` hides it. | `test_security.ErrorReportTests` |
| 15 | Low | No Content-Security-Policy, Permissions-Policy or Cross-Origin-Resource-Policy; API responses carried no caching instruction. | `core/security.py`: `default-src 'none'` on every non-page response, a script-strict policy on pages, `no-store` on `/api/`, and `Cross-Origin-Resource-Policy: same-origin` everywhere except photos and clips, which say `cross-origin` because the page showing them is not always their origin (found by running the web app: `same-origin` there made Chrome refuse every clip). | `test_security.ResponseHeaderTests`, `MediaEmbeddingTests` |
| 16 | Low | The API docs loaded Swagger UI from `@latest` on a CDN, with an inline script, on the API's own origin, and were public in production. | Pinned version, the split view (no inline script), and staff only in production unless `REPBASE_PUBLIC_API_DOCS=true`. | `test_security.ProductionSettingsTests` |
| 17 | Low | The web app's nginx block sent no CSP, HSTS, nosniff or framing headers, and advertised the nginx version. | Added per location (nginx drops inherited headers in any location that sets its own); `server_tokens off`. The built app has no inline script or style, so the policy allows none. | Not testable here; see "Not verified" below |
| 18 | Low | A build-time dependency of the web app (`source-map-js`, via vite → postcss) had a published denial-of-service advisory. | Patch-level lockfile update; `npm audit` reports none. | -- |

## Controls in place

**Authentication.** Opaque DRF tokens over HTTPS, stored in the iOS Keychain.
Passwords hashed with Django's PBKDF2 and checked by Django's four validators.
Sign-in gives the same answer for a wrong password and an unknown account.
Password reset codes are hashed, expire in 15 minutes and are spent after five
wrong guesses; a reset revokes every token. Rotation and sign-out revoke the
token in use. Suspended accounts cannot sign in and their tokens stop working.

**Authorization.** Every owner-scoped resource is filtered to the token's
owner in its queryset, so another account's row is a 404 (never a 403, which
would confirm it exists) for read, edit and delete alike. Ownership fields in
request bodies are ignored. Post visibility (`visible_posts_for`) is one
function used by every feed, the comment endpoints, reposts and the
recommender: hidden posts, blocks in either direction, private and
followers-only posts, closed profiles and suspended authors. Moderation
actions in the admin require change permission.

**Input.** Every request body goes through a serializer with bounded field
lengths and closed choice sets. Query filters naming rows or dates are parsed
before use. Post content is built by the server from the author's own records,
never from numbers the client sends. Social links are rebuilt from parsed,
allow-listed parts.

**Uploads.**

- *Photos:* base64 in JSON, at most 5 MB decoded, type decided by decoding
  (JPEG, PNG or WebP), at most 50 megapixels, metadata removed, stored under
  a server-chosen name.
- *Videos:* sent as the raw body; the declared type must be a video type
  (else 415). The size is enforced while streaming (25 MB, else 413).
  Structure, duration and size are judged from the bytes: MP4 or QuickTime
  only, H.264 or HEVC picture, a known sound format, no longer than 60
  seconds by every clock in the file, no larger than 4096 px a side, and not
  fragmented. Location and device metadata are removed, chunk offsets
  rewritten, and the clean copy is inspected again before storage.
- *Both:* written through crash-safe staging that leaves a cleanup record
  before any byte reaches storage, so nothing is ever orphaned.

**Moderation.** Text and photos are classified before publication, failing
closed. The classifier cannot see video, so where moderation is on a clip
waits in an admin queue and is shown to nobody but its author until a person
approves it; `check_moderation_queue` alerts on overdue clips as it does on
overdue reports. A hidden post's photos and clip stop being served even to
holders of a signed link; a rejected clip likewise. Posts reported by three
or more people are left out of recommendations until reviewed.

**Media serving.** Signed, expiring URLs checked with an HMAC, path traversal
refused before the signature is checked, takedowns checked after it, byte
ranges for video, `nosniff`.

**Transport and browser.** Production forces HTTPS redirects, HSTS, secure
cookies, TLS to PostgreSQL (`verify-full`) and Redis (`rediss://`), and
refuses weak secrets, wildcard hosts and DEBUG. No CORS: both clients reach the
API from its own origin (nginx in production, Vite's proxy in development), so
no other origin is granted access. Cookie sessions (the admin, the browsable
API) still need CSRF tokens. CSP, Permissions-Policy, CORP (`cross-origin` for signed media only), `X-Frame-Options:
DENY`, `Referrer-Policy: same-origin`, COOP and `nosniff` on every response.

**Logging.** The access log records method, path (no query string, so no
signed-URL signatures), status and time -- no headers. Application logs never
include request bodies, passwords, tokens, report details or moderation
payloads (tests assert this for sign-in). Error reports redact the
Authorization header.

**Rate limits.** Per address for anonymous endpoints, per user and per action
for everything else, plus failed sign-ins per username. See `config/settings.py`
for the defaults and environment variables.

## Configuration that matters

| Variable | Default | Why it matters |
| --- | --- | --- |
| `REPBASE_NUM_PROXIES` | `1` in production | Must equal the number of proxies in front of gunicorn. Too low lets clients choose their throttle identity; too high treats a proxy's address as the client's. |
| `REPBASE_PUBLIC_API_DOCS` | `false` | Leave off unless the API docs should be public. |
| `REPBASE_VIDEO_MAX_BYTES` | 25 MB | Keep at or below nginx's `client_max_body_size`. |
| `REPBASE_VIDEO_MAX_SECONDS` | 60 | |
| `REPBASE_THROTTLE_*` | see settings | Each limit is configurable; lower is safer. |
| `DJANGO_SECURE_HSTS_SECONDS` | 3600 | Raise to a year once HTTPS is settled. |

## Found after the audit

| Severity | Finding | Fix | Test |
| --- | --- | --- | --- |
| Low | A password reset for an address with an account hashed a code and waited for the mail provider before answering; one without answered at once. The 204 was the same, its timing was not. Registration already reveals whether an email is taken, so this added little, but it undid the point of the 204. | The lookup, the code and the send run in the response's `close()`, after the answer is written. Against gunicorn both cases answer in 1-2 ms. | `tests.PasswordResetTests.test_the_answer_is_ready_before_anything_is_looked_up` |
| Low (correctness) | A rotation's slot checks -- one slot per position, positions inside the rotation -- were in a `validate` method that a second `validate` in the same class replaced. Duplicate positions reached the database as a 500. | Both checks run again. | `tests.RotationSlotValidationTests` |
| Operational | With DEBUG off, Django sends request errors only to `ADMINS`, which is unset, so a 500's traceback was discarded. | `config.production` logs Django errors and the app's own loggers to stderr (journald). | `test_hosted_configuration.ProductionConfigurationTests` |

## Second audit, October 2026

A fresh pass over all three repositories, looking for what the first one
missed. Every fix has a test in `core/test_security_audit.py`, written as the
attack and seen failing against the code before the fix. The "Before" column
was measured against the unmodified commit.

| # | Severity | Finding | Before | Fix |
| --- | --- | --- | --- | --- |
| A1 | High | Three endpoints wrote a row per day (or week) up to a date the client chose, unbounded: `GET /planner/?end=`, `POST cycles/{id}/plan-ahead/`, `POST schedules/plan-week/`. Any account with one daily repeating task could fill the database and hold a worker. | One GET with `end` 20,000 days out wrote 20,000 rows in 39 s (SQLite); `end=9999-12-31` is about 2.9 million. A far-future repeat or rotation start was a 500 (date overflow). | Nothing is written past a year ahead (`PLAN_AHEAD_LIMIT_DAYS`), enforced in the writers themselves; repeats and rotations cannot start past it; at most 100 repeating tasks run at once per account. Same GET: 367 rows, 0.1 s. |
| A2 | High | Django's own sign-in pages had no limits. `/api-auth/login/` signs any account in, and the session works on the whole API (SessionAuthentication), so it was an unthrottled way round the API's sign-in limits. `/admin/login/` was the same for staff. | 30 wrong passwords at `/api-auth/login/`: all 200, then the right one signed in and `/api/v1/me/` answered 200. | `core.auth_backends.SignInLimitBackend`: every `authenticate()` -- API, admin, browsable API, insights, access -- counts failures against the username and refuses it past the limit. Django's form sign-ins also share a per-client-address limit (5 per 15 minutes). |
| A3 | Medium | The insights/access sign-in limit keyed on `REMOTE_ADDR`, which behind nginx is nginx. One client's six attempts locked every member of staff out. nginx also passed a client's own `X-Forwarded-For` through unchanged on `/insights/` and `/media/`. | After client A's six attempts, client B's first: 429. | Keyed on the proxy-written client address (`client_address`, believes X-Forwarded-For only with `REPBASE_NUM_PROXIES`); nginx now sets X-Forwarded-For on those locations. Client B: 200. |
| A4 | Medium | Password reset limits were per source address only. Across many addresses an attacker could have codes issued without limit (each spending the last, five guesses each) and flood the owner's inbox. | Unlimited codes and guesses per account. | Per email address, from anywhere: 5 codes an hour (silently dropped past it, still 204) and 20 wrong codes a day across all codes (429). Counted for unknown addresses too, so neither says whether an account exists. |
| A5 | Medium | `scope_to_owner` exempted superusers, so a superuser's token read, changed and deleted every account's workouts, meals, sessions, routes and body weight through the app API, and the superuser's own app listed all of it. | A superuser's `GET /workouts/` returned another member's private plan. | No exemption; staff use the admin for other people's records. Custom exercises likewise. |
| A6 | Medium | Changing the account email took only a token, so a stolen token became a permanent takeover: move the email, then reset the password. | `PATCH /me/ {"email": "attacker@..."}` with a token: 200. | A different email needs `current_password` (new optional write-only field); wrong guesses count against the sign-in limit. Neither client edits email, so neither is affected. |
| A7 | Medium | A gym's member list named people with closed profiles -- whose profiles hide their gym -- to anyone, including people they had blocked: where somebody trains, to a stranger. | A stranger saw the closed profile in the list. | Only people the reader could open, never either side of a block. The member count is unchanged. |
| A8 | Medium | A gym whose creator deleted their account (`SET_NULL`) could be renamed or deleted by anybody signed in. | A stranger renamed it: 200. | Only its creator, or staff with `change_gym`. |
| A9 | Medium | gunicorn listened on `0.0.0.0`. Wherever the port was reachable, a client could skip nginx's TLS and rate limits and write its own X-Forwarded-For, which the throttles believe with `NUM_PROXIES=1`. | -- | Loopback by default (`GUNICORN_BIND_HOST`), validated as an IP address. |
| A10 | Medium | `deploy/configure.py` connected the app as `doadmin`, the managed cluster's administrator, with `sslmode=require` (no hostname check), and wrote settings `config.production` refuses. | -- | Writes a `config.production` file with `verify-full`, refuses administrative role names, and sets the proxy settings; `deploy/README.md` gives the SQL for a least-privilege role. The live server was not inspected. |
| A11 | Low | Request lists had no length limit: an 8 MB body held about 230,000 step counts, each an upsert in one transaction. Likewise Health imports, saved-meal ingredients (copied onto up to 31 days at once), a new workout's exercises, personalization types. | -- | 400 days, 500 workouts, 100 ingredients, 200 exercises, 20 types. The apps send 7 days and 30 days. |
| A12 | Low | Hand-declared integer fields had no ceiling, and PostgreSQL answers an out-of-range integer with an error: a 500. | `manual_reps`, `steps`: "integer out of range" on PostgreSQL. | Bounded (reps 1000, steps 1,000,000, cardio a week). |
| A13 | Low | A food search term longer than the cache column (200) was a 500 on PostgreSQL, after spending two calls of the shared FoodData Central allowance. | "value too long for type character varying(200)". | Cut to 100 characters. |
| A14 | Low | CI jobs in all three repositories ran with the repository's default token permissions. | -- | `permissions: contents: read` in every workflow. |
| A15 | Low | nginx inherited the TLS protocol list from `nginx.conf`, which on Ubuntu still names TLSv1 and TLSv1.1. | -- | `ssl_protocols TLSv1.2 TLSv1.3` in the site; set it in the http block too (see the comment there). |
| A16 | Low (iOS) | The app opened whatever URL the server sent as a social link, so a tampered or impersonated server could make a profile icon open `tel:`, `sms:` or another app's deep link. The server already allows https only. | -- | Links that are not https are dropped when decoded. |

Checked and found sound: queryset scoping of every owned resource against
foreign keys in request bodies, the signed-media design, the MP4/JPEG/PNG/WebP
parsers (every loop advances, every count is capped), the role and moderation
endpoints (actor re-read under a lock, protected tiers), the web client (no
HTML injection sinks, relative URLs, strict CSP), and iOS token storage
(Keychain, this device only) and transport (HTTPS required in Release, no
arbitrary loads).

## Third audit, October 2026

Another pass over `main` in all three repositories, plus a read-only review of
the unmerged `feat/video-processing` branch (below). Every fix has a test in
`core/test_third_security_audit.py`, written as the attack and seen failing
against the unmodified code. "Before" was measured against a throwaway server.

| # | Severity | Finding | Before | Fix |
| --- | --- | --- | --- | --- |
| T1 | High | `POST sessions/{id}/route/` capped one upload at 20,000 points but not the session, and each upload re-read the whole track to summarise it. | Five uploads: 100,000 points, each slower (2.7 s, 4.1 s, 5.9 s, 7.2 s, 9.0 s); the per-user ceiling allowed 1,200 an hour. | At most 50,000 points a session (the app sends one track of at most 20,000); route uploads have their own limit (`route`, 30/h; reads are not limited); a retry that adds nothing does not recompute. |
| T2 | Medium | A block left waiting follow requests standing. Approving one followed straight through the block, and opening a closed profile granted it automatically. | Both reproduced: the blocked person ended up following the blocker. | Blocking deletes requests both ways and their notifications; the request list, approval and opening a profile ignore any request across a block. |
| T3 | Medium | Suspending an account (`is_active = False`) hid its posts and comments but not the account. | The suspended profile, bio included, was in search, served by id, and listed among followers, following, gym members, notifications and follow requests. | Gone from all of those until reinstated. Gym member counts still include everybody. |
| T4 | Medium | Profile photos had only the per-user ceiling, though each is decoded in full and sent to the classifier, and concurrent uploads orphaned files: the old file was deleted and the new one saved with no lock. | Ten uploads at once: ten files kept, one referenced. | Own limit (`photo`, 30/h). Written through the same staging as post photos, under a row lock, with the old file queued for deletion after commit. On PostgreSQL, ten at once: all 200, one file. |
| T5 | Low | Somebody blocked by a post's author could still edit their old comments under it: invisible to the blocker, shown to everyone else. | A stranger read the rewritten comment. | Editing needs the post (and the comment replied to) to still be visible to the comment's author. Deleting your own comment is always allowed. |
| T6 | Low | Three filters used Django's `parse_date`, which raises on a date in the right shape that does not exist. | `?since=2026-02-30` on sessions and step counts, `?today=` on training stats: 500. | `core.params.date_param`: 400 naming the parameter. |
| T7 | Low | A moderator could decide reports about their own posts and comments, dismissing complaints about themselves. | -- | Refused with 403 in the API and the access portal. The Django admin's report actions are staff-only and unchanged. |

Checked and found sound this time: idempotency keys (scoped to account and
path), the For You and impression endpoints, push registration, signed media
(path normalisation, constant-time HMAC, takedowns checked after the
signature), the administration API (actor re-read under a lock, protected
tiers), the clip parser's streaming size limit, every notification path
(deduplicated except comments), and the snapshot builder (no route points, no
withheld weights).

### The iOS app on `main` cannot read this API

Found while reviewing the video work merged into iOS `main` on 2026-10-12. Its
generated client was built from the `feat/video-processing` contract, which
makes `community_label`, `community_badge` and `community_color` required on
every user and four new fields required on a clip. Real responses decoded with
the generated types (built with `swift build` here):

- Against this backend's `main`: every user and every post fails with
  `keyNotFound: community_label`. Feed, profiles, comments and search would all
  fail to load.
- Against `feat/video-processing` as pushed: every user and post fails with
  `valueNotFound`. The branch sends `null` for those three fields whenever the
  account has no AccountAccess row, which is almost every account (DRF answers
  a missing one-to-one with None before `default` applies). The branch's own
  `ForYouContractTests` fails on exactly this.

Do not ship the iOS build from `main` until the backend it talks to sends those
fields as strings. A three-file patch for the branch exists (null to declared
default with the contract unchanged, plus the two branch issues below); with it the
branch's 911 tests pass and the iOS types decode its responses.

### `feat/video-processing`, reviewed, not modified

The transcoder runs ffmpeg without a shell, with protocol and format
whitelists, external references disabled, a timeout and one thread; playback
tokens are signed, bound to the viewer and re-check visibility; local storage
keys cannot leave their root; the browser-session sign-in is CSRF-protected and
still counted by the per-username limit. Open on the branch:

1. `community_*` sent as `null` (above), and a per-author query for reposted
   authors' flair (`RepostFeedQueryCostTests` fails: 21 queries, not 17).
2. Playback events share the `upload` limit, 20 an hour, and the app sends one
   per clip played: watching twenty clips refuses the next real upload.
3. The pipeline ignores `VIDEO_REVIEW_REQUIRED` and approves clips whenever
   `VIDEO_MODERATION_BYPASS` is set, and the deploy check only warns about it.
   With it set in production, unreviewed video is public.
4. With local storage, ten jobs in flight block every upload. Ten accounts
   (one address's registrations for a day) can keep the queue full.

The third audit's fixes apply cleanly onto the branch, and its tests pass
there.

## Remaining risks

1. **The web client keeps its token in `localStorage`.** Any script injected
   into the page could read it. The strict CSP and React's escaping make that
   unlikely, and the token can be revoked by rotation or sign-out, but the
   robust fix is an HttpOnly, SameSite session cookie for the web client, which
   needs a session-login endpoint and CSRF handling in the client.
2. **API tokens never expire and are stored unhashed.** DRF's built-in tokens
   are long-lived and a database dump contains live ones. Rotation exists;
   expiring, hashed tokens (for example django-rest-knox) would need a client
   update in both apps.
3. **Rate limits are best effort.** They live in the cache and are not
   atomic; in development the cache is per-process. nginx now also limits
   sign-in, registration, password reset and video uploads per address on
   rytivo.app (`deploy/rytivo.app.nginx.conf` in the web repository). The
   IP-address server block the iOS app uses is configured on the server and
   is in neither repository; give it the same limits.
4. **`NUM_PROXIES` relies on the deployment.** gunicorn now listens on
   loopback by default, but a systemd unit passing its own `--bind` overrides
   that; if the port is reachable without going through nginx, a client can
   again choose its own address. Keep the port firewalled too, and raise the
   count if a CDN or load balancer is put in front. Every nginx location that
   reaches Django must set X-Forwarded-For -- including the IP-address block,
   which is in neither repository.
5. **Video is moderated by people only.** The queue has to be staffed; until a
   clip is reviewed it is visible to its author alone.
6. **A blocked person can still open the blocker's public profile by id.**
   This was an existing, documented product decision; the profile's lists are
   now hidden from them.
7. **Registration reveals whether a username or email is taken.** Inherent in
   choosing a username; registration is rate limited per address.
8. **A small group can take a post out of recommendations by reporting it**
   (three open reports) until a moderator looks. It stays in its author's
   followers' feeds. This trades brigading against keeping reported content
   from spreading while it waits.
9. **The iOS For You integration has not been built in Xcode.** The app now
   shows the ranked For You page, reports views, offers "not interested", the
   Tune sheet and suggested people. It was type-checked as Mac Catalyst
   against the iOS 26.5 SDK with the project's concurrency settings, and its
   impression batching was run on macOS, but it has not been built for a
   device or simulator or tapped through. Separately, the iOS contract
   carries the `community_*` user fields of the unmerged video-processing
   branch, which this backend does not send yet: until that branch lands, the
   app cannot decode users or posts from this backend -- For You included,
   as everything else. The October 2026 change to it (A16) was syntax-checked,
   not built.
10. **Photos lose HDR gain maps and depth data** along with their metadata,
    so an iPhone HDR photo displays as standard range. HEIC uploads were
    already refused (Pillow here has no HEIF support).
11. ~~No automated dependency scanning.~~ CI now runs `pip-audit` over the
    production requirements and `npm audit --audit-level=high` in the web
    repository. The first run found two PyJWT advisories (neither on a code
    path used here); PyJWT was raised to 2.15.1.
12. **Production's settings module is unknown from here.** `deploy/README.md`
    once said the droplet runs `config.settings`. If so, the protections
    `config.production` adds are absent -- above all `REPBASE_NUM_PROXIES`,
    without which every throttle can be dodged with a made-up header. Check
    `/etc/repbase.env` and the database role it connects as.
13. **Deleting an account takes only a token.** `DELETE /me/` is permanent and
    asks for nothing more. Requiring the password would break the iOS
    deletion flow App Store review requires, so it waits for a client change.
14. **A per-username limit can be used to lock somebody out** for up to an
    hour by failing at their username on purpose. That is the trade the
    limit makes against distributed guessing; a password reset clears it.
15. **Changing email does not notify the old address.** The password is now
    required, which is the protection that matters; a notice would also tell
    the owner if their password is known.
16. **Nobody proves they own an email address.** Registration and email
    changes take any unused address, so an account can claim somebody else's,
    which then shows as taken when its owner registers, and that owner receives
    the reset codes for an account that is not theirs. Fixing it needs a
    confirmation step in both clients.

## Not verified here

- The nginx site file passes `nginx -t` on nginx 1.31.6 (October 2026), and
  was run on high ports in front of a stub backend to confirm the forwarded
  headers each location sends. The droplet runs 1.24; run `nginx -t` there
  before reloading.
- The Django-served pages -- the admin, including the clip review page, and
  the API docs -- were driven in headless Chrome under their CSP with no
  violations. The web app's own CSP is sent by nginx, which the Vite dev
  server does not do, so that policy was not exercised in a browser; the built
  web app contains no inline script, inline style or `eval`, which is what it
  forbids.
- Nothing was load-tested under concurrent traffic. The recommender's query
  count is constant in the amount of content (tested), its candidate pool is
  capped, and it was timed on PostgreSQL with 20,000 accounts and 3.2 million
  view rows (`RECOMMENDATIONS.md`, Performance).
