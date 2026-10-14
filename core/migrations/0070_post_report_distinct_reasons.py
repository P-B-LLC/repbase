from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0069_plannerrecurrence_weekdays")]
    operations = [
        migrations.RemoveConstraint(
            model_name="postreport", name="one_report_per_person_per_post"
        ),
        migrations.AddConstraint(
            model_name="postreport",
            constraint=models.UniqueConstraint(
                fields=("post", "reporter", "reason"),
                name="one_post_report_per_person_reason",
            ),
        ),
    ]
