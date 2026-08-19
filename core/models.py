import math
import pathlib
import uuid
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


def profile_photo_path(instance, filename):
    """Where an uploaded profile photo lives.

    Named from the profile id and a random suffix rather than the uploaded
    filename, which is chosen by the client and would otherwise let one user
    overwrite another's file or leak whatever they happened to call it.
    """
    suffix = pathlib.Path(filename).suffix.lower() or ".jpg"
    return f"profile-photos/{instance.pk or 'new'}-{uuid.uuid4().hex}{suffix}"


class RepbaseUser(models.Model):
    class UnitPreference(models.TextChoices):
        METRIC = "metric", "Metric (kg/cm)"
        IMPERIAL = "imperial", "Imperial (lb/in)"

    class TrainingStyle(models.TextChoices):
        """What kind of athlete someone is.

        A fixed list rather than free text: it is shown on a public profile and
        used to find people who train the way you do, neither of which works
        when everyone spells it differently.
        """

        POWERLIFTING = "powerlifting", "Powerlifting"
        BODYBUILDING = "bodybuilding", "Bodybuilding"
        CROSSFIT = "crossfit", "CrossFit"
        WEIGHTLIFTING = "weightlifting", "Olympic weightlifting"
        ROCK_CLIMBING = "rock_climbing", "Rock climbing"
        TRIATHLON = "triathlon", "Triathlon"
        RUNNING = "running", "Running"
        CYCLING = "cycling", "Cycling"
        SWIMMING = "swimming", "Swimming"
        CALISTHENICS = "calisthenics", "Calisthenics"
        GENERAL_FITNESS = "general_fitness", "General fitness"
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
    #: The uploaded file. Replaces a URL field that nothing could ever set,
    #: since the app had no way to host an image.
    profile_photo = models.ImageField(
        upload_to=profile_photo_path,
        blank=True,
        null=True,
    )
    #: Free text the user writes about themselves. Bounded so a profile stays
    #: something you can read at a glance.
    bio = models.CharField(max_length=300, blank=True)
    #: Set when the user joins one. Null means they have not said where they
    #: train, which is different from having no gym.
    gym = models.ForeignKey(
        "Gym",
        on_delete=models.SET_NULL,
        related_name="members",
        null=True,
        blank=True,
    )
    #: Each measurement is its own decision. A single flag forced height,
    #: weight and goal weight to be shared or withheld together.
    shows_height = models.BooleanField(default=False)
    shows_weight = models.BooleanField(default=False)
    shows_target_weight = models.BooleanField(default=False)
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
    #: The weekly repeat that planned this day, if it was not chosen by hand.
    #: Kept so ending a repeat clears the days it added and nothing else.
    source_recurrence = models.ForeignKey(
        "WorkoutRecurrence",
        on_delete=models.SET_NULL,
        related_name="planned_schedules",
        null=True,
        blank=True,
    )
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


def week_start_for(value):
    """The Monday of the week containing ``value``.

    Weeks are Monday-first throughout Repbase; the iOS week strip runs Monday
    to Sunday, so the server has to agree or a rule would land a day out.
    """
    return value - timedelta(days=value.weekday())


