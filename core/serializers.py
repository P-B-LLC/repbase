from datetime import timedelta

import base64
import io
import binascii
import uuid
from decimal import Decimal
from zoneinfo import available_timezones

from django.contrib.auth import authenticate, get_user_model, password_validation
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from drf_spectacular.utils import extend_schema_field

from .media import signed_media_url
from .moderation import check_public_content


class SessionFinishSerializer(serializers.Serializer):
    ended_at = serializers.DateTimeField(required=False, help_text="Original device finish time; omit for server time.")

    def validate_ended_at(self, value):
        if value > timezone.now():
            raise serializers.ValidationError("Finish time cannot be in the future.")
        return value

from .models import (
    CardioMachine,
    completed_sessions_for,
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
    Personalization,
    SavedFoodIngredient,
    SavedFoodMeal,
    UserDiscipline,
    MAX_PROFILE_SOCIAL_LINKS,
    PlannerEntry,
    PostReport,
    ProfileSocialLink,
    SocialLinkError,
    normalise_social_link,
    TASK_CATEGORIES,
    WorkoutRecurrence,
    WorkoutSchedule,
    WorkoutSession,
    WorkoutTemplate,
    MAX_FOOD_MEALS_PER_DAY,
    MAX_PLANNER_DURATION_MINUTES,
    MIN_PLANNER_DURATION_MINUTES,
    MAX_PROFILE_HIGHLIGHTS,
    MAX_PROFILE_PROMPTS,
    today_for,
    ProfileHighlight,
    ProfilePrompt,
    LIFT_KEYWORDS,
    best_set_for,
    estimated_one_rep_max,
    Block,
    Follow,
    FollowRequest,
    Notification,
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
    verify_is_an_image(decoded, field=field)
    return decoded


#: What Pillow calls each format, mapped to the extension it should be stored
#: under. Keyed on Pillow's own name rather than on a MIME type, because
#: Pillow is the thing that read the bytes.
PILLOW_FORMAT_EXTENSIONS = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "HEIF": ".heic",
    "WEBP": ".webp",
}


