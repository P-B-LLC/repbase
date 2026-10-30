"""The shared exercise library, and keeping the database in step with it.

The library is a JSON file in the repository (`core/data/exercise_library.json`),
built offline from its sources. The database holds the catalogue the app
actually reads, so the two are reconciled here: every library exercise becomes,
or updates, one *shared* row (`created_by IS NULL`), found by its slug.

What this never does matters as much as what it does:

* It never deletes. Sessions, plans and posts point at exercises with
  PROTECT, and a library that drops an entry must not take somebody's
  training history with it. A row the library no longer lists simply stays.
* It never touches a custom exercise. A person's own "Bench Press" is their
  vocabulary and their history; the library adding a shared one beside it
  changes nothing about theirs.
* It never makes a second row for an exercise the catalogue already has. A
  shared row from before the library -- no slug, and the entry's name or one
  of its aliases -- is adopted by giving it the slug, so the history already
  attached to it carries on.

It runs from a data migration with the historical model and from the
`sync_exercise_library` command with the real one, so it takes the model as a
parameter, uses nothing but plain ORM calls on it, and computes `search_text`
itself rather than relying on `Exercise.save()`, which a historical model does
not have. Running it twice changes nothing the second time.

Nothing here imports `core.models`, which imports this.
"""

import json
from pathlib import Path

#: Where the library lives in the repository.
DEFAULT_LIBRARY_PATH = Path(__file__).resolve().parent / "data" / "exercise_library.json"

#: The file layouts this server can read. A newer layout is refused rather
#: than half-read: guessing at fields would write a catalogue nobody built.
SUPPORTED_SCHEMA_VERSIONS = (1,)

#: Column widths, checked here because SQLite does not enforce them and a
#: library that only fails on PostgreSQL fails at deploy time.
MAX_NAME_LENGTH = 150
MAX_SLUG_LENGTH = 100
MAX_ATTRIBUTE_LENGTH = 50
MAX_FAMILY_LENGTH = 100

#: The display region `muscle_group` shows, from an exercise's first primary
#: muscle. `muscle_group` predates the library and is free text the app prints
#: as a heading, so it gets a word a person reads rather than a muscle slug.
MUSCLE_REGIONS = {
    "chest": "Chest",
    "lats": "Back",
    "traps": "Back",
    "upper_back": "Back",
    "lower_back": "Back",
    "shoulders": "Shoulders",
    "biceps": "Arms",
    "triceps": "Arms",
    "forearms": "Arms",
    "abdominals": "Core",
    "obliques": "Core",
    "hip_flexors": "Core",
    "quadriceps": "Legs",
    "hamstrings": "Legs",
    "adductors": "Legs",
    "abductors": "Legs",
    "calves": "Legs",
    "tibialis": "Legs",
    "glutes": "Glutes",
    "neck": "Neck",
    "full_body": "Full Body",
}

#: The single-valued text attributes, with their column widths.
_ATTRIBUTES = {
    "category": MAX_ATTRIBUTE_LENGTH,
    "equipment": MAX_ATTRIBUTE_LENGTH,
    "movement_pattern": MAX_ATTRIBUTE_LENGTH,
    "level": MAX_ATTRIBUTE_LENGTH,
    "mechanic": MAX_ATTRIBUTE_LENGTH,
    "force": MAX_ATTRIBUTE_LENGTH,
    "family": MAX_FAMILY_LENGTH,
}

#: The list attributes. Each is a list of strings.
_LISTS = ("aliases", "primary_muscles", "secondary_muscles", "disciplines", "instructions")


class ExerciseLibraryError(ValueError):
    """The library file cannot be applied as it stands."""


def search_text_for(name, aliases):
    """What a search matches: the name and every alias, lower-cased.

    One line each, so a search can match within a name or an alias but never
    across the end of one and the start of the next.
    """
    words = [name or "", *(aliases or [])]
    return "\n".join(word.strip().lower() for word in words if word and word.strip())


def muscle_index_for(muscles):
    """The primary muscles as one delimited string, for an exact-match filter.

    JSONField's `contains` lookup is what would answer "which exercises work
    the lats", and SQLite has no implementation of it. Delimiting every value
    -- `|lats|biceps|` -- lets a plain `contains` on `|lats|` find exactly
    that muscle, on both databases, and never a muscle whose name merely
    contains it.
    """
    values = [muscle.strip().lower() for muscle in muscles or [] if muscle and muscle.strip()]
    return f"|{'|'.join(values)}|" if values else ""


def region_for(primary_muscles):
    """`muscle_group` for an exercise, or None when its muscle has no region."""
    if not primary_muscles:
        return None
    return MUSCLE_REGIONS.get(primary_muscles[0].strip().lower())


def load_exercise_library(path=DEFAULT_LIBRARY_PATH):
    """The parsed library file, or ExerciseLibraryError saying why not."""
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise ExerciseLibraryError(f"No exercise library at {path}.") from None
    except (OSError, ValueError) as problem:
        raise ExerciseLibraryError(f"Could not read the exercise library at {path}: {problem}") from None


