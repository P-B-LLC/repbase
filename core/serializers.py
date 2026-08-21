from datetime import timedelta

import base64
import binascii
import uuid
from decimal import Decimal

from django.contrib.auth import authenticate, get_user_model, password_validation
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from drf_spectacular.utils import extend_schema_field

from .models import (
    CardioMachine,
    BodyWeightEntry,
    DailyStepCount,
    Gear,
    WorkoutCycleSlot,
    WorkoutCycle,
    Exercise,
    RepbaseUser,
    SessionExercise,
    SessionRoutePoint,
    SetEntry,
    WorkoutExercise,
    EVENT_CATEGORIES,
    FoodEntry,
    FoodMeal,
    Gym,
    NutritionGoal,
    SavedFoodIngredient,
    SavedFoodMeal,
    UserDiscipline,
    PlannerEntry,
    TASK_CATEGORIES,
    WorkoutRecurrence,
    WorkoutSchedule,
    WorkoutSession,
    WorkoutTemplate,
    MAX_FOOD_MEALS_PER_DAY,
    Block,
    Follow,
    Post,
    PostComment,
    PostMeal,
    PostMealEntry,
    PostPlannerEntry,
    PostWorkout,
    PostWorkoutExercise,
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


def decode_uploaded_image(raw, field="image_base64"):
    """The bytes behind a base64 image, or a validation error saying why not.

    Shared so a post's photo is refused for the same reasons, in the same
    words, as a profile's.
    """
    # Tolerate a data: URL, since it is the obvious thing to send.
    if raw.startswith("data:"):
        _, _, raw = raw.partition(",")
    try:
        decoded = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise serializers.ValidationError({field: "This is not valid base64."})
    if not decoded:
        raise serializers.ValidationError({field: "The image is empty."})
    if len(decoded) > MAX_PROFILE_PHOTO_BYTES:
        raise serializers.ValidationError(
            {field: "That image is larger than 5 MB."}
        )
    return decoded


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


class NutritionGoalSerializer(serializers.ModelSerializer):
    class Meta:
        model = NutritionGoal
        fields = [
            "calories",
            "protein_grams",
            "carbohydrate_grams",
            "fat_grams",
            "updated_at",
        ]
        read_only_fields = ["updated_at"]


class FoodEntrySerializer(serializers.ModelSerializer):
    """One food. Nutrition is per serving; totals are derived, never stored,
    so a serving count and its totals cannot drift apart."""

    total_calories = serializers.SerializerMethodField()
    total_protein_grams = serializers.SerializerMethodField()
    total_carbohydrate_grams = serializers.SerializerMethodField()
    total_fat_grams = serializers.SerializerMethodField()

    class Meta:
        model = FoodEntry
        fields = [
            "id",
            "meal",
            "name",
            "servings",
            "calories",
            "protein_grams",
            "carbohydrate_grams",
            "fat_grams",
            "total_calories",
            "total_protein_grams",
            "total_carbohydrate_grams",
            "total_fat_grams",
            "position",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_name(self, value):
        name = value.strip()
        if not name:
            raise serializers.ValidationError("Give the food a name.")
        return name

    def validate_meal(self, value):
        if value.owner_id != self.context["request"].user.repbase_profile.id:
            raise serializers.ValidationError("That meal belongs to someone else.")
        return value

    def _total(self, entry, field):
        return NUTRITION_DECIMAL.to_representation(
            (getattr(entry, field) or Decimal("0")) * (entry.servings or Decimal("0"))
        )

    @extend_schema_field(serializers.DecimalField(max_digits=10, decimal_places=2))
    def get_total_calories(self, entry):
        return self._total(entry, "calories")

    @extend_schema_field(serializers.DecimalField(max_digits=10, decimal_places=2))
    def get_total_protein_grams(self, entry):
        return self._total(entry, "protein_grams")

    @extend_schema_field(serializers.DecimalField(max_digits=10, decimal_places=2))
    def get_total_carbohydrate_grams(self, entry):
        return self._total(entry, "carbohydrate_grams")

    @extend_schema_field(serializers.DecimalField(max_digits=10, decimal_places=2))
    def get_total_fat_grams(self, entry):
        return self._total(entry, "fat_grams")


class FoodMealSerializer(serializers.ModelSerializer):
    """A meal with its foods nested, so drawing a day is one request rather
    than one per meal."""

    entries = FoodEntrySerializer(many=True, read_only=True)
    total_calories = serializers.SerializerMethodField()
    total_protein_grams = serializers.SerializerMethodField()
    total_carbohydrate_grams = serializers.SerializerMethodField()
    total_fat_grams = serializers.SerializerMethodField()

    class Meta:
        model = FoodMeal
        fields = [
            "id",
            "date",
            "name",
            "position",
            "entries",
            "total_calories",
            "total_protein_grams",
            "total_carbohydrate_grams",
            "total_fat_grams",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "entries", "created_at", "updated_at"]

    def validate_name(self, value):
        name = value.strip()
        if not name:
            raise serializers.ValidationError("Give the meal a name.")
        return name

    def _total(self, meal, field):
        total = sum(
            ((getattr(entry, field) or Decimal("0")) * (entry.servings or Decimal("0")))
            for entry in meal.entries.all()
        ) or Decimal("0")
        return NUTRITION_DECIMAL.to_representation(total)

    @extend_schema_field(serializers.DecimalField(max_digits=10, decimal_places=2))
    def get_total_calories(self, meal):
        return self._total(meal, "calories")

    @extend_schema_field(serializers.DecimalField(max_digits=10, decimal_places=2))
    def get_total_protein_grams(self, meal):
        return self._total(meal, "protein_grams")

    @extend_schema_field(serializers.DecimalField(max_digits=10, decimal_places=2))
    def get_total_carbohydrate_grams(self, meal):
        return self._total(meal, "carbohydrate_grams")

    @extend_schema_field(serializers.DecimalField(max_digits=10, decimal_places=2))
    def get_total_fat_grams(self, meal):
        return self._total(meal, "fat_grams")


class SavedFoodIngredientSerializer(serializers.ModelSerializer):
    class Meta:
        model = SavedFoodIngredient
        fields = [
            "id",
            "name",
            "servings",
            "calories",
            "protein_grams",
            "carbohydrate_grams",
            "fat_grams",
            "position",
        ]
        read_only_fields = ["id"]


class SavedFoodMealSerializer(serializers.ModelSerializer):
    """A reusable meal. Ingredients are written with it in one request: a
    recipe with no ingredients is not a thing anyone wants to save."""

    ingredients = SavedFoodIngredientSerializer(many=True)

    class Meta:
        model = SavedFoodMeal
        fields = ["id", "name", "ingredients", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_name(self, value):
        name = value.strip()
        if not name:
            raise serializers.ValidationError("Give the saved meal a name.")
        return name

    def validate(self, attrs):
        owner = self.context["request"].user.repbase_profile
        name = attrs.get("name", getattr(self.instance, "name", ""))
        clash = SavedFoodMeal.objects.filter(owner=owner, name__iexact=name)
        if self.instance:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError(
                {"name": "You already have a saved meal with this name."}
            )
        return attrs

    def create(self, validated_data):
        ingredients = validated_data.pop("ingredients", [])
        saved = SavedFoodMeal.objects.create(**validated_data)
        self._replace_ingredients(saved, ingredients)
        return saved

    def update(self, instance, validated_data):
        ingredients = validated_data.pop("ingredients", None)
        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save()
        if ingredients is not None:
            self._replace_ingredients(instance, ingredients)
        return instance

    def _replace_ingredients(self, saved, ingredients):
        # Replaced wholesale: the client sends the recipe as it should now be.
        saved.ingredients.all().delete()
        SavedFoodIngredient.objects.bulk_create(
            [
                SavedFoodIngredient(
                    saved_meal=saved,
                    position=index + 1,
                    **{k: v for k, v in ingredient.items() if k != "position"},
                )
                for index, ingredient in enumerate(ingredients)
            ]
        )


class ApplySavedMealSerializer(serializers.Serializer):
    """Which days to copy a saved meal into, and which meal on each of them.

    Days rather than a meal id: the screen that applies a saved meal asks for a
    set of dates and a meal number, and a day the user picked may not have that
    many meals on it yet. Naming an existing meal could not express "meal two on
    each of these three days" without the client first creating whichever meals
    it guessed were missing.

    `position` is the meal's place in the day counting from one, not its
    `position` column: a day whose second meal was deleted still has a second
    meal, and it is the one now sitting where that one was.
    """

    dates = serializers.ListField(
        child=serializers.DateField(),
        allow_empty=False,
        max_length=31,
    )
    position = serializers.IntegerField(min_value=1, max_value=MAX_FOOD_MEALS_PER_DAY)


class EnsureFoodDaySerializer(serializers.Serializer):
    """The day to open."""

    date = serializers.DateField()


class RecentFoodSerializer(serializers.Serializer):
    """A food the user has logged before, for the picker."""

    name = serializers.CharField()
    servings = serializers.DecimalField(max_digits=6, decimal_places=2)
    calories = serializers.DecimalField(max_digits=8, decimal_places=2)
    protein_grams = serializers.DecimalField(max_digits=8, decimal_places=2)
    carbohydrate_grams = serializers.DecimalField(max_digits=8, decimal_places=2)
    fat_grams = serializers.DecimalField(max_digits=8, decimal_places=2)


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

#: Nutrition totals are derived, so they need rendering the same way a
#: DecimalField would render a stored one: as a string, not a JSON number.
NUTRITION_DECIMAL = serializers.DecimalField(max_digits=10, decimal_places=2)


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


class PlannerSyncSerializer(serializers.Serializer):
    """The range of scheduled days to give planner tasks to.

    Named without the Request suffix: a serializer called
    PlannerSyncRequestSerializer generates PlannerSyncRequestRequest.
    """

    start = serializers.DateField()
    end = serializers.DateField()

    def validate(self, attrs):
        if attrs["end"] < attrs["start"]:
            raise serializers.ValidationError(
                {"end": "The end of the range is before its start."}
            )
        return attrs


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

        # The database refuses a second task for the same workout on the same
        # day. Caught here so it comes back as a validation error naming the
        # field, rather than as the IntegrityError escaping to a 500 -- which
        # is exactly what a duplicate workout name used to do.
        workout = attrs.get("workout", getattr(self.instance, "workout", None))
        date = attrs.get(
            "scheduled_date", getattr(self.instance, "scheduled_date", None)
        )
        # The idiom used elsewhere in this file. profile_for lives in views,
        # and importing it here would make serializers and views import each
        # other.
        owner = getattr(self.instance, "owner", None) or (
            self.context["request"].user.repbase_profile
        )
        if kind == PlannerEntry.Kind.TASK and workout is not None and date is not None:
            clash = PlannerEntry.objects.filter(
                owner=owner,
                workout=workout,
                scheduled_date=date,
                kind=PlannerEntry.Kind.TASK,
            )
            if self.instance is not None:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                raise serializers.ValidationError(
                    {
                        "workout": (
                            "That workout is already on the planner for "
                            f"{date}."
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

    number = serializers.IntegerField(read_only=True)
    seconds = serializers.FloatField(read_only=True)
    distance_km = serializers.FloatField(read_only=True)


class WorkoutSessionSerializer(serializers.ModelSerializer):
    repbase_user = serializers.PrimaryKeyRelatedField(read_only=True)
    workout_name = serializers.CharField(source="workout.name", read_only=True)
    #: The sport, so a client can tell what counts as having trained:
    #: sets for a lifting session, distance or time for a run.
    #: A session whose template was deleted has none, hence allow_null.
    workout_type = serializers.CharField(
        source="workout.workout_type",
        read_only=True,
        allow_null=True,
    )
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
    logged_set_count = serializers.SerializerMethodField()

    class Meta:
        model = WorkoutSession
        fields = [
            "id",
            "repbase_user",
            "workout",
            "workout_name",
            "workout_type",
            "status",
            "started_at",
            "ended_at",
            "duration_seconds",
            "cardio_machine",
            "cardio_seconds",
            "cardio_distance_km",
            "health_distance_km",
            "recorded_distance_km",
            "gear",
            "route_distance_km",
            "pace_seconds_per_km",
            "moving_pace_seconds_per_km",
            "average_speed_kmh",
            "max_speed_kmh",
            "moving_seconds",
            "elevation_gain_m",
            "elevation_loss_m",
            "splits",
            "logged_set_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "recorded_distance_km",
            "id",
            "repbase_user",
            "logged_set_count",
            "workout_name",
            "workout_type",
            "status",
            "started_at",
            "ended_at",
            "duration_seconds",
            "cardio_machine",
            "cardio_seconds",
            "cardio_distance_km",
            "health_distance_km",
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


    def validate_gear(self, value):
        """Gear must be the user's, and must suit the sport.

        A bike is not worn on a run. Refusing here keeps a shoe's mileage
        meaning what it says: the distance covered in that shoe, not the
        distance of whatever session it happened to be attached to.
        """
        if value is None:
            return value

        owner = self.context["request"].user.repbase_profile
        if value.owner_id != owner.id:
            raise serializers.ValidationError("Gear does not belong to this user.")
        if value.retired_at is not None:
            raise serializers.ValidationError("That gear has been retired.")

        workout = getattr(self.instance, "workout", None)
        if workout is not None:
            allowed = Gear.WORKOUT_TYPES.get(value.kind, ())
            if workout.workout_type not in allowed:
                raise serializers.ValidationError(
                    f"A {value.get_kind_display().lower()} cannot be used for a "
                    f"{workout.get_workout_type_display().lower()} session."
                )
        return value

    @extend_schema_field(serializers.IntegerField())
    def get_logged_set_count(self, session):
        """How many sets this session actually recorded.

        A session is finished by tapping Finish, not by logging anything, so a
        completed session can hold nothing at all. Clients need to tell those
        apart from real training without reading every set of every session.

        Uses the annotation the list view adds when it is there, and counts
        directly when it is not, so a single session read is still correct.
        """
        annotated = getattr(session, "logged_set_total", None)
        if annotated is not None:
            return annotated
        return SetEntry.objects.filter(
            session_exercise__session=session,
        ).exclude(weight_kg__isnull=True, reps__isnull=True).count()

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


#: Belongs beside PUBLIC_KILOGRAMS and NUTRITION_DECIMAL at lines 431-438, and
#: is down here only because this block is appended. Same 12/2 shape as
#: ExerciseProgressPointSerializer.volume_kg, so one lift's volume has one
#: shape wherever the API reports it.
VOLUME_DECIMAL = serializers.DecimalField(max_digits=12, decimal_places=2)


def nutrition_amount(row, field):
    """One food's contribution to a meal: its per-serving figure times servings.

    The arithmetic `FoodEntrySerializer._total` runs on a live entry, lifted out
    of that class so a posted meal and the meal it was copied from add up the
    same way. Returns the raw Decimal; the caller renders it, because a meal
    total is the sum of these and strings do not add.
    """
    return (getattr(row, field) or Decimal("0")) * (row.servings or Decimal("0"))


class PostWorkoutExerciseSerializer(serializers.ModelSerializer):
    """One exercise inside a posted workout."""

    # Declared rather than left to `read_only_fields`, which strips `allow_null`
    # off a model field it marks read-only. Without these the schema promises
    # numbers where a bodyweight set, an unlogged set and a lifting exercise all
    # send null, and every such post fails to decode in a generated client.
    top_set_weight_kg = serializers.DecimalField(
        max_digits=7, decimal_places=2, read_only=True, allow_null=True
    )
    top_set_reps = serializers.IntegerField(read_only=True, allow_null=True)
    total_reps = serializers.IntegerField(read_only=True, allow_null=True)
    volume_kg = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True, allow_null=True
    )
    distance_km = serializers.DecimalField(
        max_digits=7, decimal_places=3, read_only=True, allow_null=True
    )

    class Meta:
        model = PostWorkoutExercise
        fields = [
            "id",
            "name",
            "order",
            "set_count",
            "top_set_weight_kg",
            "top_set_reps",
            "total_reps",
            "volume_kg",
            "distance_km",
        ]
        read_only_fields = ["id", "name", "order", "set_count"]


class PostWorkoutSerializer(serializers.ModelSerializer):
    """A posted workout, frozen at the moment it was posted.

    The three totals are summed from the exercises nested underneath rather
    than stored, the way `FoodMealSerializer` sums a meal's foods, so a figure
    printed under a card cannot disagree with the rows printed inside it. They
    walk `exercises` and nothing else, so a page of them costs no queries as
    long as the feed prefetches that relation.

    There is no `id` here or on the other two snapshots: each is one-to-one with
    its post, the post's id already identifies it, and an id in this position
    invites a client into thinking it can fetch the workout it came from.
    """

    workout_type = serializers.CharField(read_only=True, allow_null=True)
    duration_seconds = serializers.IntegerField(read_only=True, allow_null=True)
    cardio_machine = serializers.CharField(read_only=True, allow_null=True)
    cardio_seconds = serializers.IntegerField(read_only=True, allow_null=True)
    cardio_distance_km = serializers.DecimalField(
        max_digits=7, decimal_places=3, read_only=True, allow_null=True
    )
    route_distance_km = serializers.DecimalField(
        max_digits=7, decimal_places=3, read_only=True, allow_null=True
    )
    pace_seconds_per_km = serializers.IntegerField(read_only=True, allow_null=True)
    elevation_gain_m = serializers.DecimalField(
        max_digits=7, decimal_places=1, read_only=True, allow_null=True
    )
    exercises = PostWorkoutExerciseSerializer(many=True, read_only=True)
    exercise_count = serializers.SerializerMethodField()
    total_set_count = serializers.SerializerMethodField()
    total_volume_kg = serializers.SerializerMethodField()

    class Meta:
        model = PostWorkout
        fields = [
            "title",
            "workout_type",
            "performed_at",
            "duration_seconds",
            "cardio_machine",
            "cardio_seconds",
            "cardio_distance_km",
            "route_distance_km",
            "pace_seconds_per_km",
            "elevation_gain_m",
            "exercises",
            "exercise_count",
            "total_set_count",
            "total_volume_kg",
        ]
        read_only_fields = ["title", "performed_at", "exercises"]

    @extend_schema_field(serializers.IntegerField())
    def get_exercise_count(self, snapshot):
        return len(snapshot.exercises.all())

    @extend_schema_field(serializers.IntegerField())
    def get_total_set_count(self, snapshot):
        return sum(exercise.set_count for exercise in snapshot.exercises.all())

    @extend_schema_field(serializers.DecimalField(
        max_digits=12, decimal_places=2, allow_null=True
    ))
    def get_total_volume_kg(self, snapshot):
        # Null, not zero, when no exercise logged a load: a bodyweight session
        # and a session nobody filled in must not both read "0 kg".
        volumes = [
            exercise.volume_kg
            for exercise in snapshot.exercises.all()
            if exercise.volume_kg is not None
        ]
        if not volumes:
            return None
        return VOLUME_DECIMAL.to_representation(sum(volumes))


class PostMealEntrySerializer(serializers.ModelSerializer):
    """One food inside a posted meal, per serving with its servings beside it."""

    total_calories = serializers.SerializerMethodField()
    total_protein_grams = serializers.SerializerMethodField()
    total_carbohydrate_grams = serializers.SerializerMethodField()
    total_fat_grams = serializers.SerializerMethodField()

    class Meta:
        model = PostMealEntry
        fields = [
            "id",
            "name",
            "servings",
            "calories",
            "protein_grams",
            "carbohydrate_grams",
            "fat_grams",
            "total_calories",
            "total_protein_grams",
            "total_carbohydrate_grams",
            "total_fat_grams",
            "position",
        ]
        read_only_fields = [
            "id",
            "name",
            "servings",
            "calories",
            "protein_grams",
            "carbohydrate_grams",
            "fat_grams",
            "position",
        ]

    def _total(self, entry, field):
        return NUTRITION_DECIMAL.to_representation(nutrition_amount(entry, field))

    @extend_schema_field(serializers.DecimalField(max_digits=10, decimal_places=2))
    def get_total_calories(self, entry):
        return self._total(entry, "calories")

    @extend_schema_field(serializers.DecimalField(max_digits=10, decimal_places=2))
    def get_total_protein_grams(self, entry):
        return self._total(entry, "protein_grams")

    @extend_schema_field(serializers.DecimalField(max_digits=10, decimal_places=2))
    def get_total_carbohydrate_grams(self, entry):
        return self._total(entry, "carbohydrate_grams")

    @extend_schema_field(serializers.DecimalField(max_digits=10, decimal_places=2))
    def get_total_fat_grams(self, entry):
        return self._total(entry, "fat_grams")


class PostMealSerializer(serializers.ModelSerializer):
    """A posted meal, frozen at the moment it was posted.

    Totals are derived from the foods nested underneath, exactly as
    `FoodMealSerializer` derives them from a live meal, and rendered through the
    same `NUTRITION_DECIMAL` so a frozen calorie count has the same shape on the
    wire as a live one.
    """

    entries = PostMealEntrySerializer(many=True, read_only=True)
    total_calories = serializers.SerializerMethodField()
    total_protein_grams = serializers.SerializerMethodField()
    total_carbohydrate_grams = serializers.SerializerMethodField()
    total_fat_grams = serializers.SerializerMethodField()

    class Meta:
        model = PostMeal
        fields = [
            "name",
            "date",
            "entries",
            "total_calories",
            "total_protein_grams",
            "total_carbohydrate_grams",
            "total_fat_grams",
        ]
        read_only_fields = ["name", "date", "entries"]

    def _total(self, snapshot, field):
        total = sum(
            nutrition_amount(entry, field) for entry in snapshot.entries.all()
        ) or Decimal("0")
        return NUTRITION_DECIMAL.to_representation(total)

    @extend_schema_field(serializers.DecimalField(max_digits=10, decimal_places=2))
    def get_total_calories(self, snapshot):
        return self._total(snapshot, "calories")

    @extend_schema_field(serializers.DecimalField(max_digits=10, decimal_places=2))
    def get_total_protein_grams(self, snapshot):
        return self._total(snapshot, "protein_grams")

    @extend_schema_field(serializers.DecimalField(max_digits=10, decimal_places=2))
    def get_total_carbohydrate_grams(self, snapshot):
        return self._total(snapshot, "carbohydrate_grams")

    @extend_schema_field(serializers.DecimalField(max_digits=10, decimal_places=2))
    def get_total_fat_grams(self, snapshot):
        return self._total(snapshot, "fat_grams")


class PostPlannerEntrySerializer(serializers.ModelSerializer):
    """A posted planner item, frozen at the moment it was posted."""

    kind = serializers.CharField(read_only=True)
    category = serializers.CharField(read_only=True)
    scheduled_time = serializers.TimeField(read_only=True, allow_null=True)

    class Meta:
        model = PostPlannerEntry
        fields = [
            "kind",
            "title",
            "category",
            "scheduled_date",
            "scheduled_time",
            "is_complete",
        ]
        read_only_fields = ["title", "scheduled_date", "is_complete"]


class RepostedPostSerializer(serializers.ModelSerializer):
    """The original, as it appears inside a repost.

    Deliberately not `PostSerializer`. That one carries `repost_of`, and a
    serializer that contains itself is a schema no generator can express and a
    depth no reader wants. A repost cannot be reposted, so one level is all
    there is to show.
    """

    author = PublicRepbaseUserSerializer(read_only=True)
    kind = serializers.CharField(read_only=True)
    workout = PostWorkoutSerializer(read_only=True, allow_null=True)
    meal = PostMealSerializer(read_only=True, allow_null=True)
    planner = PostPlannerEntrySerializer(read_only=True, allow_null=True)
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = Post
        fields = [
            "id",
            "author",
            "kind",
            "image_url",
            "caption",
            "workout",
            "meal",
            "planner",
            "created_at",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_image_url(self, post):
        if not post.image:
            return None
        request = self.context.get("request")
        url = post.image.url
        return request.build_absolute_uri(url) if request else url


class PostReplySerializer(serializers.ModelSerializer):
    """A reply, as it appears nested under the comment it answers.

    The same fields as a comment without `replies`, because a reply cannot
    have any -- the model refuses a reply to a reply. Kept a separate class so
    the contract can name the type instead of describing an array of anonymous
    objects, which is what a self-referential field generates.
    """

    author = PublicRepbaseUserSerializer(read_only=True)
    viewer_is_author = serializers.SerializerMethodField()

    class Meta:
        model = PostComment
        fields = [
            "id",
            "post",
            "author",
            "parent",
            "body",
            "viewer_is_author",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_viewer_is_author(self, comment) -> bool:
        request = self.context.get("request")
        viewer = getattr(getattr(request, "user", None), "repbase_profile", None)
        return viewer is not None and comment.author_id == viewer.id


class PostCommentSerializer(serializers.ModelSerializer):
    """A comment, and the replies hanging off it.

    `replies` is filled only for a top-level comment, and is never more than
    one deep because the model refuses a reply to a reply. The client can
    therefore render a thread without walking anything.
    """

    author = PublicRepbaseUserSerializer(read_only=True)
    replies = serializers.SerializerMethodField()
    viewer_is_author = serializers.SerializerMethodField()

    class Meta:
        model = PostComment
        fields = [
            "id",
            "post",
            "author",
            "parent",
            "body",
            "replies",
            "viewer_is_author",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "author", "created_at", "updated_at"]

    @extend_schema_field(PostReplySerializer(many=True))
    def get_replies(self, comment):
        if comment.parent_id is not None:
            return []
        # Prefetched by the view. Sorting here rather than in the query keeps
        # the prefetch usable: re-ordering it would fetch the rows again.
        replies = sorted(
            comment.replies.all(),
            key=lambda reply: (reply.created_at, reply.id),
        )
        return PostReplySerializer(replies, many=True, context=self.context).data

    def get_viewer_is_author(self, comment) -> bool:
        request = self.context.get("request")
        viewer = getattr(getattr(request, "user", None), "repbase_profile", None)
        return viewer is not None and comment.author_id == viewer.id

    def validate(self, attrs):
        post = attrs.get("post") or getattr(self.instance, "post", None)
        parent = attrs.get("parent")
        if parent is None:
            return attrs
        if parent.post_id != getattr(post, "id", None):
            raise serializers.ValidationError(
                {"parent": "That comment is on a different post."}
            )
        if parent.parent_id is not None:
            raise serializers.ValidationError(
                {"parent": "Reply to the comment itself, not to a reply."}
            )
        return attrs


class SavedWorkoutResultSerializer(serializers.Serializer):
    """What saving somebody else's posted workout produced.

    The name is returned because it is not always the one on the post: a user
    who already has a "Push Day" gets the copy under a name saying where it
    came from, and the app has to be able to tell them so.
    """

    workout = serializers.PrimaryKeyRelatedField(read_only=True)
    name = serializers.CharField(read_only=True)
    exercise_count = serializers.IntegerField(read_only=True)
    renamed = serializers.BooleanField(read_only=True)


class PostSerializer(serializers.ModelSerializer):
    """A post as anyone allowed to see it reads it.

    `workout`, `meal` and `planner` are three keys side by side with exactly one
    of them filled in, rather than one field whose type is chosen by `kind`. A
    client built before a fourth kind existed then fails to draw that one post;
    with a discriminated union it fails to decode the whole page around it.

    `kind` and `visibility` go out as plain strings for the same reason: a value
    added later should be a string the client does not recognise, not a decoding
    error. Requests still take a closed set — see `CreatePostSerializer`.

    Every field here is read-only. A post is created from a source it does not
    carry and edited through `UpdatePostSerializer`, so there is no shape in
    which this serializer accepts anything.
    """

    author = PublicRepbaseUserSerializer(read_only=True)
    kind = serializers.CharField(read_only=True)
    visibility = serializers.CharField(read_only=True)
    # The reverse one-to-one raises rather than returning None when the snapshot
    # is a different kind, which DRF turns into a null for a field that says it
    # allows one. Saying so is also what puts the null in the schema.
    workout = PostWorkoutSerializer(read_only=True, allow_null=True)
    meal = PostMealSerializer(read_only=True, allow_null=True)
    planner = PostPlannerEntrySerializer(read_only=True, allow_null=True)
    source_id = serializers.SerializerMethodField()
    viewer_follows_author = serializers.SerializerMethodField()
    image_url = serializers.SerializerMethodField()
    #: Counted by the database, not by the length of a list the client would
    #: otherwise have to be sent. A card shows the number; only the detail page
    #: asks who.
    like_count = serializers.SerializerMethodField()
    comment_count = serializers.SerializerMethodField()
    repost_count = serializers.SerializerMethodField()
    #: Whether the person reading has already done it, so the button can be
    #: drawn in the right state on first paint rather than after a second call.
    #: Whether the reader wrote it. Offering to save your own workout
    #: back into your own workouts is not an offer worth making.
    viewer_is_author = serializers.SerializerMethodField()
    viewer_has_liked = serializers.SerializerMethodField()
    viewer_has_reposted = serializers.SerializerMethodField()
    #: The post being passed on, present only on a repost. A repost cannot
    #: itself be reposted, so this never nests more than one deep -- which is
    #: why it can be a serializer of its own rather than a recursive reference
    #: the schema could not describe.
    repost_of = serializers.SerializerMethodField()

    class Meta:
        model = Post
        fields = [
            "id",
            "author",
            "kind",
            "image_url",
            "caption",
            "visibility",
            "workout",
            "meal",
            "planner",
            "source_id",
            "viewer_follows_author",
            "shows_weights",
            "viewer_is_author",
            "like_count",
            "comment_count",
            "repost_count",
            "viewer_has_liked",
            "viewer_has_reposted",
            "repost_of",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "author",
            "kind",
            "caption",
            "visibility",
            "workout",
            "meal",
            "planner",
            "source_id",
            "viewer_follows_author",
            "shows_weights",
            "viewer_is_author",
            "like_count",
            "comment_count",
            "repost_count",
            "viewer_has_liked",
            "viewer_has_reposted",
            "repost_of",
            "created_at",
            "updated_at",
        ]

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_image_url(self, post):
        """The absolute URL of an attached photo, or null when there is none.

        Absolute for the reason a profile photo's is: the app talks to the
        API from a different origin than the one serving the file.
        """
        if not post.image:
            return None
        request = self.context.get("request")
        url = post.image.url
        return request.build_absolute_uri(url) if request else url

    @extend_schema_field(serializers.IntegerField(allow_null=True))
    def get_source_id(self, post):
        """The workout, meal or planner entry this was made from, for its author.

        Null for everyone else, and null again once the source is deleted. Where
        a stranger's post came from is not a stranger's business; the author
        gets it so their own workout can show that it has been posted, and
        `kind` already says which of the three tables the id belongs to.

        Null as well when the context carries no request — a post rendered
        outside one, from a shell or a management command, shows no source
        rather than showing everybody's.
        """
        request = self.context.get("request")
        if request is None:
            return None
        # Django caches the reverse one-to-one on the user object, and the
        # request holds one user, so this is a query for the page rather than
        # one per post. It is also None for an anonymous user, which the
        # comparison below then refuses.
        profile = getattr(request.user, "repbase_profile", None)
        if profile is None or post.author_id != profile.id:
            return None
        return (
            post.source_session_id
            or post.source_meal_id
            or post.source_planner_entry_id
        )

    @extend_schema_field(serializers.BooleanField())
    def get_viewer_follows_author(self, post):
        """Whether the reader follows this post's author.

        Read off the annotation `views.visible_posts_for` already puts on every
        row it returns, so a page of fifty costs the one subquery that decided
        those fifty were visible in the first place. Asking the follow table per
        row is the one thing this field must never do.

        False when the annotation is absent, which happens on exactly one path:
        the author-scoped queryset behind PATCH and DELETE. Those return the
        reader's own post, and nobody follows themself, so false is the true
        answer there rather than a stand-in for one.
        """
        return bool(getattr(post, "viewer_follows_author", False))

    #: Read off the annotation the queryset added when it is there, and only
    #: counted per row when it is not -- a serializer used on a single object,
    #: such as the one a create reads back, has no annotation behind it.
    def get_like_count(self, post) -> int:
        counted = getattr(post, "like_total", None)
        return counted if counted is not None else post.likes.count()

    def get_comment_count(self, post) -> int:
        counted = getattr(post, "comment_total", None)
        return counted if counted is not None else post.comments.count()

    def get_repost_count(self, post) -> int:
        counted = getattr(post, "repost_total", None)
        return counted if counted is not None else post.reposts.count()

    def get_viewer_is_author(self, post) -> bool:
        viewer = self._viewer()
        return viewer is not None and post.author_id == viewer.id

    def get_viewer_has_liked(self, post) -> bool:
        flagged = getattr(post, "viewer_liked", None)
        if flagged is not None:
            return bool(flagged)
        viewer = self._viewer()
        return bool(viewer) and post.likes.filter(user=viewer).exists()

    def get_viewer_has_reposted(self, post) -> bool:
        flagged = getattr(post, "viewer_reposted", None)
        if flagged is not None:
            return bool(flagged)
        viewer = self._viewer()
        return bool(viewer) and post.reposts.filter(author=viewer).exists()

    @extend_schema_field(RepostedPostSerializer(allow_null=True))
    def get_repost_of(self, post):
        original = post.repost_of
        if original is None:
            return None
        return RepostedPostSerializer(original, context=self.context).data

    def _viewer(self):
        request = self.context.get("request")
        user = getattr(request, "user", None)
        return getattr(user, "repbase_profile", None)


class CreatePostSerializer(serializers.Serializer):
    """What to post, and who may see it.

    A reference and nothing else. The server reads the source itself and builds
    the snapshot from it, so no client can decide what a post says it did —
    accepting the content here would be accepting a five-hundred-kilogram squat
    from anyone who could type one.
    """

    kind = serializers.ChoiceField(choices=Post.Kind.choices)
    source_id = serializers.IntegerField(min_value=1)
    caption = serializers.CharField(
        max_length=300,
        required=False,
        allow_blank=True,
        default="",
    )
    #: Defaults to showing them. Somebody who says nothing has posted a
    #: workout, and a workout without its numbers is the unusual case.
    shows_weights = serializers.BooleanField(required=False, default=True)
    visibility = serializers.ChoiceField(
        choices=Post.Visibility.choices,
        required=False,
        default=Post.Visibility.PUBLIC,
    )
    #: An optional photo, sent the way a profile photo is: base64 in JSON,
    #: so the generated client still needs no multipart path.
    #:
    #: Carried on the create rather than uploaded afterwards. A post is made
    #: once, and a second request to attach the photo can fail on its own,
    #: which would publish a post without the picture its caption is about.
    content_type = serializers.ChoiceField(
        choices=sorted(ALLOWED_PHOTO_TYPES),
        required=False,
    )
    image_base64 = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="The image bytes, base64 encoded, without a data: prefix.",
    )

    def validate_caption(self, value):
        return value.strip()

    def validate(self, attrs):
        # Only the photo is resolved here. See the note below on source_id.
        raw = attrs.get("image_base64") or ""
        if not raw:
            attrs.pop("image_base64", None)
            attrs.pop("content_type", None)
            return attrs
        if not attrs.get("content_type"):
            raise serializers.ValidationError(
                {"content_type": "Required when sending an image."}
            )
        attrs["decoded_image"] = decode_uploaded_image(raw)
        return attrs

    # There is deliberately no `validate` resolving `source_id` here. Which
    # table the id belongs to depends on `kind`, so the lookup has to happen
    # after both fields are known - and the view has to load that row anyway,
    # with its sets or foods prefetched, to build the snapshot from it. Doing it
    # here as well would read the row twice and leave the refusals in one of the
    # two places unreachable. `views.source_for` is the single place that
    # resolves a source and the single place that refuses one.


class UpdatePostSerializer(serializers.ModelSerializer):
    """The two things about a post that can still change.

    The snapshot is not among them. It is the record of what happened, and one
    that could be edited into a different workout after people had read it would
    be worth nothing as a record.
    """

    visibility = serializers.ChoiceField(
        choices=Post.Visibility.choices,
        required=False,
    )

    class Meta:
        model = Post
        fields = ["caption", "visibility"]

    def validate_caption(self, value):
        return value.strip()


class FollowSerializer(serializers.ModelSerializer):
    """The record that one person follows another.

    Response only. Both people are named by the URL and the token, so a request
    body would carry nothing, and the checks a follow needs — not yourself, not
    someone either of you has blocked — cannot be made from a body that names
    nobody. They belong in the view.
    """

    follower = serializers.PrimaryKeyRelatedField(read_only=True)
    following = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = Follow
        fields = ["id", "follower", "following", "created_at"]
        read_only_fields = ["id", "follower", "following", "created_at"]


class BlockSerializer(serializers.ModelSerializer):
    """One person the requester has blocked.

    `blocked_user` is nested beside the plain id because the only screen that
    reads this list is a list of people, and a page of bare ids would be a
    profile fetch each. The id stays because that is what a client sends back to
    lift the block.
    """

    blocker = serializers.PrimaryKeyRelatedField(read_only=True)
    blocked = serializers.PrimaryKeyRelatedField(queryset=RepbaseUser.objects.all())
    blocked_user = PublicRepbaseUserSerializer(source="blocked", read_only=True)

    class Meta:
        model = Block
        fields = ["id", "blocker", "blocked", "blocked_user", "created_at"]
        read_only_fields = ["id", "blocker", "blocked_user", "created_at"]

    def validate_blocked(self, value):
        if value.id == self.context["request"].user.repbase_profile.id:
            raise serializers.ValidationError("You cannot block yourself.")
        return value

    def validate(self, attrs):
        # Pre-checked so a second tap on Block is a 400 naming the field, rather
        # than the unique constraint surfacing as an unhandled 500.
        blocker = self.context["request"].user.repbase_profile
        if Block.objects.filter(blocker=blocker, blocked=attrs["blocked"]).exists():
            raise serializers.ValidationError(
                {"blocked": "You have already blocked this person."}
            )
        return attrs


class DailyStepCountSerializer(serializers.ModelSerializer):
    owner = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = DailyStepCount
        fields = ["id", "owner", "day", "steps", "created_at", "updated_at"]
        read_only_fields = ["id", "owner", "created_at", "updated_at"]


class DailyStepCountEntrySerializer(serializers.Serializer):
    """One day of steps on its way in from the device."""

    day = serializers.DateField()
    steps = serializers.IntegerField(min_value=0)


class DailyStepCountRecordSerializer(serializers.Serializer):
    """A batch of days.

    The device sends several at once because Health can backfill: a watch
    synced after a day offline changes yesterday's total, not only today's.
    Sending one day at a time would leave those corrections behind.
    """

    days = DailyStepCountEntrySerializer(many=True, allow_empty=True)


class HealthWorkoutSerializer(serializers.Serializer):
    """One finished workout on its way in from Apple Health."""

    external_id = serializers.CharField(max_length=64)
    activity = serializers.ChoiceField(choices=WorkoutTemplate.WorkoutType.choices)
    started_at = serializers.DateTimeField()
    ended_at = serializers.DateTimeField()
    distance_km = serializers.DecimalField(
        max_digits=7,
        decimal_places=3,
        required=False,
        allow_null=True,
    )

    def validate(self, attrs):
        if attrs["ended_at"] < attrs["started_at"]:
            raise serializers.ValidationError(
                {"ended_at": "End time cannot be before start time."}
            )
        return attrs


class HealthWorkoutImportSerializer(serializers.Serializer):
    workouts = HealthWorkoutSerializer(many=True, allow_empty=True)


class HealthImportResultSerializer(serializers.Serializer):
    """What the import did, counted rather than described.

    Three numbers rather than one, because "nothing happened" has three very
    different causes and a user who imported nothing deserves to know which.
    """

    imported = serializers.IntegerField()
    skipped_overlapping = serializers.IntegerField()
    already_imported = serializers.IntegerField()


class GearSerializer(serializers.ModelSerializer):
    owner = serializers.PrimaryKeyRelatedField(read_only=True)
    #: Everything on it, including whatever it arrived with. Annotated onto the
    #: queryset so a list of ten shoes is one query rather than eleven.
    total_distance_km = serializers.SerializerMethodField()
    session_count = serializers.SerializerMethodField()
    #: When this was last trained in, so a client can preselect what
    #: the user reached for most recently instead of asking again.
    last_used_at = serializers.DateTimeField(read_only=True, allow_null=True)
    is_retired = serializers.BooleanField(read_only=True)

    class Meta:
        model = Gear
        fields = [
            "id",
            "owner",
            "kind",
            "name",
            "brand",
            "notes",
            "initial_distance_km",
            "retire_at_km",
            "is_default",
            "retired_at",
            "is_retired",
            "total_distance_km",
            "session_count",
            "last_used_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "owner",
            "is_retired",
            "total_distance_km",
            "session_count",
            "last_used_at",
            "created_at",
            "updated_at",
        ]

    def get_total_distance_km(self, obj) -> float:
        recorded = getattr(obj, "recorded_distance_total", None) or 0
        return float(obj.initial_distance_km) + float(recorded)

    def get_session_count(self, obj) -> int:
        return getattr(obj, "recorded_session_count", 0)


class TrainingStatsSerializer(serializers.Serializer):
    """The training record, counted once on the server.

    Every figure here used to be worked out on the device, which meant paging
    the whole session history to the phone on every visit to the dashboard so
    it could reduce it. The numbers are a property of the history, and the
    history lives here.
    """

    total_workouts = serializers.IntegerField()
    completed_this_week = serializers.IntegerField()
    completed_this_month = serializers.IntegerField()
    current_streak_weeks = serializers.IntegerField()
    best_streak_weeks = serializers.IntegerField()
    #: Oldest first, six entries, ending with the week containing `today`.
    six_week_counts = serializers.ListField(child=serializers.IntegerField())
    #: Scheduled workouts in the week containing `today`.
    weekly_goal = serializers.IntegerField()


class WorkoutCycleSlotSerializer(serializers.ModelSerializer):
    #: "Rest", rather than no key at all. Sourced from ``workout.name`` this
    #: hit a None partway down the chain, and DRF answers that by dropping the
    #: field -- so the contract promised a string the response did not carry,
    #: and every rotation with a rest day failed to decode on the client.
    workout_name = serializers.SerializerMethodField()
    is_rest = serializers.BooleanField(read_only=True)

    def get_workout_name(self, slot) -> str:
        return slot.workout.name if slot.workout_id else "Rest"

    class Meta:
        model = WorkoutCycleSlot
        fields = ["id", "position", "workout", "workout_name", "is_rest"]
        read_only_fields = ["id", "workout_name", "is_rest"]


class WorkoutCycleSerializer(serializers.ModelSerializer):
    owner = serializers.PrimaryKeyRelatedField(read_only=True)
    slots = WorkoutCycleSlotSerializer(many=True)
    is_active = serializers.BooleanField(read_only=True)
    #: Where the rotation is today, counting from one. The device should not
    #: be doing this arithmetic: two clients disagreeing about which day of
    #: the split it is would be worse than not showing it.
    current_position = serializers.SerializerMethodField()
    current_workout_name = serializers.SerializerMethodField()
    next_workout_name = serializers.SerializerMethodField()
    next_workout_date = serializers.SerializerMethodField()

    class Meta:
        model = WorkoutCycle
        fields = [
            "id",
            "owner",
            "name",
            "length",
            "anchor_date",
            "effective_from",
            "effective_until",
            "is_active",
            "slots",
            "current_position",
            "current_workout_name",
            "next_workout_name",
            "next_workout_date",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "owner",
            "effective_from",
            "effective_until",
            "is_active",
            "current_position",
            "current_workout_name",
            "next_workout_name",
            "next_workout_date",
            "created_at",
            "updated_at",
        ]

    def validate(self, attrs):
        length = attrs.get("length", getattr(self.instance, "length", None))
        slots = attrs.get("slots")
        if slots is not None and length is not None:
            positions = [slot["position"] for slot in slots]
            if len(set(positions)) != len(positions):
                raise serializers.ValidationError(
                    {"slots": "Two slots cannot share a position."}
                )
            if any(position < 1 or position > length for position in positions):
                raise serializers.ValidationError(
                    {"slots": f"Positions must be between 1 and {length}."}
                )
        return attrs

    def validate_slots(self, value):
        owner = self.context["request"].user.repbase_profile
        for slot in value:
            workout = slot.get("workout")
            if workout is None:
                continue
            if workout.owner_id != owner.id:
                raise serializers.ValidationError(
                    "That workout does not belong to this user."
                )
            # A workout driven by both a weekly repeat and a rotation would
            # have two generators writing the same dates, each undoing the
            # other's idea of the plan.
            if WorkoutRecurrence.objects.filter(
                owner=owner,
                workout=workout,
                effective_until__isnull=True,
            ).exists():
                raise serializers.ValidationError(
                    f"{workout.name} already repeats weekly. Stop the weekly "
                    "repeat before putting it in a rotation."
                )
        return value

    def _write_slots(self, cycle, slots):
        cycle.slots.all().delete()
        WorkoutCycleSlot.objects.bulk_create(
            [
                WorkoutCycleSlot(
                    cycle=cycle,
                    position=slot["position"],
                    workout=slot.get("workout"),
                )
                for slot in slots
            ]
        )

    def create(self, validated_data):
        slots = validated_data.pop("slots", [])
        cycle = WorkoutCycle.objects.create(**validated_data)
        self._write_slots(cycle, slots)
        return cycle

    def update(self, instance, validated_data):
        slots = validated_data.pop("slots", None)
        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save()
        if slots is not None:
            self._write_slots(instance, slots)
        return instance

    def _today(self):
        return timezone.localdate()

    def get_current_position(self, cycle) -> int:
        return cycle.position(self._today())

    def get_current_workout_name(self, cycle) -> str:
        slot = cycle.slot(self._today())
        if slot is None or slot.workout is None:
            return "Rest"
        return slot.workout.name

    def _next(self, cycle):
        """The next day of the rotation that is a workout rather than a rest."""
        today = self._today()
        for offset in range(0, cycle.length + 1):
            day = today + timedelta(days=offset)
            slot = cycle.slot(day)
            if slot is not None and slot.workout is not None:
                return day, slot.workout.name
        return None, None

    def get_next_workout_name(self, cycle) -> str:
        return self._next(cycle)[1] or ""

    def get_next_workout_date(self, cycle) -> str:
        day = self._next(cycle)[0]
        return day.isoformat() if day else ""


class CyclePlanAheadSerializer(serializers.Serializer):
    """How far forward to write schedule rows."""

    through = serializers.DateField()


class CycleShiftSerializer(serializers.Serializer):
    """How many days to push the rest of the rotation back."""

    days = serializers.IntegerField(min_value=1, max_value=31)


class CycleShiftResultSerializer(serializers.Serializer):
    """What the shift did, counted."""

    cycle = WorkoutCycleSerializer()
    days_shifted = serializers.IntegerField()
    removed = serializers.IntegerField()
    scheduled = serializers.IntegerField()
    kept = serializers.IntegerField()