def verify_is_an_image(decoded, field="image_base64"):
    """Open the bytes and return the extension they deserve.

    `content_type` is a label the client chose; this is what the file is. A
    JPEG announced as a PNG was previously written to disk as `.png`, and
    something that was not an image at all was written as whatever it claimed.

    `Image.verify()` reads the header and checksums without decoding the
    pixels, so a large photograph costs almost nothing here. It also leaves
    the file object unusable afterwards, which is why the image is opened
    from a fresh buffer if anything else needs it.
    """
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError:  # pragma: no cover - Pillow is a hard dependency
        return None

    try:
        with Image.open(io.BytesIO(decoded)) as image:
            detected = image.format
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        raise serializers.ValidationError(
            {field: "That file is not an image we can read."}
        )

    extension = PILLOW_FORMAT_EXTENSIONS.get((detected or "").upper())
    if extension is None:
        raise serializers.ValidationError(
            {field: "That image format is not one we accept."}
        )
    return extension


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
        # The extension comes from the bytes, not from content_type, which is
        # only ever the client's word for what it sent.
        attrs["extension"] = verify_is_an_image(decoded)
        attrs["decoded"] = decoded
        check_public_content(image=decoded, request=self.context.get("request"))
        return attrs

    def save_to(self, profile):
        extension = (
            self.validated_data.get("extension")
            or ALLOWED_PHOTO_TYPES[self.validated_data["content_type"]]
        )
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
    #: Declared rather than left to the model field, which is a TextField and
    #: would otherwise be exposed with no ceiling at all. The number matches
    #: the one a post's instructions are held to, so a recipe copied out of a
    #: post and one written in the editor can hold the same method.
    cooking_instructions = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=4000,
    )

    class Meta:
        model = SavedFoodMeal
        fields = [
            "id",
            "name",
            "ingredients",
            "cooking_instructions",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_name(self, value):
        name = value.strip()
        if not name:
            raise serializers.ValidationError("Give the saved meal a name.")
        return name

    def validate_cooking_instructions(self, value):
        return value.strip()

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

    @transaction.atomic
    def create(self, validated_data):
        ingredients = validated_data.pop("ingredients", [])
        saved = SavedFoodMeal.objects.create(**validated_data)
        self._replace_ingredients(saved, ingredients)
        return saved

    @transaction.atomic
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


class CopyFoodDaySerializer(serializers.Serializer):
    """Which day to copy the eating from, and which day to put it on.

    Two dates rather than "yesterday": the rule about which day is being
    repeated belongs to the screen asking, and the device knows what day it is
    for the person holding it. A server that guessed would be guessing in its
    own timezone.
    """

    source_date = serializers.DateField()
    target_date = serializers.DateField()


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
    #: Null on anything the app shipped with rather than a user made.
    #: Declaring the field by hand drops what the model knows -- the FK is
    #: null=True, but a serializer field defaults to allow_null=False -- so
    #: the contract promised an integer on rows that answer with null, and
    #: the generated client could not decode a seeded gym or exercise.
    created_by = serializers.PrimaryKeyRelatedField(read_only=True, allow_null=True)

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
        check_public_content(attrs, request=self.context.get("request"))
        return attrs


def feed_image_url_for(post, request):
    """The card-sized photo, or the original when there is no smaller copy.

    Falling back rather than returning null is what lets this be added without
    a migration of the existing photos and without a build of the app that
    knows about it: every post that has a picture still answers with one here.
    A null would mean "no photo", and a client drawing this field would lose
    the picture on every post made before the variant existed.
    """
    return signed_media_url(post.feed_image or post.image, request)


def profile_photo_url_for(profile, request):
    """The absolute URL of an uploaded photo, or null when there is none.

    Absolute because the app talks to the API from a different origin than the
    one serving the file, and a relative path would resolve against the app.
    """
    return signed_media_url(profile.profile_photo, request)


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
class ProfilePromptSerializer(serializers.Serializer):
    """One answered question, as anybody reading the profile sees it.

    `question` goes out as a plain string and the readable wording comes with
    it. A client draws the label, so a question added to the server after this
    build shipped renders correctly on it rather than failing to decode, which
    a closed enum in a response would guarantee.
    """

    question = serializers.CharField(read_only=True)
    question_label = serializers.CharField(
        source="get_question_display", read_only=True
    )
    answer = serializers.CharField(read_only=True)


class ProfilePromptWriteSerializer(serializers.Serializer):
    """One answer on its way in. Closed, unlike the response: a question the
    server does not offer is a mistake worth reporting, not a value to keep."""

    question = serializers.ChoiceField(choices=ProfilePrompt.Question.choices)
    answer = serializers.CharField(max_length=140)

    def validate_answer(self, value):
        answer = value.strip()
        if not answer:
            raise serializers.ValidationError("Write something, or remove the question.")
        # Deliberately not checked here. Three answers are saved together, and
        # checking each separately was three sequential provider calls for one
        # edit; the set is checked once in validate_prompts.
        return answer


class ProfileSocialLinkSerializer(serializers.ModelSerializer):
    """One outbound account, as everybody else sees it.

    Two fields only. The handle is the app own bookkeeping and the position is
    how the list was ordered, neither of which anybody reading a profile has
    any use for -- and the fewer fields leave here, the less there is to keep
    consistent between this and the public serialiser.
    """

    class Meta:
        model = ProfileSocialLink
        fields = ["platform", "url"]


class ProfileSocialLinkWriteSerializer(serializers.Serializer):
    """One link on the way in.

    ``url`` is a CharField rather than a URLField on purpose: it accepts a
    handle as readily as an address, and the normaliser is what decides which
    it got. A URLField here would refuse "@someone" before anything had a
    chance to turn it into a URL.
    """

    platform = serializers.ChoiceField(choices=ProfileSocialLink.Platform.choices)
    url = serializers.CharField(max_length=300, allow_blank=False)


class ProfileSocialLinksRequestSerializer(serializers.Serializer):
    """The whole set at once, like the prompts next door.

    Replace rather than patch, for the same reason: the screen behind this
    edits them together, and sending the set that should exist afterwards
    cannot leave a seventh link behind that nobody can see to delete.
    """

    social_links = serializers.ListField(
        child=ProfileSocialLinkWriteSerializer(),
        max_length=MAX_PROFILE_SOCIAL_LINKS,
        allow_empty=True,
    )

    def validate_social_links(self, value):
        platforms = [entry["platform"] for entry in value]
        if len(set(platforms)) != len(platforms):
            raise serializers.ValidationError(
                "Only one link per platform."
            )

        # Normalised here rather than in the view, so a bad link comes back as
        # a field error naming the platform it belongs to -- which is what the
        # editor needs to put the message beside the right row.
        cleaned = []
        errors = {}
        for index, entry in enumerate(value):
            try:
                url, handle = normalise_social_link(entry["platform"], entry["url"])
            except SocialLinkError as error:
                errors[index] = {"url": [str(error)]}
                continue
            cleaned.append(
                {"platform": entry["platform"], "url": url, "handle": handle}
            )
        if errors:
            raise serializers.ValidationError(errors)
        check_public_content(cleaned, request=self.context.get("request"))
        return cleaned


class ProfilePromptsRequestSerializer(serializers.Serializer):
    """The whole set at once.

    Replace rather than patch, because the screen behind this edits all three
    together: sending the set that should exist afterwards cannot leave a
    fourth answer behind that nobody can see to delete.
    """

    prompts = serializers.ListField(
        child=ProfilePromptWriteSerializer(),
        max_length=MAX_PROFILE_PROMPTS,
        allow_empty=True,
    )

    def validate_prompts(self, value):
        questions = [entry["question"] for entry in value]
        if len(set(questions)) != len(questions):
            raise serializers.ValidationError("Each question can only be answered once.")
        # One call for the whole set rather than one per answer.
        check_public_content(value, request=self.context.get("request"))
        return value


class ProfileHighlightSerializer(serializers.Serializer):
    """A featured lift and the set behind it.

    `source` is the important field. "logged" means the server read this out
    of a finished session and can point at the day; "manual" means the person
    typed it; "none" means there is nothing to show yet. A reader is told
    which, because a number nobody logged must not look like one that was.

    A plain string, not a closed enum, so a source added later does not stop an
    older build decoding the profile around it.
    """

    lift = serializers.CharField(read_only=True)
    lift_label = serializers.CharField(read_only=True)
    source = serializers.CharField(read_only=True)
    weight_kg = serializers.DecimalField(
        max_digits=7, decimal_places=2, read_only=True, allow_null=True
    )
    reps = serializers.IntegerField(read_only=True, allow_null=True)
    estimated_one_rep_max_kg = serializers.DecimalField(
        max_digits=7, decimal_places=2, read_only=True, allow_null=True
    )
    #: When it was lifted, and what it was called in the log. Both only for a
    #: logged set: a typed number has no day and no exercise behind it.
    performed_at = serializers.DateTimeField(read_only=True, allow_null=True)
    exercise_name = serializers.CharField(read_only=True, allow_null=True)


class ProfileHighlightWriteSerializer(serializers.Serializer):
    """One featured lift on its way in."""

    lift = serializers.ChoiceField(choices=ProfileHighlight.Lift.choices)
    manual_weight_kg = serializers.DecimalField(
        max_digits=7, decimal_places=2, required=False, allow_null=True,
        min_value=Decimal("0"),
    )
    manual_reps = serializers.IntegerField(
        required=False, allow_null=True, min_value=1,
    )

    def validate(self, attrs):
        weight = attrs.get("manual_weight_kg")
        reps = attrs.get("manual_reps")
        # Both or neither. Half a set is not a set, and a weight with no rep
        # count says less than nothing on a profile.
        if (weight is None) != (reps is None):
            raise serializers.ValidationError(
                "Give both a weight and a rep count, or neither."
            )
        return attrs


class ProfileHighlightsRequestSerializer(serializers.Serializer):
    """Which lifts to feature, in the order they should appear."""

    highlights = serializers.ListField(
        child=ProfileHighlightWriteSerializer(),
        max_length=MAX_PROFILE_HIGHLIGHTS,
        allow_empty=True,
    )

    def validate_highlights(self, value):
        lifts = [entry["lift"] for entry in value]
        if len(set(lifts)) != len(lifts):
            raise serializers.ValidationError("Each lift can only be featured once.")
        return value


def highlight_payload(highlight):
    """One featured lift, ready to serialize.

    A logged set wins over a typed one. Somebody who types a number and then
    logs a real set of the same lift should see the real one: it is the better
    evidence, and leaving the typed number in place would quietly outrank it
    forever.

    The estimate comes back None above a dozen reps, where Epley stops meaning
    much. That rule lives in `estimated_one_rep_max` and is not repeated here.
    """
    best = best_set_for(highlight.owner, highlight.lift)

    if best is not None:
        weight, reps, source = best.weight_kg, best.reps, "logged"
        performed_at = (
            best.session_exercise.session.ended_at
            or best.session_exercise.session.created_at
        )
        # Only a built-in exercise name goes on a public profile. A custom one
        # is text the owner wrote, it has never been through moderation, and it
        # can be edited after the fact -- so a highlight would be a way to put
        # arbitrary words on a public page and change them later. Exercises the
        # app shipped with have no creator; anything else falls back to the
        # canonical lift name, which is the label the card shows anyway.
        exercise = best.session_exercise.exercise
        exercise_name = exercise.name if exercise.created_by_id is None else highlight.get_lift_display()
    elif highlight.manual_weight_kg is not None and highlight.manual_reps is not None:
        weight, reps, source = highlight.manual_weight_kg, highlight.manual_reps, "manual"
        performed_at, exercise_name = None, None
    else:
        weight, reps, source = None, None, "none"
        performed_at, exercise_name = None, None

    return {
        "lift": highlight.lift,
        "lift_label": highlight.get_lift_display(),
        "source": source,
        "weight_kg": weight,
        "reps": reps,
        "estimated_one_rep_max_kg": estimated_one_rep_max(weight, reps),
        "performed_at": performed_at,
        "exercise_name": exercise_name,
    }


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
    prompts = serializers.SerializerMethodField()
    social_links = serializers.SerializerMethodField()
    is_readable = serializers.SerializerMethodField()
    viewer_follows = serializers.SerializerMethodField()
    viewer_has_requested = serializers.SerializerMethodField()

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
            "prompts",
            "social_links",
            "gym",
            "gym_name",
            "gym_city",
            "height_cm",
            "weight_kg",
            "target_weight_kg",
            "shows_height",
            "shows_weight",
            "shows_target_weight",
            "is_profile_public",
            "is_readable",
            "viewer_follows",
            "viewer_has_requested",
            "created_at",
        ]

    #: All a profile still says once it is closed.
    #:
    #: Enough to know whose door this is and that it is shut. The name and the
    #: photo are not on the list: somebody who closes their profile has said
    #: they do not want to be looked at, and a page showing their face over
    #: the word "private" would be honouring the letter of that and not much
    #: else.
    STILL_SHOWN_WHEN_PRIVATE = frozenset({
        "id",
        "username",
        "is_profile_public",
        "is_readable",
        # Kept through the emptying, and this is the point of them. A closed
        # profile still has to tell you where you stand with it, or the button
        # on it cannot say "Requested" and would offer to ask again.
        "viewer_follows",
        "viewer_has_requested",
        "created_at",
    })

    def to_representation(self, profile):
        """Empties a closed profile, leaving the shape of it intact.

        Emptied rather than dropped, and this is the important part: the
        generated iOS client requires every documented field to be present, so
        a key missing here is not a profile drawn as private -- it is a profile
        the app cannot decode at all, which is a crash rather than a closed
        door.

        Cleared by type rather than by naming each field, so that a field added
        to this serializer later is withheld by default and has to be named to
        escape. The other way round, every new field leaks until somebody
        remembers it.
        """
        data = super().to_representation(profile)
        if self._reader_may_read(profile):
            return data

        for key, value in data.items():
            if key in self.STILL_SHOWN_WHEN_PRIVATE:
                continue
            if isinstance(value, list):
                data[key] = []
            elif isinstance(value, bool):
                data[key] = False
            elif isinstance(value, str):
                data[key] = ""
            else:
                data[key] = None
        return data

    @extend_schema_field(serializers.BooleanField())
    def get_is_readable(self, profile):
        return self._reader_may_read(profile)

    def _reader_may_read(self, profile):
        """Whether this reader gets the profile itself or a closed door.

        Separate from `is_profile_public`, and the app needs both: closed says
        something about the profile, readable says something about the person
        reading it. A follower of a closed profile gets every field and still
        sees `is_profile_public` false, so a page that branched on that alone
        would draw the closed door over data it had been given.

        Closing a profile narrows its audience to the people already following
        rather than emptying it.
        """
        if profile.is_profile_public:
            return True
        reader = getattr(self.context.get("request"), "user", None)
        if not reader or not reader.is_authenticated:
            return False
        if profile.user_id == reader.id:
            return True
        return profile.id in self._followed_profile_ids

    @extend_schema_field(serializers.BooleanField())
    def get_viewer_follows(self, profile):
        return profile.id in self._followed_profile_ids

    @extend_schema_field(serializers.BooleanField())
    def get_viewer_has_requested(self, profile):
        return profile.id in self._requested_profile_ids

    @property
    def _requested_profile_ids(self):
        """Who the reader has an unanswered request out to. One query."""
        if not hasattr(self, "_requested_cache"):
            reader = getattr(self.context.get("request"), "user", None)
            if reader and reader.is_authenticated:
                self._requested_cache = set(
                    FollowRequest.objects.filter(
                        requester__user_id=reader.id
                    ).values_list("target_id", flat=True)
                )
            else:
                self._requested_cache = set()
        return self._requested_cache

    @property
    def _followed_profile_ids(self):
        """Who the reader follows, read once for the whole response.

        Asked per profile this would be a query per row when a list of them
        comes back. With `many=True` DRF reuses one child serializer for every
        object, so caching it here costs one query however many arrive.
        """
        if not hasattr(self, "_followed_cache"):
            reader = getattr(self.context.get("request"), "user", None)
            if reader and reader.is_authenticated:
                self._followed_cache = set(
                    Follow.objects.filter(
                        follower__user_id=reader.id
                    ).values_list("following_id", flat=True)
                )
            else:
                self._followed_cache = set()
        return self._followed_cache

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_profile_photo_url(self, profile):
        return profile_photo_url_for(profile, self.context.get("request"))

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_disciplines(self, profile):
        return disciplines_for(profile)

    # Prompts ride along on the profile because they are three short rows
    # that prefetch with it. Highlights do not: each one costs a query
    # into the set history, and a page of gym members would pay it per
    # person. They have their own endpoint, so the cost is asked for.
    @extend_schema_field(ProfilePromptSerializer(many=True))
    def get_prompts(self, profile):
        return ProfilePromptSerializer(profile.prompts.all(), many=True).data

    # Prefetched by the viewset alongside the prompts. Six rows at most, and a
    # profile without them serialises as an empty list rather than as a missing
    # key, so a client never has to tell "none" from "not sent".
    @extend_schema_field(ProfileSocialLinkSerializer(many=True))
    def get_social_links(self, profile):
        return ProfileSocialLinkSerializer(
            profile.social_links.all(), many=True
        ).data

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
    def validate(self, attrs):
        check_public_content(attrs, request=self.context.get("request"))
        return attrs

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
    #: Whether this account may open the private insights dashboard.
    #:
    #: Answered by the same function the dashboard itself guards with, rather
    #: than by a second copy of the rule. The client had been checking the
    #: admin address by hand, which is one of four conditions -- staff, active,
    #: that address, and an explicit AnalyticsAccess grant -- so it both
    #: offered the link to accounts the server would refuse and hid it from a
    #: future admin on a different address.
    #:
    #: Discoverability only. Django checks `allowed` again on every request to
    #: /insights/, and this field grants nothing.
    can_view_insights = serializers.SerializerMethodField()
    disciplines = DisciplineListField(
        child=serializers.ChoiceField(choices=RepbaseUser.TrainingStyle.choices),
        required=False,
        allow_empty=True,
    )

    #: Declared so blank reaches `validate_time_zone`. Left to the model field,
    #: DRF refuses an empty string before the validator runs, and "put me back
    #: on UTC" becomes an error the client cannot act on.
    time_zone = serializers.CharField(
        max_length=64, required=False, allow_blank=True
    )

    #: Read-only here. They are edited through me/social-links/, which replaces
    #: the set in one request; letting a profile PATCH carry them too would be
    #: two ways to write the same rows and one of them would drift.
    social_links = ProfileSocialLinkSerializer(many=True, read_only=True)


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
            "daily_step_goal",
            "unit_preference",
            "time_zone",
            "bio",
            "profile_photo_url",
            "disciplines",
            "can_view_insights",
            "gym",
            "gym_name",
            "gym_city",
            "shows_height",
            "shows_weight",
            "shows_target_weight",
            "is_profile_public",
            "social_links",
            "created_at",
            "updated_at",
        ]
        #: Bounded. Zero would make every day a goal day and the bar
        #: meaningless, and the ceiling stops a typo writing a number
        #: nothing on screen can render.
        extra_kwargs = {
            "daily_step_goal": {"min_value": 1_000, "max_value": 100_000},
        }
        read_only_fields = [
            "id",
            "profile_photo_url",
            "gym_name",
            "can_view_insights",
            "gym_city",
            "social_links",
            "created_at",
            "updated_at",
        ]

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_profile_photo_url(self, profile):
        return profile_photo_url_for(profile, self.context.get("request"))

    @extend_schema_field(serializers.BooleanField())
    def get_can_view_insights(self, profile):
        # Imported here rather than at module scope: analytics reads models,
        # and a top-level import would have serializers and analytics waiting
        # on each other the first time either is loaded.
        from .analytics import allowed

        request = self.context.get("request")
        if request is None or not hasattr(request, "user"):
            return False
        # Only ever about the person asking. A serializer rendering somebody
        # else's profile must not report what that person may open.
        if profile.user_id != getattr(request.user, "id", None):
            return False
        return allowed(request.user)



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
        # Judged where the user is. A date that is still today in Honolulu is
        # already tomorrow in UTC, and refusing it would be the server telling
        # somebody their birthday has not happened yet.
        if value and value > today_for(self.instance):
            raise serializers.ValidationError("Birthdate cannot be in the future.")
        return value

    def validate_time_zone(self, value):
        name = (value or "").strip()
        if not name:
            return "UTC"
        # Checked against what this machine actually knows, not a list kept
        # here that would drift every time the zone database is updated.
        if name not in available_timezones():
            raise serializers.ValidationError("Not a time zone this server knows.")
        return name

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
        check_public_content(attrs, request=self.context.get("request"))
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
    #: Null on anything the app shipped with rather than a user made.
    #: Declaring the field by hand drops what the model knows -- the FK is
    #: null=True, but a serializer field defaults to allow_null=False -- so
    #: the contract promised an integer on rows that answer with null, and
    #: the generated client could not decode a seeded gym or exercise.
    created_by = serializers.PrimaryKeyRelatedField(read_only=True, allow_null=True)

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