class WorkoutRecurrence(models.Model):
    """A workout that repeats on the same weekday, week after week.

    A rule covers the half-open range of weeks ``[effective_from,
    effective_until)``, both Mondays. Changing a plan never rewrites the past:
    it closes the rule in force at the current week and opens a new one, so
    weeks that already happened keep resolving through whatever was planned
    then. A finished week is a record of what was actually trained, and the
    progress charts read it as such.
    """

    class Weekday(models.IntegerChoices):
        MONDAY = 0, "Monday"
        TUESDAY = 1, "Tuesday"
        WEDNESDAY = 2, "Wednesday"
        THURSDAY = 3, "Thursday"
        FRIDAY = 4, "Friday"
        SATURDAY = 5, "Saturday"
        SUNDAY = 6, "Sunday"

    owner = models.ForeignKey(
        RepbaseUser,
        on_delete=models.CASCADE,
        related_name="workout_recurrences",
    )
    workout = models.ForeignKey(
        WorkoutTemplate,
        on_delete=models.CASCADE,
        related_name="recurrences",
    )
    #: Matches ``date.weekday()``: Monday is 0.
    weekday = models.PositiveSmallIntegerField(choices=Weekday.choices)
    #: Monday of the first week this rule applies to. The server always sets
    #: this to the current week, so a rule can never reach backwards.
    effective_from = models.DateField(db_index=True)
    #: Monday of the first week it no longer applies; null while it still runs.
    effective_until = models.DateField(null=True, blank=True, db_index=True)
    #: Monday of the latest week already turned into schedule rows. Without
    #: this, a workout the user deleted from a planned week would reappear the
    #: next time the week was loaded.
    materialized_through = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("weekday", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("owner", "workout", "weekday"),
                condition=models.Q(effective_until__isnull=True),
                name="unique_open_recurrence_per_weekday",
            )
        ]

    def __str__(self):
        return f"{self.get_weekday_display()}s: {self.workout}"

    def covers(self, week_start):
        """Whether this rule is in force for the week beginning ``week_start``."""
        if week_start < self.effective_from:
            return False
        return self.effective_until is None or week_start < self.effective_until

    def date_in(self, week_start):
        """The date this rule falls on in the week beginning ``week_start``."""
        return week_start + timedelta(days=self.weekday)

    def weeks_to_plan(self, week_start, current_week):
        """Mondays this rule still owes schedule rows for, up to ``week_start``.

        Starts no earlier than the current week, so weeks the user missed stay
        empty rather than filling in retroactively, and no earlier than the
        week after the last one already planned, so a deleted workout does not
        come back.
        """
        monday = max(self.effective_from, current_week)
        if self.materialized_through is not None:
            monday = max(monday, self.materialized_through + timedelta(days=7))
        while monday <= week_start:
            yield monday
            monday += timedelta(days=7)


def plan_recurring_week(owner, week_start, today):
    """Turn every recurrence in force into schedule rows for one week.

    Returns the schedules created. A week that has already finished is left
    alone: it is a record of what was trained, not a plan still to fill in.
    """
    current_week = week_start_for(today)
    if week_start < current_week:
        return []

    created = []
    recurrences = WorkoutRecurrence.objects.filter(owner=owner).select_related(
        "workout"
    )
    for rule in recurrences:
        planned_through = rule.materialized_through
        for monday in rule.weeks_to_plan(week_start, current_week):
            planned_through = monday
            if not rule.covers(monday):
                continue
            schedule, was_created = WorkoutSchedule.objects.get_or_create(
                owner=owner,
                workout=rule.workout,
                scheduled_date=rule.date_in(monday),
                defaults={"source_recurrence": rule},
            )
            if was_created:
                created.append(schedule)
        if planned_through != rule.materialized_through:
            rule.materialized_through = planned_through
            rule.save(update_fields=["materialized_through", "updated_at"])
    return created


class PlannerCategory(models.TextChoices):
    """What a planned item is for.

    Deliberately a short, fixed list: the point is to let a day be read at a
    glance, which a free-text label would not do.

    Tasks and events draw from different halves of it. A task is sorted by
    what kind of doing it is, an event by what kind of occasion it is, and
    offering "habit" while planning a holiday helps nobody. ``OTHER`` is the
    one both share.
    """

    # Tasks: kinds of doing.
    HABIT = "habit", "Habit"
    WORKOUT = "workout", "Workout"
    ERRAND = "errand", "Errand"
    STUDY = "study", "Study"
    SLEEP = "sleep", "Sleep"
    HEALTH = "health", "Health"
    WORK = "work", "Work"
    HOME = "home", "Home"

    # Events: kinds of occasion.
    BIRTHDAY = "birthday", "Birthday"
    HOLIDAY = "holiday", "Holiday"
    APPOINTMENT = "appointment", "Appointment"
    MEETING = "meeting", "Meeting"
    TRAVEL = "travel", "Travel"
    SOCIAL = "social", "Social"

    OTHER = "other", "Other"


