# Open Food Facts testing candidate — 2026-10-29

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
- Uses OFF v3 product reads and the documented CGI full-text search route.
  No account IDs, diary entries, camera frames or photos go to OFF.
- All four nutrition values must exist on the same serving/100g/100ml basis.
  Missing values are not silently zero; calories are not estimated from macros.
- OFF cache prefixes isolate old USDA results. Cache lifetime is seven days;
  stale fallback is bounded by existing 90-day retention.
- Database request gates share limits across workers without holding locks
  during HTTP. Uncached search is limited to one per seven seconds, barcode
  reads to one per five seconds. This is a small-beta strategy, not unlimited
  search capacity. Scale requires an approved allowance or local export index.

## Configuration and rollout gates

- No provider API key. `USDA_FDC_API_KEY` is no longer read.
- `OPEN_FOOD_FACTS_ORIGIN` defaults to `https://world.openfoodfacts.org`.
  Tests against a real provider must use `https://world.openfoodfacts.net`.
- `OPEN_FOOD_FACTS_USER_AGENT` defaults to `Rytivo/1.0 (support@rytivo.app)`;
  verify the contact mailbox is monitored before rollout.
- Review OFF terms/ODbL attribution and database share-alike obligations before
  release; complete their API usage form. The client displays attribution and
  a license link. This document does not assert legal compliance.
- Documentation: https://openfoodfacts.github.io/openfoodfacts-server/api/
- Preserve current privacy/consent release process when publishing updated
  provider disclosures. Do not delete old private meals or upload them to OFF.

## Validation

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
