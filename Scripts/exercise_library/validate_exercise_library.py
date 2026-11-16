#!/usr/bin/env python3
"""Validate the Rytivo exercise library JSON (core/data/exercise_library.json).

Stdlib only. This module is also the single source of truth for the controlled
vocabularies, the slug rule and the name normalization rule;
build_exercise_library.py loads it by file path and reuses them.

Usage:
    python3 Scripts/exercise_library/validate_exercise_library.py [path] [--quiet]

Exits 0 when the file is valid, 1 when it is not (or cannot be read).
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
import unicodedata
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = REPO_ROOT / "core" / "data" / "exercise_library.json"

SCHEMA_VERSION = 1

CATEGORIES = (
    "strength", "olympic", "plyometric", "cardio", "isometric", "stretching", "mobility",
)
EQUIPMENT = (
    "barbell", "dumbbell", "kettlebell", "cable", "machine", "smith_machine", "ez_bar",
    "trap_bar", "resistance_band", "bodyweight", "medicine_ball", "stability_ball",
    "foam_roller", "pull_up_bar", "rings", "suspension_trainer", "sled", "plate",
    "landmine", "box", "rope", "sandbag", "cardio_machine", "other",
)
MUSCLES = (
    "chest", "shoulders", "triceps", "biceps", "forearms", "traps", "lats", "upper_back",
    "lower_back", "abdominals", "obliques", "hip_flexors", "glutes", "quadriceps",
    "hamstrings", "adductors", "abductors", "calves", "tibialis", "neck", "full_body",
)
MOVEMENT_PATTERNS = (
    "squat", "hinge", "lunge", "horizontal_push", "vertical_push", "horizontal_pull",
    "vertical_pull", "carry", "rotation", "core", "isolation", "explosive", "locomotion",
    "mobility",
)
MECHANICS = ("compound", "isolation", None)
FORCES = ("push", "pull", "static", None)
LEVELS = ("beginner", "intermediate", "advanced")
DISCIPLINES = (
    "bodybuilding", "powerlifting", "olympic_weightlifting", "crossfit", "calisthenics",
    "strongman", "functional", "mobility", "rehab", "cardio",
)

TOP_LEVEL_KEYS = ("schema_version", "library_version", "sources", "exercises")
EXERCISE_KEYS = (
    "slug", "name", "aliases", "category", "equipment", "primary_muscles",
    "secondary_muscles", "movement_pattern", "mechanic", "force", "level", "disciplines",
    "family", "instructions",
)

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SLUG_MAX = 100
NAME_MAX = 120
LIBRARY_VERSION_RE = re.compile(r"^\d{4}\.\d{2}\.\d{2}(?:\.\d+)?$")


def ascii_fold(text: str) -> str:
    """Strip accents and drop any character that has no ASCII form."""
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def normalize_name(text: str) -> str:
    """Comparison key for names and aliases.

    Lowercase, ASCII-folded, with every run of punctuation and whitespace removed,
    so "Pull-Up", "Pull Up" and "pullup" compare equal.
    """
    return re.sub(r"[^a-z0-9]+", "", ascii_fold(text).lower())


def slugify(text: str) -> str:
    """Kebab-case slug of a name: "Farmer's Walk" -> "farmers-walk"."""
    text = ascii_fold(text).lower().replace("&", " and ")
    text = re.sub(r"['’]", "", text)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    if len(text) > SLUG_MAX:
        text = text[:SLUG_MAX].rsplit("-", 1)[0]
    return text


def sort_key(exercise: dict) -> tuple[str, str]:
    return (exercise["name"].casefold(), exercise["slug"])


def _check_string_list(value, field, where, errors, vocab=None, allow_empty=True, allow_repeats=False):
    if not isinstance(value, list):
        errors.append(f"{where}: {field} must be a list")
        return []
    if not allow_empty and not value:
        errors.append(f"{where}: {field} must not be empty")
    seen = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            errors.append(f"{where}: {field} contains a non-string or empty value: {item!r}")
            continue
        if item != item.strip():
            errors.append(f"{where}: {field} value has surrounding whitespace: {item!r}")
        if vocab is not None and item not in vocab:
            errors.append(f"{where}: {field} value {item!r} is not in the vocabulary")
        if item in seen and not allow_repeats:
            errors.append(f"{where}: {field} repeats {item!r}")
        seen.add(item)
    return [v for v in value if isinstance(v, str)]


def validate(data) -> list[str]:
    """Return a list of human-readable problems. An empty list means valid."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["top level must be a JSON object"]
    if tuple(data.keys()) != TOP_LEVEL_KEYS:
        errors.append(f"top-level keys must be exactly {list(TOP_LEVEL_KEYS)} in that order, got {list(data.keys())}")
    if data.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    lib_version = data.get("library_version")
    if not isinstance(lib_version, str) or not LIBRARY_VERSION_RE.match(lib_version):
        errors.append("library_version must be a string like 2026.10.29")
    sources = data.get("sources")
    if not isinstance(sources, list) or not sources:
        errors.append("sources must be a non-empty list")
    else:
        for i, src in enumerate(sources):
            if not isinstance(src, dict) or not isinstance(src.get("name"), str) or not isinstance(src.get("license"), str):
                errors.append(f"sources[{i}] needs string 'name' and 'license'")
    exercises = data.get("exercises")
    if not isinstance(exercises, list) or not exercises:
        errors.append("exercises must be a non-empty list")
        return errors

    slugs: dict[str, int] = {}
    name_keys: dict[str, str] = {}
    alias_owner: dict[str, str] = {}
    families: dict[str, list[str]] = collections.defaultdict(list)

    for idx, ex in enumerate(exercises):
        where = f"exercises[{idx}]"
        if not isinstance(ex, dict):
            errors.append(f"{where}: must be an object")
            continue
        slug = ex.get("slug")
        if isinstance(slug, str):
            where = f"exercises[{idx}] ({slug})"
        if tuple(ex.keys()) != EXERCISE_KEYS:
            errors.append(f"{where}: keys must be exactly {list(EXERCISE_KEYS)} in that order")

        name = ex.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append(f"{where}: name must be a non-empty string")
            name = ""
        else:
            if name != name.strip() or "  " in name:
                errors.append(f"{where}: name has stray whitespace: {name!r}")
            if len(name) > NAME_MAX:
                errors.append(f"{where}: name longer than {NAME_MAX} characters")
            if not (name[0].isupper() or name[0].isdigit()):
                errors.append(f"{where}: name must start with a capital letter or digit: {name!r}")

        if not isinstance(slug, str) or not SLUG_RE.match(slug) or len(slug) > SLUG_MAX:
            errors.append(f"{where}: slug must be lowercase kebab-case, at most {SLUG_MAX} characters")
        elif name and slug != slugify(name):
            errors.append(f"{where}: slug {slug!r} does not match the name (expected {slugify(name)!r})")
        if isinstance(slug, str):
            if slug in slugs:
                errors.append(f"{where}: duplicate slug (also exercises[{slugs[slug]}])")
            slugs[slug] = idx

        if name:
            key = normalize_name(name)
            if not key:
                errors.append(f"{where}: name normalizes to an empty string")
            elif key in name_keys:
                errors.append(f"{where}: name {name!r} duplicates {name_keys[key]!r} after normalization")
            else:
                name_keys[key] = slug if isinstance(slug, str) else name

        aliases = _check_string_list(ex.get("aliases"), "aliases", where, errors)
        own_keys = set()
        for alias in aliases:
            akey = normalize_name(alias)
            if not akey:
                errors.append(f"{where}: alias {alias!r} normalizes to an empty string")
                continue
            if name and akey == normalize_name(name):
                errors.append(f"{where}: alias {alias!r} repeats the exercise's own name")
            if akey in own_keys:
                errors.append(f"{where}: alias {alias!r} repeats another alias of the same exercise")
            own_keys.add(akey)

        if ex.get("category") not in CATEGORIES:
            errors.append(f"{where}: category {ex.get('category')!r} is not in the vocabulary")
        if ex.get("equipment") not in EQUIPMENT:
            errors.append(f"{where}: equipment {ex.get('equipment')!r} is not in the vocabulary")
        primary = _check_string_list(ex.get("primary_muscles"), "primary_muscles", where, errors, MUSCLES, allow_empty=False)
        secondary = _check_string_list(ex.get("secondary_muscles"), "secondary_muscles", where, errors, MUSCLES)
        both = sorted(set(primary) & set(secondary))
        if both:
            errors.append(f"{where}: muscles in both primary and secondary: {both}")
        if ex.get("movement_pattern") not in MOVEMENT_PATTERNS:
            errors.append(f"{where}: movement_pattern {ex.get('movement_pattern')!r} is not in the vocabulary")
        if ex.get("mechanic") not in MECHANICS:
            errors.append(f"{where}: mechanic {ex.get('mechanic')!r} is not in the vocabulary")
        if ex.get("force") not in FORCES:
            errors.append(f"{where}: force {ex.get('force')!r} is not in the vocabulary")
        if ex.get("level") not in LEVELS:
            errors.append(f"{where}: level {ex.get('level')!r} is not in the vocabulary")
        _check_string_list(ex.get("disciplines"), "disciplines", where, errors, DISCIPLINES, allow_empty=False)

        family = ex.get("family")
        if not isinstance(family, str) or not SLUG_RE.match(family) or len(family) > SLUG_MAX:
            errors.append(f"{where}: family must be lowercase kebab-case, at most {SLUG_MAX} characters")
        elif isinstance(slug, str):
            families[family].append(slug)

        instructions = _check_string_list(ex.get("instructions"), "instructions", where, errors, allow_empty=False, allow_repeats=True)
        for step in instructions:
            if "\n" in step or "\t" in step:
                errors.append(f"{where}: instruction step contains a newline or tab")

    # Aliases must be unique across the whole file and never equal another exercise's name.
    for idx, ex in enumerate(exercises):
        if not isinstance(ex, dict) or not isinstance(ex.get("aliases"), list):
            continue
        slug = ex.get("slug")
        for alias in ex["aliases"]:
            if not isinstance(alias, str):
                continue
            akey = normalize_name(alias)
            if not akey:
                continue
            if akey in name_keys and name_keys[akey] != slug:
                errors.append(f"exercises[{idx}] ({slug}): alias {alias!r} equals the name of {name_keys[akey]}")
            if akey in alias_owner and alias_owner[akey] != slug:
                errors.append(f"exercises[{idx}] ({slug}): alias {alias!r} is also an alias of {alias_owner[akey]}")
            alias_owner.setdefault(akey, slug)

    for family, members in families.items():
        if len(members) == 1 and members[0] != family:
            errors.append(f"family {family!r} has a single member ({members[0]}); it must use that exercise's own slug")

    try:
        keys = [sort_key(ex) for ex in exercises]
        if keys != sorted(keys):
            errors.append("exercises must be sorted by name, case-insensitively")
    except (KeyError, TypeError, AttributeError):
        errors.append("could not check sort order because some exercises lack a name or slug")

    return errors


