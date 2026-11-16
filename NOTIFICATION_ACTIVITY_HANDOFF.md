# Grouped notification activity — testing branch, 2026-10-29

The approved [Figma notification center](https://www.figma.com/design/FbMNgG8cuZfrRFzQTtIVCy/Rytivo-Connected-Flow?node-id=122-1562)
shows smaller bordered cards, stacked actor photos, a post preview, and one card
per post with separate like/comment/repost counts. Follows and moderation notices
remain separate. “All activity” is a heading, not a filter.

Backend changes remain on `work/testflight-calendar-sync-2026-10-28` in
[draft PR #36](https://github.com/P-B-LLC/repbase/pull/36). The iOS counterpart is
`work/testflight-fixes-2026-10-27` in
[draft PR #39](https://github.com/P-B-LLC/IOS-Frontend/pull/39).
This is not a deployment or a release. Main, TestFlight, and the existing beta
backend must not change without separate authorization.

## API and privacy

- `GET /api/v1/social/notifications/` retains its paginated event response.
  Each notification adds optional `post_preview` and `supports_scoped_read`.
- `post_preview` is null or `{id, kind, title, thumbnail_url, media_kind}`.
  `id` is the notification's post ID; a repost uses its authorized original's
  title/media. Titles are capped at 120 characters. `kind` and `media_kind`
  are plain strings, not new response enums. `media_kind` currently says
  `photo`, `video`, or `none`; `thumbnail_url` can be null.
- Ordinary activity is filtered through the same recipient post-visibility
  policy as the feed, including author/actor blocks, private profiles,
  follower permissions, suspensions, publication state, hidden posts, child
  holds, and repost-original visibility. Hidden comments/replies are excluded.
  Inconsistent internal/legacy rows whose comment belongs to a different post,
  or whose post is missing, are also excluded from lists, unread counts, and reads.
  Restricted moderation-rejection notices remain generic with no post preview.
- Previews use one page-bounded query with joined snapshots/video metadata,
  not a full-post fetch per notification. Photo previews use the existing
  signed feed-sized photo, falling back to the original when no smaller copy
  exists. Video thumbnails require approved/ready media and use the existing
  recipient-bound signed asset endpoint. Nothing downloads or plays a video.
- `POST /api/v1/social/notifications/read/` accepts optional
  `{"ids": [123, 124]}` (maximum 500 positive IDs). Only currently visible
  notifications owned by the authenticated recipient are changed. Empty IDs
  are a no-op. The response reports the actual remaining unread count.
  Omitting `ids` preserves legacy mark-all behavior for existing clients.
- **Client compatibility requirement:** send selective reads only after seeing
  `supports_scoped_read: true`. The old deployed backend ignores an IDs body
  and marks everything, so missing capability must mean **no read write**, not
  a fallback to mark-all. Empty inboxes need no read request.
- `core/schema.py` narrowly keeps the two new response fields optional in the
  canonical contract. This allows a regenerated iOS client to decode old beta
  responses without disabling validation for unrelated fields.

There are no model migrations, external provider requests, push-notification
changes, or server-side aggregation in this update. iOS groups the paginated
events by post ID and uses existing `created_at` to place each group under its
latest interaction. Counts describe notification activity, not necessarily the
post's current live engagement totals (an old like notice can outlive an unlike).

## Validation and promotion

Run from the backend repository using its local/test database configuration:

```sh
python manage.py test core.test_notification_previews core.tests.NotificationTests core.test_media_publication core.test_post_likes core.test_reporting core.test_security core.test_moderation_safety
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py spectacular --file /tmp/notification-openapi.yaml
```

Compare the generated schema with `openapi.yaml`; copy only this contract's
changes to the iOS OAS, regenerate the Swift client, and build/test on macOS.
The focused tests cover safe previews and null fallbacks, photo/video signing,
privacy/blocks/hidden replies/child holds, constant query count as posts grow,
pagination, authentication, cross-recipient reads, malformed/oversized batches,
new arrivals remaining unread, legacy bodyless reads, and optional schema fields.

Before promotion, complete Linux SQLite/PostgreSQL CI and iOS simulator/device
QA with a backend running this testing branch. Check multiple people and action
types on one post, groups spanning dates/pages, follow/profile navigation,
post-activity inspection, light/dark mode, long names, accessibility text, RTL,
VoiceOver, refresh/retry, and sign-out while requests are in flight. The existing
beta cannot provide the new thumbnail fields until separately deployed.

Local validation on Windows/Python 3.14: all 23 notification-preview/read tests
passed, as did the three schema/contract tests, Django checks, migration checks,
and diff hygiene. The final combined run with the 12 existing notification tests
passed all 35 tests. The broader 123-test run passed 120 tests; three pre-existing
`ProductionSettingsTests` failed because their POSIX `/persistent/media` fixture
is not an absolute path on Windows. This is not a passing full-suite result;
Linux SQLite/PostgreSQL CI must provide that evidence before promotion.
