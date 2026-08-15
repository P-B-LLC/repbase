import math
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone


positive_decimal = MinValueValidator(Decimal("0.01"))

EARTH_RADIUS_KM = 6371.0088

#: Below this the user is treated as stopped rather than moving, so waiting at
#: a crossing does not count against their pace.
MOVING_SPEED_FLOOR_KMH = 2.0

#: The same floor expressed for the device's own speed readings, which arrive
#: in meters per second.
MOVING_SPEED_FLOOR_MPS = 0.5

#: A gap longer than this means tracking lapsed rather than the user standing
#: still, so it is left out of moving time entirely.
MAX_ROUTE_GAP_SECONDS = 60.0

class CardioMachine(models.TextChoices):
    """Machines a workout can finish on.

    A finisher belongs to the workout it follows rather than being a workout of
    its own, so it is recorded on the template and on the session instead of
    creating a second schedule.
    """

    TREADMILL = "treadmill", "Treadmill"
    STATIONARY_BIKE = "stationary_bike", "Stationary Bike"
    STAIR_MASTER = "stair_master", "Stair Master"
    ELLIPTICAL = "elliptical", "Elliptical"
    ROWING_MACHINE = "rowing_machine", "Rowing Machine"
    ASSAULT_BIKE = "assault_bike", "Assault Bike"
    SKI_ERG = "ski_erg", "Ski Erg"
    OTHER = "other", "Other"


#: Reps beyond this stop predicting a one-rep max usefully, so a long set is
#: not treated as a record attempt.
ONE_REP_MAX_REP_LIMIT = 12


def estimated_one_rep_max(weight_kg, reps):
    """Epley estimate of the most that could be lifted once.

    Lets a heavier set of five count as progress over a lighter single, which
    a raw heaviest-weight comparison alone would miss.
    """
    if not weight_kg or not reps or reps < 1:
        return None
    if reps > ONE_REP_MAX_REP_LIMIT:
        return None
    return float(weight_kg) * (1 + reps / 30.0)


#: GPS measures height far less precisely than position, wandering by several
#: meters even standing still, so only sustained changes count as climbing.
#: Set too low, a flat run reports a hill that was never there.
ELEVATION_NOISE_METERS = 3.0

#: Readings are averaged over this many samples before any climb is measured,
#: which removes the sample-to-sample wobble the threshold alone cannot.
ELEVATION_SMOOTHING_WINDOW = 5


def _accumulate(altitudes, ascending):
    """Total climb or descent in a smoothed altitude series.

    Both filters are needed and neither suffices alone. Smoothing removes the
    sample-to-sample wobble, but summing every remaining rise still adds up the
    residue over hundreds of fixes and invents a hill. Waiting until the height
    has moved clear of a reference point discards that residue, and because the
    reference only moves when the threshold is crossed, a long steady climb is
    still counted in full.
    """
    total = 0.0
    reference = altitudes[0]
    for altitude in altitudes[1:]:
        change = altitude - reference if ascending else reference - altitude
        if change >= ELEVATION_NOISE_METERS:
            total += change
            reference = altitude
        elif change <= -ELEVATION_NOISE_METERS:
            # Moving the other way resets the baseline, so the next climb is
            # measured from the valley rather than the previous summit.
            reference = altitude
    return total


