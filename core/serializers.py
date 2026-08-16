import base64
import binascii
import uuid

from django.contrib.auth import authenticate, get_user_model, password_validation
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from drf_spectacular.utils import extend_schema_field

from .models import (
    CardioMachine,
    BodyWeightEntry,
    Exercise,
    RepbaseUser,
    SessionExercise,
    SessionRoutePoint,
    SetEntry,
    WorkoutExercise,
    EVENT_CATEGORIES,
    Gym,
    UserDiscipline,
    PlannerEntry,
    TASK_CATEGORIES,
    WorkoutRecurrence,
    WorkoutSchedule,
    WorkoutSession,
    WorkoutTemplate,
    normalize_gym_text,
)


User = get_user_model()


#: Roughly five megabytes once decoded. A profile photo has no business
#: being larger, and without a ceiling one request can exhaust memory.
MAX_PROFILE_PHOTO_BYTES = 5 * 1024 * 1024

ALLOWED_PHOTO_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/heic": ".heic",
    "image/webp": ".webp",
}


class ProfilePhotoUploadSerializer(serializers.Serializer):
    """A profile photo sent as base64.

    Base64 in JSON rather than multipart: every other call this API takes is
    JSON, and keeping it that way means the generated client needs no separate
    upload path.
    """

    content_type = serializers.ChoiceField(choices=sorted(ALLOWED_PHOTO_TYPES))
    image_base64 = serializers.CharField(
        help_text="The image bytes, base64 encoded, without a data: prefix."
    )

    def validate(self, attrs):
        raw = attrs["image_base64"]
        # Tolerate a data: URL, since it is the obvious thing to send.
        if raw.startswith("data:"):
            _, _, raw = raw.partition(",")
        try:
            decoded = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            raise serializers.ValidationError(
                {"image_base64": "This is not valid base64."}
            )
        if not decoded:
            raise serializers.ValidationError({"image_base64": "The image is empty."})
        if len(decoded) > MAX_PROFILE_PHOTO_BYTES:
            raise serializers.ValidationError(
                {"image_base64": "That image is larger than 5 MB."}
            )
        attrs["decoded"] = decoded
        return attrs

    def save_to(self, profile):
        extension = ALLOWED_PHOTO_TYPES[self.validated_data["content_type"]]
        # Replaced, not accumulated: the previous file would otherwise sit on
        # disk forever with nothing pointing at it.
        profile.profile_photo.delete(save=False)
        profile.profile_photo.save(
            f"{uuid.uuid4().hex}{extension}",
            ContentFile(self.validated_data["decoded"]),
            save=True,
        )
        return profile


class GymSerializer(serializers.ModelSerializer):
    """A gym, and how many people say they train there."""

    member_count = serializers.SerializerMethodField()
    created_by = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = Gym
        fields = [
            "id",
            "name",
            "city",
            "country",
            "member_count",
            "created_by",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "member_count", "created_by", "created_at", "updated_at"]

    @extend_schema_field(serializers.IntegerField())
    def get_member_count(self, gym):
        # Prefers the list view's annotation so a page of gyms costs one query
        # rather than one per row, and falls back to the property on the paths
        # that have no annotation, such as the response to creating one.
        annotated = getattr(gym, "members_here", None)
        return annotated if annotated is not None else gym.member_count

    def validate_name(self, value):
        name = value.strip()
        if not name:
            raise serializers.ValidationError("Give the gym a name.")
        return name

    def validate(self, attrs):
        # Checked here so a repeat comes back as a 400 naming the field rather
        # than the unique constraint surfacing as a 500.
        name = attrs.get("name", getattr(self.instance, "name", ""))
        city = attrs.get("city", getattr(self.instance, "city", ""))
        clash = Gym.objects.filter(
            normalized_name=normalize_gym_text(name),
            normalized_city=normalize_gym_text(city),
        )
        if self.instance:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError(
                {"name": "This gym is already listed. Search for it and join it instead."}
            )
        return attrs


def profile_photo_url_for(profile, request):
    """The absolute URL of an uploaded photo, or null when there is none.

    Absolute because the app talks to the API from a different origin than the
    one serving the file, and a relative path would resolve against the app.
    """
    if not profile.profile_photo:
        return None
    url = profile.profile_photo.url
    return request.build_absolute_uri(url) if request else url


