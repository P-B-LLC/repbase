import core.schedule_sync
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0080_signup_agreement_acceptance")]

    operations = [
        migrations.AddField(
            model_name="plannerentry",
            name="source_schedule",
            field=models.ForeignKey(
                blank=True,
                editable=False,
                null=True,
                on_delete=core.schedule_sync.remove_unfinished_generated_tasks,
                related_name="generated_planner_entries",
                to="core.workoutschedule",
            ),
        ),
    ]
    # Deliberately no guessed backfill: historic sync adopted manual entries,
    # and legacy rows cannot prove which tasks were automatically generated.
