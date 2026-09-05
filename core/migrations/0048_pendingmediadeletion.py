from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('core', '0047_savedfoodmeal_cooking_instructions')]

    operations = [
        migrations.CreateModel(
            name='PendingMediaDeletion',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=500, unique=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
        ),
    ]
