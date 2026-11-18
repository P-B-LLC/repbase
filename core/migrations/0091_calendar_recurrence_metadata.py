import uuid
from zoneinfo import available_timezones

import django.db.models.deletion
from django.db import migrations, models


COLORS = [
    ("coral", "Coral"), ("amber", "Amber"), ("sage", "Sage"),
    ("teal", "Teal"), ("sky", "Sky"), ("lavender", "Lavender"),
    ("rose", "Rose"), ("slate", "Slate"),
]


def retain_existing_occurrence_identity(apps, schema_editor):
    Entry = apps.get_model("core", "PlannerEntry")
    Rule = apps.get_model("core", "PlannerRecurrence")
    Profile = apps.get_model("core", "RepbaseUser")
    alias = schema_editor.connection.alias
    Entry.objects.using(alias).filter(recurrence__isnull=False).update(
        recurrence_date=models.F("scheduled_date")
    )
    zones = available_timezones()
    for profile in Profile.objects.using(alias).only("id", "time_zone").iterator():
        zone = profile.time_zone if profile.time_zone in zones else "UTC"
        Entry.objects.using(alias).filter(
            owner_id=profile.pk, scheduled_time__isnull=False,
        ).update(scheduling_timezone=zone)
        Rule.objects.using(alias).filter(
            owner_id=profile.pk, scheduled_time__isnull=False,
        ).update(scheduling_timezone=zone)
    for rule in Rule.objects.using(alias).all().iterator():
        # AddField's callable default can be evaluated once for a whole table;
        # assign separately so unrelated pre-existing series never share an ID.
        rule.series_id = uuid.uuid4()
        first = Entry.objects.using(alias).filter(
            recurrence_id=rule.pk, parent__isnull=True,
        ).order_by("scheduled_date", "id").first()
        if first is not None:
            rule.workout_id = first.workout_id
        rule.save(using=alias, update_fields=["series_id", "workout"])


class Migration(migrations.Migration):
    dependencies = [("core", "0090_food_portion_precision")]

    operations = [
        migrations.AddField(
            model_name="plannerentry", name="color",
            field=models.CharField(blank=True, choices=COLORS, default="", max_length=12),
        ),
        migrations.AddField(
            model_name="plannerentry", name="scheduling_timezone",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="plannerentry", name="recurrence_date",
            field=models.DateField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="plannerrecurrence", name="series_id",
            field=models.UUIDField(db_index=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="plannerrecurrence", name="kind",
            field=models.CharField(choices=[("task", "Task"), ("event", "Event")], default="task", max_length=10),
        ),
        migrations.AddField(
            model_name="plannerrecurrence", name="color",
            field=models.CharField(blank=True, choices=COLORS, default="", max_length=12),
        ),
        migrations.AddField(
            model_name="plannerrecurrence", name="scheduling_timezone",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="plannerrecurrence", name="identity_offset_days",
            field=models.IntegerField(default=0, editable=False),
        ),
        migrations.AddField(
            model_name="plannerrecurrence", name="workout",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="planner_recurrences", to="core.workouttemplate",
            ),
        ),
        migrations.RunPython(retain_existing_occurrence_identity, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="plannerrecurrence", name="series_id",
            field=models.UUIDField(db_index=True, default=uuid.uuid4, editable=False),
        ),
        migrations.AddIndex(
            model_name="plannerentry",
            index=models.Index(fields=["recurrence", "recurrence_date"], name="planner_occurrence_identity"),
        ),
        migrations.AddIndex(
            model_name="plannerentry",
            index=models.Index(fields=["recurrence", "scheduled_date"], name="planner_shared_date"),
        ),
        migrations.RemoveConstraint(
            model_name="plannerrecurrence", name="planner_repeat_ends_after_it_starts",
        ),
        migrations.AddConstraint(
            model_name="plannerrecurrence",
            constraint=models.CheckConstraint(
                condition=models.Q(ends_on__isnull=True) | models.Q(ends_on__gte=models.F("starts_on")),
                name="planner_repeat_ends_after_it_starts",
            ),
        ),
    ]
