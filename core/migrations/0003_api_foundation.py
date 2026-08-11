import decimal

import django.core.validators
import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.db import migrations, models


def link_profiles_to_auth_users(apps, schema_editor):
    Profile = apps.get_model("core", "RepbaseUser")
    app_label, model_name = settings.AUTH_USER_MODEL.split(".")
    User = apps.get_model(app_label, model_name)
    used_user_ids = set()

    for profile in Profile.objects.order_by("pk"):
        email = profile.email.strip().lower()
        user = None

        if email:
            user = (
                User.objects.filter(email__iexact=email)
                .exclude(pk__in=used_user_ids)
                .first()
            )
        if user is None and profile.username:
            user = (
                User.objects.filter(username__iexact=profile.username)
                .exclude(pk__in=used_user_ids)
                .first()
            )

        if user is None:
            base_username = (profile.username or f"repbase-{profile.pk}")[:140]
            username = base_username
            suffix = 1
            while User.objects.filter(username__iexact=username).exists():
                suffix += 1
                username = f"{base_username}-{suffix}"[:150]
            user = User.objects.create(
                username=username,
                email=email,
                first_name=profile.first_name,
                last_name=profile.last_name,
                password=make_password(None),
                is_active=True,
            )
        else:
            user.first_name = profile.first_name
            user.last_name = profile.last_name
            if not user.email:
                user.email = email
            user.save(update_fields=["first_name", "last_name", "email"])

        profile.user_id = user.pk
        profile.save(update_fields=["user"])
        used_user_ids.add(user.pk)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0002_session"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="repbaseuser",
            name="user",
            field=models.OneToOneField(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="repbase_profile",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RunPython(link_profiles_to_auth_users, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="repbaseuser",
            name="user",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="repbase_profile",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RemoveField(model_name="repbaseuser", name="first_name"),
        migrations.RemoveField(model_name="repbaseuser", name="last_name"),
        migrations.RemoveField(model_name="repbaseuser", name="username"),
        migrations.RemoveField(model_name="repbaseuser", name="email"),
        migrations.AddField(
            model_name="repbaseuser",
            name="birthdate",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="repbaseuser",
            name="gym",
            field=models.CharField(blank=True, max_length=150),
        ),
        migrations.AddField(
            model_name="repbaseuser",
            name="is_body_metrics_public",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="repbaseuser",
            name="profile_photo_url",
            field=models.URLField(blank=True),
        ),
        migrations.AddField(
            model_name="repbaseuser",
            name="target_weight_kg",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                max_digits=6,
                null=True,
                validators=[
                    django.core.validators.MinValueValidator(decimal.Decimal("0.01"))
                ],
            ),
        ),
        migrations.AddField(
            model_name="repbaseuser",
            name="training_style",
            field=models.CharField(
                blank=True,
                choices=[
                    ("powerlifting", "Powerlifting"),
                    ("bodybuilding", "Bodybuilding"),
                    ("crossfit", "CrossFit"),
                    ("other", "Other"),
                ],
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="repbaseuser",
            name="unit_preference",
            field=models.CharField(
                choices=[("metric", "Metric (kg/cm)"), ("imperial", "Imperial (lb/in)")],
                default="metric",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="repbaseuser",
            name="updated_at",
            field=models.DateTimeField(
                auto_now=True,
                default=django.utils.timezone.now,
            ),
            preserve_default=False,
        ),
        migrations.AlterField(
            model_name="repbaseuser",
            name="weight_kg",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                max_digits=6,
                null=True,
                validators=[
                    django.core.validators.MinValueValidator(decimal.Decimal("0.01"))
                ],
            ),
        ),
        migrations.RenameModel(old_name="Session", new_name="WorkoutSession"),
        migrations.RenameField(
            model_name="workoutsession",
            old_name="start_datetime",
            new_name="started_at",
        ),
        migrations.RenameField(
            model_name="workoutsession",
            old_name="end_datetime",
            new_name="ended_at",
        ),
        migrations.AlterField(
            model_name="workoutsession",
            name="repbase_user",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="workout_sessions",
                to="core.repbaseuser",
            ),
        ),
        migrations.AlterField(
            model_name="workoutsession",
            name="started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="workoutsession",
            name="ended_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="workoutsession",
            name="status",
            field=models.CharField(
                choices=[
                    ("planned", "Planned"),
                    ("active", "Active"),
                    ("completed", "Completed"),
                ],
                db_index=True,
                default="planned",
                max_length=12,
            ),
        ),
        migrations.AddField(
            model_name="workoutsession",
            name="updated_at",
            field=models.DateTimeField(
                auto_now=True,
                default=django.utils.timezone.now,
            ),
            preserve_default=False,
        ),
        migrations.AlterModelOptions(
            name="workoutsession",
            options={"ordering": ("-created_at",)},
        ),
        migrations.CreateModel(
            name="Exercise",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("name", models.CharField(max_length=150)),
                ("muscle_group", models.CharField(blank=True, max_length=100)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="custom_exercises",
                        to="core.repbaseuser",
                    ),
                ),
            ],
            options={"ordering": ("name",)},
        ),
        migrations.CreateModel(
            name="WorkoutTemplate",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("name", models.CharField(max_length=150)),
                ("description", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "owner",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="workout_templates",
                        to="core.repbaseuser",
                    ),
                ),
            ],
            options={"ordering": ("name",)},
        ),
        migrations.AddConstraint(
            model_name="workouttemplate",
            constraint=models.UniqueConstraint(
                fields=("owner", "name"),
                name="unique_workout_name_per_owner",
            ),
        ),
        migrations.AddField(
            model_name="workoutsession",
            name="workout",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="sessions",
                to="core.workouttemplate",
            ),
        ),
        migrations.CreateModel(
            name="WorkoutExercise",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("order", models.PositiveIntegerField(default=1)),
                ("target_sets", models.PositiveIntegerField(default=1)),
                ("target_reps", models.PositiveIntegerField(blank=True, null=True)),
                (
                    "target_weight_kg",
                    models.DecimalField(
                        blank=True,
                        decimal_places=2,
                        max_digits=7,
                        null=True,
                        validators=[
                            django.core.validators.MinValueValidator(
                                decimal.Decimal("0.01")
                            )
                        ],
                    ),
                ),
                ("notes", models.CharField(blank=True, max_length=300)),
                (
                    "exercise",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="workout_entries",
                        to="core.exercise",
                    ),
                ),
                (
                    "workout",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="workout_exercises",
                        to="core.workouttemplate",
                    ),
                ),
            ],
            options={"ordering": ("order", "id")},
        ),
        migrations.AddConstraint(
            model_name="workoutexercise",
            constraint=models.UniqueConstraint(
                fields=("workout", "order"),
                name="unique_exercise_order_per_workout",
            ),
        ),
        migrations.CreateModel(
            name="WorkoutSchedule",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("scheduled_date", models.DateField(db_index=True)),
                ("notes", models.CharField(blank=True, max_length=300)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "owner",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="workout_schedules",
                        to="core.repbaseuser",
                    ),
                ),
                (
                    "workout",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="schedule_entries",
                        to="core.workouttemplate",
                    ),
                ),
            ],
            options={"ordering": ("scheduled_date", "id")},
        ),
        migrations.AddConstraint(
            model_name="workoutschedule",
            constraint=models.UniqueConstraint(
                fields=("owner", "workout", "scheduled_date"),
                name="unique_scheduled_workout_per_day",
            ),
        ),
        migrations.CreateModel(
            name="SessionExercise",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("order", models.PositiveIntegerField(default=1)),
                ("notes", models.CharField(blank=True, max_length=300)),
                (
                    "exercise",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="session_entries",
                        to="core.exercise",
                    ),
                ),
                (
                    "session",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="session_exercises",
                        to="core.workoutsession",
                    ),
                ),
            ],
            options={"ordering": ("order", "id")},
        ),
        migrations.AddConstraint(
            model_name="sessionexercise",
            constraint=models.UniqueConstraint(
                fields=("session", "order"),
                name="unique_exercise_order_per_session",
            ),
        ),
        migrations.CreateModel(
            name="SetEntry",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("set_number", models.PositiveIntegerField()),
                (
                    "weight_kg",
                    models.DecimalField(
                        blank=True,
                        decimal_places=2,
                        max_digits=7,
                        null=True,
                        validators=[
                            django.core.validators.MinValueValidator(
                                decimal.Decimal("0.01")
                            )
                        ],
                    ),
                ),
                ("reps", models.PositiveIntegerField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "session_exercise",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="sets",
                        to="core.sessionexercise",
                    ),
                ),
            ],
            options={"ordering": ("set_number", "id")},
        ),
        migrations.AddConstraint(
            model_name="setentry",
            constraint=models.UniqueConstraint(
                fields=("session_exercise", "set_number"),
                name="unique_set_number_per_session_exercise",
            ),
        ),
        migrations.CreateModel(
            name="BodyWeightEntry",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "weight_kg",
                    models.DecimalField(
                        decimal_places=2,
                        max_digits=6,
                        validators=[
                            django.core.validators.MinValueValidator(
                                decimal.Decimal("0.01")
                            )
                        ],
                    ),
                ),
                (
                    "recorded_at",
                    models.DateTimeField(
                        db_index=True,
                        default=django.utils.timezone.now,
                    ),
                ),
                ("notes", models.CharField(blank=True, max_length=300)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "owner",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="body_weight_entries",
                        to="core.repbaseuser",
                    ),
                ),
            ],
            options={"ordering": ("-recorded_at", "-id")},
        ),
    ]
