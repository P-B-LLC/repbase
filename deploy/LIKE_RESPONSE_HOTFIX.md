# Beta friend-post like response hotfix — 2026-10-28

## Confirmed cause

Read-only SSH inspection found the beta checkout and `/etc/repbase-release`
at `de27d1b000de2f9e1c9b76154bfde3433840b3f8`. The checkout is clean and its
`PostViewSet._card` still uses `get(pk=pk, author=self.owner_profile())`.
The iOS client correctly sends the documented bodyless like POST.

Liking another person's visible post can save the like, then raise
`Post.DoesNotExist` when the response tries to read it as the viewer's own
post. The result is HTTP 500 rather than the confirmed card. Repeated taps
cannot repair that query. No customer posts, likes or credentials were changed
to diagnose this. Journal access required root; the failure was reproduced
locally using the deployed query, not claimed from an unreadable live traceback.

The fix already exists in main at `42281fd`, but was not deployed. This branch
backports its `_card` change onto the exact deployed base. Full validation
also exposed two pre-existing baseline issues. The user explicitly approved
including the already-tested Pillow 13.0.0 security upgrade and one-line
`published_at` admin read-only correction. No pending schedule, ranking or
UI changes are included.

## Repair and regression coverage

- Read back posts the viewer is permitted to see, as well as their own
  submission status; do not restrict all responses to viewer-owned posts.
- Recheck visibility before responding. Missing/newly private content returns
  404, not an unhandled exception or a private post body.
- Keep the existing unique `(post, user)` like constraint and idempotent
  POST/DELETE behavior. A retry can confirm a previously saved like without
  incrementing the count twice.
- `core/test_post_likes.py` adds 10 regressions for friend/self likes,
  retries/unlikes/counts, followers-only access, blocks, unpublished/hidden/
  moderated content, privacy changes, deleted posts and anonymous requests.

Validation on the initial isolated tree: 69 focused tests passed. After the
two explicitly approved additions, 77 passed (`test_post_likes`,
`NotificationTests`, `test_content_creation`, `test_reporting`,
`ModerationAuditTests`), with a disposable SQLite database and media directory.
Restoring the deployed
owner-only query in memory reproduces the friend's-post failure. Full Linux
and PostgreSQL CI evidence belongs in the validation PR, not inferred from
these local tests.

## Deployment boundary

`deploy-like-response-hotfix.sh` is root-run only, accepts a pinned full commit
SHA, and refuses an unexpected current release, dirty checkout or changes
outside this hotfix's allowlist. It upgrades only Pillow to 13.0.0 without
installing other dependencies, runs no migrations, and changes no environment
settings. Existing deploy checks still run; no checks are acknowledged or
disabled by this helper.

It restarts only the backend, verifies the health/API probes and records the
release. On failure after switching, it attempts to restore and restart the
original code revision. The security upgrade is retained during code rollback;
it does not silently reinstall vulnerable Pillow. There are no data changes
to reverse.

Main and TestFlight are not changed. The backup/deployed-base branch is only
a rollback reference and validation-PR base, not a release branch to merge.
Deploy only a reviewed, validated SHA after the operator authorizes it.

The initial like-only candidate `8486cd1` inherited the deployed Pillow 12.3.0
audit failure and moderation-admin form test failure. The approved additions
correct those failures without suppressing checks. Do not treat that initial
candidate as validated or deploy it; use the newer green candidate pinned in
the validation PR and operator command.

## Post-deploy verification

Confirm the recorded revision, then refresh the Social feed and like/unlike
one permitted test post from a second test account. Check HTTP 200, the
returned `viewer_has_liked`/`like_count`, and one notification rather than
duplicates. Retry and reload to confirm persistence. Do not generate actions
on real members' posts as an automated smoke test.

A prior HTTP-500 banner can remain on an already-open client screen; refreshing
or reopening clears that old display. Rebuilding the iOS app is not needed
for this backend fix. Existing saved likes must not be deleted as a workaround.
