# Rytivo changelog

Entries cover all three repositories (IOS-Frontend, repbase, rytivo-web) and
are kept identical in each. Newest first.

## 2026-11-19 — Cleanup, optimization and test coverage

Branch `chore/cleanup-and-tests` in all three repositories, one pull request
each. No API, request/response, model, migration, settings, signing or
user-facing change; the iOS build stays 1.1 on iOS 26.5 for the TestFlight
beta.

- **IOS-Frontend**
  - Removed about 70 unused views, helpers and properties and the 6 assets
    only they used.
  - Compiler warnings 43 → 10.
  - Two screens no longer build date formatters on every render.
  - The 29 notes moved to `docs/`; a few misplaced source files moved to
    their folders.
  - New README.
  - Account-safety tests 638 → 655: icon-only tab bar localization and
    assets, Health source naming, take credits.
- **repbase**
  - Removed unused imports and members.
  - Malformed calendar share ids now return 404 instead of 500.
  - Fixed the one test that failed on macOS.
  - The calendar views look up the signed-in profile once per request.
  - The sign-in throttle key's hash is set explicitly, with no change to the
    keys; this removes a Django deprecation warning.
  - Docs moved to `docs/`, and the v1 PDFs to `docs/archive/`.
  - Tests 2177 (1 failing) → 2232 (all passing) on SQLite and
    PostgreSQL: Health import, per-set version marker, calendar sharing,
    sounds, "I tried this", rollout commands, experiment statistics.
- **rytivo-web**
  - Removed unused API client methods.
  - The request helpers share one code path, and the screens use the
    existing day helpers instead of private copies.
  - Fixed a food-search race where stale results could replace newer ones.
  - Lint warnings 1 → 0.
  - Docs moved to `docs/`; new README.
  - Unit tests 51 → 79: calendar days, client error handling.
