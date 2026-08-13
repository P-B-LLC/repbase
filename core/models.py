import math
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone


positive_decimal = MinValueValidator(Decimal("0.01"))

EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance between two coordinates, in kilometers."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


class RepbaseUser(models.Model):
    class UnitPreference(models.TextChoices):
        METRIC = "metric", "Metric (kg/cm)"
        IMPERIAL = "imperial", "Imperial (lb/in)"

    class TrainingStyle(models.TextChoices):
        POWERLIFTING = "powerlifting", "Powerlifting"
        BODYBUILDING = "bodybuilding", "Bodybuilding"
        CROSSFIT = "crossfit", "CrossFit"
        OTHER = "other", "Other"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="repbase_profile",
    )
    birthdate = models.DateField(null=True, blank=True)
    height_cm = models.PositiveIntegerField(null=True, blank=True)
    weight_kg = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[positive_decimal],
    )
    target_weight_kg = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[positive_decimal],
    )
    unit_preference = models.CharField(
        max_length=10,
        choices=UnitPreference.choices,
        default=UnitPreference.METRIC,
    )
    profile_photo_url = models.URLField(blank=True)
    training_style = models.CharField(
        max_length=20,
        choices=TrainingStyle.choices,
        blank=True,
    )
    gym = models.CharField(max_length=150, blank=True)
    is_body_metrics_public = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def first_name(self):
        return self.user.first_name

    @property
    def last_name(self):
        return self.user.last_name

    @property
    def username(self):
        return self.user.username

    @property
    def email(self):
        return self.user.email

    def __str__(self):
        return self.user.get_full_name() or self.user.username


class Exercise(models.Model):
    name = models.CharField(max_length=150)
    muscle_group = models.CharField(max_length=100, blank=True)
    created_by = models.ForeignKey(
        RepbaseUser,
        on_delete=models.CASCADE,
        related_name="custom_exercises",
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name",)

    def __str__(self):
        return self.name


class WorkoutTemplate(models.Model):
    class WorkoutType(models.TextChoices):
        LIFTING = "lifting", "Lifting"
        RUNNING = "running", "Running"
        BIKING = "biking", "Biking"
        SWIMMING = "swimming", "Swimming"

    #: Types logged as a distance covered rather than as weighted reps.
    DISTANCE_TYPES = (
        WorkoutType.RUNNING,
        WorkoutType.BIKING,
        WorkoutType.SWIMMING,
    )

    owner = models.ForeignKey(
        RepbaseUser,
        on_delete=models.CASCADE,
        related_name="workout_templates",
    )
    name = models.CharField(max_length=150)
    workout_type = models.CharField(
        max_length=20,
        choices=WorkoutType.choices,
        default=WorkoutType.LIFTING,
    )
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name",)
        constraints = [
            models.UniqueConstraint(
                fields=("owner", "name"),
                name="unique_workout_name_per_owner",
            )
        ]

    @property
    def tracks_distance(self):
        return self.workout_type in self.DISTANCE_TYPES

    def __str__(self):
        return self.name


class WorkoutExercise(models.Model):
    workout = models.ForeignKey(
        WorkoutTemplate,
        on_delete=models.CASCADE,
        related_name="workout_exercises",
    )
    exercise = models.ForeignKey(
        Exercise,
        on_delete=models.PROTECT,
        related_name="workout_entries",
    )
    order = models.PositiveIntegerField(default=1)
    target_sets = models.PositiveIntegerField(default=1)
    target_reps = models.PositiveIntegerField(null=True, blank=True)
    target_weight_kg = models.DecimalField(
        max_digits=7,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[positive_decimal],
    )
    notes = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ("order", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("workout", "order"),
                name="unique_exercise_order_per_workout",
            )
        ]

    def __str__(self):
        return f"{self.workout}: {self.exercise}"


class WorkoutSchedule(models.Model):
    owner = models.ForeignKey(
        RepbaseUser,
        on_delete=models.CASCADE,
        related_name="workout_schedules",
    )
    workout = models.ForeignKey(
        WorkoutTemplate,
        on_delete=models.CASCADE,
        related_name="schedule_entries",
    )
    scheduled_date = models.DateField(db_index=True)
    notes = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("scheduled_date", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("owner", "workout", "scheduled_date"),
                name="unique_scheduled_workout_per_day",
            )
        ]

    def __str__(self):
        return f"{self.scheduled_date}: {self.workout}"