def _text(entry, key, max_length, slug, *, required=False):
    value = entry.get(key)
    if value is None:
        if required:
            raise ExerciseLibraryError(f"Exercise {slug!r} has no {key}.")
        return ""
    if not isinstance(value, str):
        raise ExerciseLibraryError(f"Exercise {slug!r}: {key} must be text.")
    value = value.strip()
    if required and not value:
        raise ExerciseLibraryError(f"Exercise {slug!r} has no {key}.")
    if len(value) > max_length:
        raise ExerciseLibraryError(
            f"Exercise {slug!r}: {key} is longer than {max_length} characters."
        )
    return value


def _strings(entry, key, slug):
    value = entry.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ExerciseLibraryError(f"Exercise {slug!r}: {key} must be a list of text.")
    return [item.strip() for item in value if item.strip()]


def library_rows(data):
    """Every library entry as the column values its row should hold.

    Checked in full before anything is written, so a bad entry near the end
    of the file cannot leave the catalogue half updated.
    """
    if not isinstance(data, dict):
        raise ExerciseLibraryError("The exercise library must be a JSON object.")
    version = data.get("schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ExerciseLibraryError(
            f"Unknown exercise library schema_version {version!r}; this server "
            f"reads {', '.join(str(v) for v in SUPPORTED_SCHEMA_VERSIONS)}. "
            f"Deploy a server that understands it before the file."
        )
    entries = data.get("exercises")
    if not isinstance(entries, list):
        raise ExerciseLibraryError("The exercise library has no list of exercises.")

    rows = []
    seen = set()
    for position, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ExerciseLibraryError(f"Exercise {position} is not an object.")
        slug = _text(entry, "slug", MAX_SLUG_LENGTH, f"#{position}", required=True)
        if slug in seen:
            raise ExerciseLibraryError(f"Exercise {slug!r} appears twice.")
        seen.add(slug)
        values = {
            "slug": slug,
            "name": _text(entry, "name", MAX_NAME_LENGTH, slug, required=True),
        }
        for key, width in _ATTRIBUTES.items():
            values[key] = _text(entry, key, width, slug)
        for key in _LISTS:
            values[key] = _strings(entry, key, slug)
        values["search_text"] = search_text_for(values["name"], values["aliases"])
        values["primary_muscle_index"] = muscle_index_for(values["primary_muscles"])
        rows.append(values)
    return rows


def sync_exercise_library(Exercise, data):
    """Bring the shared catalogue in line with the library. Returns counts.

    `Exercise` is the model class -- historical in a migration, real from the
    command. `data` is the parsed library file. Call it inside a transaction;
    both callers do.
    """
    rows = library_rows(data)
    # Only the columns this model has. A migration runs against the model as
    # it stood then, and a later field the library learns to fill must not
    # make that migration fail on a database being built from scratch.
    columns = {field.name for field in Exercise._meta.get_fields()}

    custom_slugs = set(
        Exercise.objects.filter(created_by__isnull=False, slug__isnull=False)
        .values_list("slug", flat=True)
    )
    clashes = sorted(custom_slugs & {row["slug"] for row in rows})
    if clashes:
        raise ExerciseLibraryError(
            f"Custom exercises already use the library slugs {clashes}; clear "
            f"them in the admin before syncing."
        )

    shared = Exercise.objects.filter(created_by__isnull=True)
    by_slug = {row.slug: row for row in shared.filter(slug__isnull=False)}
    # The oldest unslugged shared row of each name is the one adopted, so the
    # choice is the same every run.
    unslugged = {}
    for row in shared.filter(slug__isnull=True).order_by("pk"):
        unslugged.setdefault(row.name.strip().lower(), row)

    # Each entry's row: by slug, else adopted by name, else adopted by an
    # alias -- the old "Lateral Raise" becomes "Lateral Raise (Dumbbell)"
    # rather than staying beside a new row and splitting its history. Every
    # name is tried before any alias, so an alias never takes the row another
    # entry's own name would have claimed.
    matches = []
    for values in rows:
        row = by_slug.get(values["slug"])
        adopted = False
        if row is None:
            row = unslugged.pop(values["name"].strip().lower(), None)
            adopted = row is not None
        matches.append([values, row, adopted])
    for match in matches:
        if match[1] is not None:
            continue
        for alias in match[0].get("aliases") or []:
            row = unslugged.pop(alias.strip().lower(), None)
            if row is not None:
                match[1], match[2] = row, True
                break

    counts = {"created": 0, "adopted": 0, "updated": 0, "unchanged": 0}
    for values, row, adopted in matches:
        values = {key: value for key, value in values.items() if key in columns}
        region = region_for(values.get("primary_muscles"))
        if row is None:
            values["muscle_group"] = region or ""
            Exercise.objects.create(**values)
            counts["created"] += 1
            continue

        # A muscle with no region keeps whatever heading the row had, rather
        # than blanking one somebody chose before the library existed.
        values["muscle_group"] = region if region is not None else row.muscle_group
        changed = [key for key, value in values.items() if getattr(row, key) != value]
        if not changed:
            counts["unchanged"] += 1
            continue
        for key in changed:
            setattr(row, key, values[key])
        row.save(update_fields=[*changed, "updated_at"])
        counts["adopted" if adopted else "updated"] += 1
    return counts
