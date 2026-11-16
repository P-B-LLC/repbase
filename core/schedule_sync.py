"""One plan behind Training, Home and the calendar.

`WorkoutSchedule` is the plan: Training and Home's workout card read it. The
calendar and Home's agenda read `PlannerEntry`, so every scheduled day is
given one task standing for it, linked by `source_schedule`. The link is what
lets a change made on either screen reach the other:

* Removing a day from the plan removes its task. A task already ticked off is
  a record of training rather than a plan, so it stays where it happened and
  is only unlinked.
* Deleting, moving or re-pointing a linked task does the same to its day
  (see `plan_workout_task`).

Keep this module's deletion callable importable: migrations refer to it.
The FK is deliberately private to the backend, not a writable API field.
"""

from django.db import models, transaction
from django.db.models import Exists, OuterRef, Q, Subquery
from django.db.models.deletion import CASCADE, SET_NULL
from django.utils import timezone


def unfinished():
    return Q(completed_at__isnull=True)


def stands_for_a_workout(entry):
    """Whether this entry is the calendar's copy of a workout on its day.

    A step belongs to its task, and an event that names a workout is a
    different kind of thing; neither is a plan.
    """
    return entry.kind == "task" and entry.parent_id is None and entry.workout_id is not None


def remove_unfinished_generated_tasks(collector, field, sub_objs, using):
    """A removed day takes its task with it; a ticked-off task stays.

    Django's collector calls this for individual, queryset and cascading
    deletions. Working in batches avoids one signal/query per scheduled day.
    Whatever was written on the task -- a time, notes, steps -- was written
    about a day that is no longer planned, so it goes too. Only completion
    makes it history, and history is detached rather than deleted.
    """
    CASCADE(collector, field, sub_objs.filter(unfinished()), using)
    SET_NULL(collector, field, sub_objs.exclude(unfinished()), using)


remove_unfinished_generated_tasks.lazy_sub_objs = True


def _same_day_schedules(schedule_ids, using):
    from .models import WorkoutSchedule

    return WorkoutSchedule.objects.using(using).filter(
        pk__in=schedule_ids,
        owner_id=OuterRef("owner_id"),
        workout_id=OuterRef("workout_id"),
        scheduled_date=OuterRef("scheduled_date"),
    )


def unlinked_same_day_tasks(schedule_ids, using="default"):
    """Unlinked workout tasks standing for one of these schedules' days.

    Before tasks carried a link, the app wrote them itself and the first sync
    adopted them without one. They are the same workout on the same day --
    the database allows one such task -- so they are that day's task.
    """
    from .models import PlannerEntry

    return PlannerEntry.objects.using(using).filter(
        source_schedule__isnull=True, kind="task", parent__isnull=True,
    ).filter(Exists(_same_day_schedules(schedule_ids, using)))


def lock_schedule_rows(rows):
    """Within a transaction, lock schedules then their tasks consistently."""
    from .models import PlannerEntry

    ids = list(rows.order_by("scheduled_date", "pk").select_for_update(of=("self",)).values_list("pk", flat=True))
    if ids:
        # Completion and customization UPDATEs must finish before cleanup
        # decides which tasks are records, not after it collected them.
        tasks = PlannerEntry.objects.using(rows.db).filter(
            Q(source_schedule_id__in=ids)
            | Q(pk__in=unlinked_same_day_tasks(ids, rows.db).values("pk"))
        )
        list(tasks.order_by("pk").select_for_update().values_list("pk", flat=True))
    return ids


def delete_schedule_rows(rows):
    """Remove days from the plan, and their tasks from the calendar.

    Locks before collecting dependent tasks, matching sync-planner's lock.
    A concurrent sync must finish before deletion inspects the provenance
    rows, or start after removal and find nothing to generate. The locks and
    cleanup share one transaction, including all bulk removal paths.
    """
    with transaction.atomic(using=rows.db):
        # Match sync-planner's lock order even when dates were inserted out of
        # order, so overlapping bulk sync/clear requests do not invert locks.
        ids = lock_schedule_rows(rows)
        if not ids:
            return 0
        # Claimed first so the cascade treats them exactly like tasks that
        # were linked from the start.
        unlinked_same_day_tasks(ids, rows.db).update(
            source_schedule_id=Subquery(_same_day_schedules(ids, rows.db).values("pk")[:1])
        )
        _, counts = rows.model.objects.using(rows.db).filter(pk__in=ids).delete()
        return counts.get(rows.model._meta.label, 0)


def retitle_workout_tasks(workout, old_name):
    """Carry a renamed workout's name onto the tasks still showing the old one.

    A title somebody chose themselves is kept, and so is a finished task's:
    that is what the day was called when it was trained.
    """
    from .models import PlannerEntry

    if workout.name == old_name:
        return
    PlannerEntry.objects.filter(
        owner=workout.owner, workout=workout, kind="task", parent__isnull=True, title=old_name,
    ).filter(unfinished()).update(title=workout.name, updated_at=timezone.now())


