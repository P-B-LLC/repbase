# Serving references and daily nutrients — testing only (2026-11-02)

## User-visible changes

- Database foods retain their nutrition reference amount and unit. A labelled
  20 g serving can be logged by entering only the weight eaten; weight/ounces/
  servings switches preserve the amount. Liquid references use ml, not an
  assumed gram conversion. When label mass is missing, complete 100 g/ml
  nutrition can supply an explicit reference instead of asking for a guess.
- Saved records, recents, recipes, copied days and public meal/cookbook
  snapshots preserve the same optional metadata. Historical entries without
  these facts remain unknown; their original macros/totals are not backfilled.
  Choose a fresh database product to obtain its reference, rather than assuming
  that an old manually entered serving weighs 100 g.
- A compact Daily nutrients card opens the selected day's nutrient details.
  The portion screen also has an expandable nutrient breakdown.
- Tracked optional values: dietary fiber, saturated fat, total sugars, sodium,
  potassium, calcium, iron, magnesium, vitamin C and vitamin D.
- Known zero stays zero; absent data stays unavailable. Coverage counts show
  when only some logged foods have a nutrient value. Percentages are known
  intake only, not a claim that incomplete records meet a person's needs.
- FDA label Daily Values are general references, not individualized medical
  targets or a health grade. Sodium and saturated fat are limits, not
  achievements; total sugars has no invented %DV. Existing personalized
  calorie/macro targets and completion celebrations are unchanged.
- Adaptive app colors, existing rounded surfaces, native percent formatting,
  Dynamic Type and Reduced Motion are reused. New UI copy covers ten languages.

## Snapshot and unit contract

Five additive optional fields on food/detail/diary/recent/saved/public ingredient
DTOs: `source_id`, `serving_description`, `reference_quantity`,
`reference_unit`, `nutrients`. Reference unit is blank, g or ml (a plain response
string, not a new enum). All amounts refer to the SAME basis as existing
calories/macros; `servings` multiplies them into the consumed portion.

Nutrient keys carry their canonical units:
`fiber_g`, `saturated_fat_g`, `sugars_g`, `sodium_mg`,
`potassium_mg`, `calcium_mg`, `iron_mg`, `magnesium_mg`,
`vitamin_c_mg`, `vitamin_d_ug`. Amounts are decimal strings. The dictionary
is bounded/validated; an omitted key means unknown. Open Food Facts normalized
mass `*_100g` / `*_serving` values are in grams, even where the contributor's
input `*_unit` was mg. Convert those normalized amounts once, not twice.

Serving multipliers support six decimal places, preserving the old maximum.
The UI rounds once before preview/save, so 73 g divided by a 7 g reference can
save as 10.428571. Recovery files use a local snapshot version so old captured
requests retain their exact old two-decimal wire values and idempotency key.
Optional metadata is not introduced into an old captured retry request.
Renaming/changing quantity preserves metadata; changing manual macro values
clears the source/nutrients that are no longer an authoritative snapshot.

## Migrations and rollout

Backend adds `0089_food_nutrition_reference` and
`0090_food_portion_precision`. They are additive/precision changes without
guessing old serving masses or replacing diary records.

- iOS: `work/testflight-fixes-2026-10-27`,
  [draft PR #42](https://github.com/P-B-LLC/IOS-Frontend/pull/42).
- Backend: `work/testflight-calendar-sync-2026-10-28`,
  [draft PR #40](https://github.com/P-B-LLC/repbase/pull/40).
- Mac-generated client artifact:
  https://github.com/P-B-LLC/IOS-Frontend/actions/runs/41502117283
- Main, the live backend and TestFlight are unchanged. Deploy the matching
  backend/migrations before relying on new metadata or six-decimal writes.
- The existing integration branches still need reconciliation with main:
  the backend calendar/cookbook migration leaves and the iOS merge conflicts
  remain separate promotion blockers. Branch CI is not merged-main proof.

## Validation / simulator checklist

Local checks and CI results are still being collected. Do not infer a final
all-green result from this implementation note.

Local backend evidence: the broader 138-test food/cookbook run passed with
three PostgreSQL-only skips. A full isolated run completed 1,601 tests with
34 skips using temporary fast password hashing; the last small unit-conflict
normalization refinement was then separately covered by all 20 metadata tests.
Django checks, migration checks and iOS/backend schema parity passed. A bounded
read-only OFF staging product check supplied an explicit 100 g reference and
known optional nutrients; it changed no user data or shared cache. Exact-head
Linux/PostgreSQL CI and Mac build/interaction checks remain separate gates.

Verify with test accounts before release:

1. Search/scan a product with a 20 g label serving; enter 75 g and confirm
   3.75 reference servings and proportional calories/macros/nutrients.
2. Check a 100 g fallback, a 250 ml drink, ounces, fractional references,
   switches between modes, blank/invalid quantities and an actual known zero.
3. Save/refetch/reuse the food; copy a day; apply a recipe; share/save a meal
   or cookbook. Reference and optional nutrients must stay attached.
4. An old/manual entry with no known mass offers servings without demanding
   an invented gram value. Old pending retries must not change their payload.
5. Daily/portion nutrient progress scales with quantity, updates after edits
   or deletion, follows the selected date, and never carries another account.
6. Missing/partial data stays visibly unknown; sodium/saturated-fat limits
   are distinct from target references. No food grades or medical claims.
7. Light/dark, VoiceOver, large accessibility text, RTL, Reduced Motion,
   localized decimals/percentages and scroll clearance.

## Primary references

- [FDA Daily Values](https://www.fda.gov/food/nutrition-facts-label/daily-value-nutrition-and-supplement-facts-labels)
- [FDA label interpretation and limits](https://www.fda.gov/food/nutrition-facts-label/how-understand-and-use-nutrition-facts-label)
- [OFF normalized nutrition schema](https://openfoodfacts.github.io/documentation/docs/Product-Opener/schemas/schemas/product_nutrition/)