#: Which categories each kind of entry may carry. Enforced in the
#: serializer so a mismatch is a 400 rather than a row nothing can display
#: sensibly.
TASK_CATEGORIES = frozenset(
    {
        PlannerCategory.HABIT,
        PlannerCategory.WORKOUT,
        PlannerCategory.ERRAND,
        PlannerCategory.STUDY,
        PlannerCategory.SLEEP,
        PlannerCategory.HEALTH,
        PlannerCategory.WORK,
        PlannerCategory.HOME,
        PlannerCategory.OTHER,
    }
)

EVENT_CATEGORIES = frozenset(
    {
        PlannerCategory.BIRTHDAY,
        PlannerCategory.HOLIDAY,
        PlannerCategory.APPOINTMENT,
        PlannerCategory.MEETING,
        PlannerCategory.TRAVEL,
        PlannerCategory.SOCIAL,
        PlannerCategory.OTHER,
    }
)


class PlannerEntry(models.Model):
    """Something the user has planned for a day.

    A **task** is finished or not, and carries a checkbox. An **event** simply
    happens at a time and is never completed. They share a row because a day is
    read as one list, and separating them would mean merging two paginated
    feeds to draw it.
    """

    class Kind(models.TextChoices):
        TASK = "task", "Task"
        EVENT = "event", "Event"

    owner = models.ForeignKey(
        RepbaseUser,
        on_delete=models.CASCADE,
        related_name="planner_entries",
    )
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.TASK)
    title = models.CharField(max_length=150)
    category = models.CharField(
        max_length=20,
        choices=PlannerCategory.choices,
        default=PlannerCategory.OTHER,
    )
    scheduled_date = models.DateField(db_index=True)
    #: When it needs to be done. Null means the day is enough.
    scheduled_time = models.TimeField(null=True, blank=True)
    #: Set when a task is ticked off, cleared when it is unticked. Kept as a
    #: timestamp rather than a flag so "when did I do this" stays answerable.
    completed_at = models.DateTimeField(null=True, blank=True)
    #: A workout task can stand for a workout the user already has, which is
    #: what connects this page to the workout page. Nullable: most entries have
    #: nothing to do with training.
    workout = models.ForeignKey(
        WorkoutTemplate,
        on_delete=models.SET_NULL,
        related_name="planner_entries",
        null=True,
        blank=True,
    )
    notes = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Untimed items first within a day, then by time: a day reads as
        # "these at some point, these at these hours".
        ordering = ("scheduled_date", "scheduled_time", "id")
        indexes = [
            models.Index(fields=("owner", "scheduled_date")),
        ]
        constraints = [
            # One task per workout per day. The app puts a scheduled workout on
            # the calendar automatically, and a client that could not see the
            # entry it had already made added another on every launch --
            # seventeen rows for one Tuesday before anyone noticed. Careful
            # client code was what failed; this is the guarantee that does not
            # depend on it.
            #
            # Conditional, because the rule is about workout tasks only. Two
            # hand-written tasks on one day are ordinary, and an event that
            # happens to name the same workout is a different kind of thing.
            models.UniqueConstraint(
                fields=("owner", "workout", "scheduled_date"),
                condition=models.Q(kind="task", workout__isnull=False),
                name="unique_workout_task_per_day",
            )
        ]

    def __str__(self):
        return f"{self.scheduled_date}: {self.title}"

    @property
    def is_complete(self):
        return self.completed_at is not None


#: Dropped outright rather than turned into a space, so a possessive matches
#: the same name written without one.
GYM_APOSTROPHES = "'\u2019\u02bc`\u00b4"


def normalize_gym_text(value):
    """A gym's match key: casefolded, punctuation dropped, spaces collapsed.

    "Gold's Gym", "Golds Gym" and "GOLDS  GYM" all reduce to the same thing,
    which is what keeps one gym from being listed six ways. Stored on the row
    so the database enforces it rather than every caller remembering to.

    Apostrophes vanish while other punctuation becomes a space: replacing them
    made "Gold's" into "gold s", which then failed to match "Golds" — the very
    duplicate this is for.
    """
    lowered = (value or "").casefold()
    without_apostrophes = "".join(
        character for character in lowered if character not in GYM_APOSTROPHES
    )
    kept = "".join(character if character.isalnum() or character.isspace() else " "
                   for character in without_apostrophes)
    return " ".join(kept.split())