class DisciplineListField(serializers.ListField):
    """A list of disciplines that reads a reverse relation and writes a list.

    Read and write rather than write-only: dropping it from the response would
    drop it from the schema, and so from every generated client.
    """

    def to_representation(self, value):
        if hasattr(value, "all"):
            return [row.discipline for row in value.all()]
        return super().to_representation(value)


def disciplines_for(profile):
    return [row.discipline for row in profile.disciplines.all()]


def replace_disciplines(profile, disciplines):
    """Sets the profile's disciplines to exactly this list.

    Replaced wholesale rather than merged: the app sends the complete set the
    user selected, so anything missing from it was deselected.
    """
    wanted = list(dict.fromkeys(disciplines))
    profile.disciplines.exclude(discipline__in=wanted).delete()
    existing = set(profile.disciplines.values_list("discipline", flat=True))
    UserDiscipline.objects.bulk_create(
        [
            UserDiscipline(profile=profile, discipline=discipline)
            for discipline in wanted
            if discipline not in existing
        ]
    )


#: Renders a Decimal the way DecimalField does, so a measurement has the same
#: shape here as everywhere else in the API. Deliberately not a class
#: attribute: DRF collects those as declared fields.
PUBLIC_KILOGRAMS = serializers.DecimalField(max_digits=6, decimal_places=2)


class PublicRepbaseUserSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source="user.username", read_only=True)
    first_name = serializers.CharField(source="user.first_name", read_only=True)
    last_name = serializers.CharField(source="user.last_name", read_only=True)
    gym_name = serializers.CharField(
        source="gym.name", read_only=True, allow_null=True, default=None
    )
    gym_city = serializers.CharField(
        source="gym.city", read_only=True, allow_null=True, default=None
    )
    height_cm = serializers.SerializerMethodField()
    weight_kg = serializers.SerializerMethodField()
    target_weight_kg = serializers.SerializerMethodField()
    profile_photo_url = serializers.SerializerMethodField()
    disciplines = serializers.SerializerMethodField()

    class Meta:
        model = RepbaseUser
        fields = [
            "id",
            "username",
            "first_name",
            "last_name",
            "bio",
            "profile_photo_url",
            "disciplines",
            "gym",
            "gym_name",
            "gym_city",
            "height_cm",
            "weight_kg",
            "target_weight_kg",
            "shows_height",
            "shows_weight",
            "shows_target_weight",
            "created_at",
        ]

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_profile_photo_url(self, profile):
        return profile_photo_url_for(profile, self.context.get("request"))

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_disciplines(self, profile):
        return disciplines_for(profile)

    # `is_body_metrics_public` had nothing reading it: the public profile never
    # carried measurements at all, so the switch the user was offered did
    # nothing. These honour it, and withhold by returning null rather than by
    # dropping the key, so the shape of the response does not change with it.

    @extend_schema_field(serializers.IntegerField(allow_null=True))
    def get_height_cm(self, profile):
        return profile.height_cm if profile.shows_height else None

    def _public_kilograms(self, profile, shown, value):
        if not shown or value is None:
            return None
        return PUBLIC_KILOGRAMS.to_representation(value)

    @extend_schema_field(serializers.DecimalField(
        max_digits=6, decimal_places=2, allow_null=True
    ))
    def get_weight_kg(self, profile):
        return self._public_kilograms(profile, profile.shows_weight, profile.weight_kg)

    @extend_schema_field(serializers.DecimalField(
        max_digits=6, decimal_places=2, allow_null=True
    ))
    def get_target_weight_kg(self, profile):
        return self._public_kilograms(
            profile, profile.shows_target_weight, profile.target_weight_kg
        )


class RepbaseUserSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source="user.username", max_length=150)
    first_name = serializers.CharField(source="user.first_name", max_length=150)
    last_name = serializers.CharField(source="user.last_name", max_length=150)
    email = serializers.EmailField(source="user.email")
    gym_name = serializers.CharField(
        source="gym.name", read_only=True, allow_null=True, default=None
    )
    gym_city = serializers.CharField(
        source="gym.city", read_only=True, allow_null=True, default=None
    )
    profile_photo_url = serializers.SerializerMethodField()
    disciplines = DisciplineListField(
        child=serializers.ChoiceField(choices=RepbaseUser.TrainingStyle.choices),
        required=False,
        allow_empty=True,
    )

    class Meta:
        model = RepbaseUser
        fields = [
            "id",
            "username",
            "first_name",
            "last_name",
            "email",
            "birthdate",
            "height_cm",
            "weight_kg",
            "target_weight_kg",
            "unit_preference",
            "bio",
            "profile_photo_url",
            "disciplines",
            "gym",
            "gym_name",
            "gym_city",
            "shows_height",
            "shows_weight",
            "shows_target_weight",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "profile_photo_url",
            "gym_name",
            "gym_city",
            "created_at",
            "updated_at",
        ]

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_profile_photo_url(self, profile):
        return profile_photo_url_for(profile, self.context.get("request"))



    def validate_username(self, value):
        queryset = User.objects.filter(username__iexact=value)
        if self.instance:
            queryset = queryset.exclude(pk=self.instance.user_id)
        if queryset.exists():
            raise serializers.ValidationError("This username is already in use.")
        return value

    def validate_email(self, value):
        queryset = User.objects.filter(email__iexact=value)
        if self.instance:
            queryset = queryset.exclude(pk=self.instance.user_id)
        if queryset.exists():
            raise serializers.ValidationError("This email address is already in use.")
        return value.lower()

    def validate_birthdate(self, value):
        if value and value > timezone.localdate():
            raise serializers.ValidationError("Birthdate cannot be in the future.")
        return value

    @transaction.atomic
    def update(self, instance, validated_data):
        user_data = validated_data.pop("user", {})
        # Held aside because disciplines live in their own table, not on the
        # profile row, so the model serializer cannot assign them.
        has_disciplines = "disciplines" in validated_data
        disciplines = validated_data.pop("disciplines", None)
        instance = super().update(instance, validated_data)
        if user_data:
            for field, value in user_data.items():
                setattr(instance.user, field, value)
            instance.user.save(update_fields=list(user_data))
        if has_disciplines:
            replace_disciplines(instance, disciplines or [])
        return instance


class RegisterSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150)
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, min_length=8)
    first_name = serializers.CharField(max_length=150)
    last_name = serializers.CharField(max_length=150)

    def validate_username(self, value):
        if User.objects.filter(username__iexact=value).exists():
            raise serializers.ValidationError("This username is already in use.")
        return value

    def validate_email(self, value):
        value = value.lower()
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("This email address is already in use.")
        return value

    def validate(self, attrs):
        candidate = User(
            username=attrs["username"],
            email=attrs["email"],
            first_name=attrs["first_name"],
            last_name=attrs["last_name"],
        )
        password_validation.validate_password(attrs["password"], candidate)
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        password = validated_data.pop("password")
        user = User.objects.create_user(password=password, **validated_data)
        return RepbaseUser.objects.create(user=user)


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        user = authenticate(
            request=self.context.get("request"),
            username=attrs["username"],
            password=attrs["password"],
        )
        if user is None or not user.is_active:
            raise serializers.ValidationError("Invalid username or password.")
        attrs["user"] = user
        return attrs


class AuthResponseSerializer(serializers.Serializer):
    token = serializers.CharField(read_only=True)
    user = RepbaseUserSerializer(read_only=True)


