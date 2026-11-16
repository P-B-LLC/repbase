from django.conf import settings
from django.db import migrations

from core.exercise_library import (
    DEFAULT_LIBRARY_PATH,
    load_exercise_library,
    sync_exercise_library,
)


def sync(apps, schema_editor):
    """Load the shared exercise library into the catalogue.

    A no-op when the file is not there. The library is built separately from
    this code, and a checkout without it -- or a database migrated before it
    was committed -- must still migrate; `manage.py sync_exercise_library`
    applies it whenever it arrives, and is what later library updates use.

    Also a no-op when `EXERCISE_LIBRARY_SYNC_ON_MIGRATE` is off, which the test
    runner does: a test database seeded from whatever the library holds today
    would make every exercise test depend on it, and the transactional tests'
    flush would make that depend on the order tests run in.

    A file that is there but cannot be applied stops the migration. Deploying
    a catalogue nobody can read is worse than not deploying.
    """
    if not getattr(settings, "EXERCISE_LIBRARY_SYNC_ON_MIGRATE", True):
        return
    if not DEFAULT_LIBRARY_PATH.exists():
        return
    Exercise = apps.get_model("core", "Exercise")
    sync_exercise_library(Exercise, load_exercise_library(DEFAULT_LIBRARY_PATH))


class Migration(migrations.Migration):
    dependencies = [("core", "0084_backfill_activity_and_search_text")]

    # Never reversed by deleting: sessions and plans point at these rows, and
    # the rows outlive the library that described them.
    operations = [migrations.RunPython(sync, migrations.RunPython.noop)]