class Gym(models.Model):
    """A place people train, shared between the users who train there.

    Created by users rather than imported: the point is that two people can see
    they are at the same gym, which only needs the gyms those people actually
    go to.
    """

    name = models.CharField(max_length=150)
    city = models.CharField(max_length=100, blank=True)
    country = models.CharField(max_length=100, blank=True)
    #: Match keys, maintained in `save` and unique together, so a near-repeat
    #: is refused by the database and not merely discouraged in the UI.
    normalized_name = models.CharField(max_length=150, db_index=True, editable=False)
    normalized_city = models.CharField(max_length=100, db_index=True, editable=False)
    #: Who added it. Kept for provenance; the gym outlives them leaving.
    created_by = models.ForeignKey(
        RepbaseUser,
        on_delete=models.SET_NULL,
        related_name="created_gyms",
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name", "city")
        constraints = [
            models.UniqueConstraint(
                fields=("normalized_name", "normalized_city"),
                name="unique_gym_per_city",
            )
        ]

    def save(self, *args, **kwargs):
        self.name = (self.name or "").strip()
        self.city = (self.city or "").strip()
        self.country = (self.country or "").strip()
        self.normalized_name = normalize_gym_text(self.name)
        self.normalized_city = normalize_gym_text(self.city)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name}, {self.city}" if self.city else self.name

    @property
    def member_count(self):
        return self.members.count()


class UserDiscipline(models.Model):
    """One athlete type a person identifies with.

    A row each rather than a list on the profile: "who else here climbs" stays
    a question the database can answer on any backend, which a JSON list cannot
    on SQLite.
    """

    profile = models.ForeignKey(
        RepbaseUser,
        on_delete=models.CASCADE,
        related_name="disciplines",
    )
    discipline = models.CharField(
        max_length=20,
        choices=RepbaseUser.TrainingStyle.choices,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("discipline",)
        constraints = [
            models.UniqueConstraint(
                fields=("profile", "discipline"),
                name="unique_discipline_per_profile",
            )
        ]

    def __str__(self):
        return f"{self.profile}: {self.get_discipline_display()}"


#: Nutrition is stored to two decimal places, like every other measurement in
#: Repbase, and travels as a decimal string so it does not round through a
#: binary float on the way to the app.
NUTRITION_FIELD = dict(max_digits=8, decimal_places=2, validators=[MinValueValidator(0)])


class NutritionGoal(models.Model):
    """What someone is aiming for in a day. One row per person."""

    owner = models.OneToOneField(
        RepbaseUser,
        on_delete=models.CASCADE,
        related_name="nutrition_goal",
    )
    calories = models.DecimalField(default=Decimal("2000"), **NUTRITION_FIELD)
    protein_grams = models.DecimalField(default=Decimal("150"), **NUTRITION_FIELD)
    carbohydrate_grams = models.DecimalField(default=Decimal("200"), **NUTRITION_FIELD)
    fat_grams = models.DecimalField(default=Decimal("70"), **NUTRITION_FIELD)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.owner}: {self.calories} kcal"


class FoodMeal(models.Model):
    """One meal on one day.

    Meals are numbered rather than named breakfast/lunch/dinner, because people
    do not all eat on that schedule. `position` is what orders them; it is
    deliberately not unique per day, since deleting the second of four meals
    would otherwise have to renumber the rest inside the same transaction or
    fail.
    """

    owner = models.ForeignKey(
        RepbaseUser,
        on_delete=models.CASCADE,
        related_name="food_meals",
    )
    date = models.DateField(db_index=True)
    name = models.CharField(max_length=100)
    position = models.PositiveSmallIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("date", "position", "id")
        indexes = [models.Index(fields=("owner", "date"))]

    def __str__(self):
        return f"{self.date}: {self.name}"


