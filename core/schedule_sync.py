"""Ownership-safe cleanup of planner tasks generated from workout schedules.

Keep this module's deletion callable importable: migrations refer to it.
The FK is deliberately private to the backend, not a writable API field.
"""

from django.db import models, transaction
from django.db.models.deletion import CASCADE, SET_NULL


def untouched_task_filter():
    return models.Q(
        completed_at__isnull=True,
        parent__isnull=True,
        kind="task",
        category="workout",
        owner_id=models.F("source_schedule__owner_id"),
        workout_id=models.F("source_schedule__workout_id"),
        scheduled_date=models.F("source_schedule__scheduled_date"),
        title=models.F("source_schedule__workout__name"),
        notes="",
        scheduled_time__isnull=True,
        duration_minutes__isnull=True,
        priority="normal",
        recurrence__isnull=True,
        subtasks__isnull=True,
    )


def remove_unfinished_generated_tasks(collector, field, sub_objs, using):
    """Delete only untouched pending generated tasks; retain personal history.

    Django's collector calls this for individual, queryset and cascading
    deletions. Working in batches avoids one signal/query per scheduled day.
    A completed task, a task moved to another date/workout, or one containing
    user-authored details/steps is independent data and is only detached.
    Legacy tasks with no provenance never enter this callback.
    """
    unchanged = untouched_task_filter()
    CASCADE(collector, field, sub_objs.filter(unchanged), using)
    SET_NULL(collector, field, sub_objs.exclude(unchanged), using)


remove_unfinished_generated_tasks.lazy_sub_objs = True


def lock_schedule_rows(rows):
    """Within a transaction, lock schedules then their task copies consistently."""
    from .models import PlannerEntry

    ids = list(rows.order_by("scheduled_date", "pk").select_for_update(of=("self",)).values_list("pk", flat=True))
    if ids:
        # Completion and customization UPDATEs must finish before cleanup
        # decides which tasks are disposable, not after it collected them.
        list(PlannerEntry.objects.using(rows.db).filter(source_schedule_id__in=ids)
             .order_by("pk").select_for_update().values_list("pk", flat=True))
    return ids


def untouched_generated_tasks(schedule_ids, using="default"):
    from .models import PlannerEntry

    return PlannerEntry.objects.using(using).filter(
        source_schedule_id__in=schedule_ids,
    ).filter(untouched_task_filter())


def delete_schedule_rows(rows):
    """Lock before collecting dependent tasks, matching sync-planner's lock.

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
        _, counts = rows.model.objects.using(rows.db).filter(pk__in=ids).delete()
        return counts.get(rows.model._meta.label, 0)
