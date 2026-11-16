"""Link every workout task on the calendar to the scheduled day it stands for.

0081 added the link but deliberately guessed nothing for older rows, so every
task written before it -- by the app itself, then by the first sync endpoint,
which adopted tasks without linking them -- was left out of the cleanup that
follows a removed day. Deleting a workout from Training left it on the
calendar and on Home's agenda, permanently.

The plan (`WorkoutSchedule`) is what Training reads and the source of truth,
so this repairs the calendar against it:

1. A linked task moved off its day under the old rules goes back to that day.
   One already ticked off stays where it was ticked, unlinked: it records
   when the workout was trained.
2. An unlinked task on a day its workout is scheduled is that day's task, and
   is linked. The database allows only one per workout per day.
3. An unfinished workout task on a day its workout is not scheduled is what
   a removed day left behind, and is deleted, with any steps. Ticked-off
   tasks, events and tasks with no workout are left exactly as they are.

Days ahead that have no task are given one by the next sync-planner, which
now repairs those itself. Reversing this leaves the data as it is: the rows it
removed were stale copies, and the links it wrote are what 0081 intended.
"""

from django.db import migrations
from django.db.models import Exists, F, OuterRef, Subquery
from django.utils import timezone


def link_workout_calendar_tasks(apps, schema_editor):
    PlannerEntry = apps.get_model("core", "PlannerEntry")
    WorkoutSchedule = apps.get_model("core", "WorkoutSchedule")
    workout_tasks = PlannerEntry.objects.filter(kind="task", parent__isnull=True, workout__isnull=False)

    # 1. Moved off their day.
    moved = workout_tasks.filter(source_schedule__isnull=False).exclude(
        workout_id=F("source_schedule__workout_id"),
        scheduled_date=F("source_schedule__scheduled_date"),
    )
    for task in moved.select_related("source_schedule__workout", "workout").order_by("pk"):
        schedule = task.source_schedule
        if task.completed_at is not None:
            PlannerEntry.objects.filter(pk=task.pk).update(source_schedule=None)
            continue
        occupied = workout_tasks.filter(
            owner_id=task.owner_id, workout_id=schedule.workout_id, scheduled_date=schedule.scheduled_date,
        ).exclude(pk=task.pk)
        if occupied.exists():
            # The day already has this workout's task; step 2 links that one.
            PlannerEntry.objects.filter(pk=task.pk).delete()
            continue
        title = schedule.workout.name if task.title == task.workout.name else task.title
        PlannerEntry.objects.filter(pk=task.pk).update(
            workout_id=schedule.workout_id, scheduled_date=schedule.scheduled_date, title=title,
            updated_at=timezone.now(),
        )

    # 2. Unlinked, on a day the workout is scheduled.
    same_day = WorkoutSchedule.objects.filter(
        owner_id=OuterRef("owner_id"), workout_id=OuterRef("workout_id"), scheduled_date=OuterRef("scheduled_date"),
    )
    adoptable = (
        workout_tasks.filter(source_schedule__isnull=True)
        .annotate(schedule_id=Subquery(same_day.values("pk")[:1]))
        .filter(schedule_id__isnull=False)
    )
    for task_id, schedule_id in adoptable.values_list("pk", "schedule_id").order_by("pk"):
        if PlannerEntry.objects.filter(source_schedule_id=schedule_id).exists():
            continue
        PlannerEntry.objects.filter(pk=task_id).update(source_schedule_id=schedule_id)
        WorkoutSchedule.objects.filter(pk=schedule_id, planner_synced_at__isnull=True).update(
            planner_synced_at=timezone.now()
        )

    # 3. Left behind by a removed day.
    workout_tasks.filter(source_schedule__isnull=True, completed_at__isnull=True).exclude(
        Exists(same_day)
    ).delete()


class Migration(migrations.Migration):
    dependencies = [("core", "0086_media_posts")]

    operations = [
        migrations.RunPython(link_workout_calendar_tasks, migrations.RunPython.noop),
    ]