class FoodEntry(models.Model):
    """One food inside a meal.

    Nutrition is held per serving with a separate serving count, so editing
    "two portions" to "three" does not require rescaling four numbers by hand
    and rounding each of them.
    """

    meal = models.ForeignKey(
        FoodMeal,
        on_delete=models.CASCADE,
        related_name="entries",
    )
    name = models.CharField(max_length=150)
    servings = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=Decimal("1"),
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    calories = models.DecimalField(default=Decimal("0"), **NUTRITION_FIELD)
    protein_grams = models.DecimalField(default=Decimal("0"), **NUTRITION_FIELD)
    carbohydrate_grams = models.DecimalField(default=Decimal("0"), **NUTRITION_FIELD)
    fat_grams = models.DecimalField(default=Decimal("0"), **NUTRITION_FIELD)
    position = models.PositiveSmallIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("position", "id")

    def __str__(self):
        return f"{self.name} x{self.servings}"


class SavedFoodMeal(models.Model):
    """A meal kept to reuse, with its ingredients.

    Separate from `FoodMeal`: a saved meal belongs to no day and is a template
    the user applies, so deleting the Tuesday it came from must not take it.
    """

    owner = models.ForeignKey(
        RepbaseUser,
        on_delete=models.CASCADE,
        related_name="saved_food_meals",
    )
    name = models.CharField(max_length=150)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("owner", "name"),
                name="unique_saved_meal_name_per_owner",
            )
        ]

    def __str__(self):
        return self.name


class SavedFoodIngredient(models.Model):
    """One food inside a saved meal. Mirrors FoodEntry so applying one is a
    straight copy rather than a translation."""

    saved_meal = models.ForeignKey(
        SavedFoodMeal,
        on_delete=models.CASCADE,
        related_name="ingredients",
    )
    name = models.CharField(max_length=150)
    servings = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=Decimal("1"),
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    calories = models.DecimalField(default=Decimal("0"), **NUTRITION_FIELD)
    protein_grams = models.DecimalField(default=Decimal("0"), **NUTRITION_FIELD)
    carbohydrate_grams = models.DecimalField(default=Decimal("0"), **NUTRITION_FIELD)
    fat_grams = models.DecimalField(default=Decimal("0"), **NUTRITION_FIELD)
    position = models.PositiveSmallIntegerField(default=1)

    class Meta:
        ordering = ("position", "id")

    def __str__(self):
        return f"{self.name} x{self.servings}"


class Follow(models.Model):
    """One person choosing to see another's posts.

    One-directional and immediate: following is not an agreement between two
    people, so there is no pending state and no matching row the other way. The
    pair is unique and nobody may follow themself, both in the database rather
    than in the view, because a duplicate row would show every one of that
    author's posts twice in the feed that joins through here.
    """

    follower = models.ForeignKey(
        RepbaseUser,
        on_delete=models.CASCADE,
        related_name="following",
    )
    following = models.ForeignKey(
        RepbaseUser,
        on_delete=models.CASCADE,
        related_name="followers",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(fields=("following", "-created_at")),
            models.Index(fields=("follower", "-created_at")),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=("follower", "following"),
                name="unique_follow_per_pair",
            ),
            # `condition` rather than `check`: Django renamed the keyword in 5.1
            # and dropped the old spelling in 6.0, and this project is past that.
            models.CheckConstraint(
                condition=~models.Q(follower=models.F("following")),
                name="follow_is_not_self",
            ),
        ]

    def __str__(self):
        return f"{self.follower} follows {self.following}"


class Block(models.Model):
    """One person refusing to appear to another.

    Its own row rather than a flag on `Follow`, because a block has to work
    when neither person ever followed the other, and has to outlive the follows
    it removes: making one drops any follow in either direction, and this row is
    what stops them being remade. Like a follow it is unique per pair and cannot
    point at its own maker.

    No `updated_at`, because there is nothing to change — a block is made or
    lifted.
    """

    blocker = models.ForeignKey(
        RepbaseUser,
        on_delete=models.CASCADE,
        related_name="blocks",
    )
    blocked = models.ForeignKey(
        RepbaseUser,
        on_delete=models.CASCADE,
        related_name="blocked_by",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at", "-id")
        # The unique constraint already indexes the pair blocker-first. This is
        # the other direction, which the feed asks on every page: has the author
        # of this row blocked the person reading it.
        indexes = [
            models.Index(fields=("blocked", "blocker")),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=("blocker", "blocked"),
                name="unique_block_per_pair",
            ),
            models.CheckConstraint(
                condition=~models.Q(blocker=models.F("blocked")),
                name="block_is_not_self",
            ),
        ]

    def __str__(self):
        return f"{self.blocker} blocks {self.blocked}"