class InitialWorkoutExerciseSerializer(WorkoutExerciseSerializer):
    class Meta(WorkoutExerciseSerializer.Meta):
        fields = ["exercise", "order", "target_sets", "target_reps", "target_weight_kg", "notes"]
        read_only_fields = []


class WorkoutPlanExerciseSerializer(InitialWorkoutExerciseSerializer):
    relation_id = serializers.IntegerField(required=False, min_value=1)
    order = serializers.IntegerField(min_value=1, max_value=10000)

    class Meta(InitialWorkoutExerciseSerializer.Meta):
        fields = ["relation_id", *InitialWorkoutExerciseSerializer.Meta.fields]


class WorkoutTemplateSerializer(serializers.ModelSerializer):
    exercise_plan = WorkoutPlanExerciseSerializer(
        many=True, required=False, write_only=True, max_length=200,
        help_text="Update-only: atomically replace the exercise plan, preserving supplied relation IDs.",
    )
    initial_exercises = InitialWorkoutExerciseSerializer(
        many=True, required=False, write_only=True,
        help_text="Create-only: save all initial exercises in the same transaction as the template.",
    )
    initial_date = serializers.DateField(
        required=False, write_only=True,
        help_text="Create-only: optionally schedule the new template in the same transaction.",
    )
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
            "initial_exercises",
            "initial_date",
            "exercise_plan",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "owner", "created_at", "updated_at"]

    def validate(self, attrs):
        if not self.instance and "exercise_plan" in attrs:
            raise serializers.ValidationError("Use initial_exercises when creating a workout.")
        if self.instance and ("initial_exercises" in attrs or "initial_date" in attrs):
            raise serializers.ValidationError("Initial exercises and date are only accepted for a new workout.")
        orders = [entry.get("order", 1) for entry in attrs.get("initial_exercises", [])]
        if len(orders) != len(set(orders)):
            raise serializers.ValidationError({"initial_exercises": "Each exercise needs a distinct order."})
        plan = attrs.get("exercise_plan", [])
        plan_orders = [entry["order"] for entry in plan]
        relations = [entry["relation_id"] for entry in plan if "relation_id" in entry]
        if len(plan_orders) != len(set(plan_orders)) or len(relations) != len(set(relations)):
            raise serializers.ValidationError({"exercise_plan": "Orders and relation IDs must be unique."})
        return attrs

    @transaction.atomic
    def update(self, instance, validated_data):
        instance = WorkoutTemplate.objects.select_for_update().get(pk=instance.pk)
        plan = validated_data.pop("exercise_plan", None)
        if plan is not None:
            rows = list(instance.workout_exercises.all())
            by_id = {row.pk: row for row in rows}
            if any(entry.get("relation_id") not in by_id for entry in plan if "relation_id" in entry):
                raise serializers.ValidationError({"exercise_plan": "An exercise relation is not in this workout. Reload the plan."})
            # Move existing rows aside first so exchanging positions never
            # violates the unique(workout, order) constraint halfway through.
            offset = max([row.order for row in rows] + [entry["order"] for entry in plan] + [0]) + 1
            original = {(row.order, row.exercise_id): row for row in rows}
            for index, row in enumerate(rows):
                row.order = offset + index
                row.save(update_fields=["order"])
            kept = set()
            reserved = {entry["relation_id"] for entry in plan if "relation_id" in entry}
            for entry in plan:
                values = dict(entry)
                relation_id = values.pop("relation_id", None)
                row = by_id.get(relation_id) if relation_id else original.get((values["order"], values["exercise"].pk))
                if not relation_id and row is not None and row.pk in reserved:
                    row = None
                if row is not None and row.pk not in kept:
                    for field, value in values.items():
                        setattr(row, field, value)
                    row.save()
                else:
                    row = WorkoutExercise.objects.create(workout=instance, **values)
                kept.add(row.pk)
            instance.workout_exercises.exclude(pk__in=kept).delete()
        return super().update(instance, validated_data)

    @transaction.atomic
    def create(self, validated_data):
        exercises = validated_data.pop("initial_exercises", [])
        date = validated_data.pop("initial_date", None)
        workout = super().create(validated_data)
        for entry in exercises:
            WorkoutExercise.objects.create(workout=workout, **entry)
        if date is not None:
            WorkoutSchedule.objects.create(owner=workout.owner, workout=workout, scheduled_date=date)
        return workout

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


