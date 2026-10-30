"""Bring the shared exercise catalogue in line with the library file.

Migration 0085 does this once; this is how every later library update reaches
a database, and how one that migrated before the file existed catches up. It
only ever adds and updates shared exercises -- see `core.exercise_library` for
what it never touches -- and running it again changes nothing.
"""

import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.exercise_library import (
    DEFAULT_LIBRARY_PATH,
    ExerciseLibraryError,
    load_exercise_library,
    sync_exercise_library,
)
from core.models import Exercise


class Command(BaseCommand):
    help = "Upsert the shared exercise library into the exercise catalogue."

    def add_arguments(self, parser):
        parser.add_argument(
            "--file",
            default=str(DEFAULT_LIBRARY_PATH),
            help="The library JSON to apply (default: core/data/exercise_library.json).",
        )

    def handle(self, *args, **options):
        try:
            data = load_exercise_library(options["file"])
            # All or nothing: a library that fails half way must not leave
            # the catalogue half updated.
            with transaction.atomic():
                counts = sync_exercise_library(Exercise, data)
        except ExerciseLibraryError as problem:
            raise CommandError(str(problem)) from None
        self.stdout.write(json.dumps(counts, sort_keys=True))
