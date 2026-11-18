from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0087_cookbooks")]
    operations = [
        migrations.RemoveConstraint(model_name="savedfoodmeal", name="unique_saved_meal_name_per_owner"),
        migrations.AddConstraint(model_name="savedfoodmeal", constraint=models.UniqueConstraint(
            fields=("cookbook", "name"), name="unique_saved_meal_name_per_cookbook")),
    ]