def move_schedule_tasks(schedule, old_name):
    """Follow a day that moved, or now holds another workout, with its task."""
    from .models import PlannerEntry

    linked = PlannerEntry.objects.filter(source_schedule=schedule)
    # A ticked-off task records the day it was trained. It stays on it.
    linked.exclude(unfinished()).update(source_schedule=None)
    occupant = PlannerEntry.objects.filter(
        owner=schedule.owner, workout=schedule.workout,
        scheduled_date=schedule.scheduled_date, kind="task", parent__isnull=True,
    ).exclude(source_schedule=schedule).first()
    if occupant is not None:
        # The new day already has this workout's task. It becomes the day's,
        # rather than a second one colliding with the uniqueness rule.
        linked.delete()
        PlannerEntry.objects.filter(pk=occupant.pk).update(source_schedule=schedule)
    else:
        now = timezone.now()
        linked.filter(title=old_name).update(title=schedule.workout.name, updated_at=now)
        linked.update(workout=schedule.workout, scheduled_date=schedule.scheduled_date, updated_at=now)
    has_task = PlannerEntry.objects.filter(source_schedule=schedule).exists()
    schedule.planner_synced_at = timezone.now() if has_task else None
    schedule.save(update_fields=["planner_synced_at", "updated_at"])


def lock_workout_days(owner, days, schedule_id=None):
    """Lock a task's schedule, and those for (workout id, date) pairs, with tasks.

    Taken before a calendar write touches a task, so the order is the same as
    every schedule write's: schedules first, then their tasks.
    """
    from .models import WorkoutSchedule

    match = Q(pk=schedule_id) if schedule_id else Q(pk__in=[])
    for workout_id, day in days:
        if workout_id is not None and day is not None:
            match |= Q(workout_id=workout_id, scheduled_date=day)
    return lock_schedule_rows(WorkoutSchedule.objects.filter(owner=owner).filter(match))


def schedule_id_for(entry):
    """The day a workout task stands for: its link, or else its own day's.

    The fallback is for tasks written before the link existed.
    """
    from .models import WorkoutSchedule

    if entry.source_schedule_id is not None:
        return entry.source_schedule_id
    if not stands_for_a_workout(entry):
        return None
    return WorkoutSchedule.objects.filter(
        owner_id=entry.owner_id, workout_id=entry.workout_id, scheduled_date=entry.scheduled_date,
    ).values_list("pk", flat=True).first()


def plan_workout_task(entry, previous_id=None):
    """Make a calendar write to a workout task true of the plan as well.

    `previous_id` is the schedule the task stood for before the write (see
    `schedule_id_for`). Call inside the transaction that wrote the task, after
    `lock_workout_days`.

    * Still a workout task, on the same day: nothing to do beyond the link.
    * Moved, or pointed at another workout: the day moves with it -- or, when
      that workout is already planned on the new day, the two become one.
    * A new workout task: the workout is planned on its day.
    * No longer a workout task: the day it stood for is removed.
    """
    from .models import PlannerEntry, WorkoutSchedule

    previous = WorkoutSchedule.objects.filter(pk=previous_id).first() if previous_id else None
    if not stands_for_a_workout(entry):
        if previous is not None:
            PlannerEntry.objects.filter(pk=entry.pk).update(source_schedule=None)
            delete_schedule_rows(WorkoutSchedule.objects.filter(pk=previous.pk))
        return None

    target = WorkoutSchedule.objects.filter(
        owner_id=entry.owner_id, workout_id=entry.workout_id, scheduled_date=entry.scheduled_date,
    ).first()
    if target is None and previous is not None:
        previous.workout_id = entry.workout_id
        previous.scheduled_date = entry.scheduled_date
        previous.save(update_fields=["workout", "scheduled_date", "updated_at"])
        target = previous
    elif target is None:
        # get_or_create: a plan written for the same day by another request
        # meanwhile is this one, not a uniqueness error.
        target, _ = WorkoutSchedule.objects.get_or_create(
            owner_id=entry.owner_id, workout_id=entry.workout_id, scheduled_date=entry.scheduled_date,
        )
    PlannerEntry.objects.filter(pk=entry.pk).update(source_schedule=target)
    if previous is not None and previous.pk != target.pk:
        delete_schedule_rows(WorkoutSchedule.objects.filter(pk=previous.pk))
    if target.planner_synced_at is None:
        target.planner_synced_at = timezone.now()
        target.save(update_fields=["planner_synced_at", "updated_at"])
    entry.source_schedule_id = target.pk
    return target


def unplan_workout_task(entry):
    """Remove the day a workout task stood for, before the task is deleted.

    Deleting the calendar's copy of a planned workout is removing the workout
    from that day. Leaving the day behind kept it on Training and Home while
    the calendar said it was gone.
    """
    from .models import WorkoutSchedule

    schedule_id = schedule_id_for(entry)
    if schedule_id is None or not stands_for_a_workout(entry):
        return 0
    return delete_schedule_rows(WorkoutSchedule.objects.filter(owner_id=entry.owner_id, pk=schedule_id))


def schedules_missing_tasks(owner, start, end, today):
    """The days in a range the calendar still has to be given a task for.

    A day never offered one, wherever it falls; and any day from today on
    that has lost its task. With calendar deletions unplanning the day, a
    planned day ahead without a task is always something gone wrong -- most
    often a task removed before that was true -- and Training would show a
    workout the calendar does not. Past days are left as they were.
    """
    from .models import PlannerEntry, WorkoutSchedule

    return (
        WorkoutSchedule.objects.select_for_update(of=("self",))
        .filter(owner=owner, scheduled_date__gte=start, scheduled_date__lte=end)
        .filter(Q(planner_synced_at__isnull=True) | Q(scheduled_date__gte=today))
        .exclude(Exists(PlannerEntry.objects.filter(source_schedule=OuterRef("pk"))))
        .order_by("scheduled_date", "id")
    )
