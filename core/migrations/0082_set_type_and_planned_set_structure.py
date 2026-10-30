import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0081_plannerentry_source_schedule")]

    # Additive only. Every existing set becomes "normal", which is what it
    # already meant to everything that read it, and every existing relation
    # gets no rest, no superset and no set types -- exactly how it behaved
    # before. No backfill is needed and none is attempted.
    operations = [
        migrations.AddField(
            model_name="setentry",
            name="set_type",
            field=models.CharField(
                choices=[
                    ("normal", "Normal"),
                    ("warmup", "Warm-up"),
                    ("drop", "Drop set"),
                    ("failure", "Failure"),
                ],
                default="normal",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="workoutexercise",
            name="rest_seconds",
            field=models.PositiveIntegerField(
                blank=True,
                null=True,
                validators=[django.core.validators.MaxValueValidator(3600)],
            ),
        ),
        migrations.AddField(
            model_name="workoutexercise",
            name="set_types",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="workoutexercise",
            name="superset_group",
            field=models.PositiveSmallIntegerField(
                blank=True,
                null=True,
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(100),
                ],
            ),
        ),
    ]
