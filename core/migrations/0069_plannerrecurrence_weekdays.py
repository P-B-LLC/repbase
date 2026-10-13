from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('core', '0068_merge_20261011_0123')]
    operations = [
        migrations.AddField(
            model_name='plannerrecurrence', name='weekdays',
            field=models.JSONField(blank=True, default=list),
        ),
    ]