class PlannerSubtaskSerializer(serializers.ModelSerializer):
    """One step of a task, as it appears nested under its parent.

    Deliberately not the full serializer. Subtasks are one level deep, so a
    step has no steps of its own, and nesting the full thing would advertise
    a `subtasks` array that is always empty and invite a client to recurse
    into it. The fields here are what a checklist row draws.
    """

    is_complete = serializers.BooleanField(read_only=True)

    class Meta:
        model = PlannerEntry
        fields = ["id", "title", "is_complete", "completed_at", "notes"]
        read_only_fields = fields


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
    # Declared rather than inferred: left to the model field, the schema
    # advertised 0 to 2**63 and a generated client would happily send a
    # number the server refuses. The bounds belong in the contract.
    duration_minutes = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=MIN_PLANNER_DURATION_MINUTES,
        max_value=MAX_PLANNER_DURATION_MINUTES,
    )
    #: The steps of this task, in the day's own order. Empty on a task that
    #: has none, and always empty on a step, which cannot have its own.
    subtasks = PlannerSubtaskSerializer(many=True, read_only=True)
    #: So a client can draw "2 of 5" without counting the array itself, and
    #: without the array being the only way to know a task is a heading.
    subtask_count = serializers.SerializerMethodField()
    completed_subtask_count = serializers.SerializerMethodField()
    #: Create-only: make this task repeat. 1 is every day, 2 every other day.
    #: Absent means it happens once, which is what most tasks are.
    repeat_every_days = serializers.IntegerField(
        required=False, write_only=True, min_value=1, max_value=365
    )
    #: The first day the repeat no longer applies. Absent or null means it
    #: runs until it is stopped, which is the ordinary case for a habit.
    repeat_ends_on = serializers.DateField(
        required=False, write_only=True, allow_null=True
    )
    #: How often this task repeats, or null when it does not. Read from the
    #: rule rather than stored on the row, so ending a repeat does not have to
    #: rewrite every day it already wrote.
    repeat_interval_days = serializers.SerializerMethodField()

    @extend_schema_field(serializers.IntegerField(allow_null=True))
    def get_repeat_interval_days(self, entry):
        return entry.recurrence.interval_days if entry.recurrence_id else None

    @extend_schema_field(serializers.IntegerField())
    def get_subtask_count(self, entry):
        return len(entry.subtasks.all())

    @extend_schema_field(serializers.IntegerField())
    def get_completed_subtask_count(self, entry):
        return sum(1 for step in entry.subtasks.all() if step.completed_at is not None)

    class Meta:
        model = PlannerEntry
        fields = [
            "id",
            "owner",
            "kind",
            "title",
            "category",
            "priority",
            "scheduled_date",
            "scheduled_time",
            "duration_minutes",
            "is_complete",
            "completed_at",
            "workout",
            "workout_name",
            "notes",
            "parent",
            "subtasks",
            "subtask_count",
            "completed_subtask_count",
            "repeat_every_days",
            "repeat_ends_on",
            "repeat_interval_days",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "owner",
            "completed_at",
            "workout_name",
            "subtasks",
            "subtask_count",
            "completed_subtask_count",
            "repeat_interval_days",
            "created_at",
            "updated_at",
        ]

    #: Cleared when a full update leaves them out.
    #:
    #: PUT replaces, and DRF's default is to leave an absent optional field
    #: alone -- which makes PUT behave exactly like PATCH and leaves a client
    #: with no way to take anything back off. That matters here because the
    #: generated iOS client omits a nil rather than sending null, so "no time"
    #: and "don't touch the time" look identical on the wire. Naming these two
    #: is what lets a start time, or a length, be removed once it is set.
    SCHEDULE_FIELDS_CLEARED_BY_PUT = ("scheduled_time", "duration_minutes")

    def _resulting(self, attrs, field):
        """What ``field`` will hold once this request has been applied.

        A rule about a pair of fields has to be judged on what the row becomes,
        and absence means different things depending on the verb: on a PATCH it
        means "leave it", on a PUT it means "clear it".
        """
        if field in attrs:
            return attrs[field]
        if self.instance is None:
            return None
        if self.partial or field not in self.SCHEDULE_FIELDS_CLEARED_BY_PUT:
            return getattr(self.instance, field)
        return None

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

    def validate_parent(self, value):
        if value is None:
            return value
        if value.owner_id != self.context["request"].user.repbase_profile.id:
            raise serializers.ValidationError("That task belongs to someone else.")
        # One level. A step of a step is refused here so the client is told
        # which field was wrong; the database constraint behind it is what
        # makes the rule true regardless of who is writing.
        if value.is_subtask:
            raise serializers.ValidationError(
                "That is already a step of another task. Steps do not nest."
            )
        if value.kind != PlannerEntry.Kind.TASK:
            raise serializers.ValidationError("An event cannot have steps.")
        if self.instance is not None and value.pk == self.instance.pk:
            raise serializers.ValidationError("A task cannot be a step of itself.")
        # Re-parenting a task that already has steps would make them three
        # deep the moment it moved.
        if self.instance is not None and self.instance.subtasks.exists():
            raise serializers.ValidationError(
                "This task has steps of its own, so it cannot become a step."
            )
        return value

    def validate(self, attrs):
        # A length needs a start. Checked against what the row will end up as
        # rather than only what this request carries, so clearing the time on
        # something an hour long is refused too -- otherwise a PATCH could
        # leave the pair in the state the database constraint forbids, and the
        # refusal would arrive as a 500 instead of a named field error.
        duration = self._resulting(attrs, "duration_minutes")
        start = self._resulting(attrs, "scheduled_time")
        # The range is the field's own business. What is left here is the
        # part no single field can see: a length is only meaningful next to a
        # start, and either half can arrive in a different request.
        if duration is not None and start is None:
            raise serializers.ValidationError(
                {"duration_minutes": "Give it a start time before a length."}
            )

        # An event happens at a time; it is not something to tick off. Letting
        # one be "completed" would put a checkbox on a birthday.
        kind = attrs.get("kind", getattr(self.instance, "kind", None))
        if kind == PlannerEntry.Kind.EVENT and attrs.get("is_complete"):
            raise serializers.ValidationError(
                {"is_complete": "An event happens rather than being completed."}
            )

        # A task with steps does not own its own checkbox: the steps are the
        # record of the work and the heading is derived from them. The same
        # shape as the workout rule above, and for the same reason -- the half
        # carrying no evidence is the half that gives.
        if (
            self.instance is not None
            and "is_complete" in attrs
            and self.instance.subtasks.exists()
        ):
            raise serializers.ValidationError(
                {
                    "is_complete": (
                        "This task is done when its steps are. Tick the steps "
                        "off instead."
                    )
                }
            )

        # A step belongs to its parent's day. Left free, a checklist could
        # scatter itself across the week and the heading would sit on a day
        # holding none of its own work.
        parent = attrs.get("parent", getattr(self.instance, "parent", None))
        if parent is not None:
            attrs["scheduled_date"] = parent.scheduled_date
            attrs["kind"] = PlannerEntry.Kind.TASK

        # Repeats belong to tasks that stand on their own. An event happens
        # once by definition, and a step repeating independently of the task
        # it belongs to describes nothing.
        interval = attrs.get("repeat_every_days")
        if interval is not None:
            if kind == PlannerEntry.Kind.EVENT:
                raise serializers.ValidationError(
                    {"repeat_every_days": "An event happens once. Use a task to repeat."}
                )
            if parent is not None:
                raise serializers.ValidationError(
                    {"repeat_every_days": "A step repeats with its task, not on its own."}
                )
            ends = attrs.get("repeat_ends_on")
            start = attrs.get("scheduled_date", getattr(self.instance, "scheduled_date", None))
            if ends is not None and start is not None and ends <= start:
                raise serializers.ValidationError(
                    {"repeat_ends_on": "The repeat has to end after the day it starts."}
                )
        elif attrs.get("repeat_ends_on") is not None:
            raise serializers.ValidationError(
                {"repeat_ends_on": "Say how often it repeats before saying when it stops."}
            )

        # A workout that was trained cannot be un-trained by a checkbox.
        #
        # The session is the record of the training; the tick is only the
        # plan's account of it. While they were allowed to differ, Training
        # showed a finished session with its sets and volume while Home said
        # "up next" and offered Start, and the calendar counted the day as
        # outstanding -- the app telling somebody to do a workout they had
        # just done. The tick is the one that has to give, because it is the
        # one carrying no evidence.
        #
        # Undo remains the way out: it deletes the session, and deleting the
        # session unticks the task (see `WorkoutSessionViewSet.perform_destroy`).
        # That is a different act from clearing a checkbox, and it says so.
        if (
            self.instance is not None
            and attrs.get("is_complete") is False
            and kind == PlannerEntry.Kind.TASK
            and completed_sessions_for(self.instance)
        ):
            raise serializers.ValidationError(
                {
                    "is_complete": (
                        "This workout has a recorded session, so it stays "
                        "done. Use Undo in Training to remove the session."
                    )
                }
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
        # PUT replaces: anything the client left out is gone, not merely
        # unmentioned. Only the schedule pair, because those are the two a
        # client has no other way to clear.
        if not self.partial:
            for field in self.SCHEDULE_FIELDS_CLEARED_BY_PUT:
                validated_data.setdefault(field, None)
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
    #: allow_null for the same reason workout_type has it, which is the whole
    #: bug: without it DRF does not send null when workout is None, it drops
    #: the key entirely, and the generated client decodes the key as required
    #: and throws. Deleting a workout template sets workout to NULL on every
    #: session that used it, so one deletion made a user's whole training
    #: history undecodable -- and a route upload answered 200 and then failed
    #: in the app, for points the server had already stored.
    workout_name = serializers.CharField(
        source="workout.name",
        read_only=True,
        allow_null=True,
    )
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
            "cooking_instructions",
            "total_calories",
            "total_protein_grams",
            "total_carbohydrate_grams",
            "total_fat_grams",
        ]
        read_only_fields = ["name", "date", "entries", "cooking_instructions"]

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
    feed_image_url = serializers.SerializerMethodField()

    class Meta:
        model = Post
        fields = [
            "id",
            "author",
            "kind",
            "image_url",
            "feed_image_url",
            "caption",
            "workout",
            "meal",
            "planner",
            "created_at",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_image_url(self, post):
        return signed_media_url(post.image, self.context.get("request"))

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_feed_image_url(self, post):
        return feed_image_url_for(post, self.context.get("request"))


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
        request = self.context.get("request")
        viewer = getattr(getattr(request, "user", None), "repbase_profile", None)
        if viewer is None or comment.is_hidden:
            return []
        from .views import visible_comments_for
        # Read endpoints prefetch the filtered set. PATCH responses do not;
        # they must apply the same visibility rules before serializing replies.
        cached = getattr(comment, '_prefetched_objects_cache', {})
        rows = comment.replies.all() if 'replies' in cached else visible_comments_for(viewer, comment.replies.all())
        # Prefetched by the view. Sorting here rather than in the query keeps
        # the prefetch usable: re-ordering it would fetch the rows again.
        replies = sorted(
            rows,
            key=lambda reply: (reply.created_at, reply.id),
        )
        return PostReplySerializer(replies, many=True, context=self.context).data

    def get_viewer_is_author(self, comment) -> bool:
        request = self.context.get("request")
        viewer = getattr(getattr(request, "user", None), "repbase_profile", None)
        return viewer is not None and comment.author_id == viewer.id

    def validate(self, attrs):
        check_public_content({'body': attrs.get('body', '')}, request=self.context.get("request"))
        if self.instance is not None:
            for field in ('post', 'parent'):
                if field in attrs and getattr(attrs[field], 'pk', None) != getattr(self.instance, field + '_id'):
                    raise serializers.ValidationError({field: 'A comment cannot be moved to another thread.'})
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


class PreviousSetSerializer(serializers.Serializer):
    """One set from the last time an exercise was done."""

    exercise = serializers.IntegerField(read_only=True)
    set_number = serializers.IntegerField(read_only=True)
    weight_kg = serializers.DecimalField(
        max_digits=7, decimal_places=2, read_only=True, allow_null=True
    )
    reps = serializers.IntegerField(read_only=True, allow_null=True)
    performed_at = serializers.DateTimeField(read_only=True, allow_null=True)


class SavedWorkoutResultSerializer(serializers.Serializer):
    """What saving somebody else's posted workout produced.

    The name is returned because it is not always the one on the post: a user
    who already has a "Push Day" gets the copy under a name saying where it
    came from, and the app has to be able to tell them so.
    """

    #: The pk of the saved copy. See the note on SavedMealResultSerializer:
    #: a PrimaryKeyRelatedField documents as a string and returns a number.
    workout = serializers.IntegerField(read_only=True)
    name = serializers.CharField(read_only=True)
    exercise_count = serializers.IntegerField(read_only=True)
    renamed = serializers.BooleanField(read_only=True)
    #: True when this workout was already saved and nothing new was made.
    already_saved = serializers.BooleanField(read_only=True, default=False)


class ReportPostSerializer(serializers.Serializer):
    """What a reporter sends.

    ``reason`` is closed, because the whole point of the list is that reports
    can be counted and triaged. ``detail`` is free text and optional, for the
    case the list does not cover.
    """

    reason = serializers.ChoiceField(choices=PostReport.Reason.choices)
    detail = serializers.CharField(
        max_length=500, required=False, allow_blank=True, default=""
    )


class PostReportResultSerializer(serializers.Serializer):
    """What came of reporting.

    ``already_reported`` rather than an error on a second press: reporting
    twice is far more often someone unsure the first one worked than someone
    with a second complaint, and an error would tell them off for it.
    """

    reason = serializers.CharField(read_only=True)
    already_reported = serializers.BooleanField(read_only=True)


class SavedMealResultSerializer(serializers.Serializer):
    """What saving somebody else's posted meal produced.

    The same shape as `SavedWorkoutResultSerializer` and for the same reason:
    saved meal names are unique per user, so the name it landed under is not
    always the one on the post and the app has to be able to say so.
    """

    #: The pk of the saved copy. An IntegerField rather than a
    #: PrimaryKeyRelatedField: the latter has no queryset to infer from, so
    #: the schema called it a string while the response carried a number,
    #: and the generated client refused to decode its own contract.
    meal = serializers.IntegerField(read_only=True)
    name = serializers.CharField(read_only=True)
    item_count = serializers.IntegerField(read_only=True)
    renamed = serializers.BooleanField(read_only=True)
    #: True when this meal was already saved and nothing new was made.
    already_saved = serializers.BooleanField(read_only=True, default=False)


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
    #: True when this reader has already copied the post into their own.
    #:
    #: Read off the annotation rather than queried, and defaulted to False
    #: for the few paths that serialize a post without it -- a freshly
    #: created one, which nobody can have saved yet.
    viewer_saved = serializers.SerializerMethodField()
    viewer_follows_author = serializers.SerializerMethodField()
    image_url = serializers.SerializerMethodField()
    #: The same picture at card size. Both are sent because they answer
    #: different screens: a feed draws dozens of these and wants the small
    #: one, while opening a post is a deliberate request to see it properly.
    feed_image_url = serializers.SerializerMethodField()
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
            "feed_image_url",
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
            "viewer_saved",
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
            "viewer_saved",
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
        return signed_media_url(post.image, self.context.get("request"))

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_feed_image_url(self, post):
        """The card-sized photo, falling back to the original.

        Never null while `image_url` is set, so a client can draw this one
        field and be right about every post, including the ones made before
        the smaller copy existed.
        """
        return feed_image_url_for(post, self.context.get("request"))

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
    def get_viewer_saved(self, post) -> bool:
        """Whether the reader already copied this post into their own.

        Off the annotation, like every other viewer_ field here: the Save
        button is on every card, so asking per row would be a query a card.
        Defaults to False where a post is serialized without the annotation
        -- creating one, where nobody can have saved it yet.
        """
        return bool(getattr(post, "viewer_saved", False))

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
        if counted is not None:
            return counted
        from .views import visible_comments_for
        viewer = self._viewer()
        return visible_comments_for(viewer, post.comments.all()).count() if viewer else 0

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

    def _visible_repost_targets(self):
        """Which reposted originals this viewer may see, for the whole page.

        One query per response rather than one per repost. It asks
        `visible_posts_for` rather than re-stating the visibility rules as an
        annotation: a second opinion about who may see what is exactly the
        thing that drifts, and the drift would be silent and in the unsafe
        direction.
        """
        cached = self.context.get('_visible_repost_targets')
        if cached is not None:
            return cached

        from .views import visible_posts_for

        parent = self.parent
        batch = parent.instance if isinstance(parent, serializers.ListSerializer) else self.instance
        if batch is None:
            batch = []
        elif isinstance(batch, Post):
            batch = [batch]
        wanted = {
            item.repost_of_id for item in batch
            if getattr(item, 'repost_of_id', None) is not None
        }
        viewer = self._viewer()
        visible = set()
        if wanted and viewer is not None:
            visible = set(
                visible_posts_for(viewer, Post.objects.filter(pk__in=wanted))
                .values_list('pk', flat=True)
            )
        self.context['_visible_repost_targets'] = visible
        return visible

    @extend_schema_field(RepostedPostSerializer(allow_null=True))
    def get_repost_of(self, post):
        original = post.repost_of
        if original is None:
            return None
        if original.pk not in self._visible_repost_targets():
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
    #: How the meal was made. Ignored for any other kind of post.
    #:
    #: Written by the author rather than copied from the meal, because a meal
    #: is a list of foods and a recipe is what somebody did with them. Kept on
    #: the snapshot with the rest, so editing the meal later cannot rewrite
    #: what people already read.
    cooking_instructions = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=4000,
        help_text=(
            "How the meal was made, for a meal post. Ignored for other kinds."
        ),
    )

    def validate_caption(self, value):
        return value.strip()

    def validate_cooking_instructions(self, value):
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
        attrs["image_extension"] = verify_is_an_image(attrs["decoded_image"])
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
        check_public_content({'caption': value}, request=self.context.get("request"))
        return value.strip()


