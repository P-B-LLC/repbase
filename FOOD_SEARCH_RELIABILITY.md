# Food-search reliability — testing only, 2026-11-02

## Diagnosis

The app's generic "food database could not be reached" message masked two
different failures:

- The backend rejected every uncached search inside a shared seven-second
  window, including another person's search. This reproduced locally as a
  successful first query and a 503 second query with no second provider call.
- Bounded public reads from the deployed server (`faac069`) returned 503 for
  the legacy CGI search and structured bulk search, while modern product
  discovery returned 200 and a v3 barcode read returned 200. These are point-in-
  time connectivity checks, not load, coverage, accuracy, or uptime guarantees.

The user requested repair of the existing API integration first, not a local
catalog mirror. No provider database dump, storage service, or scheduled sync
is installed by this change.

## Implementation

- Add authenticated `GET /api/v1/food/discover/?q=...` using the fixed HTTPS
  Open Food Facts search service. Candidates contain only `source_id`, `name`,
  and `brand`. They are not loggable food entries.
- On selection, the app uses the existing barcode endpoint to load authoritative
  nutrition and its serving/weight basis before opening the portion sheet.
  Search-index `*_100g` fields alone cannot distinguish 100 g from 100 ml:
  never infer a serving or substitute missing macros with zeros.
- Existing clients retain their complete-food response contract. If the legacy
  text endpoint fails, its fallback discovers candidates then resolves at most
  three products within a bounded overall network budget. It does not issue
  25 detail requests. A failed detail read never becomes a fabricated empty
  success or a made-up food.
- Fresh caches, bounded retained nonempty stale answers, separate discovery
  keys, shorter empty-result caching, and short shared in-flight leases reduce
  provider traffic. Followers wait only briefly; coalescing is not a guarantee
  every concurrent cold request succeeds.
- Shared rolling request windows preserve the provider's published allowances
  (10 searches/minute and 15 product reads/minute). Busy responses carry a
  retry interval; no unlimited automatic retry loop or rate-limit bypass.
- The iOS client prevents duplicate in-flight submits and fences searches and
  detail responses on cancellation/account changes. Old authenticated catalog
  clients are cleared on sign-out and failed reconnection. Busy, expired-session,
  incomplete-nutrition, and detail-retry feedback are distinct and localized.
- No USDA restoration, model-generated nutrition, diary rewrite, moderation
  changes, or database migration is part of this repair.

## Branches and rollout

- iOS: `work/testflight-fixes-2026-10-27`.
- Backend: `work/testflight-calendar-sync-2026-10-28`.
- These are shared integration branches with other developers' existing work.
  Do not treat their entire diff as a minimal production hotfix.
- Main, the running backend, and TestFlight must remain unchanged until a
  separately approved rollout. The backend must be deployed before expecting
  the new discovery endpoint to work in an installed app. A new app against an
  older backend falls back only on a genuine missing discovery endpoint (404),
  not on permission, busy, or provider failures.
- The new Swift API operation is generated, not hand-edited. Mac generation:
  https://github.com/P-B-LLC/IOS-Frontend/actions/runs/41490277471

## Validation and remaining checks

Implementation validation is in progress; do not infer a full-suite pass from
this document. Record exact CI head SHAs/results before promotion.

Final local focused run: 82 tests ran, 80 passed, and two PostgreSQL-specific
concurrency cases were skipped on SQLite. They cover shared capacity under 20
simultaneous requests and coalescing cold readers without an HTTP transaction.
Django checks, migration checks (none required), schema parity, and diff hygiene
passed. The full core suite and Linux/PostgreSQL CI remain separate gates.
Draft validation PRs: [backend #40](https://github.com/P-B-LLC/repbase/pull/40)
and [iOS #42](https://github.com/P-B-LLC/IOS-Frontend/pull/42).

PR merge-candidate CI on food commit `f037e39` stops before tests because main's
`0087_link_workout_calendar_tasks` and this branch's `0088_recipe_names_per_cookbook`
are separate migration leaves. This is an existing integration conflict, not a
passing CI result. The same read-only CI now permits manual exact-branch runs;
their results do not validate the main merge candidate. Reconcile the migration
graph and iOS main conflicts, then rerun required checks before promotion.

A bounded real-provider adapter smoke check, with a temporary in-memory cache,
returned 25 rice candidates, loaded complete details for the selected rice with
an explicit 100 g basis, and served its repeated discovery from cache. The old-
client compatibility path also returned complete foods during the CGI failure.
No live application database, user diary, or running service was changed.
These spot checks do not establish label accuracy or general availability.

Before release, use a test backend and test accounts to verify:

1. Search rice, another food immediately, a brand, an unknown name, and a barcode.
2. Select a discovery result: loading feedback, verified nutrition, weight vs
   serving selection, editable quantity, and correct saved meal totals.
3. Missing/incomplete labels cannot be logged from a discovery candidate.
   Detail retry retains the selected candidate and manual entry remains usable.
4. Provider failure, quota/busy response, empty-cache expiry, retained cache,
   duplicate submits, fast query edits, switching accounts, and signing out.
5. Existing installed clients still receive only complete food records.
6. Light/dark mode, VoiceOver, large text, and all supported UI languages.

This remains a free external service. Cached/recent/manual foods stay useful,
but a never-seen product can still be unavailable during an upstream outage or
quota exhaustion. Always-available independent catalog search needs a local
mirror and sync, which the user deferred.

## Provider references

- https://openfoodfacts.github.io/openfoodfacts-server/api/
- https://openfoodfacts.github.io/search-a-licious/users/ref-openapi/
- https://openfoodfacts.github.io/openfoodfacts-server/api/ref-cheatsheet/
