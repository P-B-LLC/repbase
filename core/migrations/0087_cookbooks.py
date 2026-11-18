import django.db.models.deletion
from django.db import migrations, models


def group_existing_recipes(apps, schema_editor):
    Cookbook = apps.get_model("core", "Cookbook")
    Recipe = apps.get_model("core", "SavedFoodMeal")
    alias = schema_editor.connection.alias
    owner_ids = Recipe.objects.using(alias).order_by().values_list("owner_id", flat=True).distinct()
    for owner_id in owner_ids.iterator():
        ids = list(Recipe.objects.using(alias).filter(owner_id=owner_id).order_by("id").values_list("id", flat=True))
        for offset in range(0, len(ids), 20):
            number = offset // 20 + 1
            cookbook = Cookbook.objects.using(alias).create(owner_id=owner_id,
                name="My recipes" if number == 1 else f"My recipes {number}")
            Recipe.objects.using(alias).filter(pk__in=ids[offset:offset+20]).update(cookbook_id=cookbook.pk)


class Migration(migrations.Migration):
    dependencies = [("core", "0086_media_posts")]
    operations = [
        migrations.CreateModel(name="Cookbook", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("name", models.CharField(max_length=80)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("updated_at", models.DateTimeField(auto_now=True)),
            ("owner", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="cookbooks", to="core.repbaseuser")),
            ("source_post", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="cookbook_copies", to="core.post")),
        ], options={"ordering": ("name", "id"), "constraints": [models.UniqueConstraint(fields=("owner", "name"), name="unique_cookbook_name_per_owner")]}),
        migrations.AddField(model_name="savedfoodmeal", name="cookbook", field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.RESTRICT, related_name="recipes", to="core.cookbook")),
        migrations.RunPython(group_existing_recipes, migrations.RunPython.noop),
        migrations.AlterField(model_name="savedfoodmeal", name="cookbook", field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name="recipes", to="core.cookbook")),
        migrations.AlterField(model_name="post", name="kind", field=models.CharField(choices=[("workout", "Workout"), ("meal", "Meal"), ("cookbook", "Cookbook"), ("planner", "Planner"), ("repost", "Repost"), ("media", "Photo or video")], max_length=20)),
        migrations.CreateModel(name="PostCookbook", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("name", models.CharField(max_length=80)),
            ("recipe_count", models.PositiveSmallIntegerField()),
            ("recipes", models.JSONField(default=list)),
            ("post", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="cookbook", to="core.post")),
        ], options={"constraints": [models.CheckConstraint(condition=models.Q(recipe_count__gte=1, recipe_count__lte=20), name="published_cookbook_recipe_limit")]}),
    ]