class WorkoutSession(models.Model):
    class Status(models.TextChoices):
        PLANNED = "planned", "Planned"
        ACTIVE = "active", "Active"
        COMPLETED = "completed", "Completed"

    repbase_user = models.ForeignKey(
        RepbaseUser,
        on_delete=models.CASCADE,
        related_name="workout_sessions",
    )
    workout = models.ForeignKey(
        WorkoutTemplate,
        on_delete=models.SET_NULL,
        related_name="sessions",
        null=True,
        blank=True,
    )
    status = models.CharField(
        max_length=12,
        choices=Status.choices,
        default=Status.PLANNED,
        db_index=True,
    )
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)

    def clean(self):
        if self.started_at and self.ended_at and self.ended_at < self.started_at:
            raise ValidationError({"ended_at": "End time cannot be before start time."})

    @property
    def duration_seconds(self):
        if self.started_at is None or self.ended_at is None:
            return None
        return (self.ended_at - self.started_at).total_seconds()

    @property
    def route_distance_km(self):
        """Distance along the recorded GPS route.

        Computed here rather than on the device so every client reports the
        same number for the same track. Returns None when the session has no
        usable route.
        """
        points = list(self.route_points.all())
        if len(points) < 2:
            return None

        total = 0.0
        for previous, current in zip(points, points[1:]):
            total += haversine_km(
                float(previous.latitude),
                float(previous.longitude),
                float(current.latitude),
                float(current.longitude),
            )
        return round(total, 3)

    @property
    def pace_seconds_per_km(self):
        """Average pace over the session, in seconds per kilometer."""
        distance = self.route_distance_km
        duration = self.duration_seconds
        if not distance or duration is None or duration <= 0:
            return None
        return round(duration / distance, 2)

    def __str__(self):
        return f"{self.repbase_user} - {self.created_at:%Y-%m-%d}"


class SessionExercise(models.Model):
    session = models.ForeignKey(
        WorkoutSession,
        on_delete=models.CASCADE,
        related_name="session_exercises",
    )
    exercise = models.ForeignKey(
        Exercise,
        on_delete=models.PROTECT,
        related_name="session_entries",
    )
    order = models.PositiveIntegerField(default=1)
    notes = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ("order", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("session", "order"),
                name="unique_exercise_order_per_session",
            )
        ]

    def __str__(self):
        return f"{self.session}: {self.exercise}"


class SetEntry(models.Model):
    session_exercise = models.ForeignKey(
        SessionExercise,
        on_delete=models.CASCADE,
        related_name="sets",
    )
    set_number = models.PositiveIntegerField()
    weight_kg = models.DecimalField(
        max_digits=7,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[positive_decimal],
    )
    reps = models.PositiveIntegerField(null=True, blank=True)
    #: Distance covered, for running/biking/swimming efforts. Stored in
    #: kilometers like every other measurement; clients convert for display
    #: according to the owner's unit_preference. Null for lifting sets.
    distance_km = models.DecimalField(
        max_digits=7,
        decimal_places=3,
        null=True,
        blank=True,
        validators=[positive_decimal],
    )
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("set_number", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("session_exercise", "set_number"),
                name="unique_set_number_per_session_exercise",
            )
        ]

    def __str__(self):
        return f"{self.session_exercise} set {self.set_number}"


class SessionRoutePoint(models.Model):
    """One GPS fix recorded during a session.

    Points are stored raw and ordered by time; the session derives distance
    and pace from them so the numbers never depend on the device.
    """

    session = models.ForeignKey(
        WorkoutSession,
        on_delete=models.CASCADE,
        related_name="route_points",
    )
    latitude = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        validators=[MinValueValidator(-90), MaxValueValidator(90)],
    )
    longitude = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        validators=[MinValueValidator(-180), MaxValueValidator(180)],
    )
    recorded_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("recorded_at", "id")

    def __str__(self):
        return f"{self.session_id} @ {self.latitude},{self.longitude}"


class BodyWeightEntry(models.Model):
    owner = models.ForeignKey(
        RepbaseUser,
        on_delete=models.CASCADE,
        related_name="body_weight_entries",
    )
    weight_kg = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        validators=[positive_decimal],
    )
    recorded_at = models.DateTimeField(default=timezone.now, db_index=True)
    notes = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-recorded_at", "-id")

    def __str__(self):
        return f"{self.owner}: {self.weight_kg} kg"
