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
   atomic; in development the cache is per-process. Add ingress limits in
   nginx (`limit_req`) for sign-in, registration and uploads in production,
   as `/insights/login/` already has.
4. **`NUM_PROXIES` relies on the deployment.** gunicorn binds `0.0.0.0`; if
   its port is reachable without going through nginx, a client can again
   choose its own address. Keep the port firewalled, and raise the count if a
   CDN or load balancer is put in front.
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
9. **The iOS app is unchanged.** It keeps working against this backend -- every
   contract change is additive -- but does not yet show For You, report views,
   play clips or offer "not interested". Its generated client must be
   regenerated on a Mac with Xcode; see `RECOMMENDATIONS.md`.
10. **Photos lose HDR gain maps and depth data** along with their metadata,
    so an iPhone HDR photo displays as standard range. HEIC uploads were
    already refused (Pillow here has no HEIF support).
11. **No automated dependency scanning.** Add `pip-audit` and `npm audit` to
    CI.

## Not verified here

- The nginx configuration was checked for structure but not with `nginx -t`,
  which needs nginx installed; run it before reloading.
- The Django-served pages -- the admin, including the clip review page, and
  the API docs -- were driven in headless Chrome under their CSP with no
  violations. The web app's own CSP is sent by nginx, which the Vite dev
  server does not do, so that policy was not exercised in a browser; the built
  web app contains no inline script, inline style or `eval`, which is what it
  forbids.
- Nothing was load-tested. The recommender's query count is constant in the
  amount of content (tested), and its candidate pool is capped.