class ExerciseSerializer(serializers.ModelSerializer):
    created_by = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = Exercise
        fields = [
            "id",
            "name",
            "muscle_group",
            "created_by",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_by", "created_at", "updated_at"]


class WorkoutExerciseSerializer(serializers.ModelSerializer):
    exercise_name = serializers.CharField(source="exercise.name", read_only=True)

    class Meta:
        model = WorkoutExercise
        fields = [
            "id",
            "workout",
            "exercise",
            "exercise_name",
            "order",
            "target_sets",
            "target_reps",
            "target_weight_kg",
            "notes",
        ]
        read_only_fields = ["id", "exercise_name"]

    def validate(self, attrs):
        profile = self.context["request"].user.repbase_profile
        workout = attrs.get("workout", getattr(self.instance, "workout", None))
        exercise = attrs.get("exercise", getattr(self.instance, "exercise", None))
        if workout and workout.owner_id != profile.id:
            raise serializers.ValidationError("Workout does not belong to this user.")
        if exercise and exercise.created_by_id not in (None, profile.id):
            raise serializers.ValidationError("Exercise is not available to this user.")
        return attrs


class WorkoutTemplateSerializer(serializers.ModelSerializer):
    owner = serializers.PrimaryKeyRelatedField(read_only=True)
    # Declared explicitly so the field is "a machine or nothing" rather than
    # also accepting an empty string, which would put a third, meaningless
    # case in every generated client.
    cardio_machine = serializers.ChoiceField(
        choices=CardioMachine.choices,
        required=False,
        allow_null=True,
    )
    exercises = WorkoutExerciseSerializer(
        source="workout_exercises",
        many=True,
        read_only=True,
    )

    class Meta:
        model = WorkoutTemplate
        fields = [
            "id",
            "owner",
            "name",
            "workout_type",
            "cardio_machine",
            "cardio_target_minutes",
            "description",
            "exercises",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "owner", "created_at", "updated_at"]

    def validate_name(self, value):
        """Report a duplicate name as a validation error, not a crash.

        Workout names are unique per owner, but `owner` is read-only and set
        during creation, so the generated validators cannot see it. Without
        this the database constraint surfaces as an unhandled 500.
        """
        request = self.context.get("request")
        if request is None:
            return value

        owner = request.user.repbase_profile
        duplicates = WorkoutTemplate.objects.filter(owner=owner, name=value)
        if self.instance is not None:
            duplicates = duplicates.exclude(pk=self.instance.pk)
        if duplicates.exists():
            raise serializers.ValidationError(
                "You already have a workout with this name."
            )
        return value


class WorkoutScheduleSerializer(serializers.ModelSerializer):
    owner = serializers.PrimaryKeyRelatedField(read_only=True)
    workout_name = serializers.CharField(source="workout.name", read_only=True)

    class Meta:
        model = WorkoutSchedule
        fields = [
            "id",
            "owner",
            "workout",
            "workout_name",
            "scheduled_date",
            "notes",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "owner", "workout_name", "created_at", "updated_at"]

    def validate_workout(self, value):
        if value.owner_id != self.context["request"].user.repbase_profile.id:
            raise serializers.ValidationError("Workout does not belong to this user.")
        return value


class PlannerEntrySerializer(serializers.ModelSerializer):
    """A task or event on the planner.

    ``is_complete`` is what the client writes; ``completed_at`` is the record
    of when it happened and is read-only. Keeping both means ticking a task off
    does not throw away the time it was done.
    """

    owner = serializers.PrimaryKeyRelatedField(read_only=True)
    # Explicitly nullable: most entries have no workout, and without this the
    # schema promises a string where the API sends null, which makes every
    # ordinary task fail to decode in a generated client.
    workout_name = serializers.CharField(
        source="workout.name", read_only=True, allow_null=True, default=None
    )
    is_complete = serializers.BooleanField(required=False)

    class Meta:
        model = PlannerEntry
        fields = [
            "id",
            "owner",
            "kind",
            "title",
            "category",
            "scheduled_date",
            "scheduled_time",
            "is_complete",
            "completed_at",
            "workout",
            "workout_name",
            "notes",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "owner",
            "completed_at",
            "workout_name",
            "created_at",
            "updated_at",
        ]

    def validate_title(self, value):
        title = value.strip()
        if not title:
            raise serializers.ValidationError("Give the task a name.")
        return title

    def validate_workout(self, value):
        if value is None:
            return value
        if value.owner_id != self.context["request"].user.repbase_profile.id:
            raise serializers.ValidationError("Workout does not belong to this user.")
        return value

    def validate(self, attrs):
        # An event happens at a time; it is not something to tick off. Letting
        # one be "completed" would put a checkbox on a birthday.
        kind = attrs.get("kind", getattr(self.instance, "kind", None))
        if kind == PlannerEntry.Kind.EVENT and attrs.get("is_complete"):
            raise serializers.ValidationError(
                {"is_complete": "An event happens rather than being completed."}
            )

        # Tasks and events draw from different halves of the category list.
        # Checked on whatever the row will end up as, not only on what this
        # request happens to carry, so switching a task to an event cannot
        # leave "habit" behind on it.
        category = attrs.get("category", getattr(self.instance, "category", None))
        if kind is not None and category is not None:
            allowed = (
                EVENT_CATEGORIES
                if kind == PlannerEntry.Kind.EVENT
                else TASK_CATEGORIES
            )
            if category not in allowed:
                raise serializers.ValidationError(
                    {
                        "category": (
                            f"'{category}' is not a category "
                            f"{'an event' if kind == PlannerEntry.Kind.EVENT else 'a task'} can have."
                        )
                    }
                )
        return attrs

    def _apply_completion(self, instance, is_complete):
        if is_complete and instance.completed_at is None:
            instance.completed_at = timezone.now()
        elif not is_complete:
            instance.completed_at = None

    def create(self, validated_data):
        is_complete = validated_data.pop("is_complete", False)
        instance = PlannerEntry(**validated_data)
        self._apply_completion(instance, is_complete)
        instance.save()
        return instance

    def update(self, instance, validated_data):
        has_completion = "is_complete" in validated_data
        is_complete = validated_data.pop("is_complete", False)
        for field, value in validated_data.items():
            setattr(instance, field, value)
        if has_completion:
            self._apply_completion(instance, is_complete)
        instance.save()
        return instance


class WorkoutRecurrenceSerializer(serializers.ModelSerializer):
    """A workout repeating weekly.

    ``effective_from`` and ``effective_until`` are read-only on purpose. The
    server always opens a rule at the current week and closes it at the current
    week, which is what stops a plan reaching backwards into weeks that have
    already been trained.
    """

    owner = serializers.PrimaryKeyRelatedField(read_only=True)
    workout_name = serializers.CharField(source="workout.name", read_only=True)

    class Meta:
        model = WorkoutRecurrence
        fields = [
            "id",
            "owner",
            "workout",
            "workout_name",
            "weekday",
            "effective_from",
            "effective_until",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "owner",
            "workout_name",
            "effective_from",
            "effective_until",
            "created_at",
            "updated_at",
        ]

    def validate_workout(self, value):
        if value.owner_id != self.context["request"].user.repbase_profile.id:
            raise serializers.ValidationError("Workout does not belong to this user.")
        return value

    def validate(self, attrs):
        # Checked here so a repeat the user already has comes back as a 400
        # rather than the partial unique constraint raising a 500.
        workout = attrs.get("workout")
        weekday = attrs.get("weekday")
        if workout is not None and weekday is not None:
            clash = WorkoutRecurrence.objects.filter(
                workout=workout,
                weekday=weekday,
                effective_until__isnull=True,
            )
            if clash.exists():
                raise serializers.ValidationError(
                    "This workout already repeats on that day."
                )
        return attrs


class PlanWeekSerializer(serializers.Serializer):
    """Which week to fill in from the user's weekly repeats."""

    start = serializers.DateField(
        help_text="Any date in the week. The week's Monday is used."
    )


class SessionCardioSerializer(serializers.Serializer):
    """A cardio finisher performed after a session's exercises."""

    machine = serializers.ChoiceField(choices=CardioMachine.choices)
    seconds = serializers.IntegerField(min_value=1)
    distance_km = serializers.DecimalField(
        max_digits=7,
        decimal_places=3,
        required=False,
        allow_null=True,
        min_value=0,
    )


class PersonalRecordSerializer(serializers.Serializer):
    """A best set during a session that beat everything logged before it."""

    exercise = serializers.IntegerField(read_only=True)
    exercise_name = serializers.CharField(read_only=True)
    kind = serializers.ChoiceField(
        choices=["heaviest_weight", "best_estimated_1rm"],
        read_only=True,
    )
    value = serializers.FloatField(read_only=True)
    #: Null when this is the first time the exercise has been logged.
    previous_value = serializers.FloatField(read_only=True, allow_null=True)
    reps = serializers.IntegerField(read_only=True, allow_null=True)


class SessionSplitSerializer(serializers.Serializer):
    """Time taken for one kilometer of a session.

    The final entry may cover less than a kilometer, which `distance_km`
    reports so a partial split is not mistaken for a fast one.
    """

    kilometer = serializers.IntegerField(read_only=True)
    seconds = serializers.FloatField(read_only=True)
    distance_km = serializers.FloatField(read_only=True)


class WorkoutSessionSerializer(serializers.ModelSerializer):
    repbase_user = serializers.PrimaryKeyRelatedField(read_only=True)
    workout_name = serializers.CharField(source="workout.name", read_only=True)
    duration_seconds = serializers.FloatField(read_only=True, allow_null=True)
    cardio_machine = serializers.ChoiceField(
        choices=CardioMachine.choices,
        read_only=True,
        allow_null=True,
    )
    route_distance_km = serializers.FloatField(read_only=True, allow_null=True)
    pace_seconds_per_km = serializers.FloatField(read_only=True, allow_null=True)
    moving_pace_seconds_per_km = serializers.FloatField(
        read_only=True, allow_null=True
    )
    average_speed_kmh = serializers.FloatField(read_only=True, allow_null=True)
    max_speed_kmh = serializers.FloatField(read_only=True, allow_null=True)
    moving_seconds = serializers.FloatField(read_only=True, allow_null=True)
    elevation_gain_m = serializers.FloatField(read_only=True, allow_null=True)
    elevation_loss_m = serializers.FloatField(read_only=True, allow_null=True)
    splits = SessionSplitSerializer(many=True, read_only=True)

    class Meta:
        model = WorkoutSession
        fields = [
            "id",
            "repbase_user",
            "workout",
            "workout_name",
            "status",
            "started_at",
            "ended_at",
            "duration_seconds",
            "cardio_machine",
            "cardio_seconds",
            "cardio_distance_km",
            "route_distance_km",
            "pace_seconds_per_km",
            "moving_pace_seconds_per_km",
            "average_speed_kmh",
            "max_speed_kmh",
            "moving_seconds",
            "elevation_gain_m",
            "elevation_loss_m",
            "splits",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "repbase_user",
            "workout_name",
            "status",
            "started_at",
            "ended_at",
            "duration_seconds",
            "cardio_machine",
            "cardio_seconds",
            "cardio_distance_km",
            "route_distance_km",
            "pace_seconds_per_km",
            "moving_pace_seconds_per_km",
            "average_speed_kmh",
            "max_speed_kmh",
            "moving_seconds",
            "elevation_gain_m",
            "elevation_loss_m",
            "splits",
            "created_at",
            "updated_at",
        ]

    def validate_workout(self, value):
        if value and value.owner_id != self.context["request"].user.repbase_profile.id:
            raise serializers.ValidationError("Workout does not belong to this user.")
        return value


class SessionRoutePointSerializer(serializers.ModelSerializer):
    class Meta:
        model = SessionRoutePoint
        fields = [
            "id",
            "latitude",
            "longitude",
            "recorded_at",
            "speed_mps",
            "altitude_m",
        ]
        read_only_fields = ["id"]


class SessionRouteUploadSerializer(serializers.Serializer):
    """A batch of GPS fixes recorded during one session."""

    points = SessionRoutePointSerializer(many=True, allow_empty=False)

    def validate_points(self, value):
        if len(value) > 20000:
            raise serializers.ValidationError(
                "Too many points in a single upload."
            )
        return value


class SessionExerciseSerializer(serializers.ModelSerializer):
    exercise_name = serializers.CharField(source="exercise.name", read_only=True)

    class Meta:
        model = SessionExercise
        fields = ["id", "session", "exercise", "exercise_name", "order", "notes"]
        read_only_fields = ["id", "exercise_name"]

    def validate(self, attrs):
        profile = self.context["request"].user.repbase_profile
        session = attrs.get("session", getattr(self.instance, "session", None))
        exercise = attrs.get("exercise", getattr(self.instance, "exercise", None))
        if session and session.repbase_user_id != profile.id:
            raise serializers.ValidationError("Session does not belong to this user.")
        if exercise and exercise.created_by_id not in (None, profile.id):
            raise serializers.ValidationError("Exercise is not available to this user.")
        return attrs


class SetEntrySerializer(serializers.ModelSerializer):
    class Meta:
        model = SetEntry
        fields = [
            "id",
            "session_exercise",
            "set_number",
            "weight_kg",
            "reps",
            "distance_km",
            "completed_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_session_exercise(self, value):
        profile_id = self.context["request"].user.repbase_profile.id
        if value.session.repbase_user_id != profile_id:
            raise serializers.ValidationError("Session does not belong to this user.")
        return value


class BodyWeightEntrySerializer(serializers.ModelSerializer):
    owner = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = BodyWeightEntry
        fields = [
            "id",
            "owner",
            "weight_kg",
            "recorded_at",
            "notes",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "owner", "created_at", "updated_at"]


class ExerciseProgressPointSerializer(serializers.Serializer):
    completed_at = serializers.DateTimeField()
    weight_kg = serializers.DecimalField(max_digits=7, decimal_places=2)
    reps = serializers.IntegerField()
    volume_kg = serializers.DecimalField(max_digits=12, decimal_places=2)
    #: Which session the set belongs to, so progress can be charted per
    #: workout. Grouping by date instead would merge two sessions trained on
    #: the same day into a single point.
    session = serializers.IntegerField()