def smoothed(values, window=ELEVATION_SMOOTHING_WINDOW):
    """Moving average over a series, used to settle noisy altitude readings."""
    if window <= 1 or len(values) < window:
        return list(values)
    result = []
    for index in range(len(values)):
        start = max(0, index - window // 2)
        end = min(len(values), start + window)
        chunk = values[start:end]
        result.append(sum(chunk) / len(chunk))
    return result


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
    #: Optional cardio to finish on. Blank means the workout ends with its
    #: exercises. This is part of the workout, not a second one scheduled
    #: after it.
    # Nullable rather than blank: "no finisher" is an absent value, and a
    # null field generates a plain optional in clients instead of a
    # choice-or-empty-string union.
    cardio_machine = models.CharField(
        max_length=20,
        choices=CardioMachine.choices,
        null=True,
        blank=True,
        default=None,
    )
    #: How long the finisher is meant to last, if the user set a target.
    cardio_target_minutes = models.PositiveIntegerField(null=True, blank=True)
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
    #: The cardio finisher actually performed after this session's exercises.
    #: Recorded on the session itself so a workout and the cardio that followed
    #: it stay one training session rather than two.
    # Nullable rather than blank: "no finisher" is an absent value, and a
    # null field generates a plain optional in clients instead of a
    # choice-or-empty-string union.
    cardio_machine = models.CharField(
        max_length=20,
        choices=CardioMachine.choices,
        null=True,
        blank=True,
        default=None,
    )
    cardio_seconds = models.PositiveIntegerField(null=True, blank=True)
    #: Optional, read off the machine's own display.
    cardio_distance_km = models.DecimalField(
        max_digits=7,
        decimal_places=3,
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
    )
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
            # Skip steps the device recorded while stationary. A phone's
            # position keeps drifting a few meters between fixes when it is
            # not moving, and summing that adds distance the user never
            # covered. The speed reading is the reliable way to tell them
            # apart; steps without one are counted, as before.
            if current.speed_mps is not None:
                if float(current.speed_mps) < MOVING_SPEED_FLOOR_MPS:
                    continue

            total += haversine_km(
                float(previous.latitude),
                float(previous.longitude),
                float(current.latitude),
                float(current.longitude),
            )
        return round(total, 3)

    @property
    def route_elapsed_seconds(self):
        """Time spanned by the recorded track itself.

        Used in place of the session's wall clock for anything derived from
        the route, so distance and time always describe the same stretch of
        activity. Time before the first fix or after the last one belongs to
        the session, not to the route.
        """
        points = list(self.route_points.all())
        if len(points) < 2:
            return None
        span = (points[-1].recorded_at - points[0].recorded_at).total_seconds()
        return round(span, 2) if span > 0 else None

    @property
    def pace_seconds_per_km(self):
        """Average pace across the recorded route, in seconds per kilometer."""
        distance = self.route_distance_km
        elapsed = self.route_elapsed_seconds
        if not distance or not elapsed:
            return None
        return round(elapsed / distance, 2)

    @property
    def average_speed_kmh(self):
        """Average speed across the recorded route, including any stops."""
        distance = self.route_distance_km
        elapsed = self.route_elapsed_seconds
        if not distance or not elapsed:
            return None
        return round(distance / (elapsed / 3600), 2)

    @property
    def moving_seconds(self):
        """Time spent actually moving, ignoring stops.

        Anything slower than a slow walk counts as stopped, so waiting at a
        crossing does not drag the reported pace down.
        """
        points = list(self.route_points.all())
        if len(points) < 2:
            return None

        total = 0.0
        for previous, current in zip(points, points[1:]):
            gap = (current.recorded_at - previous.recorded_at).total_seconds()
            if gap <= 0 or gap > MAX_ROUTE_GAP_SECONDS:
                continue
            step_km = haversine_km(
                float(previous.latitude),
                float(previous.longitude),
                float(current.latitude),
                float(current.longitude),
            )
            if step_km / (gap / 3600) >= MOVING_SPEED_FLOOR_KMH:
                total += gap
        return round(total, 2)

    @property
    def moving_pace_seconds_per_km(self):
        """Pace over moving time only — the number a runner compares."""
        distance = self.route_distance_km
        moving = self.moving_seconds
        if not distance or not moving:
            return None
        return round(moving / distance, 2)

    def personal_records(self):
        """Bests set during this session that beat everything logged before it.

        Two kinds are reported. The heaviest single lift is what a lifter
        usually means by a record, and the estimated one-rep max catches
        progress the raw weight misses: five reps at 80 kg beats one at 85,
        but only the estimate shows it.
        """
        if self.status != WorkoutSession.Status.COMPLETED:
            return []

        cutoff = self.ended_at or timezone.now()
        records = []

        for session_exercise in self.session_exercises.select_related("exercise"):
            performed = [
                entry
                for entry in session_exercise.sets.all()
                if entry.weight_kg is not None
                and entry.reps
                and entry.completed_at is not None
            ]
            if not performed:
                continue

            # Everything logged for this exercise before this session. Earlier
            # sets from this same session are excluded, so a session cannot
            # beat itself and report two records for one lift.
            history = SetEntry.objects.filter(
                session_exercise__exercise=session_exercise.exercise,
                session_exercise__session__repbase_user=self.repbase_user,
                completed_at__isnull=False,
                completed_at__lt=cutoff,
                weight_kg__isnull=False,
                reps__isnull=False,
            ).exclude(session_exercise__session=self)

            heaviest = max(performed, key=lambda entry: entry.weight_kg)
            previous_heaviest = history.aggregate(
                best=models.Max("weight_kg")
            )["best"]

            if (
                previous_heaviest is None
                or heaviest.weight_kg > previous_heaviest
            ):
                records.append(
                    {
                        "exercise": session_exercise.exercise_id,
                        "exercise_name": session_exercise.exercise.name,
                        "kind": "heaviest_weight",
                        "value": float(heaviest.weight_kg),
                        "previous_value": (
                            float(previous_heaviest)
                            if previous_heaviest is not None
                            else None
                        ),
                        "reps": heaviest.reps,
                    }
                )

            best = max(
                (
                    (estimated_one_rep_max(entry.weight_kg, entry.reps), entry)
                    for entry in performed
                ),
                key=lambda pair: pair[0] or 0,
                default=(None, None),
            )
            estimate, entry = best
            if estimate is None:
                continue

            previous_estimate = max(
                (
                    value
                    for value in (
                        estimated_one_rep_max(item.weight_kg, item.reps)
                        for item in history
                    )
                    if value is not None
                ),
                default=None,
            )

            # A hair above the previous best is rounding, not a record.
            if previous_estimate is None or estimate > previous_estimate + 0.05:
                records.append(
                    {
                        "exercise": session_exercise.exercise_id,
                        "exercise_name": session_exercise.exercise.name,
                        "kind": "best_estimated_1rm",
                        "value": round(estimate, 1),
                        "previous_value": (
                            round(previous_estimate, 1)
                            if previous_estimate is not None
                            else None
                        ),
                        "reps": entry.reps,
                    }
                )

        return records

    @property
    def elevation_gain_m(self):
        """Total height climbed, ignoring GPS drift.

        Only the ascents are summed, which is how climbing is normally
        reported: a hill counts once going up, not again coming down.
        """
        altitudes = self._smoothed_altitudes()
        if altitudes is None:
            return None

        return round(_accumulate(altitudes, ascending=True), 1)

    @property
    def elevation_loss_m(self):
        """Total height descended, as a positive number."""
        altitudes = self._smoothed_altitudes()
        if altitudes is None:
            return None

        return round(_accumulate(altitudes, ascending=False), 1)

    def _smoothed_altitudes(self):
        """Altitude readings with the sample-to-sample wobble averaged out."""
        raw = [
            float(point.altitude_m)
            for point in self.route_points.all()
            if point.altitude_m is not None
        ]
        if len(raw) < 2:
            return None
        return smoothed(raw)

    @property
    def max_speed_kmh(self):
        """Fastest speed reached, from the device's own speed readings."""
        speeds = [
            float(point.speed_mps)
            for point in self.route_points.all()
            if point.speed_mps is not None
        ]
        if not speeds:
            return None
        return round(max(speeds) * 3.6, 2)

    @property
    def splits(self):
        """Time taken for each kilometer, in order.

        The per-kilometer breakdown is what shows whether a run was even or
        started too fast, which a single average cannot.
        """
        points = list(self.route_points.all())
        if len(points) < 2:
            return []

        splits = []
        total_km = 0.0
        next_boundary = 1.0
        split_start = points[0].recorded_at

        for previous, current in zip(points, points[1:]):
            step_km = haversine_km(
                float(previous.latitude),
                float(previous.longitude),
                float(current.latitude),
                float(current.longitude),
            )
            if step_km <= 0:
                continue

            step_seconds = (current.recorded_at - previous.recorded_at).total_seconds()
            step_start_km = total_km
            total_km += step_km

            # A single step can span more than one kilometer, so close out
            # every boundary it crosses, interpolating the moment of each.
            while total_km >= next_boundary:
                fraction = (next_boundary - step_start_km) / step_km
                boundary_time = previous.recorded_at + timedelta(
                    seconds=step_seconds * fraction
                )
                splits.append(
                    {
                        "kilometer": len(splits) + 1,
                        "seconds": round(
                            (boundary_time - split_start).total_seconds(), 2
                        ),
                        "distance_km": 1.0,
                    }
                )
                split_start = boundary_time
                next_boundary += 1.0

        # Whatever is left after the last whole kilometer is still part of the
        # run. Dropping it would hide the finish of anything that does not end
        # exactly on a kilometer, which is almost every run.
        remainder = total_km - (next_boundary - 1.0)
        if remainder >= 0.01:
            splits.append(
                {
                    "kilometer": len(splits) + 1,
                    "seconds": round(
                        (points[-1].recorded_at - split_start).total_seconds(), 2
                    ),
                    "distance_km": round(remainder, 3),
                }
            )

        return splits

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
    #: Instantaneous speed reported by the device, in meters per second.
    #: Read from the GPS Doppler shift rather than derived from consecutive
    #: positions, so it does not accumulate positional error. Null when the
    #: device could not determine it.
    speed_mps = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
    )
    #: Height above sea level in meters, as reported by the device. Null when
    #: no vertical fix was available, which is common indoors.
    altitude_m = models.DecimalField(
        max_digits=7,
        decimal_places=2,
        null=True,
        blank=True,
    )
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
