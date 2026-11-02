# Open Food Facts testing candidate — 2026-11-02

Branch: `work/testflight-calendar-sync-2026-10-28`, draft PR #36.
Counterpart: IOS-Frontend `work/testflight-fixes-2026-10-27`, draft PR #39.
This is not deployed and must not be merged/released without approval.

## Behavior

- Replaces USDA upstream reads, not saved diary data. Historical food entries
  retain their nutrition snapshots. No migration or user-data deletion.
- Authenticated `GET /api/v1/food/search/?q=...` keeps its response shape.
  Submit names/brands explicitly, or `barcode:<GTIN>`. Invalid barcodes return
  400; provider failures return the existing unavailable response, not false
  empty success. Missing/incomplete products return an empty list.
- New authenticated `GET /api/v1/food/discover/?q=...` uses the working modern
  `https://search.openfoodfacts.org/search` index. Its response contains only
  `{source_id, name, brand}` candidates, with no guessed nutrition or serving
  basis. Select one, then resolve its `off:<GTIN>` through the existing
  `food/search/?q=barcode:<GTIN>` endpoint before offering portions or saving.
- Legacy name searches try CGI with a two-second budget; when it fails or is
  malformed, modern discovery supplies candidates and at most three v3 product
  reads supply authoritative complete foods within an eight-second overall
  I/O budget. Old apps retain their complete decimal-string DTO. Modern apps
  avoid this N+1 fallback and read just the selected product's details.
- Uses OFF v3 product reads and a separately pinned modern discovery host.
  No account IDs, diary entries, camera frames or photos go to OFF.
- All four nutrition values must exist on the same serving/100g/100ml basis.
  Missing values are not silently zero; calories are not estimated from macros.
- OFF cache prefixes isolate old USDA results. Discovery and complete-food
  snapshots use separate prefixes. Positive cache lifetime is seven days;
  empty results refresh after 15 minutes. Nonempty stale fallback is bounded
  by existing 90-day retention. Transient failures and partial legacy fallback
  results are never stored as authoritative no-matches/full search snapshots.
- Database request gates share limits across workers without holding locks
  during HTTP. A strict rolling minute allows a modest burst of ten searches
  (CGI plus modern discovery) and fifteen product reads; cache hits cost no
  upstream slots. Service-specific provider cooldowns honor bounded Retry-After
  values without allowing an unavailable CGI route to disable working modern
  discovery. Busy responses carry Retry-After, not a made-up empty success.
- A shared 12-second per-key lease coalesces simultaneous cold lookups. Another
  request can reuse retained data or poll for at most 0.6 seconds; it does not
  hold a web worker asleep for seven seconds. Only the current lease owner may
  write the cache or delete its lease. Released leases are deleted immediately,
  including failed queries; normal cleanup also prunes abandoned lease rows
  older than two minutes, without applying that short TTL to food snapshots.
  Response bodies are size-bounded, read in chunks and checked
  against their remaining deadline. DNS/OS scheduling is outside this budget.
- This is still an external service integration, not an unlimited/offline local
  catalogue. New uncached queries can fail if OFF or the network is unavailable.
  A larger approved allowance or separately approved local export index would
  be needed for higher sustained traffic. No local mirror is added here.

## Configuration and rollout gates

- No provider API key. `USDA_FDC_API_KEY` is no longer read.
- `OPEN_FOOD_FACTS_ORIGIN` defaults to `https://world.openfoodfacts.org` for
  authoritative products/legacy CGI. Product smoke checks should use staging
  `https://world.openfoodfacts.net`. Modern discovery has a separate fixed
  `https://search.openfoodfacts.org` origin; it never receives member identity.
- `OPEN_FOOD_FACTS_USER_AGENT` defaults to `Rytivo/1.0 (support@rytivo.app)`;
  verify the contact mailbox is monitored before rollout.
- Review OFF terms/ODbL attribution and database share-alike obligations before
  release; complete their API usage form. The client displays attribution and
  a license link. This document does not assert legal compliance.
- Documentation: https://openfoodfacts.github.io/openfoodfacts-server/api/
- Preserve current privacy/consent release process when publishing updated
  provider disclosures. Do not delete old private meals or upload them to OFF.

## Validation

The November 2 regression reproduces the original artificial rejection: an
uncached search used to reserve the entire platform for seven seconds, so a
second valid query could return 503 before any provider request. The modern
candidate/selection path also avoids depending on CGI availability or treating
index-only `_100g` values as a confirmed gram/volume/serving basis.

Focused commands (isolated test database only):

```sh
python manage.py test core.test_food_search_reliability core.test_open_food_facts core.tests.FoodSearchViewTests core.test_row_expiry core.test_cleanup_check core.test_security_audit.FoodSearchTermTests
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py spectacular --file /tmp/food-openapi.yaml
```

Coverage includes identity-only candidate responses, barcode validation and
canonicalization, escaped literal queries, malformed/timed-out responses,
authoritative liquid basis, wrong-product rejection, burst/rolling window
limits, provider cooldowns, bounded follower waits, lease ownership, proper
stale/negative cache timestamps, legacy fallback count/deadline and avoiding
transient cache poisoning. Linux PostgreSQL and Mac build/interaction checks
remain required before release. This document does not assert a passing run
until the candidate SHA and results are recorded by the main session.

Local November 2 final focused validation: 82 tests ran on Windows/Python 3.14
using an isolated test database: 80 passed and the two PostgreSQL-only concurrency
tests were skipped. They must execute in PostgreSQL CI before promotion.
Django system checks and migration checks passed; no migrations were detected.
These results do not substitute for the full core/PostgreSQL suite, a Mac build,
or simulator/device interaction checks.

Earlier implementation evidence (October 29):

37 focused adapter, API, cache retention and input tests passed on Windows.
Read-only OFF staging requests returned a product and three text-search
results. These smoke checks do not establish catalogue coverage or accuracy.
The broader Windows run executed 1,284 tests with four failures and 31 skips:
one password-throttle assertion used the E2E settings' deliberately raised
limits; three production-settings subprocess checks reject POSIX media paths
on Windows. This is not a green full-suite result. Linux/PostgreSQL CI and
Mac/device checks remain release gates. No schema migrations were detected.
Test valid/invalid barcodes, unavailable/limited provider, incomplete labels,
retry, portions, saved totals, account changes and historical diary entries.