def summarize(data) -> str:
    exercises = data.get("exercises", []) if isinstance(data, dict) else []
    lines = [f"Exercises: {len(exercises)}"]

    def block(title, counter):
        lines.append(f"\n{title}:")
        for key, count in sorted(counter.items(), key=lambda kv: (-kv[1], str(kv[0]))):
            lines.append(f"  {str(key):<24}{count:>5}")

    block("By category", collections.Counter(ex.get("category") for ex in exercises))
    block("By equipment", collections.Counter(ex.get("equipment") for ex in exercises))
    block("By movement pattern", collections.Counter(ex.get("movement_pattern") for ex in exercises))
    block("By discipline", collections.Counter(d for ex in exercises for d in ex.get("disciplines", [])))
    block("By level", collections.Counter(ex.get("level") for ex in exercises))
    block("By primary muscle", collections.Counter(m for ex in exercises for m in ex.get("primary_muscles", [])))
    block("By muscle (primary or secondary)", collections.Counter(
        m for ex in exercises for m in set(ex.get("primary_muscles", [])) | set(ex.get("secondary_muscles", []))))
    family_sizes = collections.Counter(ex.get("family") for ex in exercises)
    multi = sum(1 for n in family_sizes.values() if n > 1)
    aliases = sum(len(ex.get("aliases", [])) for ex in exercises)
    with_alias = sum(1 for ex in exercises if ex.get("aliases"))
    lines.append(f"\nFamilies: {len(family_sizes)} ({multi} with variations)")
    lines.append(f"Aliases: {aliases} across {with_alias} exercises")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("path", nargs="?", default=str(DEFAULT_PATH), help="library JSON to check")
    parser.add_argument("--quiet", action="store_true", help="print only problems and the verdict")
    args = parser.parse_args(argv)

    path = Path(args.path)
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"FAIL: cannot read {path}: {exc}", file=sys.stderr)
        return 1

    errors = validate(data)
    if not args.quiet:
        print(summarize(data))
        print()
    if errors:
        for err in errors[:200]:
            print(f"ERROR: {err}", file=sys.stderr)
        if len(errors) > 200:
            print(f"... and {len(errors) - 200} more", file=sys.stderr)
        print(f"FAIL: {len(errors)} problem(s) in {path}", file=sys.stderr)
        return 1
    print(f"OK: {path} is valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