class UnreadCountSerializer(serializers.Serializer):
    """How many notifications are still unseen."""

    unread = serializers.IntegerField(read_only=True)


class NotificationSerializer(serializers.ModelSerializer):
    """One line of the notifications page.

    Flattened rather than nested. Every row names a person and sometimes a
    post, and a page of them should not cost a nested serializer per row for
    fields that are three strings and a URL.
    """

    actor_id = serializers.IntegerField(source="actor.id", read_only=True)
    actor_username = serializers.CharField(
        source="actor.user.username", read_only=True
    )
    actor_first_name = serializers.CharField(
        source="actor.user.first_name", read_only=True
    )
    actor_last_name = serializers.CharField(
        source="actor.user.last_name", read_only=True
    )
    actor_photo_url = serializers.SerializerMethodField()
    #: What was said, on the kinds where something was. Null elsewhere rather
    #: than an empty string, so "no comment" and "an empty comment" stay
    #: different things.
    comment_body = serializers.CharField(
        source="comment.body", read_only=True, allow_null=True, default=None
    )
    is_read = serializers.SerializerMethodField()

    class Meta:
        model = Notification
        fields = [
            "id",
            "kind",
            "actor_id",
            "actor_username",
            "actor_first_name",
            "actor_last_name",
            "actor_photo_url",
            "post",
            "comment_body",
            "is_read",
            "created_at",
        ]

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_actor_photo_url(self, notification):
        return profile_photo_url_for(
            notification.actor, self.context.get("request")
        )

    @extend_schema_field(serializers.BooleanField())
    def get_is_read(self, notification):
        return notification.read_at is not None


