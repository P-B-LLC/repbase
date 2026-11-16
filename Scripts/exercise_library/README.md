# Exercise library

Generates `core/data/exercise_library.json`, the shared exercise catalogue bundled in the iOS app (search, filters, details) and seeded into the backend.

## Files

| File | Purpose |
| --- | --- |
| `build_exercise_library.py` | Builder. Reads the base dataset, renames, maps, applies curation and overrides, merges the supplement, de-duplicates, validates, sorts and writes atomically. Stdlib only, deterministic. |
| `validate_exercise_library.py` | Validator and the single source of truth for the vocabularies, the slug rule and the name normalization rule. Exits non-zero on failure and prints counts. |
| `base_curation.json` | Changes to free-exercise-db entries, keyed by the base `id`: `drop`, `merge` (duplicate id into kept id), `equipment` and `category` fixes applied before naming, and explicit `rename`s. |
| `overrides.json` | Hand corrections keyed by the final `slug`. Any schema field except `slug`/`name`, plus `aliases_add`, `aliases_remove`, `disciplines_add`, `disciplines_remove`, `secondary_muscles_add`. |
| `supplement.json` | Rytivo's own exercises (CrossFit, calisthenics, rehab, mobility, strongman, powerlifting and gym gaps), written for Rytivo. |

## Regenerate

```sh
# 1. Fetch the base data into its own empty directory (never run anything from it).
mkdir -p /tmp/free-exercise-db
curl -sSL https://raw.githubusercontent.com/yuhonas/free-exercise-db/main/dist/exercises.json \
  -o /tmp/free-exercise-db/exercises.json

# 2. Build (from the repbase root) and validate.
python3 -I Scripts/exercise_library/build_exercise_library.py --base /tmp/free-exercise-db/exercises.json
python3 -I Scripts/exercise_library/validate_exercise_library.py
```

Builder options: `--out PATH` (default `core/data/exercise_library.json`), `--review-tsv PATH` (writes a table of every exercise for review), `--library-version 2026.10.29` (bump it whenever the content changes). The build fails, and leaves the old file untouched, if anything does not validate: name collisions, an unknown override slug, a curated alias that clashes with another exercise, or any schema error. Warnings (dropped generated aliases, rehab entries that are not beginner level, names missing their equipment label) are printed but do not stop the build.

## Add or edit an exercise

- **New exercise:** add an object to `supplement.json` with `name`, `category`, `equipment`, `primary_muscles`, `level`, 3 to 6 `instructions`, and ideally `aliases`, `secondary_muscles`, `movement_pattern`, `mechanic`, `force`, `disciplines`, `family`. Missing pattern, disciplines and family are derived by rules. The slug is always derived from the name.
- **Fix a base exercise:** use `overrides.json` with the exercise's slug as the key. To rename it, add its base `id` to `rename` in `base_curation.json` (and move any override to the new slug, because the build rejects unknown slugs).
- **Duplicate or junk base entry:** add it to `merge` or `drop` in `base_curation.json`. Merged names become aliases of the kept entry.
- Rebuild, validate and review the diff of `core/data/exercise_library.json`.

## Naming rules

- Title Case, US spelling, singular ("Hammer Curl", not "Hammer Curls").
- Equipment in parentheses at the end when it distinguishes variants: "Bench Press (Barbell)", "Lat Pulldown (Cable)", "Leg Press (Machine)", "Squat (Smith Machine)", "Lateral Raise (Band)". Labels: Barbell, Dumbbell, Kettlebell, Cable, Machine, Smith Machine, EZ Bar, Trap Bar, Band, Medicine Ball, Stability Ball, Suspension Trainer, Plate.
- Bodyweight, pull-up bar, rings, sled, landmine, box, rope, sandbag, foam roller and cardio machine movements carry no suffix ("Pull Up", "Ring Dip", "Sled Push", "T-Bar Row").
- Grip and stance qualifiers go after a dash: "Bench Press - Close Grip (Barbell)", "Lat Pulldown - Underhand Grip (Cable)".
- Unhyphenated compounds: Pull Up, Push Up, Chin Up, Sit Up, Step Up, Muscle Up, Get Up. Letter shapes keep the hyphen: L-Sit, V-Up, T-Bar, Y-T-W.
- "Single Arm" / "Single Leg" (not "One-Arm"), "Alternating" (not "Alternate"), "Bicep Curl" and "Triceps ...".
- Renamed base entries keep their original free-exercise-db name as an alias.

## De-duplication

Names and aliases are compared with `normalize_name`: lowercase, accents folded, every run of punctuation and whitespace removed, so "Pull-Up", "Pull Up" and "pullup" are equal. No two exercises may share a normalized name, no alias may equal another exercise's name, and no alias may belong to two exercises. When generated aliases clash, priority decides: curated (overrides and supplement) > the base's original name > merged names > generated "Barbell Bench Press" style forms. A tie between curated aliases fails the build.

## Vocabularies

- `category`: strength, olympic, plyometric, cardio, isometric, stretching, mobility
- `equipment` (one, the defining piece): barbell, dumbbell, kettlebell, cable, machine, smith_machine, ez_bar, trap_bar, resistance_band, bodyweight, medicine_ball, stability_ball, foam_roller, pull_up_bar, rings, suspension_trainer, sled, plate, landmine, box, rope, sandbag, cardio_machine, other
- muscles (primary and secondary, never both): chest, shoulders, triceps, biceps, forearms, traps, lats, upper_back, lower_back, abdominals, obliques, hip_flexors, glutes, quadriceps, hamstrings, adductors, abductors, calves, tibialis, neck, full_body
- `movement_pattern`: squat, hinge, lunge, horizontal_push, vertical_push, horizontal_pull, vertical_pull, carry, rotation, core, isolation, explosive, locomotion, mobility
- `mechanic`: compound, isolation or null (null for stretching and mobility)
- `force`: push, pull, static or null
- `level`: beginner, intermediate, advanced (the base's "expert" maps to advanced)
- `disciplines` (one or more): bodybuilding, powerlifting, olympic_weightlifting, crossfit, calisthenics, strongman, functional, mobility, rehab, cardio
- `family`: kebab-case key shared by variations of one movement (every bench press variant has "bench-press"); an exercise without siblings uses its own slug.

Rehab-tagged entries are beginner level and use conservative wording; they describe movements, not treatments, and make no medical claims.

## Sources and licenses

- **free-exercise-db** (https://github.com/yuhonas/free-exercise-db), released under the Unlicense (public domain dedication). Names, fields and instructions are used; images are not copied.
- **Rytivo curated supplement** (`supplement.json`, and the instructions written in `overrides.json`): proprietary to Rytivo.
- wger (CC-BY-SA) and ExerciseDB (commercial) data are deliberately not used. Do not add content copied from them or from other sites.
