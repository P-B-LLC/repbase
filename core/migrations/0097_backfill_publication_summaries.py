"""Fill in, for posts published before 0089, what a new post now stores when
it is written: the card figures on each workout and recipe snapshot, and the
hashtags in each caption.

The arithmetic and the hashtag rule are written out here rather than imported
from `core.models` and `core.publication`. A migration runs against the
schema as it stood at this point, for as long as the project exists; code
imported from the app would change underneath it.

Nothing is undone on the way back: the columns go with 0089.
"""

import re
import unicodedata
from decimal import Decimal

from django.db import migrations

BATCH = 500
MAX_HASHTAGS = 10
MAX_HASHTAG_LENGTH = 50
HASHTAG = re.compile(r"(?<![\w&#＃/])[#＃](\w+)")
NUTRIENTS = ("calories", "protein_grams", "carbohydrate_grams", "fat_grams")


def hashtags_in(caption):
    found = []
    for raw in HASHTAG.findall(caption or ""):
        tag = unicodedata.normalize("NFKC", raw).casefold()
        if not tag or len(tag) > MAX_HASHTAG_LENGTH or tag.isdigit() or tag in found:
            continue
        found.append(tag)
        if len(found) == MAX_HASHTAGS:
            break
    return found


def in_batches(queryset):
    batch = []
    for row in queryset.iterator(chunk_size=BATCH):
        batch.append(row)
        if len(batch) == BATCH:
            yield batch
            batch = []
    if batch:
        yield batch


def summarise_workouts(apps):
    PostWorkout = apps.get_model("core", "PostWorkout")
    PostWorkoutExercise = apps.get_model("core", "PostWorkoutExercise")
    for workouts in in_batches(PostWorkout.objects.order_by("pk")):
        rows = {}
        for exercise in PostWorkoutExercise.objects.filter(post_workout__in=workouts):
            rows.setdefault(exercise.post_workout_id, []).append(exercise)
        for workout in workouts:
            exercises = rows.get(workout.pk, [])
            volumes = [row.volume_kg for row in exercises if row.volume_kg is not None]
            workout.exercise_count = len(exercises)
            workout.total_set_count = sum(row.set_count for row in exercises)
            workout.total_volume_kg = sum(volumes) if volumes else None
        PostWorkout.objects.bulk_update(
            workouts, ["exercise_count", "total_set_count", "total_volume_kg"]
        )


def summarise_meals(apps):
    PostMeal = apps.get_model("core", "PostMeal")
    PostMealEntry = apps.get_model("core", "PostMealEntry")
    for meals in in_batches(PostMeal.objects.order_by("pk")):
        rows = {}
        for entry in PostMealEntry.objects.filter(post_meal__in=meals):
            rows.setdefault(entry.post_meal_id, []).append(entry)
        for meal in meals:
            entries = rows.get(meal.pk, [])
            meal.entry_count = len(entries)
            for field in NUTRIENTS:
                total = sum(
                    (
                        (getattr(row, field) or Decimal("0")) * (row.servings or Decimal("0"))
                        for row in entries
                    ),
                    Decimal("0"),
                )
                setattr(meal, f"total_{field}", total.quantize(Decimal("0.01")))
        PostMeal.objects.bulk_update(
            meals, ["entry_count", *(f"total_{field}" for field in NUTRIENTS)]
        )


def tag_captions(apps):
    Post = apps.get_model("core", "Post")
    PostHashtag = apps.get_model("core", "PostHashtag")
    tagged = Post.objects.filter(caption__contains="#").order_by("pk")
    for posts in in_batches(tagged):
        links = []
        changed = []
        for post in posts:
            tags = hashtags_in(post.caption)
            if not tags:
                continue
            post.hashtags = tags
            changed.append(post)
            links.extend(PostHashtag(post=post, tag=tag) for tag in tags)
        Post.objects.bulk_update(changed, ["hashtags"])
        # Safe to run twice: a row already there is left alone.
        PostHashtag.objects.bulk_create(links, ignore_conflicts=True)


def forwards(apps, schema_editor):
    summarise_workouts(apps)
    summarise_meals(apps)
    tag_captions(apps)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0096_publication_metadata"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