class FollowRequestSerializer(serializers.ModelSerializer):
    """A pending request, described by whoever is asking.

    The requester rather than the target: this list is only ever read by the
    person being asked, and they know who they are.
    """

    requester_id = serializers.IntegerField(source="requester.id", read_only=True)
    username = serializers.CharField(
        source="requester.user.username", read_only=True
    )
    first_name = serializers.CharField(
        source="requester.user.first_name", read_only=True
    )
    last_name = serializers.CharField(
        source="requester.user.last_name", read_only=True
    )
    profile_photo_url = serializers.SerializerMethodField()

    class Meta:
        model = FollowRequest
        fields = [
            "id",
            "requester_id",
            "username",
            "first_name",
            "last_name",
            "profile_photo_url",
            "created_at",
        ]

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_profile_photo_url(self, request_row):
        return profile_photo_url_for(
            request_row.requester, self.context.get("request")
        )


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
            "stop_conflicting_repeats",
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
        return value

    def validate(self, attrs):
        attrs = super().validate(attrs)
        owner = self.context["request"].user.repbase_profile

        # A workout driven by both a weekly repeat and a rotation would have
        # two generators writing the same dates, each undoing the other's idea
        # of the plan. So one of them has to go -- and rather than refusing and
        # leaving the user to find the weekly repeat themselves, the app can
        # ask, and send `stop_conflicting_repeats` when they say yes.
        slots = attrs.get("slots")
        if slots is None:
            return attrs

        clashing = WorkoutRecurrence.objects.filter(
            owner=owner,
            workout__in=[
                slot["workout"] for slot in slots if slot.get("workout") is not None
            ],
            effective_until__isnull=True,
        ).select_related("workout")

        if clashing and not attrs.get("stop_conflicting_repeats"):
            names = sorted({rule.workout.name for rule in clashing})
            joined = names[0] if len(names) == 1 else ", ".join(names)
            raise serializers.ValidationError(
                {
                    "slots": (
                        f"{joined} already repeats weekly. Stop the weekly "
                        "repeat before putting it in a rotation."
                    ),
                    # Named so the app can offer to stop them rather than
                    # making somebody go and find them.
                    "conflicting_workouts": names,
                }
            )

        # Carried to create/update, which closes them inside the same
        # transaction that writes the rotation. Stopping them here would end
        # somebody's weekly repeat for a rotation that then failed to save.
        self._repeats_to_stop = list(clashing)
        return attrs

    #: Write-only. Says the user has been asked and agreed, so the weekly
    #: repeats standing in the way may be closed as part of this save.
    stop_conflicting_repeats = serializers.BooleanField(
        write_only=True, required=False, default=False
    )

    def _stop_clashing_repeats(self, owner):
        """Ends the weekly repeats this rotation is replacing.

        Closed rather than deleted, the same as stopping one by hand: weeks
        already trained keep resolving through whatever was planned then.
        """
        for rule in getattr(self, "_repeats_to_stop", []):
            rule.effective_until = today_for(owner)
            rule.save(update_fields=["effective_until", "updated_at"])

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
        validated_data.pop("stop_conflicting_repeats", None)
        cycle = WorkoutCycle.objects.create(**validated_data)
        self._stop_clashing_repeats(cycle.owner)
        self._write_slots(cycle, slots)
        return cycle

    def update(self, instance, validated_data):
        slots = validated_data.pop("slots", None)
        validated_data.pop("stop_conflicting_repeats", None)
        self._stop_clashing_repeats(instance.owner)
        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save()
        if slots is not None:
            self._write_slots(instance, slots)
        return instance

    def _today(self, cycle):
        # The rotation owner's day, not the server's. "Day 3 of 8" advancing
        # early every evening was this: UTC rolls over at seven in US Central.
        #
        # The cycle is required rather than optional so that no caller can
        # quietly fall back to the server's clock again.
        return today_for(cycle.owner)

    def get_current_position(self, cycle) -> int:
        return cycle.position(self._today(cycle))

    def get_current_workout_name(self, cycle) -> str:
        slot = cycle.slot(self._today(cycle))
        if slot is None or slot.workout is None:
            return "Rest"
        return slot.workout.name

    def _next(self, cycle):
        """The next day of the rotation that is a workout rather than a rest."""
        today = self._today(cycle)
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


