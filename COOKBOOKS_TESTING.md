# Cookbooks testing branch

This feature belongs to `work/testflight-calendar-sync-2026-10-28` and the
matching iOS `work/testflight-fixes-2026-10-27` branch. It is not a production
deployment or authorization to merge/release.

## Behavior and storage

- `food/cookbooks/` is an owner-scoped, paginated collection of folder summaries.
  `recipe_count` is an SQL annotation, not a query per folder.
- Saved recipes have a non-null cookbook. New recipe requests and Social
  `save-meal` requests must select an owned cookbook. PATCH can omit the field
  to keep the existing destination. Moving uses PATCH with only `cookbook`.
- All recipe/collection writers take the same account row lock before checking
  destination capacity, allocating names, or deleting. Transactions contain SQL,
  not provider calls. Editing a recipe in its current full collection is allowed.
- Every collection holds at most 20 recipes. Empty folders may be deleted;
  nonempty folders must be emptied first. Account deletion still cascades.
- Recipe names are unique within their cookbook. Different cookbooks may keep
  the same recipe name; moving into a name conflict is rejected atomically.
- `food/saved-meals/?cookbook=ID` loads only the selected collection. Opening
  the cookbook overview does not load ingredients. The full saved-meal list
  remains available to the explicit meal-planning workflow.
- Posting uses `kind=cookbook` and an owned cookbook `source_id`. A bounded
  immutable snapshot freezes every recipe's name, ingredients, per-serving
  nutrition, serving quantities, and method at publication. No private IDs travel
  in that snapshot. Existing visibility, block, report, and moderation rules apply.
- Feed responses carry only cookbook name/count. Full recipe contents are fetched
  through `social/posts/ID/cookbook/`, with current post visibility checked again.
- `social/posts/ID/save-cookbook/` makes a private editable copy using bulk inserts.
  Repeating the request returns the same copy (200 instead of 201), including
  after a lost response. There is no follower fan-out or media duplication.

## Migration and deployment boundary

Migration `0087_cookbooks` preserves existing recipe IDs, ingredients and methods,
placing recipes in `My recipes`, `My recipes 2`, etc., in groups of at most 20.
No empty cookbook is created for accounts without saved recipes.
Migration 0088 moves the old owner-wide recipe-name constraint to the cookbook,
so testing databases that already applied 0087 can upgrade normally.

Take a database backup and stop older API writers before applying the migration
to a testing deployment. The new required destination is a write-contract change:
older iOS builds omit it and cannot create/save recipes against this backend.
Validate the matching regenerated iOS client on a testing backend before any
future coordinated beta rollout. A passing CI run does not deploy the service.
After new cookbook posts exist, use a forward fix or a tested backup restoration
rather than dropping their snapshot table as a casual rollback.

## Validation

`python manage.py test core.test_cookbooks` covers required/foreign destinations,
the 20/21 boundary, full-folder edits, atomic failed moves, two-client refresh,
immutable publication, visibility/block restrictions, idempotent collection saves,
summary query cost, account deletion, and migration of 43 existing recipes.

The PostgreSQL-specific test submits concurrent requests for the last available
slot and requires exactly one success. SQLite skips it because it cannot verify
row-lock behavior. The separate Cookbook integration workflow runs that test and
the affected existing recipe/recovery suites on PostgreSQL 17. The normal full
CI suite remains unchanged.

The iOS branch contains race tests for reordered library refreshes and writes,
all nine non-English translations, and an API client generated on macOS.
Run native CI, then test creating/renaming folders, saving Social meals, moving
recipes, posting/viewing/saving complete cookbooks, refresh from another signed-in
client, account switches, both appearance modes, and Dynamic Type on the simulator.
