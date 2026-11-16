from django.db import migrations
from django.db.models import Q

from core.activities import DEFAULT_ACTIVITY, LEGACY_DEFAULT_ACTIVITY
from core.exercise_library import search_text_for


def backfill(apps, schema_editor):
    """Give every existing row the value 0083 added an empty column for.

    Workouts: the activity their `workout_type` has always meant -- a lifting
    workout is weightlifting, a biking one cycling. Nothing more specific can
    be inferred, and nothing less specific is allowed: a response always
    carries an activity. One UPDATE per family rather than a save per row.

    Posted workouts: the same, from the type frozen on the snapshot. An ad-hoc
    session's post has no type and keeps no activity, as before.

    Exercises: the search text `?search=` now reads, from the name alone --
    nothing before the library has aliases. Every row, custom ones included,
    or a person's own exercises would stop turning up in their own search.
    """
    WorkoutTemplate = apps.get_model("core", "WorkoutTemplate")
    PostWorkout = apps.get_model("core", "PostWorkout")
    Exercise = apps.get_model("core", "Exercise")

    for family, activity in LEGACY_DEFAULT_ACTIVITY.items():
        WorkoutTemplate.objects.filter(activity="", workout_type=family).update(activity=activity)
        PostWorkout.objects.filter(activity__isnull=True, workout_type=family).update(activity=activity)
    # A type outside the four cannot be written through the API, but a row
    # that somehow holds one still needs an activity to send.
    WorkoutTemplate.objects.filter(activity="").update(activity=DEFAULT_ACTIVITY)

    pending = Exercise.objects.filter(Q(search_text="") | Q(search_text__isnull=True))
    for exercise in pending.only("pk", "name", "aliases").iterator():
        Exercise.objects.filter(pk=exercise.pk).update(
            search_text=search_text_for(exercise.name, exercise.aliases)
        )


class Migration(migrations.Migration):
    dependencies = [("core", "0083_activity_tracking_and_exercise_library")]

    # Reversing leaves the values in place: 0083's reverse drops the columns
    # that hold them, and until then they are correct data, not debris.
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