class ClearScheduleResultSerializer(serializers.Serializer):
    """How many planned days were cleared."""

    cleared = serializers.IntegerField(read_only=True)


class CycleActivateSerializer(serializers.Serializer):
    """When the new rotation takes over.

    Optional, and today when it is left out. A date in the future is the point
    of it: the rotation you are on keeps running right up to that day, so
    switching is something you plan rather than something that happens to next
    week the moment you tap.
    """

    start_on = serializers.DateField(required=False)


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


class PersonalizationSerializer(serializers.ModelSerializer):
    """The three answers, as the app already spells them.

    The list is checked for shape but not for membership: it holds whatever
    the app currently offers, and a choice retired from the app should not turn
    an existing account into a validation error on read.
    """

    training_types = serializers.ListField(
        child=serializers.CharField(max_length=60), required=False, allow_empty=True
    )

    class Meta:
        model = Personalization
        fields = (
            "training_types",
            "weekly_target",
            "emphasis",
        )

    def validate_weekly_target(self, value):
        # A week has seven days. Nought is a person who has not decided yet.
        if not 0 <= value <= 7:
            raise serializers.ValidationError(
                "A weekly target is between 0 and 7 sessions."
            )
        return value


class FoodSearchResultSerializer(serializers.Serializer):
    """One food from the public catalogue, in the shape the app logs.

    Deliberately the same four figures a FoodEntry holds, and nothing else the
    app would have to learn. Nutrition is carried as a string for the same
    reason every other nutrition figure here is: a calorie count that travels
    as a JSON number comes back from some parsers as 232.99999999999997.
    """

    source_id = serializers.CharField()
    name = serializers.CharField()
    brand = serializers.CharField(allow_blank=True)
    #: What the figures below are for -- "1 slice (28 g)", or "100 g".
    serving_description = serializers.CharField()
    calories = serializers.DecimalField(max_digits=8, decimal_places=2)
    protein_grams = serializers.DecimalField(max_digits=8, decimal_places=2)
    carbohydrate_grams = serializers.DecimalField(max_digits=8, decimal_places=2)
    fat_grams = serializers.DecimalField(max_digits=8, decimal_places=2)


class PasswordResetRequestSerializer(serializers.Serializer):
    """Asking for a code.

    Takes an email and nothing else, and the view answers the same way
    whether or not an account has it. Telling an unauthenticated caller which
    addresses are registered turns this into a way to enumerate the userbase.
    """

    email = serializers.EmailField()


class PasswordResetConfirmSerializer(serializers.Serializer):
    """Spending a code on a new password.

    The email comes back with it so the code is only ever checked against the
    account it was issued for; a code good for whoever happens to present it
    would be a six digit skeleton key.
    """

    email = serializers.EmailField()
    code = serializers.CharField(min_length=6, max_length=6)
    new_password = serializers.CharField(write_only=True, min_length=8)

    def validate_new_password(self, value):
        # The same rules registration applies. A reset is not a way around
        # the password policy.
        password_validation.validate_password(value)
        return value