def post_photo_path(instance, filename):
    """Where a photo attached to a post lives.

    Named from the post id and a random suffix rather than the uploaded
    filename, which is chosen by the client and would otherwise let one
    author overwrite another's file.
    """
    suffix = pathlib.Path(filename).suffix.lower() or ".jpg"
    return f"post-photos/{instance.pk or 'new'}-{uuid.uuid4().hex}{suffix}"


class Post(models.Model):
    """Something a user has chosen to show other people.

    A post carries its own copy of what was posted rather than rendering the
    live object. Editing last week's workout must not rewrite what people have
    already read, and deleting it must not empty a post that has been up for a
    month, so the links back to the source go null and the copy stays.

    `kind` says which of the three snapshot siblings exists. It is stored rather
    than inferred from which sibling is present, so a page of the feed can be
    ordered, counted and filtered without touching three more tables.
    """

    class Kind(models.TextChoices):
        WORKOUT = "workout", "Workout"
        MEAL = "meal", "Meal"
        PLANNER = "planner", "Planner"

    class Visibility(models.TextChoices):
        PUBLIC = "public", "Public"
        FOLLOWERS = "followers", "Followers"
        PRIVATE = "private", "Private"

    author = models.ForeignKey(
        RepbaseUser,
        on_delete=models.CASCADE,
        related_name="posts",
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    caption = models.CharField(max_length=300, blank=True)
    #: A photo the author chose to attach, if any.
    #:
    #: Deliberately separate from the snapshot beside it. The snapshot is
    #: what the server measured and no client may write; this is what the
    #: author wanted shown, and carries no claim about what was done.
    image = models.ImageField(
        upload_to=post_photo_path,
        blank=True,
        null=True,
    )
    visibility = models.CharField(
        max_length=20,
        choices=Visibility.choices,
        default=Visibility.PUBLIC,
    )
    # What the post was made from. Kept so the author's own workout, meal or
    # planner row can show that it has been posted, and shown to nobody else:
    # where a stranger's post came from is not a stranger's business. Null once
    # the source is deleted, which is the whole point of copying it first.
    source_session = models.ForeignKey(
        WorkoutSession,
        on_delete=models.SET_NULL,
        related_name="posts",
        null=True,
        blank=True,
    )
    source_meal = models.ForeignKey(
        FoodMeal,
        on_delete=models.SET_NULL,
        related_name="posts",
        null=True,
        blank=True,
    )
    source_planner_entry = models.ForeignKey(
        PlannerEntry,
        on_delete=models.SET_NULL,
        related_name="posts",
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at", "-id")
        # Named in the order the feed reads them: narrowed to the people being
        # followed, then walked backwards from a (created_at, id) cursor. The
        # second index is the same walk without the author filter, which is what
        # a single profile's posts and any unfiltered page do.
        indexes = [
            models.Index(fields=("author", "-created_at", "-id")),
            models.Index(fields=("-created_at", "-id")),
        ]

    def __str__(self):
        return f"{self.author}: {self.get_kind_display()}"


class PostWorkout(models.Model):
    """What a posted workout looked like the moment it was posted.

    Every figure here was computed once, at post time, from the session's sets
    and route, then frozen. A page of fifty posts cannot walk fifty lists of
    route points to report fifty distances, and the points themselves are never
    copied: the first and last fix of a run from home is a home address.

    The `related_name` is the key the post goes out under: the response carries
    `workout`, `meal` and `planner` side by side with exactly one of them
    non-null, and nothing has to translate between the column and the wire.

    What is nullable here is what can genuinely be absent — a session with no
    template has no type, one that was never started has no duration, a lifting
    session has no distance. Everything a card must print is not.
    """

    post = models.OneToOneField(
        Post,
        on_delete=models.CASCADE,
        related_name="workout",
    )
    #: Resolved at post time and never looked up again: the session's template
    #: link is nullable and a template can be renamed, so reading it live would
    #: show a name the post never went out with, or none at all.
    title = models.CharField(max_length=150)
    # Nullable rather than blank: an ad-hoc session has no template to take a
    # type from, and a null generates a plain optional in clients instead of a
    # choice-or-empty-string union. Clients draw the lifting card when it is
    # absent.
    workout_type = models.CharField(
        max_length=20,
        choices=WorkoutTemplate.WorkoutType.choices,
        null=True,
        blank=True,
        default=None,
    )
    #: When the training happened, taken from the session's end, start or
    #: creation in that order. The post's own `created_at` is when it was
    #: shared, which is a different date whenever someone posts yesterday.
    performed_at = models.DateTimeField()
    duration_seconds = models.PositiveIntegerField(null=True, blank=True)
    #: The cardio finisher, copied whole or not at all: a machine with no time,
    #: or a time with no machine, gives a card nothing to draw.
    cardio_machine = models.CharField(
        max_length=20,
        choices=CardioMachine.choices,
        null=True,
        blank=True,
        default=None,
    )
    cardio_seconds = models.PositiveIntegerField(null=True, blank=True)
    cardio_distance_km = models.DecimalField(
        max_digits=7,
        decimal_places=3,
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
    )
    #: Kilometers and meters, like every other measurement in Repbase. Nothing
    #: about the author is copied into a snapshot, their unit preference least
    #: of all, or the card would keep the poster's units on every reader's
    #: screen forever.
    route_distance_km = models.DecimalField(
        max_digits=7,
        decimal_places=3,
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
    )
    #: Whole seconds. The live session reports this as a float, and a fraction
    #: of a second of pace is not something anyone reads off a card.
    pace_seconds_per_km = models.PositiveIntegerField(null=True, blank=True)
    elevation_gain_m = models.DecimalField(
        max_digits=7,
        decimal_places=1,
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
    )

    def __str__(self):
        return self.title


class PostWorkoutExercise(models.Model):
    """One exercise as it stood in a posted workout.

    Per exercise rather than per set, because "Bench Press 4 x 8 at 80 kg" is
    the line a reader wants and set-by-set detail is training-log material for
    the person who did it; at fifty posts a page, per-set rows would put around
    fifteen hundred objects on the wire to render nothing. Per exercise rather
    than one summary line on the post, because a post has a detail view and the
    session it came from may be long deleted by the time anyone opens it.

    Weight, reps, volume and distance are null when no set carried them: a
    bodyweight session and an unlogged one must not both read "0 kg". The
    totals for the whole workout are summed from these rows when the post is
    rendered rather than stored, so a figure under a card cannot disagree with
    the rows printed inside it.
    """

    post_workout = models.ForeignKey(
        PostWorkout,
        on_delete=models.CASCADE,
        related_name="exercises",
    )
    #: Copied, not linked. The exercise itself survives — the FK on a live
    #: session is PROTECT — but it can be renamed, and a snapshot records what
    #: the exercise was called on the day.
    name = models.CharField(max_length=150)
    order = models.PositiveIntegerField(default=1)
    #: Sets that recorded a weight, a rep count or a distance. Deliberately not
    #: "sets with a `completed_at`", which is how the records and progress
    #: queries count: whether the app writes that timestamp for every set a user
    #: ticks is not visible from the backend, and a set with numbers in it was
    #: plainly performed.
    set_count = models.PositiveIntegerField(default=0)
    #: The heaviest logged set, which is the pair a card line quotes.
    top_set_weight_kg = models.DecimalField(
        max_digits=7,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
    )
    top_set_reps = models.PositiveIntegerField(null=True, blank=True)
    total_reps = models.PositiveIntegerField(null=True, blank=True)
    #: Sum of weight times reps over the sets that logged both, in the same
    #: 12/2 shape the exercise progress endpoint already sends volume in, so one
    #: lift's volume has one shape across the API.
    volume_kg = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
    )
    distance_km = models.DecimalField(
        max_digits=7,
        decimal_places=3,
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
    )

    class Meta:
        ordering = ("order", "id")

    def __str__(self):
        return f"{self.name} x{self.set_count}"


class PostMeal(models.Model):
    """What a posted meal looked like the moment it was posted.

    Totals are not stored. They are summed from the rows below when the post is
    rendered, the same arithmetic `FoodMealSerializer` runs over a live meal, so
    the numbers under a card can never drift from the foods listed inside it.
    """

    post = models.OneToOneField(
        Post,
        on_delete=models.CASCADE,
        related_name="meal",
    )
    name = models.CharField(max_length=100)
    date = models.DateField()

    def __str__(self):
        return f"{self.date}: {self.name}"


class PostMealEntry(models.Model):
    """One food inside a posted meal.

    Mirrors `FoodEntry` field for field, per-serving split included, so
    freezing a meal is a copy rather than a translation and both sides derive
    their totals by multiplying the same two numbers.
    """

    post_meal = models.ForeignKey(
        PostMeal,
        on_delete=models.CASCADE,
        related_name="entries",
    )
    name = models.CharField(max_length=150)
    servings = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=Decimal("1"),
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    calories = models.DecimalField(default=Decimal("0"), **NUTRITION_FIELD)
    protein_grams = models.DecimalField(default=Decimal("0"), **NUTRITION_FIELD)
    carbohydrate_grams = models.DecimalField(default=Decimal("0"), **NUTRITION_FIELD)
    fat_grams = models.DecimalField(default=Decimal("0"), **NUTRITION_FIELD)
    position = models.PositiveSmallIntegerField(default=1)

    class Meta:
        ordering = ("position", "id")

    def __str__(self):
        return f"{self.name} x{self.servings}"


class PostPlannerEntry(models.Model):
    """What a posted planner item looked like the moment it was posted.

    `is_complete` is frozen as a boolean instead of copying `completed_at`: the
    minute a task was ticked is not feed material, and an event can never be
    completed at all, so the timestamp is structurally absent for half of them.

    Notes are not copied here, nor from a session's exercises. Those fields were
    written by people who had no social feature to consider, and carrying them
    into a post would disclose old text retroactively.
    """

    post = models.OneToOneField(
        Post,
        on_delete=models.CASCADE,
        related_name="planner",
    )
    kind = models.CharField(max_length=10, choices=PlannerEntry.Kind.choices)
    title = models.CharField(max_length=150)
    category = models.CharField(max_length=20, choices=PlannerCategory.choices)
    scheduled_date = models.DateField()
    #: Null means the day was enough, exactly as on the entry it was taken from.
    scheduled_time = models.TimeField(null=True, blank=True)
    is_complete = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.scheduled_date}: {self.title}"


#: What a day starts with when someone first opens it. Four is what the app has
#: always drawn, and it lives here now rather than there: the screens are meant
#: to show what the server holds, and a slot drawn only by the client is a row
#: the server does not have.
DEFAULT_FOOD_MEAL_COUNT = 4

#: A ceiling on how many meals one day can be made to hold. Without it, asking
#: to apply a saved meal at position 900 would quietly create 900 rows.
MAX_FOOD_MEALS_PER_DAY = 12


def food_meals_for_day(owner, date, at_least=0):
    """The meals on one day, creating any missing up to `at_least`.

    A day is not a row of its own; it is however many meals sit on that date.
    This is what gives a freshly opened day the empty slots to log into, and it
    is idempotent, so opening the same day twice does not double them.

    New meals are numbered up from the highest position already there rather
    than from the count, because positions are deliberately not unique per day
    and a deleted middle meal would otherwise make the next one collide with a
    position still in use.
    """
    def current():
        return list(
            FoodMeal.objects.filter(owner=owner, date=date)
            .prefetch_related("entries")
            .order_by("position", "id")
        )

    meals = current()
    wanted = min(max(at_least, 0), MAX_FOOD_MEALS_PER_DAY)
    if len(meals) >= wanted:
        return meals

    next_position = max((meal.position for meal in meals), default=0) + 1
    FoodMeal.objects.bulk_create([
        FoodMeal(
            owner=owner,
            date=date,
            name=f"Meal {next_position + offset}",
            position=next_position + offset,
        )
        for offset in range(wanted - len(meals))
    ])
    return current()
