import secrets
import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.core.mail import send_mail
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.db import models, transaction
from django.utils.dateparse import parse_date
from django.db.models import (
    Count,
    Exists,
    Max,
    OuterRef,
    Prefetch,
    Q,
    Subquery,
    Sum,
    Value,
)
from django.db.models.functions import Coalesce
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiParameter,
    extend_schema,
    extend_schema_view,
)
from rest_framework import mixins, pagination, status, viewsets
from rest_framework.exceptions import (
    APIException,
    NotFound,
    PermissionDenied,
    ValidationError,
)
from rest_framework.authtoken.models import Token
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.generics import RetrieveUpdateDestroyAPIView

from . import food_sources
from .models import (
    CYCLE_MATERIALIZE_DAYS,
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
    FoodEntry,
    FoodMeal,
    Gym,
    NutritionGoal,
    DEFAULT_FOOD_MEAL_COUNT,
    MAX_PROFILE_HIGHLIGHTS,
    MAX_PROFILE_PROMPTS,
    ProfileHighlight,
    ProfilePrompt,
    PlannerCategory,
    PostReport,
    ProfileSocialLink,
    SavedFoodIngredient,
    SavedFoodMeal,
    PlannerEntry,
    WorkoutRecurrence,
    WorkoutSchedule,
    WorkoutSession,
    WorkoutTemplate,
    Block,
    Follow,
    FoodSearchCache,
    PasswordResetCode,
    Personalization,
    Post,
    PostComment,
    PostLike,
    PostMeal,
    PostMealEntry,
    PostPlannerEntry,
    PostWorkout,
    PostWorkoutExercise,
    food_meals_for_day,
    today_for,
    normalize_gym_text,
    plan_recurring_week,
    week_start_for,
)
from .permissions import IsCustomExerciseOwnerOrAdmin
from .serializers import (
    ALLOWED_PHOTO_TYPES,
    AuthResponseSerializer,
    BodyWeightEntrySerializer,
    DailyStepCountRecordSerializer,
    TrainingStatsSerializer,
    GearSerializer,
    CycleShiftResultSerializer,
    CycleShiftSerializer,
    CycleActivateSerializer,
    CyclePlanAheadSerializer,
    WorkoutCycleSerializer,
    HealthImportResultSerializer,
    HealthWorkoutImportSerializer,
    DailyStepCountSerializer,
    ExerciseProgressPointSerializer,
    ExerciseSerializer,
    LoginSerializer,
    PublicRepbaseUserSerializer,
    RegisterSerializer,
    RepbaseUserSerializer,
    SessionExerciseSerializer,
    PersonalRecordSerializer,
    SessionCardioSerializer,
    SessionRoutePointSerializer,
    SessionRouteUploadSerializer,
    SetEntrySerializer,
    ApplySavedMealSerializer,
    EnsureFoodDaySerializer,
    FoodEntrySerializer,
    FoodMealSerializer,
    GymSerializer,
    NutritionGoalSerializer,
    ProfileHighlightSerializer,
    ProfileHighlightsRequestSerializer,
    ProfilePhotoUploadSerializer,
    ProfilePromptSerializer,
    ProfilePromptsRequestSerializer,
    ProfileSocialLinkSerializer,
    ProfileSocialLinksRequestSerializer,
    highlight_payload,
    RecentFoodSerializer,
    SavedFoodMealSerializer,
    PlanWeekSerializer,
    PlannerEntrySerializer,
    PlannerSyncSerializer,
    WorkoutExerciseSerializer,
    WorkoutRecurrenceSerializer,
    WorkoutScheduleSerializer,
    WorkoutSessionSerializer,
    WorkoutTemplateSerializer,
    BlockSerializer,
    CreatePostSerializer,
    PostCommentSerializer,
    FoodSearchResultSerializer,
    PasswordResetConfirmSerializer,
    PersonalizationSerializer,
    PasswordResetRequestSerializer,
    PostSerializer,
    PreviousSetSerializer,
    PostReportResultSerializer,
    ReportPostSerializer,
    SavedMealResultSerializer,
    SavedWorkoutResultSerializer,
    UpdatePostSerializer,
)


def home(request):
    return JsonResponse(
        {
            "message": "Welcome to the Repbase API",
            "api": "/api/v1/",
            "schema": "/api/schema/",
            "documentation": "/api/docs/",
            "repbase": "/api/repbase/",
            "admin": "/admin/",
        }
    )


def health(request):
    return JsonResponse({"status": "ok"})


def repbase_users(request):
    users = RepbaseUser.objects.select_related("user").order_by("-created_at")
    return render(request, "core/repbase_users.html", {"users": users})


User = get_user_model()


def profile_for(user):
    profile, _ = RepbaseUser.objects.get_or_create(user=user)
    return profile


class RegisterView(APIView):
    # Per address rather than per account, which is how throwaway accounts
    # get made in bulk.
    throttle_scope = "register"
    throttle_classes = [ScopedRateThrottle]
    authentication_classes = []
    permission_classes = [AllowAny]

    @extend_schema(request=RegisterSerializer, responses={201: AuthResponseSerializer})
    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile = serializer.save()
        token = Token.objects.create(user=profile.user)
        response = AuthResponseSerializer({"token": token.key, "user": profile})
        return Response(response.data, status=status.HTTP_201_CREATED)


class LoginView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    # Tighter than anything else. An unlimited password field is the whole
    # attack, and somebody who has genuinely forgotten theirs does not try
    # twenty times inside an hour.
    throttle_scope = "login"
    throttle_classes = [ScopedRateThrottle]

    @extend_schema(request=LoginSerializer, responses={200: AuthResponseSerializer})
    def post(self, request):
        serializer = LoginSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]
        profile = profile_for(user)
        token, _ = Token.objects.get_or_create(user=user)
        response = AuthResponseSerializer({"token": token.key, "user": profile})
        return Response(response.data)


class ServiceUnavailable(APIException):
    """Something upstream is down, and it is not the caller's fault.

    503 rather than 500: the request was fine, the answer is simply not
    available at the moment, and the difference decides whether the app tells
    somebody to try again or that something is broken.
    """

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_code = "service_unavailable"


def _weekly_goal(owner, week_start):
    """How many sessions this week is meant to hold.

    It used to be however many were on the schedule, which made it a goal that
    could not be missed: whatever you planned became the target, so the card
    read "goal met" the moment the week was laid out and never said anything
    again. A number that moves to wherever you already are is a description,
    not a goal.

    The personalization flow asks for this outright -- "workouts each week" --
    and until now did nothing with the answer. So the answer is the goal.

    An account that has never been through that flow keeps the old behaviour
    rather than dropping to nothing. It is still a weak goal, but it is the
    one that account has always had, and a beta is a bad place to have
    somebody's dashboard change because of a question they were never asked.
    """
    chosen = (
        Personalization.objects.filter(repbase_user=owner)
        .values_list("weekly_target", flat=True)
        .first()
    )
    if chosen is not None:
        return chosen

    return WorkoutSchedule.objects.filter(
        owner=owner,
        scheduled_date__gte=week_start,
        scheduled_date__lt=week_start + timedelta(days=7),
    ).count()


class PersonalizationView(APIView):
    """Read and update what somebody wants from Repbase.

    Created on first read rather than at sign-up, so an account that has never
    finished the flow answers with the defaults instead of a 404 the app would
    have to special-case.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: PersonalizationSerializer})
    def get(self, request):
        return Response(PersonalizationSerializer(self._record(request)).data)

    @extend_schema(
        request=PersonalizationSerializer,
        responses={200: PersonalizationSerializer},
        description="Updates only the answers included in the request.",
    )
    def patch(self, request):
        serializer = PersonalizationSerializer(
            self._record(request), data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def _record(self, request):
        profile = profile_for(request.user)
        record, _ = Personalization.objects.get_or_create(repbase_user=profile)
        return record


class FoodSearchView(APIView):
    """Look a food up in FoodData Central.

    Proxied rather than called from the app, for four reasons that all point
    the same way: the API key stays here; every device is not separately
    telling a third party what its owner is eating; the results can be cached
    against a rate limit that is shared by everybody; and the reading of the
    upstream response -- which is where the wrong numbers come from -- happens
    once, in one place, with tests around it.
    """

    permission_classes = [IsAuthenticated]
    throttle_scope = "food_search"
    throttle_classes = [ScopedRateThrottle]

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="q",
                type=str,
                location=OpenApiParameter.QUERY,
                required=True,
                description="What to search for. Two characters or more.",
            )
        ],
        responses={200: FoodSearchResultSerializer(many=True)},
        description=(
            "Searches USDA FoodData Central and returns foods in the shape a "
            "food entry is logged in. Results are cached for a week."
        ),
    )
    def get(self, request):
        term = FoodSearchCache.normalize(request.query_params.get("q", ""))
        if len(term) < 2:
            return Response([])

        cached = FoodSearchCache.objects.filter(term=term).first()
        if cached is not None and cached.is_fresh:
            return Response(cached.payload)

        try:
            foods = food_sources.search(term)
        except food_sources.FoodSourceUnavailable:
            # A stale answer beats no answer: the figures are reference data,
            # and a week-old calorie count for oats is still the calorie count
            # for oats.
            if cached is not None:
                return Response(cached.payload)
            raise ServiceUnavailable(
                "The food database could not be reached. You can still reuse a "
                "food you have logged before, or enter one by hand."
            )

        payload = FoodSearchResultSerializer(foods, many=True).data
        FoodSearchCache.objects.update_or_create(
            term=term, defaults={"payload": payload}
        )
        return Response(payload)


class PasswordResetRequestView(APIView):
    """Send a code to an address, if it belongs to somebody.

    Always answers 204, whether or not it found an account. The alternative
    tells an unauthenticated caller which email addresses are registered here,
    which is worth more to somebody enumerating a userbase than the clearer
    message is worth to a person who mistyped their own address.

    Any code already outstanding for that account is spent first, so asking
    twice does not leave two live codes.
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_scope = "password_reset"
    throttle_classes = [ScopedRateThrottle]

    @extend_schema(
        request=PasswordResetRequestSerializer,
        responses={204: None},
        description=(
            "Emails a six digit reset code. Answers 204 whether or not the "
            "address belongs to an account."
        ),
    )
    def post(self, request):
        payload = PasswordResetRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        email = payload.validated_data["email"]

        user = User.objects.filter(email__iexact=email).first()
        if user is not None:
            with transaction.atomic():
                PasswordResetCode.objects.filter(
                    user=user, used_at__isnull=True
                ).update(used_at=timezone.now())

                code = f"{secrets.randbelow(1_000_000):06d}"
                PasswordResetCode.objects.create(
                    user=user,
                    code_hash=make_password(code),
                    expires_at=timezone.now() + PasswordResetCode.LIFETIME,
                )
            minutes = int(PasswordResetCode.LIFETIME.total_seconds() // 60)
            send_mail(
                subject="Your Repbase reset code",
                message=(
                    f"Your Repbase password reset code is {code}.\n\n"
                    f"It works once and expires in {minutes} minutes. "
                    "If you did not ask to reset your password, you can "
                    "ignore this email and nothing will change."
                ),
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[user.email],
                fail_silently=False,
            )

        return Response(status=status.HTTP_204_NO_CONTENT)


class PasswordResetConfirmView(APIView):
    """Set a new password with a code.

    On success every existing token is deleted and a fresh one issued. Someone
    resetting a password has often lost control of the account, and leaving
    the old sessions signed in would leave whoever they are worried about
    signed in too.
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    # Its own scope, wider than asking for a code. Sharing one budget with the
    # request endpoint capped the pair at six calls an hour between them, so
    # the five wrong guesses the model allows could never actually be made.
    throttle_scope = "password_reset_confirm"
    throttle_classes = [ScopedRateThrottle]

    @extend_schema(
        request=PasswordResetConfirmSerializer,
        responses={200: AuthResponseSerializer},
        description=(
            "Sets a new password using the emailed code, signs every other "
            "session out, and returns a fresh token."
        ),
    )
    def post(self, request):
        payload = PasswordResetConfirmSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        email = payload.validated_data["email"]
        submitted = payload.validated_data["code"]

        invalid = ValidationError(
            {"code": "That code is wrong or has expired. Ask for a new one."}
        )

        user = User.objects.filter(email__iexact=email).first()
        if user is None:
            # The same words as a wrong code, for the same reason the request
            # endpoint says nothing: an address that is not registered should
            # not be distinguishable from one that is.
            raise invalid

        with transaction.atomic():
            entry = (
                PasswordResetCode.objects.select_for_update()
                .filter(user=user, used_at__isnull=True)
                .order_by("-created_at")
                .first()
            )
            live = entry is not None and entry.is_live
            correct = live and check_password(submitted, entry.code_hash)

            if live and not correct:
                entry.attempts += 1
                # Spent rather than merely counted, so the ceiling cannot be
                # walked around by asking for the row again.
                if entry.attempts >= PasswordResetCode.MAX_ATTEMPTS:
                    entry.used_at = timezone.now()
                entry.save(update_fields=["attempts", "used_at"])

            if correct:
                entry.used_at = timezone.now()
                entry.save(update_fields=["used_at"])

                user.set_password(payload.validated_data["new_password"])
                user.save(update_fields=["password"])

                Token.objects.filter(user=user).delete()
                token = Token.objects.create(user=user)

        # Outside the transaction on purpose. Raising from inside it rolls the
        # block back, which would undo the attempt counter with the same
        # exception that was supposed to record it -- a ceiling that resets
        # itself on every wrong guess is not a ceiling.
        if not correct:
            raise invalid

        profile = profile_for(user)
        return Response(
            AuthResponseSerializer({"token": token.key, "user": profile}).data
        )


class RotateTokenView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=None, responses={200: AuthResponseSerializer})
    @transaction.atomic
    def post(self, request):
        if isinstance(request.auth, Token):
            request.auth.delete()
        else:
            Token.objects.filter(user=request.user).delete()
        token = Token.objects.create(user=request.user)
        profile = profile_for(request.user)
        response = AuthResponseSerializer({"token": token.key, "user": profile})
        return Response(response.data)


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=None, responses={204: None})
    def post(self, request):
        Token.objects.filter(user=request.user).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(RetrieveUpdateDestroyAPIView):
    serializer_class = RepbaseUserSerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):
        return profile_for(self.request.user)

    def perform_destroy(self, instance):
        # Deleting the auth user cascades through the Repbase profile and every
        # account-owned resource. It also invalidates all authentication tokens.
        self.request.user.delete()


class MePromptsView(APIView):
    """The questions the signed-in user has answered."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: ProfilePromptSerializer(many=True)})
    def get(self, request):
        profile = profile_for(request.user)
        return Response(
            ProfilePromptSerializer(profile.prompts.all(), many=True).data
        )

    @extend_schema(
        request=ProfilePromptsRequestSerializer,
        responses={200: ProfilePromptSerializer(many=True)},
        description=(
            "Replace every answered question with the set sent. Sending an "
            "empty list clears them."
        ),
    )
    def put(self, request):
        serializer = ProfilePromptsRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile = profile_for(request.user)

        with transaction.atomic():
            # Replaced wholesale rather than reconciled. Three rows is not
            # worth a diff, and a reconcile that goes wrong leaves an answer
            # on a profile whose owner has already stopped seeing it.
            profile.prompts.all().delete()
            ProfilePrompt.objects.bulk_create([
                ProfilePrompt(
                    owner=profile,
                    question=entry["question"],
                    answer=entry["answer"],
                    position=index + 1,
                )
                for index, entry in enumerate(serializer.validated_data["prompts"])
            ])

        return Response(
            ProfilePromptSerializer(profile.prompts.all(), many=True).data
        )


class MeSocialLinksView(APIView):
    """The outbound accounts on the signed-in user profile.

    Only ever this user own: the route carries no id, and the profile is read
    from the token. There is no shape of request that addresses somebody
    else links, which is a stronger guarantee than a permission check on one.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: ProfileSocialLinkSerializer(many=True)})
    def get(self, request):
        profile = profile_for(request.user)
        return Response(
            ProfileSocialLinkSerializer(profile.social_links.all(), many=True).data
        )

    @extend_schema(
        request=ProfileSocialLinksRequestSerializer,
        responses={200: ProfileSocialLinkSerializer(many=True)},
        description=(
            "Replace every social link with the set sent. Each entry may carry "
            "a full https URL or a bare handle; a handle is turned into that "
            "platform canonical URL. Sending an empty list removes them all."
        ),
    )
    def put(self, request):
        serializer = ProfileSocialLinksRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile = profile_for(request.user)

        with transaction.atomic():
            # Replaced wholesale rather than reconciled, as the prompts are.
            # Six rows is not worth a diff, and a reconcile that goes wrong
            # leaves a link on a profile whose owner has stopped seeing it.
            profile.social_links.all().delete()
            ProfileSocialLink.objects.bulk_create([
                ProfileSocialLink(
                    owner=profile,
                    platform=entry["platform"],
                    url=entry["url"],
                    handle=entry["handle"],
                    position=index + 1,
                )
                for index, entry in enumerate(
                    serializer.validated_data["social_links"]
                )
            ])

        return Response(
            ProfileSocialLinkSerializer(profile.social_links.all(), many=True).data
        )


class MeHighlightsView(APIView):
    """The lifts the signed-in user has chosen to show."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: ProfileHighlightSerializer(many=True)})
    def get(self, request):
        profile = profile_for(request.user)
        highlights = profile.highlights.all()
        return Response(
            ProfileHighlightSerializer(
                [highlight_payload(highlight) for highlight in highlights],
                many=True,
            ).data
        )

    @extend_schema(
        request=ProfileHighlightsRequestSerializer,
        responses={200: ProfileHighlightSerializer(many=True)},
        description=(
            "Replace the featured lifts with the exercises sent, in the order "
            "sent. An empty list clears them."
        ),
    )
    def put(self, request):
        serializer = ProfileHighlightsRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile = profile_for(request.user)
        wanted = serializer.validated_data["highlights"]

        with transaction.atomic():
            # Replaced wholesale rather than reconciled: three rows is not
            # worth a diff, and the screen behind this edits all of them.
            profile.highlights.all().delete()
            ProfileHighlight.objects.bulk_create([
                ProfileHighlight(
                    owner=profile,
                    lift=entry["lift"],
                    manual_weight_kg=entry.get("manual_weight_kg"),
                    manual_reps=entry.get("manual_reps"),
                    position=index + 1,
                )
                for index, entry in enumerate(wanted)
            ])

        highlights = profile.highlights.all()
        return Response(
            ProfileHighlightSerializer(
                [highlight_payload(highlight) for highlight in highlights],
                many=True,
            ).data
        )


class MePhotoView(APIView):
    """The signed-in user's profile photo."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=ProfilePhotoUploadSerializer,
        responses={200: RepbaseUserSerializer},
        description="Replace the profile photo. Any previous file is deleted.",
    )
    def put(self, request):
        serializer = ProfilePhotoUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile = serializer.save_to(profile_for(request.user))
        return Response(
            RepbaseUserSerializer(profile, context={"request": request}).data
        )

    @extend_schema(
        responses={200: RepbaseUserSerializer},
        description="Remove the profile photo.",
    )
    def delete(self, request):
        profile = profile_for(request.user)
        profile.profile_photo.delete(save=True)
        return Response(
            RepbaseUserSerializer(profile, context={"request": request}).data
        )


class RepbaseUserViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = (
        RepbaseUser.objects.select_related("user")
        .prefetch_related("prompts", "social_links")
        .order_by("-created_at")
    )
    serializer_class = PublicRepbaseUserSerializer
    permission_classes = [IsAuthenticated]

    @extend_schema(
        methods=["POST"],
        request=None,
        responses={201: None, 200: None, 409: None},
        description=(
            "Follow this user. Following again changes nothing and answers 200, "
            "so a double tap is not an error."
        ),
    )
    @extend_schema(
        methods=["DELETE"],
        request=None,
        responses={204: None},
        description="Stop following this user.",
    )
    @action(detail=True, methods=["post", "delete"])
    def follow(self, request, pk=None):
        """Follow or unfollow, from the one place the button lives.

        Unfollowing something you were not following is not an error. The button
        said "following", the user pressed it, and afterwards they are not; a
        404 would ask the client to handle a state it cannot see and does not
        care about.

        A block between the two people is refused out loud instead of being
        accepted and silently ignored. It does tell the blocked person a block
        exists, which is a real disclosure; the alternative is storing a follow
        that will never show them a post, so the app draws "following" forever
        over a relationship that does nothing. Hiding it properly would mean
        hiding the profile too, which increment 1 does not do.
        """
        target = self.get_object()
        follower = profile_for(request.user)

        if request.method == "DELETE":
            Follow.objects.filter(follower=follower, following=target).delete()
            return Response(status=status.HTTP_204_NO_CONTENT)

        if target == follower:
            # The model forbids it too; caught here so the client gets a 400
            # rather than an integrity error out of the database.
            raise ValidationError({"detail": "You cannot follow yourself."})
        if Block.objects.filter(
            Q(blocker=follower, blocked=target) | Q(blocker=target, blocked=follower)
        ).exists():
            return Response(
                {"detail": "A block stands between these two people."},
                status=status.HTTP_409_CONFLICT,
            )
        _, created = Follow.objects.get_or_create(follower=follower, following=target)
        return Response(status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    @extend_schema(
        responses={200: ProfileHighlightSerializer(many=True)},
        description=(
            "The lifts this user features, each with the best set they have "
            "logged of it. Its own request because every highlight costs a "
            "look through their set history."
        ),
    )
    @action(detail=True, methods=["get"], pagination_class=None)
    def highlights(self, request, pk=None):
        target = self.get_object()
        highlights = target.highlights.all()
        return Response(
            ProfileHighlightSerializer(
                [highlight_payload(highlight) for highlight in highlights],
                many=True,
            ).data
        )

    @extend_schema(responses=PublicRepbaseUserSerializer(many=True))
    @action(detail=True, methods=["get"])
    def followers(self, request, pk=None):
        """Who follows this user, most recent first.

        Paged, unlike the gym member list this otherwise resembles: a gym holds
        the people who train there and a well-followed account holds orders of
        magnitude more.

        Paged over the follow rows rather than over the profiles, because
        RepbaseUser has no Meta.ordering — paginating it would slice an
        unordered result and hand the same person to two different pages. The
        follows are ordered by when they were made, which is also the order this
        list wants.

        A block cannot leave a stale row here: making one deletes the follows
        both ways, and the follow action refuses to remake them.
        """
        target = self.get_object()
        follows = (
            Follow.objects.filter(following=target)
            .select_related("follower__user", "follower__gym")
            .prefetch_related("follower__disciplines")
        )
        page = self.paginate_queryset(follows)
        people = [follow.follower for follow in page]
        # get_serializer, not the bare serializer class: it carries the request
        # in context, without which every profile photo comes back as a relative
        # URL the app cannot load.
        return self.get_paginated_response(self.get_serializer(people, many=True).data)

    @extend_schema(responses=PublicRepbaseUserSerializer(many=True))
    @action(detail=True, methods=["get"])
    def following(self, request, pk=None):
        """Who this user follows, most recently followed first."""
        target = self.get_object()
        follows = (
            Follow.objects.filter(follower=target)
            .select_related("following__user", "following__gym")
            .prefetch_related("following__disciplines")
        )
        page = self.paginate_queryset(follows)
        people = [follow.following for follow in page]
        return self.get_paginated_response(self.get_serializer(people, many=True).data)


@extend_schema_view(
    list=extend_schema(
        parameters=[
            OpenApiParameter(
                name="name",
                type=str,
                location=OpenApiParameter.QUERY,
                description=(
                    "Return only exercises with this exact name, matched "
                    "without regard to case or surrounding space. Lets a "
                    "client resolve one exercise instead of reading every "
                    "page of the catalogue to find it."
                ),
            ),
        ]
    )
)
class ExerciseViewSet(viewsets.ModelViewSet):
    queryset = Exercise.objects.all()
    serializer_class = ExerciseSerializer
    permission_classes = [IsAuthenticated, IsCustomExerciseOwnerOrAdmin]

    def get_queryset(self):
        queryset = super().get_queryset().select_related("created_by")
        if not self.request.user.is_superuser:
            profile = profile_for(self.request.user)
            queryset = queryset.filter(
                Q(created_by__isnull=True) | Q(created_by=profile)
            )
        # Matched the way the app matches names: ignoring case and surrounding
        # space. Without this a client reads the whole catalogue to find one
        # row, on every workout save.
        name = self.request.query_params.get("name")
        if name:
            queryset = queryset.filter(name__iexact=name.strip())
        return queryset

    def perform_create(self, serializer):
        serializer.save(created_by=profile_for(self.request.user))


class OwnedViewSetMixin:
    owner_lookup = "owner"

    def owner_profile(self):
        return profile_for(self.request.user)

    def scope_to_owner(self, queryset):
        if self.request.user.is_superuser:
            return queryset
        return queryset.filter(**{self.owner_lookup: self.owner_profile()})


class WorkoutTemplateViewSet(OwnedViewSetMixin, viewsets.ModelViewSet):
    queryset = WorkoutTemplate.objects.prefetch_related(
        "workout_exercises__exercise"
    ).select_related("owner")
    serializer_class = WorkoutTemplateSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return self.scope_to_owner(super().get_queryset())

    def perform_create(self, serializer):
        serializer.save(owner=self.owner_profile())


class WorkoutExerciseViewSet(OwnedViewSetMixin, viewsets.ModelViewSet):
    queryset = WorkoutExercise.objects.select_related("workout", "exercise")
    serializer_class = WorkoutExerciseSerializer
    permission_classes = [IsAuthenticated]
    owner_lookup = "workout__owner"

    def get_queryset(self):
        return self.scope_to_owner(super().get_queryset())


class WorkoutScheduleViewSet(OwnedViewSetMixin, viewsets.ModelViewSet):
    queryset = WorkoutSchedule.objects.select_related("owner", "workout")
    serializer_class = WorkoutScheduleSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        queryset = self.scope_to_owner(super().get_queryset())
        scheduled_date = self.request.query_params.get("scheduled_date")
        return queryset.filter(scheduled_date=scheduled_date) if scheduled_date else queryset

    def perform_create(self, serializer):
        serializer.save(owner=self.owner_profile())

    @extend_schema(
        request=PlanWeekSerializer,
        responses={200: WorkoutScheduleSerializer(many=True)},
        description=(
            "Fill a week in from the user's weekly repeats and return "
            "everything scheduled in it. Safe to call on every load: it only "
            "adds days a repeat still owes, and it refuses to plan a week that "
            "has already finished, since a past week records what was trained."
        ),
    )
    @action(
        detail=False,
        methods=["post"],
        url_path="plan-week",
        pagination_class=None,
    )
    def plan_week(self, request):
        serializer = PlanWeekSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        owner = self.owner_profile()
        week_start = week_start_for(serializer.validated_data["start"])
        plan_recurring_week(owner, week_start, today_for(owner))
        schedules = (
            WorkoutSchedule.objects.filter(
                owner=owner,
                scheduled_date__gte=week_start,
                scheduled_date__lt=week_start + timedelta(days=7),
            )
            .select_related("workout")
            .order_by("scheduled_date", "id")
        )
        return Response(WorkoutScheduleSerializer(schedules, many=True).data)

    @extend_schema(
        request=PlannerSyncSerializer,
        responses=PlannerEntrySerializer(many=True),
        description=(
            "Put a planner task on every scheduled day in the range that has "
            "not had one yet, and return the tasks that were created. "
            "Idempotent: a day whose task already exists, or whose task the "
            "user deleted, is left alone."
        ),
    )
    @action(
        detail=False,
        methods=["post"],
        url_path="sync-planner",
        pagination_class=None,
    )
    def sync_planner(self, request):
        """Create the planner tasks a range of scheduled workouts still needs.

        The client used to do this: read the planner, work out which schedules
        had no task, and post one for each. It could not tell a day the user
        had cleared from a day never offered, so a deleted task came back on
        the next launch. Only the server can answer that, because only the
        server remembers having asked.
        """
        serializer = PlannerSyncSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        owner = self.owner_profile()
        start = serializer.validated_data["start"]
        end = serializer.validated_data["end"]

        created = []
        with transaction.atomic():
            pending = (
                WorkoutSchedule.objects.select_for_update()
                .filter(
                    owner=owner,
                    scheduled_date__gte=start,
                    scheduled_date__lte=end,
                    planner_synced_at__isnull=True,
                )
                .select_related("workout")
                .order_by("scheduled_date", "id")
            )
            for schedule in pending:
                # A task may already exist from before this endpoint, or from a
                # day the user made by hand. Adopt it rather than colliding
                # with the uniqueness rule.
                entry = PlannerEntry.objects.filter(
                    owner=owner,
                    workout=schedule.workout,
                    scheduled_date=schedule.scheduled_date,
                    kind=PlannerEntry.Kind.TASK,
                ).first()
                if entry is None:
                    entry = PlannerEntry.objects.create(
                        owner=owner,
                        kind=PlannerEntry.Kind.TASK,
                        title=schedule.workout.name,
                        category=PlannerCategory.WORKOUT,
                        scheduled_date=schedule.scheduled_date,
                        workout=schedule.workout,
                    )
                    created.append(entry)
                schedule.planner_synced_at = timezone.now()
                schedule.save(update_fields=["planner_synced_at", "updated_at"])

        return Response(PlannerEntrySerializer(created, many=True).data)


@extend_schema_view(
    list=extend_schema(
        parameters=[
            OpenApiParameter(
                name="start",
                type=OpenApiTypes.DATE,
                location=OpenApiParameter.QUERY,
                description="Return only meals on or after this date.",
            ),
            OpenApiParameter(
                name="end",
                type=OpenApiTypes.DATE,
                location=OpenApiParameter.QUERY,
                description="Return only meals on or before this date.",
            ),
        ]
    )
)
class FoodMealViewSet(OwnedViewSetMixin, viewsets.ModelViewSet):
    """Meals, with their foods nested on read.

    A day is drawn from one request rather than one per meal, which is why the
    entries come back inside the meal instead of behind another call.
    """

    queryset = FoodMeal.objects.prefetch_related("entries").select_related("owner")
    serializer_class = FoodMealSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        queryset = self.scope_to_owner(super().get_queryset())
        start = self.request.query_params.get("start")
        end = self.request.query_params.get("end")
        if start:
            queryset = queryset.filter(date__gte=start)
        if end:
            queryset = queryset.filter(date__lte=end)
        return queryset

    def perform_create(self, serializer):
        owner = self.owner_profile()
        # Numbered after whatever is already on that day, so a new meal lands
        # at the end rather than colliding with an existing position.
        date = serializer.validated_data.get("date")
        used = FoodMeal.objects.filter(owner=owner, date=date).count()
        serializer.save(owner=owner, position=used + 1)

    @extend_schema(responses={200: RecentFoodSerializer(many=True)})
    @action(detail=False, methods=["get"], url_path="recent-foods", pagination_class=None)
    def recent_foods(self, request):
        """Foods this person has logged before, most recent first.

        One row per distinct name: the picker offers a food to reuse, and the
        same yoghurt logged nine times is one choice, not nine.
        """
        entries = (
            FoodEntry.objects
            .filter(meal__owner=self.owner_profile())
            .order_by("-created_at")
        )
        seen = set()
        recent = []
        for entry in entries:
            key = entry.name.casefold()
            if key in seen:
                continue
            seen.add(key)
            recent.append(entry)
            # Stated rather than silent: the picker searches this list, so its
            # length is part of the contract.
            if len(recent) >= 100:
                break
        return Response(RecentFoodSerializer(recent, many=True).data)

    @extend_schema(
        request=EnsureFoodDaySerializer,
        responses={200: FoodMealSerializer(many=True)},
        description=(
            "Open a day and get its meals, creating the day's empty meal slots "
            "the first time. Safe to call every time a day is shown: it only "
            "adds slots a day is short of, so opening the same day twice does "
            "not double them."
        ),
    )
    @action(detail=False, methods=["post"], url_path="ensure-day", pagination_class=None)
    def ensure_day(self, request):
        """The meals on a day, with the empty ones a day starts with.

        A POST rather than something the day's GET does on the side: this
        writes rows, and a read that quietly created them would fill the
        database with days somebody scrolled past.

        The slots are laid down only on a day that has none. A day someone has
        already edited keeps exactly what they left on it - deleting a meal and
        coming back tomorrow must not quietly restore it, which is what topping
        every day up to four would do.
        """
        serializer = EnsureFoodDaySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        owner = self.owner_profile()
        date = serializer.validated_data["date"]
        meals = food_meals_for_day(owner, date)
        if not meals:
            meals = food_meals_for_day(owner, date, at_least=DEFAULT_FOOD_MEAL_COUNT)
        return Response(FoodMealSerializer(meals, many=True).data)


class FoodEntryViewSet(OwnedViewSetMixin, viewsets.ModelViewSet):
    """Foods inside meals. Ownership is the meal's owner, one step away."""

    queryset = FoodEntry.objects.select_related("meal", "meal__owner")
    serializer_class = FoodEntrySerializer
    permission_classes = [IsAuthenticated]
    owner_lookup = "meal__owner"

    def get_queryset(self):
        queryset = self.scope_to_owner(super().get_queryset())
        meal = self.request.query_params.get("meal")
        return queryset.filter(meal_id=meal) if meal else queryset

    def perform_create(self, serializer):
        meal = serializer.validated_data["meal"]
        used = meal.entries.count()
        serializer.save(position=used + 1)


class NutritionGoalView(APIView):
    """The signed-in user's daily targets. A singleton, so no list or id."""

    permission_classes = [IsAuthenticated]

    def _goal(self, request):
        # Created on first read with the documented defaults, so the app never
        # has to handle "no goals yet" as a separate state.
        goal, _ = NutritionGoal.objects.get_or_create(
            owner=profile_for(request.user)
        )
        return goal

    @extend_schema(responses={200: NutritionGoalSerializer})
    def get(self, request):
        return Response(NutritionGoalSerializer(self._goal(request)).data)

    @extend_schema(request=NutritionGoalSerializer, responses={200: NutritionGoalSerializer})
    def patch(self, request):
        serializer = NutritionGoalSerializer(
            self._goal(request), data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class SavedFoodMealViewSet(OwnedViewSetMixin, viewsets.ModelViewSet):
    """Meals kept to reuse."""

    queryset = SavedFoodMeal.objects.prefetch_related("ingredients").select_related("owner")
    serializer_class = SavedFoodMealSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return self.scope_to_owner(super().get_queryset())

    def perform_create(self, serializer):
        serializer.save(owner=self.owner_profile())

    @extend_schema(
        request=ApplySavedMealSerializer,
        responses={200: FoodMealSerializer(many=True)},
        description=(
            "Copy this saved meal's ingredients into the same numbered meal on "
            "each of several days, creating any meal that is not there yet. The "
            "ingredients are copied, not linked, so editing one afterwards does "
            "not change the recipe it came from, and deleting the recipe does "
            "not empty the days it was applied to."
        ),
    )
    @action(detail=True, methods=["post"], pagination_class=None)
    def apply(self, request, pk=None):
        """Copy a saved meal onto every day named, in one transaction.

        All the days or none of them. Half of a week's meal prep applied, with
        no way to tell which half, is worse than a refusal the user can repeat.

        Repeated dates are collapsed. The same day sent twice reads as a client
        retrying rather than as a request for two helpings, and the latter is
        what an unfiltered loop would silently produce.
        """
        saved = self.get_object()
        serializer = ApplySavedMealSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        owner = self.owner_profile()
        position = serializer.validated_data["position"]
        ingredients = list(saved.ingredients.all())
        dates = list(dict.fromkeys(serializer.validated_data["dates"]))

        touched = []
        with transaction.atomic():
            for date in dates:
                meal = food_meals_for_day(owner, date, at_least=position)[position - 1]
                used = meal.entries.count()
                FoodEntry.objects.bulk_create([
                    FoodEntry(
                        meal=meal,
                        name=ingredient.name,
                        servings=ingredient.servings,
                        calories=ingredient.calories,
                        protein_grams=ingredient.protein_grams,
                        carbohydrate_grams=ingredient.carbohydrate_grams,
                        fat_grams=ingredient.fat_grams,
                        position=used + offset + 1,
                    )
                    for offset, ingredient in enumerate(ingredients)
                ])
                touched.append(meal.pk)

        meals = (
            FoodMeal.objects.filter(pk__in=touched)
            .prefetch_related("entries")
            .order_by("date", "position", "id")
        )
        return Response(FoodMealSerializer(meals, many=True).data)


@extend_schema_view(
    list=extend_schema(
        parameters=[
            OpenApiParameter(
                name="search",
                type=str,
                location=OpenApiParameter.QUERY,
                description=(
                    "Match gyms whose name or city contains this, ignoring "
                    "case, punctuation and extra spaces."
                ),
            )
        ]
    )
)
class GymViewSet(viewsets.ModelViewSet):
    """Gyms, shared by everyone who trains at them.

    Not owned by anyone: a gym one user adds is exactly the gym the next user
    should be able to join, which is the whole point of the list. Anyone signed
    in may add one; nobody may edit or delete somebody else's.
    """

    queryset = Gym.objects.all()
    serializer_class = GymSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        # Not named member_count: that is a property on the model, and an
        # annotation of the same name is assigned onto the instance, which
        # a property without a setter refuses.
        queryset = super().get_queryset().annotate(members_here=Count("members"))
        search = self.request.query_params.get("search")
        if search:
            # Matched against the stored keys so "golds" finds "Gold\'s Gym".
            key = normalize_gym_text(search)
            if key:
                queryset = queryset.filter(
                    Q(normalized_name__contains=key) | Q(normalized_city__contains=key)
                )
        return queryset

    def perform_create(self, serializer):
        serializer.save(created_by=profile_for(self.request.user))

    def _assert_owner(self, instance):
        owner = profile_for(self.request.user)
        if instance.created_by_id not in (None, owner.id) and not self.request.user.is_superuser:
            raise PermissionDenied("Only the person who added this gym can change it.")

    def perform_update(self, serializer):
        self._assert_owner(serializer.instance)
        serializer.save()

    def perform_destroy(self, instance):
        self._assert_owner(instance)
        if instance.members.exists():
            raise ValidationError(
                "People train here, so this gym cannot be removed."
            )
        instance.delete()

    @extend_schema(responses={200: PublicRepbaseUserSerializer(many=True)})
    @action(detail=True, methods=["get"], pagination_class=None)
    def members(self, request, pk=None):
        """Who trains here. This is what makes a shared gym worth having."""
        gym = self.get_object()
        people = gym.members.select_related("user").order_by("user__username")
        return Response(PublicRepbaseUserSerializer(people, many=True).data)


@extend_schema_view(
    list=extend_schema(
        parameters=[
            OpenApiParameter(
                name="start",
                type=OpenApiTypes.DATE,
                location=OpenApiParameter.QUERY,
                description="Return only entries on or after this date.",
            ),
            OpenApiParameter(
                name="end",
                type=OpenApiTypes.DATE,
                location=OpenApiParameter.QUERY,
                description="Return only entries on or before this date.",
            ),
            OpenApiParameter(
                name="category",
                type=str,
                location=OpenApiParameter.QUERY,
                enum=[choice[0] for choice in PlannerCategory.choices],
                description="Return only entries in this category.",
            ),
            OpenApiParameter(
                name="kind",
                type=str,
                location=OpenApiParameter.QUERY,
                enum=[choice[0] for choice in PlannerEntry.Kind.choices],
                description="Return only tasks, or only events.",
            ),
            OpenApiParameter(
                name="is_complete",
                type=bool,
                location=OpenApiParameter.QUERY,
                description=(
                    "Return only finished tasks, or only unfinished ones. "
                    "Events are never complete, so this excludes them when "
                    "true."
                ),
            ),
        ]
    )
)
class PlannerEntryViewSet(OwnedViewSetMixin, viewsets.ModelViewSet):
    """Tasks and events on the planner.

    The date range is a filter rather than a required window so the same
    endpoint draws a month of calendar marks, a week strip, and one day's list.
    """

    queryset = PlannerEntry.objects.select_related("owner", "workout")
    serializer_class = PlannerEntrySerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        queryset = self.scope_to_owner(super().get_queryset())
        start = self.request.query_params.get("start")
        end = self.request.query_params.get("end")
        category = self.request.query_params.get("category")
        kind = self.request.query_params.get("kind")
        is_complete = self.request.query_params.get("is_complete")
        if start:
            queryset = queryset.filter(scheduled_date__gte=start)
        if end:
            queryset = queryset.filter(scheduled_date__lte=end)
        if category:
            queryset = queryset.filter(category=category)
        if kind:
            queryset = queryset.filter(kind=kind)
        if is_complete is not None:
            # Completion is stored as the time it happened, so "finished" is
            # simply having one.
            wants_complete = is_complete.lower() in ("true", "1")
            queryset = queryset.filter(completed_at__isnull=not wants_complete)
        return queryset

    def perform_create(self, serializer):
        serializer.save(owner=self.owner_profile())


class WorkoutRecurrenceViewSet(
    OwnedViewSetMixin,
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """Workouts that repeat weekly.

    There is deliberately no update endpoint. Changing a repeat means ending
    the rule in force at this week and starting a new one, which is delete
    followed by create; expressing it that way keeps finished weeks resolving
    through the plan that was actually in place then.

    Listing returns only rules still in force. A closed rule is history: it
    explains what a past week held, and no screen lists it.
    """

    queryset = WorkoutRecurrence.objects.select_related("owner", "workout")
    serializer_class = WorkoutRecurrenceSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return self.scope_to_owner(super().get_queryset()).filter(
            effective_until__isnull=True
        )

    def perform_create(self, serializer):
        owner = self.owner_profile()
        today = today_for(owner)
        recurrence = serializer.save(
            owner=owner,
            effective_from=week_start_for(today),
        )
        # Claim the current week straight away. The workout is already on the
        # calendar for the day the user turned this on, so nothing new appears
        # now; it stops the rule re-adding it if they remove it this week.
        plan_recurring_week(recurrence.owner, week_start_for(today), today)

    def perform_destroy(self, instance):
        current_week = week_start_for(today_for(instance.owner))
        # Clear only the weeks this rule had run ahead and planned. The current
        # week is left exactly as it is: those days are on the calendar in
        # front of the user, and one of them may already have been trained.
        # Days the user put there by hand are untouched, which is what the
        # source_recurrence link is for.
        WorkoutSchedule.objects.filter(
            source_recurrence=instance,
            scheduled_date__gte=current_week + timedelta(days=7),
        ).delete()
        if instance.materialized_through is None:
            # Never reached a calendar, so no week depends on it.
            instance.delete()
        else:
            instance.effective_until = current_week
            instance.save(update_fields=["effective_until", "updated_at"])


@extend_schema_view(
    list=extend_schema(
        parameters=[
            OpenApiParameter(
                name="since",
                type=OpenApiTypes.DATE,
                location=OpenApiParameter.QUERY,
                description=(
                    "Only sessions on or after this date, so a client can ask "
                    "for a week without paging its whole history."
                ),
            ),
            OpenApiParameter(
                name="status",
                type=str,
                location=OpenApiParameter.QUERY,
                enum=[choice[0] for choice in WorkoutSession.Status.choices],
                description="Return only sessions in this state.",
            ),
            OpenApiParameter(
                name="workout",
                type=int,
                location=OpenApiParameter.QUERY,
                description="Return only sessions for this workout template.",
            ),
            OpenApiParameter(
                name="workout_name",
                type=str,
                location=OpenApiParameter.QUERY,
                description=(
                    "Return only sessions whose workout has this name, "
                    "matched without regard to case. Groups a workout's "
                    "history by what it is called rather than by record."
                ),
            ),
        ]
    )
)
class WorkoutSessionViewSet(OwnedViewSetMixin, viewsets.ModelViewSet):
    # Every session in a list reports distance, pace, climb and splits, and
    # each of those walks its route. Without prefetching, asking for a history
    # of runs issues a query per session per figure.
    queryset = (
        WorkoutSession.objects.select_related("repbase_user", "workout")
        .prefetch_related("route_points")
        # Counted in the same query rather than per session. A history of a
        # hundred sessions would otherwise be a hundred extra counts.
        .annotate(
            logged_set_total=Count(
                "session_exercises__sets",
                filter=Q(session_exercises__sets__weight_kg__isnull=False)
                | Q(session_exercises__sets__reps__isnull=False),
                distinct=True,
            )
        )
    )
    serializer_class = WorkoutSessionSerializer
    permission_classes = [IsAuthenticated]
    owner_lookup = "repbase_user"

    def get_queryset(self):
        queryset = self.scope_to_owner(super().get_queryset())

        # Bounded reads. Without this the only way to ask for the
        # sessions of one week was to page every session ever
        # recorded, which is what the dashboard used to do.
        since = self.request.query_params.get("since")
        if since:
            parsed = parse_date(since)
            if parsed is not None:
                queryset = queryset.filter(
                    Q(ended_at__date__gte=parsed)
                    | Q(ended_at__isnull=True, started_at__date__gte=parsed)
                )
        session_status = self.request.query_params.get("status")
        workout = self.request.query_params.get("workout")
        workout_name = self.request.query_params.get("workout_name")
        if session_status:
            queryset = queryset.filter(status=session_status)
        if workout:
            queryset = queryset.filter(workout_id=workout)
        if workout_name:
            queryset = queryset.filter(workout__name__iexact=workout_name)
        return queryset

    @transaction.atomic
    def perform_create(self, serializer):
        session = serializer.save(repbase_user=self.owner_profile())
        if session.workout_id:
            planned = session.workout.workout_exercises.select_related("exercise")
            SessionExercise.objects.bulk_create(
                [
                    SessionExercise(
                        session=session,
                        exercise=item.exercise,
                        order=item.order,
                        notes=item.notes,
                    )
                    for item in planned
                ]
            )

    @extend_schema(request=None, responses=WorkoutSessionSerializer)
    @action(detail=True, methods=["post"])
    def start(self, request, pk=None):
        with transaction.atomic():
            session = get_object_or_404(
                self.get_queryset().select_for_update(),
                pk=pk,
            )
            if session.status != WorkoutSession.Status.PLANNED:
                return Response(
                    {"detail": "Only a planned session can be started."},
                    status=status.HTTP_409_CONFLICT,
                )
            session.status = WorkoutSession.Status.ACTIVE
            session.started_at = timezone.now()
            session.save(update_fields=["status", "started_at", "updated_at"])
        return Response(self.get_serializer(session).data)

    @extend_schema(request=None, responses=WorkoutSessionSerializer)
    @action(detail=True, methods=["post"])
    def end(self, request, pk=None):
        with transaction.atomic():
            session = get_object_or_404(
                self.get_queryset().select_for_update(),
                pk=pk,
            )
            if session.status != WorkoutSession.Status.ACTIVE:
                return Response(
                    {"detail": "Only an active session can be ended."},
                    status=status.HTTP_409_CONFLICT,
                )
            session.status = WorkoutSession.Status.COMPLETED
            session.ended_at = timezone.now()
            session.full_clean()
            session.save(update_fields=["status", "ended_at", "updated_at"])
        return Response(self.get_serializer(session).data)

    @extend_schema(
        request=SessionCardioSerializer,
        responses=WorkoutSessionSerializer,
    )
    @action(detail=True, methods=["post"])
    def cardio(self, request, pk=None):
        """Record the cardio finisher performed after this session.

        Written onto the session rather than creating a second one, because a
        workout and the cardio that followed it are one training session.
        Allowed after the session has ended, since the finisher happens after
        the exercises are done.
        """
        session = get_object_or_404(self.get_queryset(), pk=pk)
        payload = SessionCardioSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        session.cardio_machine = payload.validated_data["machine"]
        session.cardio_seconds = payload.validated_data["seconds"]
        session.cardio_distance_km = payload.validated_data.get("distance_km")
        session.save(
            update_fields=[
                "cardio_machine",
                "cardio_seconds",
                "cardio_distance_km",
                "updated_at",
            ]
        )
        return Response(self.get_serializer(session).data)

    @extend_schema(request=None, responses=PersonalRecordSerializer(many=True))
    # A session sets a handful of records at most, so the whole list is
    # returned at once. Without disabling the paginator the schema would
    # promise a paged envelope the response does not send.
    @action(detail=True, methods=["get"], pagination_class=None)
    def records(self, request, pk=None):
        """Bests set during this session that beat everything logged before.

        Kept off the session list, where it would run a history query per
        exercise for every session returned.
        """
        session = get_object_or_404(self.get_queryset(), pk=pk)
        return Response(
            PersonalRecordSerializer(
                session.personal_records(),
                many=True,
            ).data
        )

    @extend_schema(
        methods=["GET"],
        request=None,
        responses=SessionRoutePointSerializer(many=True),
    )
    @extend_schema(
        methods=["POST"],
        request=SessionRouteUploadSerializer,
        responses=WorkoutSessionSerializer,
    )
    @action(detail=True, methods=["get", "post"])
    def route(self, request, pk=None):
        """Read or append the GPS track recorded during a session.

        Uploaded points are stored raw; distance and pace are derived from them
        on the server so every client agrees on the result.
        """
        session = get_object_or_404(self.get_queryset(), pk=pk)

        if request.method == "GET":
            return Response(
                SessionRoutePointSerializer(
                    session.route_points.all(),
                    many=True,
                ).data
            )

        upload = SessionRouteUploadSerializer(data=request.data)
        upload.is_valid(raise_exception=True)

        with transaction.atomic():
            SessionRoutePoint.objects.bulk_create(
                [
                    SessionRoutePoint(session=session, **point)
                    for point in upload.validated_data["points"]
                ]
            )

        session.refresh_from_db()
        # Store what the track came to. route_distance_km recomputes it from
        # the points every time it is read, which is fine for one session and
        # impossible to sum in SQL across every session a shoe was worn for.
        distance = session.route_distance_km
        if distance is not None:
            session.recorded_distance_km = round(distance, 3)
            session.save(update_fields=["recorded_distance_km", "updated_at"])

        # 200, matching the documented contract and the start/end actions.
        return Response(self.get_serializer(session).data)


@extend_schema_view(
    list=extend_schema(
        parameters=[
            OpenApiParameter(
                name="session",
                type=int,
                location=OpenApiParameter.QUERY,
                description=(
                    "Return only the exercises belonging to this session. "
                    "The filter already worked; undeclared, a client had to "
                    "read every session-exercise row it owns in order to "
                    "open one session."
                ),
            ),
        ]
    )
)
class SessionExerciseViewSet(OwnedViewSetMixin, viewsets.ModelViewSet):
    queryset = SessionExercise.objects.select_related("session", "exercise")
    serializer_class = SessionExerciseSerializer
    permission_classes = [IsAuthenticated]
    owner_lookup = "session__repbase_user"

    def get_queryset(self):
        queryset = self.scope_to_owner(super().get_queryset())
        session = self.request.query_params.get("session")
        return queryset.filter(session_id=session) if session else queryset


@extend_schema_view(
    list=extend_schema(
        parameters=[
            OpenApiParameter(
                name="session_exercise",
                type=int,
                location=OpenApiParameter.QUERY,
                description="Return only the sets logged against this session exercise.",
            ),
            OpenApiParameter(
                name="session",
                type=int,
                location=OpenApiParameter.QUERY,
                description=(
                    "Return every set logged in this session, across all of "
                    "its exercises. Lets a client read one whole session in a "
                    "single request instead of one request per exercise."
                ),
            ),
        ]
    )
)
class SetEntryViewSet(OwnedViewSetMixin, viewsets.ModelViewSet):
    queryset = SetEntry.objects.select_related(
        "session_exercise__session__repbase_user"
    )
    serializer_class = SetEntrySerializer
    permission_classes = [IsAuthenticated]
    owner_lookup = "session_exercise__session__repbase_user"

    def get_queryset(self):
        queryset = self.scope_to_owner(super().get_queryset())
        session_exercise = self.request.query_params.get("session_exercise")
        if session_exercise:
            queryset = queryset.filter(session_exercise_id=session_exercise)
        # Whole session at once. Showing what was lifted last time needs every
        # set of the previous session, and asking exercise by exercise costs a
        # request per exercise for data one query already has.
        session = self.request.query_params.get("session")
        if session:
            queryset = queryset.filter(session_exercise__session_id=session)
        return queryset


class BodyWeightEntryViewSet(OwnedViewSetMixin, viewsets.ModelViewSet):
    queryset = BodyWeightEntry.objects.select_related("owner")
    serializer_class = BodyWeightEntrySerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return self.scope_to_owner(super().get_queryset())

    def perform_create(self, serializer):
        serializer.save(owner=self.owner_profile())


class ExerciseProgressView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="workout_name",
                type=str,
                location=OpenApiParameter.QUERY,
                description=(
                    "Only count sets performed in workouts with this name, "
                    "matched without regard to case. Keeps one workout's "
                    "history of a lift separate from another's."
                ),
            )
        ],
        responses=ExerciseProgressPointSerializer(many=True),
    )
    def get(self, request, exercise_id):
        profile = profile_for(request.user)
        entries = SetEntry.objects.filter(
            session_exercise__session__repbase_user=profile,
            session_exercise__exercise_id=exercise_id,
            completed_at__isnull=False,
            weight_kg__isnull=False,
            reps__isnull=False,
        ).select_related("session_exercise").order_by("completed_at")

        workout_name = request.query_params.get("workout_name")
        if workout_name:
            entries = entries.filter(
                session_exercise__session__workout__name__iexact=workout_name
            )
        points = [
            {
                "completed_at": entry.completed_at,
                "weight_kg": entry.weight_kg,
                "reps": entry.reps,
                "volume_kg": entry.weight_kg * Decimal(entry.reps),
                "session": entry.session_exercise.session_id,
            }
            for entry in entries
        ]
        return Response(ExerciseProgressPointSerializer(points, many=True).data)


class FeedCursorPagination(pagination.CursorPagination):
    """Keyset paging for a list that is being written to while it is read.

    Numbered pages shift under the reader: someone they follow posts, everything
    slides down one, and page two returns a post page one already showed while
    hiding the one it displaced. A cursor names a position in the ordering
    instead of a distance from the top, so a page boundary means the same thing
    after that write as before it.

    A page size above the maximum is refused, not quietly reduced. A client that
    asked for 200 and got 50 cannot tell that from a feed with 50 posts left in
    it, and carries on paging in strides the server is not using.
    """

    #: A feed is read by scrolling and the cards are heavy — an author, a
    #: snapshot and its rows each. Twenty is a screen or two of work per request
    #: rather than the fifty the paged endpoints elsewhere in this API return.
    page_size = 20
    max_page_size = 50
    page_size_query_param = "page_size"
    #: Matches Post.Meta.ordering and the index the feed walks. The id is what
    #: makes the order total: two posts written in the same instant would
    #: otherwise sit in whatever order the database felt like, and a boundary
    #: drawn between them would repeat one page's last row or skip it.
    ordering = ("-created_at", "-id")

    def get_page_size(self, request):
        # Replaced rather than called and then checked: DRF runs the requested
        # size through a positive-int helper with a cutoff, and by the time that
        # returns, the number the client actually asked for is gone.
        raw = request.query_params.get(self.page_size_query_param)
        if raw is None:
            return self.page_size
        try:
            size = int(raw)
        except (TypeError, ValueError):
            raise ValidationError({"page_size": "Page size must be a whole number."})
        if size < 1:
            raise ValidationError({"page_size": "Page size must be at least 1."})
        if size > self.max_page_size:
            raise ValidationError(
                {"page_size": f"Page size may not be more than {self.max_page_size}."}
            )
        return size


def annotate_social_counts(viewer, queryset):
    """Add the like, comment and repost figures a card shows.

    Subqueries rather than `Count` with joins. Three counts pulled through
    three multi-valued relations in one query multiply out: a post with four
    likes and three comments reports twelve of each. The usual repair is
    `distinct=True` on every aggregate, which is a full pass per count; a
    correlated subquery is one indexed lookup and cannot inflate a row.
    """
    def total(model, field):
        return Coalesce(
            Subquery(
                model.objects.filter(**{field: OuterRef("pk")})
                .order_by()
                .values(field)
                .annotate(n=Count("pk"))
                .values("n")[:1]
            ),
            Value(0),
        )

    return queryset.annotate(
        like_total=total(PostLike, "post"),
        comment_total=total(PostComment, "post"),
        repost_total=total(Post, "repost_of"),
        viewer_liked=Exists(
            PostLike.objects.filter(post=OuterRef("pk"), user=viewer)
        ),
        viewer_reposted=Exists(
            Post.objects.filter(repost_of=OuterRef("pk"), author=viewer)
        ),
    )


def visible_posts_for(viewer, queryset):
    """Narrow a post queryset to what one person is allowed to see.

    Both tests are correlated `Exists` subqueries rather than joins onto the
    follow and block tables. An OR that reaches through a multi-valued relation
    returns a row per matching related row, and two people who have blocked each
    other have two block rows, so a join would hand the same post to the
    paginator twice. The usual repair is `.distinct()`, which corrupts a count
    on one side and a cursor walk on the other. A subquery adds a column, never
    a row.

    `author=viewer` sits outside the block clause deliberately. Nobody can block
    themself — the model forbids it — and an author's own private post has to
    stay visible to them, which is the single case where visibility is not
    consulted at all.

    There is no superuser bypass here, unlike `scope_to_owner`. `private` is a
    user's word for author-only, and an account with a staff flag is still a
    reader.
    """
    follows_author = Follow.objects.filter(
        follower=viewer,
        following=OuterRef("author"),
    )
    blocked_either_way = Block.objects.filter(
        Q(blocker=viewer, blocked=OuterRef("author"))
        | Q(blocker=OuterRef("author"), blocked=viewer)
    )
    return queryset.annotate(
        viewer_follows_author=Exists(follows_author),
        viewer_is_blocked=Exists(blocked_either_way),
    ).exclude(
        # Taken down by a moderator. Excluded before the visibility rules
        # rather than inside them, so the author's own unconditional access
        # below cannot put it back.
        is_hidden=True
    ).filter(
        Q(author=viewer)
        | (
            Q(viewer_is_blocked=False)
            & (
                Q(visibility=Post.Visibility.PUBLIC)
                | Q(visibility=Post.Visibility.FOLLOWERS, viewer_follows_author=True)
            )
        )
    )


def posts_for_cards():
    """Posts with everything a rendered card touches already loaded.

    A card prints the author's name, photo, gym and disciplines, then the
    snapshot and its rows. Left alone that is six queries a post, so a page of
    fifty is three hundred; the feed is the one endpoint where that is not a
    slow page but an unusable one. Shared by the posts list and the feed so the
    two cannot drift into loading different things.

    The source links are not selected. They are shown to the author as bare ids,
    which are already columns on the post row.
    """
    return Post.objects.select_related(
        "author__user",
        "author__gym",
        "workout",
        "meal",
        "planner",
    ).prefetch_related(
        "author__disciplines",
        "workout__exercises",
        "meal__entries",
    )


def source_for(author, kind, source_id):
    """The workout, meal or planner entry a post is being made from.

    Loaded with everything the snapshot reads, because the builder walks the
    sets, the foods and the route once each and a lazy relation in there is a
    query per exercise.

    Someone else's id is refused exactly as a missing one is, naming `source_id`
    and saying nothing about whether the row exists. The house answer for a row
    that is not yours is 404 — `scope_to_owner` drops it from the queryset and
    DRF reports what it cannot find — and the reasoning behind that answer holds
    here: a refusal must not become an oracle for other people's training. But
    404 is a statement about the thing in the URL, and the thing in this URL is
    the collection of posts, which is very much there. The id arrived in a
    field, so the refusal belongs to that field, which is what every other
    cross-owner check in this codebase does. 403 is wrong under either reading:
    it answers "that row exists, and it is not yours".
    """
    if kind == Post.Kind.WORKOUT:
        source = (
            WorkoutSession.objects.filter(pk=source_id, repbase_user=author)
            .select_related("workout")
            .prefetch_related(
                "session_exercises__exercise",
                "session_exercises__sets",
                "route_points",
            )
            .first()
        )
    elif kind == Post.Kind.MEAL:
        source = (
            FoodMeal.objects.filter(pk=source_id, owner=author)
            .prefetch_related("entries")
            .first()
        )
    else:
        # kind reached here through a closed ChoiceField, so the last branch is
        # the planner and there is no fourth case to fall through to.
        source = PlannerEntry.objects.filter(pk=source_id, owner=author).first()

    if source is None:
        raise ValidationError(
            {"source_id": "No workout, meal or planner entry of yours with that id."}
        )

    # Two sources that load perfectly well and make an unreadable card. Refusing
    # them at post time costs one comparison; a feed full of blank cards costs
    # the feature.
    if kind == Post.Kind.WORKOUT and source.status != WorkoutSession.Status.COMPLETED:
        raise ValidationError({"source_id": "Only a finished session can be posted."})
    # Asked of the prefetched rows rather than with .exists(), which would go
    # back to the database and throw the prefetch away.
    if kind == Post.Kind.MEAL and not source.entries.all():
        raise ValidationError({"source_id": "A meal with no foods in it has nothing to show."})
    return source


def as_decimal(value):
    """A model property's float as a Decimal that means what it printed.

    The route figures come back from `round(total, 3)` as floats, and
    Decimal(0.1) is not one tenth. Going through the printed form is what keeps
    the stored number the same number the session reported.
    """
    return None if value is None else Decimal(str(value))


def whole_seconds(value):
    """Seconds a card can print, from a session property that reports fractions."""
    return None if value is None else int(round(value))


def snapshot_workout(post, session, shows_weights=True):
    """Copy a finished session onto a post.

    The route figures are read once here and stored as numbers. Each of those
    properties walks the whole point list, so a feed page that computed them at
    render time would walk fifty lists to draw one screen. The points themselves
    are never copied: the first and last fix of a run from home is a home
    address.
    """
    has_cardio = session.cardio_machine is not None and session.cardio_seconds is not None
    workout = PostWorkout.objects.create(
        post=post,
        # An ad-hoc session has no template and so no name. "Workout" beside the
        # date the card already prints still reads as a sentence, and a non-null
        # column means every card has something to put at the top.
        title=session.workout.name if session.workout_id else "Workout",
        workout_type=session.workout.workout_type if session.workout_id else None,
        performed_at=session.ended_at or session.started_at or session.created_at,
        duration_seconds=whole_seconds(session.duration_seconds),
        # The finisher goes over whole or not at all: a machine with no time, or
        # a time with no machine, gives a card nothing to draw.
        cardio_machine=session.cardio_machine if has_cardio else None,
        cardio_seconds=session.cardio_seconds if has_cardio else None,
        cardio_distance_km=session.cardio_distance_km if has_cardio else None,
        route_distance_km=as_decimal(session.route_distance_km),
        pace_seconds_per_km=whole_seconds(session.pace_seconds_per_km),
        elevation_gain_m=as_decimal(session.elevation_gain_m),
    )

    rows = []
    for session_exercise in session.session_exercises.all():
        # A set counts when it recorded a weight, a rep count or a distance.
        # The records and progress queries instead count sets carrying a
        # completed_at, which is the app's tick; whether the app writes that for
        # every set a user ticks is not visible from the backend, and a set with
        # numbers in it was plainly performed.
        sets = [
            entry
            for entry in session_exercise.sets.all()
            if entry.weight_kg is not None
            or entry.reps is not None
            or entry.distance_km is not None
        ]
        if not sets:
            # Planned and skipped. A row reading "Bench Press, 0 sets" is not
            # what the person did, and the post is a record of what they did.
            continue

        weighted = [entry for entry in sets if entry.weight_kg is not None]
        top = max(weighted, key=lambda entry: (entry.weight_kg, entry.reps or 0), default=None)
        reps = [entry.reps for entry in sets if entry.reps is not None]
        volumes = [
            entry.weight_kg * Decimal(entry.reps)
            for entry in sets
            if entry.weight_kg is not None and entry.reps is not None
        ]
        distances = [entry.distance_km for entry in sets if entry.distance_km is not None]
        rows.append(
            PostWorkoutExercise(
                post_workout=workout,
                name=session_exercise.exercise.name,
                order=session_exercise.order,
                set_count=len(sets),
                # None rather than zero throughout: a bodyweight session and an
                # unlogged one must not both read "0 kg".
                # Held back at the author's word, and so never stored.
                # Reps stay: "4 x 8" is what was done, and it is the load
                # people are shy about, not the count.
                top_set_weight_kg=(
                    top.weight_kg if shows_weights and top is not None else None
                ),
                top_set_reps=top.reps if top is not None else None,
                total_reps=sum(reps) if reps else None,
                volume_kg=sum(volumes) if shows_weights and volumes else None,
                distance_km=sum(distances) if distances else None,
            )
        )
    PostWorkoutExercise.objects.bulk_create(rows)


def snapshot_meal(post, meal):
    """Copy a meal and its foods onto a post.

    Per-serving figures and the serving count are copied separately, exactly as
    FoodEntry holds them, so both sides multiply the same two numbers and the
    total under a card cannot come out different from the one beside the live
    meal it was taken from.
    """
    snapshot = PostMeal.objects.create(post=post, name=meal.name, date=meal.date)
    PostMealEntry.objects.bulk_create(
        [
            PostMealEntry(
                post_meal=snapshot,
                name=entry.name,
                servings=entry.servings,
                calories=entry.calories,
                protein_grams=entry.protein_grams,
                carbohydrate_grams=entry.carbohydrate_grams,
                fat_grams=entry.fat_grams,
                position=entry.position,
            )
            for entry in meal.entries.all()
        ]
    )


def snapshot_planner(post, entry):
    """Copy a planner entry onto a post.

    Notes are left behind. They were written by someone who had nowhere to
    publish them, and carrying them into a post discloses old text after the
    fact.
    """
    PostPlannerEntry.objects.create(
        post=post,
        kind=entry.kind,
        title=entry.title,
        category=entry.category,
        scheduled_date=entry.scheduled_date,
        scheduled_time=entry.scheduled_time,
        is_complete=entry.is_complete,
    )


@transaction.atomic
def create_post_from_source(
    author, kind, source, caption, visibility, shows_weights=True
):
    """Freeze a source object into a post.

    The client sends which object it is and nothing about its contents, so the
    numbers in a post are numbers the server read out of that person's own
    training. A body carrying the snapshot would let anyone post a five hundred
    kilogram squat.

    What this reads is the session, the meal or the entry, and nothing else. In
    particular it never reads RepbaseUser.weight_kg or BodyWeightEntry: a
    bodyweight-inclusive volume, the way some lifting apps report pull-ups,
    would turn a measurement the profile keeps behind `shows_weight` into a
    public number a reader can subtract back out.

    Atomic because a post whose snapshot half wrote renders as a blank card, and
    the post row is what the feed joins from — it would keep rendering blank on
    every page load for as long as it existed.
    """
    post = Post.objects.create(
        author=author,
        kind=kind,
        caption=caption,
        visibility=visibility,
        source_session=source if kind == Post.Kind.WORKOUT else None,
        source_meal=source if kind == Post.Kind.MEAL else None,
        source_planner_entry=source if kind == Post.Kind.PLANNER else None,
        shows_weights=shows_weights,
    )
    if kind == Post.Kind.WORKOUT:
        snapshot_workout(post, source, shows_weights=shows_weights)
    elif kind == Post.Kind.MEAL:
        snapshot_meal(post, source)
    else:
        snapshot_planner(post, source)
    return post


class PreviousSetsView(OwnedViewSetMixin, APIView):
    """What this user last lifted, per exercise.

    One pass over their own set entries, newest first, keeping the first
    occurrence of each (exercise, set number). The app used to do this by
    reading recent sessions one at a time until it found one that had logged
    something, which fails quietly on a run of sessions started and abandoned
    -- and that run is nine long on real data.

    Scoped to the caller throughout. Somebody else's numbers are not a hint,
    they are somebody else's training.
    """

    permission_classes = [IsAuthenticated]
    owner_lookup = "repbase_user"

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="exclude_session",
                type=int,
                location=OpenApiParameter.QUERY,
                description=(
                    "Ignore this session, which is the one being logged now."
                ),
            ),
            OpenApiParameter(
                name="workout",
                type=int,
                location=OpenApiParameter.QUERY,
                description=(
                    "Only exercises in this workout. Narrows the answer to "
                    "what the screen is about to draw."
                ),
            ),
        ],
        responses=PreviousSetSerializer(many=True),
    )
    def get(self, request):
        owner = self.owner_profile()

        entries = (
            SetEntry.objects.filter(
                session_exercise__session__repbase_user=owner,
                session_exercise__session__status=WorkoutSession.Status.COMPLETED,
            )
            .exclude(weight_kg__isnull=True, reps__isnull=True)
            .select_related("session_exercise__session")
            .order_by("-session_exercise__session__started_at", "-id")
        )

        exclude = request.query_params.get("exclude_session")
        if exclude:
            entries = entries.exclude(session_exercise__session_id=exclude)

        workout = request.query_params.get("workout")
        if workout:
            entries = entries.filter(
                session_exercise__exercise__workout_entries__workout_id=workout
            )

        # Newest first, so the first time an (exercise, set) pair is seen is
        # the most recent one. Capped: a long history is a long scan, and
        # nothing past the first occurrence of each pair is ever used.
        seen = {}
        for entry in entries[:600]:
            key = (entry.session_exercise.exercise_id, entry.set_number)
            if key in seen:
                continue
            seen[key] = {
                "exercise": entry.session_exercise.exercise_id,
                "set_number": entry.set_number,
                "weight_kg": entry.weight_kg,
                "reps": entry.reps,
                "performed_at": entry.session_exercise.session.started_at,
            }

        return Response(
            PreviousSetSerializer(list(seen.values()), many=True).data
        )


@extend_schema_view(
    list=extend_schema(
        parameters=[
            OpenApiParameter(
                name="author",
                type=int,
                location=OpenApiParameter.QUERY,
                description=(
                    "Return only posts by this user, still filtered by what the "
                    "reader is allowed to see."
                ),
            )
        ]
    ),
    create=extend_schema(
        request=CreatePostSerializer,
        responses={201: PostSerializer},
        description=(
            "Post a workout, meal or planner entry the requester owns. The "
            "server reads the source and builds the snapshot; the request names "
            "the object and never carries its contents."
        ),
    ),
    partial_update=extend_schema(
        request=UpdatePostSerializer,
        responses={200: PostSerializer},
        description=(
            "Change the caption or who can see this post. The snapshot records "
            "what happened and is not editable."
        ),
    ),
    # PUT is refused at runtime by http_method_names below. Excluded here as
    # well because I could not check from this machine whether spectacular
    # consults http_method_names before writing the router's method map into the
    # schema, and a documented PUT that answers 405 is worse than no PUT.
    update=extend_schema(exclude=True),
)
class PostViewSet(OwnedViewSetMixin, viewsets.ModelViewSet):
    """Posts: what someone has chosen to show other people.

    Reading and writing use different querysets on purpose. A reader gets
    everything the visibility rules allow; an author gets their own rows and
    nothing else, so somebody else's post is a 404 to a PATCH for the same
    reason it is a 404 to a GET of a session that is not theirs — it was never
    in the set.
    """

    queryset = posts_for_cards()
    serializer_class = PostSerializer
    permission_classes = [IsAuthenticated]
    # Reading the feed is not the concern; writing something everybody else
    # then sees is.
    throttle_scope = "post"
    owner_lookup = "author"
    # PATCH without PUT. UpdateModelMixin brings both or neither, and a PUT here
    # would have to accept the snapshot fields it replaces, which is the one
    # thing a post must never take from a client.
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_queryset(self):
        profile = self.owner_profile()
        if self.action in ("partial_update", "destroy"):
            # Editing and deleting belong to the author alone. Scoped rather
            # than checked in the handler, which is what makes someone else's
            # post a 404 instead of a 403. Deliberately not scope_to_owner:
            # that lets a superuser through, and a support account rewriting
            # somebody's caption is not a capability this endpoint should have.
            return super().get_queryset().filter(author=profile)

        queryset = annotate_social_counts(
            profile, visible_posts_for(profile, super().get_queryset())
        )
        if self.action != "list":
            # The author filter is the list's alone: a create reads its own new
            # post back through this queryset, and a stray query string on the
            # POST must not be able to hide it.
            return queryset
        author = self.request.query_params.get("author")
        return queryset.filter(author_id=author) if author else queryset

    @extend_schema(
        methods=["POST"],
        request=None,
        responses=PostSerializer,
        description="Like this post.",
    )
    @extend_schema(
        methods=["DELETE"],
        request=None,
        # Keyed by status. A bare serializer on a DELETE is overridden with
        # the 204 drf-spectacular assumes a delete returns, and the contract
        # would then promise an empty body this view does not send.
        responses={200: PostSerializer},
        description="Remove your like. Answers with the post, not 204.",
    )
    @action(detail=True, methods=["post", "delete"])
    def like(self, request, pk=None):
        post = self.get_object()
        profile = self.owner_profile()
        if request.method == "POST":
            # get_or_create rather than create: a second tap, or a retry of a
            # request whose answer was lost, must not be an error.
            PostLike.objects.get_or_create(post=post, user=profile)
        else:
            PostLike.objects.filter(post=post, user=profile).delete()
        return Response(self._card(post.pk))

    @extend_schema(
        methods=["POST"],
        request=None,
        responses=PostSerializer,
        description=(
            "Pass this post on to your followers. Reposting a repost passes "
            "on the original, and the original is what comes back."
        ),
    )
    @extend_schema(
        methods=["DELETE"],
        request=None,
        # Keyed by status. A bare serializer on a DELETE is overridden with
        # the 204 drf-spectacular assumes a delete returns, and the contract
        # would then promise an empty body this view does not send.
        responses={200: PostSerializer},
        description="Withdraw your repost. Answers with the post, not 204.",
    )
    @action(detail=True, methods=["post", "delete"])
    def repost(self, request, pk=None):
        post = self.get_object()
        profile = self.owner_profile()
        # A repost of a repost points at the original. Otherwise a chain builds
        # up, and each link has to be walked to find the thing being shown.
        original = post.repost_of or post

        if request.method == "POST":
            Post.objects.get_or_create(
                author=profile,
                repost_of=original,
                defaults={
                    "kind": Post.Kind.REPOST,
                    # A repost carries no caption of its own. The words on
                    # screen stay the original author's, which is the whole
                    # claim a repost makes.
                    "caption": "",
                    "visibility": Post.Visibility.PUBLIC,
                },
            )
        else:
            Post.objects.filter(
                author=profile,
                repost_of=original,
                kind=Post.Kind.REPOST,
            ).delete()
        return Response(self._card(original.pk))

    @extend_schema(
        request=None,
        responses={201: SavedWorkoutResultSerializer},
        description=(
            "Copy a posted workout into your own workouts. The exercises and "
            "their set counts are taken; weights are not, because a workout "
            "you save is a plan to follow rather than a record of somebody "
            "else's session."
        ),
    )
    @action(detail=True, methods=["post"], url_path="save-workout")
    def save_workout(self, request, pk=None):
        post = self.get_object()
        owner = self.owner_profile()
        # A repost carries nothing of its own; what is being saved is the
        # workout in the post underneath it.
        source = post.repost_of or post
        snapshot = getattr(source, "workout", None)
        if snapshot is None:
            raise ValidationError({"post": "That post is not a workout."})

        exercises = list(snapshot.exercises.all())
        if not exercises:
            raise ValidationError(
                {"post": "That workout has no exercises to save."}
            )

        name = self._free_copy_name(
            set(
                WorkoutTemplate.objects.filter(owner=owner)
                .values_list("name", flat=True)
            ),
            snapshot.title,
            source.author,
            "Workout",
        )
        with transaction.atomic():
            template = WorkoutTemplate.objects.create(
                owner=owner,
                name=name,
                workout_type=(
                    snapshot.workout_type
                    or WorkoutTemplate.WorkoutType.LIFTING
                ),
            )
            for order, line in enumerate(exercises, start=1):
                # The shared row first, this user's own next, and a new one
                # only when neither exists. A private "Bench Press" per person
                # who saves a workout would fork the exercise that progress
                # and personal records are grouped by.
                candidates = Exercise.objects.filter(
                    name__iexact=line.name
                ).filter(Q(created_by__isnull=True) | Q(created_by=owner))
                exercise = (
                    candidates.filter(created_by__isnull=True).first()
                    or candidates.first()
                    or Exercise.objects.create(name=line.name, created_by=owner)
                )
                WorkoutExercise.objects.create(
                    workout=template,
                    exercise=exercise,
                    order=order,
                    target_sets=line.set_count or None,
                )

        return Response(
            SavedWorkoutResultSerializer(
                {
                    "workout": template,
                    "name": name,
                    "exercise_count": len(exercises),
                    "renamed": name != snapshot.title,
                },
                context=self.get_serializer_context(),
            ).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        request=ReportPostSerializer,
        responses={201: PostReportResultSerializer, 200: PostReportResultSerializer},
        description=(
            "Report a post for a moderator to look at. The reason comes from a "
            "fixed list so reports can be counted; detail is optional. "
            "Reporting a post you have already reported succeeds with 200 "
            "rather than failing."
        ),
    )
    @action(detail=True, methods=["post"], url_path="report")
    def report(self, request, pk=None):
        post = self.get_object()
        reporter = self.owner_profile()
        # Reporting your own post is not a thing anyone means to do, and the
        # queue it would join is for complaints about other people.
        if post.author_id == reporter.id:
            raise ValidationError({"post": "You cannot report your own post."})

        body = ReportPostSerializer(data=request.data)
        body.is_valid(raise_exception=True)

        _, created = PostReport.objects.get_or_create(
            post=post,
            reporter=reporter,
            defaults={
                "reason": body.validated_data["reason"],
                "detail": body.validated_data.get("detail", ""),
            },
        )
        return Response(
            PostReportResultSerializer(
                {
                    "reason": body.validated_data["reason"],
                    "already_reported": not created,
                }
            ).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @extend_schema(
        request=None,
        responses={201: SavedMealResultSerializer},
        description=(
            "Copy a posted meal into your own saved meals. The foods and their "
            "servings are taken whole, so applying it to a day later gives the "
            "same numbers the post showed."
        ),
    )
    @action(detail=True, methods=["post"], url_path="save-meal")
    def save_meal(self, request, pk=None):
        post = self.get_object()
        owner = self.owner_profile()
        # A repost carries nothing of its own; what is being saved is the meal
        # in the post underneath it.
        source = post.repost_of or post
        snapshot = getattr(source, "meal", None)
        if snapshot is None:
            raise ValidationError({"post": "That post is not a meal."})

        entries = list(snapshot.entries.all())
        if not entries:
            raise ValidationError({"post": "That meal has no food in it to save."})

        name = self._free_copy_name(
            set(
                SavedFoodMeal.objects.filter(owner=owner)
                .values_list("name", flat=True)
            ),
            snapshot.name,
            source.author,
            "Meal",
        )
        with transaction.atomic():
            saved = SavedFoodMeal.objects.create(owner=owner, name=name)
            # A straight copy: PostMealEntry mirrors SavedFoodIngredient field
            # for field, per-serving split included, so nothing is recomputed
            # and the saved meal cannot come out to a different total than the
            # card it was taken from.
            SavedFoodIngredient.objects.bulk_create(
                SavedFoodIngredient(
                    saved_meal=saved,
                    name=entry.name,
                    servings=entry.servings,
                    calories=entry.calories,
                    protein_grams=entry.protein_grams,
                    carbohydrate_grams=entry.carbohydrate_grams,
                    fat_grams=entry.fat_grams,
                    position=position,
                )
                for position, entry in enumerate(entries, start=1)
            )

        return Response(
            SavedMealResultSerializer(
                {
                    "meal": saved,
                    "name": name,
                    "item_count": len(entries),
                    "renamed": name != snapshot.name,
                },
                context=self.get_serializer_context(),
            ).data,
            status=status.HTTP_201_CREATED,
        )

    @staticmethod
    def _free_copy_name(taken, title, author, fallback):
        """A name this user does not already have.

        Workouts and saved meals are both unique by name per user, so saving
        somebody's "Push Day" -- or their "Meal 1", which everyone has -- when
        you have your own would either fail or quietly hand back yours.
        Neither is what was asked for, so the copy is named after who it came
        from, and numbered after that.

        One rule for both, because the awkward part is the numbering and two
        copies of it would drift.
        """
        base = (title or fallback).strip() or fallback
        if base not in taken:
            return base

        from_author = f"{base} (from @{author.user.username})"[:150]
        if from_author not in taken:
            return from_author

        for suffix in range(2, 100):
            numbered = f"{from_author} {suffix}"[:150]
            if numbered not in taken:
                return numbered
        raise ValidationError({"post": "You already have too many copies of that."})

    def _card(self, pk):
        """The post read back with its counts, as the feed would send it."""
        post = self.get_queryset().get(pk=pk)
        return self.get_serializer(post).data

    def create(self, request, *args, **kwargs):
        payload = CreatePostSerializer(
            data=request.data,
            context=self.get_serializer_context(),
        )
        payload.is_valid(raise_exception=True)
        author = self.owner_profile()
        kind = payload.validated_data["kind"]
        post = create_post_from_source(
            author,
            kind,
            source_for(author, kind, payload.validated_data["source_id"]),
            payload.validated_data.get("caption", ""),
            payload.validated_data.get("visibility", Post.Visibility.PUBLIC),
            payload.validated_data.get("shows_weights", True),
        )
        # Saved after the row exists, because the upload path is named from
        # its id. Done before the card is read back, so a post never goes
        # out over the wire without the photo it was created with.
        decoded = payload.validated_data.get("decoded_image")
        if decoded is not None:
            # What Pillow read out of the bytes, falling back to the
            # client's label only if Pillow is somehow unavailable.
            extension = (
                payload.validated_data.get("image_extension")
                or ALLOWED_PHOTO_TYPES[payload.validated_data["content_type"]]
            )
            post.image.save(
                f"{uuid.uuid4().hex}{extension}",
                ContentFile(decoded),
                save=True,
            )
        # Read back through the list queryset so the card returned from a create
        # is assembled by the code that assembles the card in the feed, rather
        # than by a second path that can disagree with it.
        return Response(
            self.get_serializer(self.get_queryset().get(pk=post.pk)).data,
            status=status.HTTP_201_CREATED,
        )

    def partial_update(self, request, *args, **kwargs):
        post = self.get_object()
        payload = UpdatePostSerializer(data=request.data, partial=True)
        payload.is_valid(raise_exception=True)
        for field, value in payload.validated_data.items():
            setattr(post, field, value)
        post.save(update_fields=[*payload.validated_data, "updated_at"])
        return Response(self.get_serializer(post).data)


@extend_schema_view(
    list=extend_schema(
        parameters=[
            OpenApiParameter(
                name="cursor",
                type=str,
                location=OpenApiParameter.QUERY,
                description=(
                    "The position carried by the previous page's `next` link. "
                    "Absent means start at the newest post."
                ),
            ),
            OpenApiParameter(
                name="page_size",
                type=int,
                location=OpenApiParameter.QUERY,
                description=(
                    "How many posts to return, at most 50. Asking for more is "
                    "refused rather than reduced, so a page is always the size "
                    "it was asked for."
                ),
            ),
        ]
    )
)
class FeedViewSet(OwnedViewSetMixin, mixins.ListModelMixin, viewsets.GenericViewSet):
    """What the people you follow have posted, newest first.

    Fanned out on read: a page is one indexed walk over the posts of everyone
    the reader follows. Fanning out on write would mean a row per follower per
    post, nearly all of them never looked at, and a second copy of the
    visibility rules to keep in step with this one.

    The reader's own posts are in it, minus the private ones. Posting and being
    returned to a feed that does not contain what you just posted reads as a
    failure, and this is the cheapest confirmation the app can give; a private
    post is one deliberately held back, so it stays on the profile list where it
    was put and out of the stream that exists to be shared.
    """

    queryset = posts_for_cards()
    serializer_class = PostSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = FeedCursorPagination
    owner_lookup = "author"

    def get_queryset(self):
        profile = self.owner_profile()
        queryset = annotate_social_counts(
            profile, visible_posts_for(profile, super().get_queryset())
        )
        # visible_posts_for has already annotated the follow test, so narrowing
        # to the people being followed reuses that subquery instead of asking
        # the follow table the same question twice.
        return queryset.filter(
            Q(viewer_follows_author=True)
            | (Q(author=profile) & ~Q(visibility=Post.Visibility.PRIVATE))
        )


class PostCommentViewSet(OwnedViewSetMixin, viewsets.ModelViewSet):
    """Comments on a post.

    Reading is governed by the post, not by the comment: if you may see the
    post you may read what people said about it, and if you may not, its
    comments are a 404 for the same reason the post is. Writing and deleting
    belong to the comment's own author.
    """

    queryset = PostComment.objects.all()
    serializer_class = PostCommentSerializer
    permission_classes = [IsAuthenticated]
    owner_lookup = "author"
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_queryset(self):
        profile = self.owner_profile()
        if self.action in ("partial_update", "destroy"):
            # A 404 rather than a 403 on someone else's comment, matching how
            # posts scope editing: it was never in the set.
            return PostComment.objects.filter(author=profile)

        readable = visible_posts_for(profile, Post.objects.all())
        queryset = (
            PostComment.objects.filter(post__in=readable)
            .select_related("author", "author__user")
            .prefetch_related(
                Prefetch(
                    "replies",
                    queryset=PostComment.objects.select_related(
                        "author", "author__user"
                    ),
                )
            )
        )
        post = self.request.query_params.get("post")
        if post:
            queryset = queryset.filter(post_id=post)
        if self.action == "list":
            # Top level only. The replies come nested inside their parent, and
            # returning them again beside it would show every reply twice.
            queryset = queryset.filter(parent__isnull=True)
        return queryset

    @extend_schema(parameters=[
        OpenApiParameter(
            name="post",
            type=int,
            location=OpenApiParameter.QUERY,
            description="Only comments on this post.",
        )
    ])
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    def perform_create(self, serializer):
        profile = self.owner_profile()
        post = serializer.validated_data.get("post")
        # Commenting on a post you cannot see would tell you it exists.
        if not visible_posts_for(profile, Post.objects.all()).filter(
            pk=getattr(post, "pk", None)
        ).exists():
            raise NotFound("No such post.")
        serializer.save(author=profile)


class BlockViewSet(
    OwnedViewSetMixin,
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """People this user has blocked.

    No update endpoint, for the reason a recurrence has none: there is nothing
    inside a block to change. Lifting one is a delete, and blocking the same
    person again is a new row with a new date, which is what actually happened.

    Only blocks the requester made are listed. Who has blocked you is not
    something this API answers, and a list would answer it.
    """

    queryset = Block.objects.select_related(
        "blocked__user", "blocked__gym"
    ).prefetch_related("blocked__disciplines")
    serializer_class = BlockSerializer
    permission_classes = [IsAuthenticated]
    owner_lookup = "blocker"

    def get_queryset(self):
        return self.scope_to_owner(super().get_queryset())

    def perform_create(self, serializer):
        """Make the block and drop whatever following existed either way.

        Both halves in one transaction: a block stored while the follow it was
        supposed to remove survives leaves the blocked person on the blocker's
        follower list and still able to be followed back, which is the state
        this feature exists to make impossible.

        Blocking yourself and blocking someone twice are refused by
        `BlockSerializer`, which has already run by the time this is called, and
        the database constraints stand behind both. Repeating either test here
        would only add a branch that can never be taken.
        """
        blocker = self.owner_profile()
        blocked = serializer.validated_data["blocked"]
        with transaction.atomic():
            serializer.save(blocker=blocker)
            Follow.objects.filter(
                Q(follower=blocker, following=blocked)
                | Q(follower=blocked, following=blocker)
            ).delete()


class DailyStepCountViewSet(
    OwnedViewSetMixin,
    mixins.ListModelMixin,
    viewsets.GenericViewSet,
):
    """Steps as Health reported them. Read here, written only by ``record``."""

    queryset = DailyStepCount.objects.select_related("owner")
    serializer_class = DailyStepCountSerializer
    permission_classes = [IsAuthenticated]

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="since",
                type=OpenApiTypes.DATE,
                location=OpenApiParameter.QUERY,
                description=(
                    "Only days on or after this date. Without it every day "
                    "ever recorded comes back, which grows without bound."
                ),
            )
        ]
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    def get_queryset(self):
        queryset = self.scope_to_owner(super().get_queryset())
        since = self.request.query_params.get("since")
        if since:
            parsed = parse_date(since)
            if parsed is not None:
                queryset = queryset.filter(day__gte=parsed)
        return queryset

    @extend_schema(
        request=DailyStepCountRecordSerializer,
        # 204, not the stored rows. The rows would come back wrapped in the
        # pagination envelope this view declares, which would be a lie: the
        # action returns everything it touched in one response and paginates
        # nothing. The client re-reads through `list` regardless.
        responses={204: None},
    )
    @action(detail=False, methods=["post"])
    def record(self, request):
        """Store a batch of days, replacing any already held for those days.

        Health is the source of truth for steps, so a day arriving again
        overwrites rather than adds. Anything else would double a total every
        time the app reopened.
        """
        payload = DailyStepCountRecordSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        owner = self.owner_profile()
        days = payload.validated_data["days"]

        with transaction.atomic():
            for entry in days:
                DailyStepCount.objects.update_or_create(
                    owner=owner,
                    day=entry["day"],
                    defaults={"steps": entry["steps"]},
                )

        return Response(status=status.HTTP_204_NO_CONTENT)


class HealthWorkoutImportView(APIView):
    """Takes workouts Apple Health holds and keeps the ones Repbase does not.

    The device sends everything Health reported for the window; deciding what
    is already known happens here, where the sessions are, rather than on the
    device, which would have to download its own history to find out.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=HealthWorkoutImportSerializer,
        responses=HealthImportResultSerializer,
    )
    def post(self, request):
        payload = HealthWorkoutImportSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        owner = profile_for(request.user)
        incoming = payload.validated_data["workouts"]

        imported = 0
        skipped_overlapping = 0
        already_imported = 0

        with transaction.atomic():
            for entry in incoming:
                if WorkoutSession.objects.filter(
                    repbase_user=owner,
                    health_external_id=entry["external_id"],
                ).exists():
                    already_imported += 1
                    continue

                # A run tracked in Repbase and by the Watch is one run. The
                # session Repbase recorded itself wins: it has the route and
                # the climb, and this one would be the same effort counted
                # twice. Only sessions Repbase recorded are compared against,
                # so two imported workouts that happen to abut do not cancel
                # each other out.
                if WorkoutSession.objects.filter(
                    repbase_user=owner,
                    health_external_id__isnull=True,
                    started_at__lt=entry["ended_at"],
                    ended_at__gt=entry["started_at"],
                ).exists():
                    skipped_overlapping += 1
                    continue

                activity = entry["activity"]
                template, _ = WorkoutTemplate.objects.get_or_create(
                    owner=owner,
                    name=WorkoutTemplate.WorkoutType(activity).label,
                    defaults={"workout_type": activity},
                )
                WorkoutSession.objects.create(
                    repbase_user=owner,
                    workout=template,
                    status=WorkoutSession.Status.COMPLETED,
                    started_at=entry["started_at"],
                    ended_at=entry["ended_at"],
                    health_external_id=entry["external_id"],
                    health_distance_km=entry.get("distance_km"),
                    recorded_distance_km=entry.get("distance_km"),
                )
                imported += 1

        return Response(
            HealthImportResultSerializer(
                {
                    "imported": imported,
                    "skipped_overlapping": skipped_overlapping,
                    "already_imported": already_imported,
                }
            ).data
        )


class GearViewSet(OwnedViewSetMixin, viewsets.ModelViewSet):
    """Shoes and bikes, and how far each has been.

    Mileage is summed here rather than counted on the device: it is a property
    of every session the gear was used for, and only the server has them all.
    """

    queryset = Gear.objects.select_related("owner")
    serializer_class = GearSerializer
    permission_classes = [IsAuthenticated]

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="kind",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Only shoes, or only bikes.",
                enum=["shoe", "bike"],
            ),
            OpenApiParameter(
                name="include_retired",
                type=bool,
                location=OpenApiParameter.QUERY,
                description=(
                    "Retired gear is left out unless this is true, so the "
                    "picker offers only what is still in use."
                ),
            ),
        ]
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    def get_queryset(self):
        queryset = self.scope_to_owner(super().get_queryset()).annotate(
            recorded_distance_total=Coalesce(
                Sum("sessions__recorded_distance_km"),
                Decimal("0"),
            ),
            recorded_session_count=Count("sessions", distinct=True),
            # The most recent session it was attached to. Preselecting the
            # last thing worn beats preselecting a flag the user set once and
            # forgot, and it costs nothing extra: the join is already here.
            last_used_at=Max("sessions__started_at"),
        )

        kind = self.request.query_params.get("kind")
        if kind in Gear.Kind.values:
            queryset = queryset.filter(kind=kind)

        if self.request.query_params.get("include_retired") not in ("true", "1"):
            queryset = queryset.filter(retired_at__isnull=True)

        # Ordered explicitly. Annotating drops the model's Meta ordering, and
        # an unordered queryset makes paging non-deterministic: the same row
        # can appear on two pages or on none.
        return queryset.order_by("kind", "name", "pk")

    def perform_create(self, serializer):
        gear = serializer.save(owner=self.owner_profile())
        self._clear_other_defaults(gear)

    def perform_update(self, serializer):
        gear = serializer.save()
        self._clear_other_defaults(gear)

    def _clear_other_defaults(self, gear):
        """Only one default per kind.

        A constraint enforces this too, but a constraint can only refuse. This
        is what makes choosing a new default work instead of failing.
        """
        if not gear.is_default:
            return
        Gear.objects.filter(
            owner=gear.owner,
            kind=gear.kind,
            is_default=True,
        ).exclude(pk=gear.pk).update(is_default=False)


class TrainingStatsView(APIView):
    """Totals, streaks and the six-week trend.

    Counted here rather than on the device. The device used to page every
    session it had ever recorded in order to reduce them, which is a growing
    download for a handful of integers, and it meant two clients could
    disagree about the same history.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="today",
                type=OpenApiTypes.DATE,
                location=OpenApiParameter.QUERY,
                description=(
                    "The device's own date. Week and month boundaries are cut "
                    "against this rather than the server's clock, so the "
                    "figures match the calendar the user is looking at."
                ),
            )
        ],
        responses=TrainingStatsSerializer,
    )
    def get(self, request):
        owner = profile_for(request.user)
        today = parse_date(request.query_params.get("today") or "") or today_for(owner)

        # Only what the reduction needs. Reading whole session objects here
        # would repeat, on the server, the mistake this endpoint exists to fix.
        rows = (
            WorkoutSession.objects.filter(
                repbase_user=owner,
                status=WorkoutSession.Status.COMPLETED,
            )
            .annotate(
                logged_sets=Count(
                    "session_exercises__sets",
                    filter=Q(session_exercises__sets__weight_kg__isnull=False)
                    | Q(session_exercises__sets__reps__isnull=False),
                    distinct=True,
                )
            )
            .values(
                "workout__name",
                "workout__workout_type",
                "started_at",
                "ended_at",
                "recorded_distance_km",
                "logged_sets",
            )
        )

        # One workout trained on one day, however many sessions that took.
        # Counting sessions made starting Tuesday's workout six times read as
        # six workouts, which is what it once did.
        days = set()
        for row in rows:
            performed = row["ended_at"] or row["started_at"]
            if performed is None:
                continue

            workout_type = row["workout__workout_type"]
            if workout_type in WorkoutTemplate.DISTANCE_TYPES:
                # A run logs no sets. Distance when it was measured, and the
                # time it took when it was not: a treadmill run still happened.
                started, ended = row["started_at"], row["ended_at"]
                duration = (ended - started).total_seconds() if started and ended else 0
                trained = (row["recorded_distance_km"] or 0) > 0 or duration > 0
            else:
                trained = row["logged_sets"] > 0

            if trained:
                days.add((row["workout__name"], timezone.localtime(performed).date()))

        week_of = {}
        for _, day in days:
            start = week_start_for(day)
            week_of[start] = week_of.get(start, 0) + 1

        this_week = week_start_for(today)
        active_weeks = set(week_of)

        # The streak may run up to last week without this week having started.
        cursor = this_week if this_week in active_weeks else this_week - timedelta(days=7)
        current_streak = 0
        while cursor in active_weeks:
            current_streak += 1
            cursor -= timedelta(days=7)

        best_streak = 0
        run = 0
        previous = None
        for start in sorted(active_weeks):
            run = run + 1 if previous is not None and (start - previous).days == 7 else 1
            best_streak = max(best_streak, run)
            previous = start

        six_weeks = [
            week_of.get(this_week - timedelta(days=7 * offset), 0)
            for offset in reversed(range(6))
        ]

        return Response(
            TrainingStatsSerializer(
                {
                    "total_workouts": len(days),
                    "completed_this_week": week_of.get(this_week, 0),
                    "completed_this_month": sum(
                        1
                        for _, day in days
                        if day.year == today.year and day.month == today.month
                    ),
                    "current_streak_weeks": current_streak,
                    "best_streak_weeks": best_streak,
                    "six_week_counts": six_weeks,
                    "weekly_goal": _weekly_goal(owner, this_week),
                }
            ).data
        )


@extend_schema_view(
    list=extend_schema(
        parameters=[
            OpenApiParameter(
                name="include_ended",
                type=bool,
                location=OpenApiParameter.QUERY,
                description=(
                    "Include rotations that have been closed. Off by default, "
                    "because most screens want the one you are on -- but a "
                    "list you choose from needs the ones you are not."
                ),
            )
        ]
    )
)
class WorkoutCycleViewSet(OwnedViewSetMixin, viewsets.ModelViewSet):
    """Rotations that repeat every N days rather than every week."""

    queryset = WorkoutCycle.objects.prefetch_related("slots__workout").select_related(
        "owner"
    )
    serializer_class = WorkoutCycleSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        queryset = self.scope_to_owner(super().get_queryset())
        if self.request.query_params.get("include_ended") not in ("true", "1"):
            queryset = queryset.filter(effective_until__isnull=True)
        return queryset.order_by("-effective_from", "-id")

    def perform_create(self, serializer):
        # A rule can never reach backwards: it starts today, whatever anchor
        # the user picked, so weeks already trained keep resolving through
        # whatever was planned then.
        owner = self.owner_profile()
        today = today_for(owner)
        with transaction.atomic():
            # Making a rotation is choosing it. Two rotations both writing
            # days would put two workouts on the same date with nothing saying
            # which plan either came from.
            self._retire_active_cycles(owner, today)
            cycle = serializer.save(owner=owner, effective_from=today)
            # Written now rather than on a later plan-ahead, so the calendar
            # fills in the moment the rotation is saved.
            self._materialize(cycle, today + timedelta(days=CYCLE_MATERIALIZE_DAYS))

    def _retire_active_cycles(self, owner, handover, keeping=None):
        """Closes whatever rotation is running, as of `handover`.

        Closing alone is not enough: the days it already wrote stay on the
        calendar, so switching would leave the old plan sitting in next week.
        Only days from the handover forward are removed -- everything before it
        belongs to the rotation still running until then, and days already
        trained are history either way.
        """
        running = WorkoutCycle.objects.filter(
            owner=owner, effective_until__isnull=True
        )
        if keeping is not None:
            running = running.exclude(pk=keeping.pk)
        running = list(running)
        if not running:
            return

        WorkoutSchedule.objects.filter(
            owner=owner,
            source_cycle__in=running,
            scheduled_date__gte=handover,
        ).delete()
        for cycle in running:
            cycle.effective_until = handover
            cycle.save(update_fields=["effective_until", "updated_at"])

    @extend_schema(
        request=CycleActivateSerializer,
        responses=WorkoutCycleSerializer,
        description=(
            "Make this the rotation you are on, from `start_on` -- today if "
            "it is left out. Whatever is running keeps its days right up to "
            "that date and is closed there; this one begins at day 1 on it, "
            "and the calendar is written forward from there."
        ),
    )
    @action(detail=True, methods=["post"])
    def activate(self, request, pk=None):
        owner = self.owner_profile()
        today = today_for(owner)
        # Ended rotations are switchable back to, so this looks past the
        # default filter that hides them.
        cycle = get_object_or_404(
            self.scope_to_owner(WorkoutCycle.objects.all()), pk=pk
        )

        body = CycleActivateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        start_on = body.validated_data.get("start_on") or today
        if start_on < today:
            raise ValidationError(
                {"start_on": "A rotation cannot start before today."}
            )

        with transaction.atomic():
            # The rotation on the way out keeps every day up to the handover,
            # so the weeks between now and the switch stay planned.
            self._retire_active_cycles(owner, start_on, keeping=cycle)
            # Day 1 lands on the start date. Picking a rotation is saying
            # "this is what I am doing from here", and resuming one mid-way
            # through a turn nobody remembers being on is a stranger answer.
            cycle.anchor_date = start_on
            cycle.effective_from = start_on
            cycle.effective_until = None
            cycle.materialized_through = None
            cycle.save(update_fields=[
                "anchor_date", "effective_from", "effective_until",
                "materialized_through", "updated_at",
            ])
            self._materialize(
                cycle, start_on + timedelta(days=CYCLE_MATERIALIZE_DAYS)
            )

        cycle.refresh_from_db()
        return Response(self.get_serializer(cycle).data)

    @extend_schema(
        request=CyclePlanAheadSerializer,
        responses=WorkoutCycleSerializer,
    )
    @action(detail=True, methods=["post"], url_path="plan-ahead")
    def plan_ahead(self, request, pk=None):
        """Writes schedule rows for the rotation up to a date."""
        cycle = get_object_or_404(self.get_queryset(), pk=pk)
        payload = CyclePlanAheadSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        with transaction.atomic():
            self._materialize(cycle, through=payload.validated_data["through"])

        cycle.refresh_from_db()
        return Response(self.get_serializer(cycle).data)

    @extend_schema(
        request=CycleShiftSerializer,
        responses=CycleShiftResultSerializer,
    )
    @action(detail=True, methods=["post"])
    def shift(self, request, pk=None):
        """Pushes the rest of the rotation back, after an unplanned rest day.

        The anchor moves and every future date moves with it. That is the whole
        reason a rotation is anchored to a date: a weekly rule could only be
        shifted by becoming a rule about a different weekday.
        """
        payload = CycleShiftSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        days = payload.validated_data["days"]
        return self._reanchor(pk, shift_days=days)

    @extend_schema(request=None, responses=CycleShiftResultSerializer)
    @action(detail=True, methods=["post"], url_path="resume-today")
    def resume_today(self, request, pk=None):
        """Makes the next workout in the rotation happen today.

        For somebody who has already drifted: rather than counting how many
        days behind they are, the rotation is re-anchored so the workout they
        owe lands on today and the rest follows from there.
        """
        return self._reanchor(pk, shift_days=None)

    # MARK: - The work

    def _reanchor(self, pk, shift_days):
        cycle = get_object_or_404(self.get_queryset(), pk=pk)
        owner = self.owner_profile()
        today = today_for(owner)

        if shift_days is None:
            # Where the rotation currently says the next workout is. Its
            # position is what has to land on today.
            target_position = None
            for offset in range(0, cycle.length + 1):
                slot = cycle.slot(today + timedelta(days=offset))
                if slot is not None and slot.workout is not None:
                    target_position = slot.position
                    break
            if target_position is None:
                return Response(
                    {"detail": "This rotation has no workouts in it."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            new_anchor = today - timedelta(days=target_position - 1)
            moved = (new_anchor - cycle.anchor_date).days
        else:
            new_anchor = cycle.anchor_date + timedelta(days=shift_days)
            moved = shift_days

        with transaction.atomic():
            # Rows this cycle wrote, from today on, that nothing has happened
            # in yet. Days already trained are history and are left alone; days
            # the user added themselves were never this cycle's to remove.
            future = WorkoutSchedule.objects.filter(
                owner=owner,
                source_cycle=cycle,
                scheduled_date__gte=today,
            )
            touched = WorkoutSession.objects.filter(
                repbase_user=owner,
                workout_id=models.OuterRef("workout_id"),
                started_at__date=models.OuterRef("scheduled_date"),
            )
            removable = future.annotate(
                has_session=Exists(touched)
            ).filter(has_session=False)

            kept = future.count() - removable.count()
            removed = removable.count()
            removable.delete()

            # Close the rule in force and open its replacement, rather than
            # editing the anchor in place. The past keeps resolving through
            # what was planned at the time.
            previous_through = cycle.materialized_through
            cycle.effective_until = today
            cycle.save(update_fields=["effective_until", "updated_at"])

            replacement = WorkoutCycle.objects.create(
                owner=owner,
                name=cycle.name,
                length=cycle.length,
                anchor_date=new_anchor,
                effective_from=today,
            )
            WorkoutCycleSlot.objects.bulk_create(
                [
                    WorkoutCycleSlot(
                        cycle=replacement,
                        position=slot.position,
                        workout=slot.workout,
                    )
                    for slot in cycle.slots.all()
                ]
            )

            scheduled = self._materialize(
                replacement,
                through=previous_through or today,
            )

        replacement.refresh_from_db()
        return Response(
            CycleShiftResultSerializer(
                {
                    "cycle": replacement,
                    "days_shifted": moved,
                    "removed": removed,
                    "scheduled": scheduled,
                    "kept": kept,
                }
            ).data
        )

    def _materialize(self, cycle, through):
        """Writes one schedule row per workout slot up to `through`.

        Returns how many were written. Days already holding this workout are
        left as they are: scheduling the same workout twice on one day would
        read as two sessions to everything that counts them.
        """
        owner = cycle.owner
        start = max(cycle.effective_from, today_for(cycle.owner))
        if cycle.materialized_through and cycle.materialized_through >= start:
            start = cycle.materialized_through + timedelta(days=1)

        written = 0
        day = start
        while day <= through:
            if cycle.effective_until and day >= cycle.effective_until:
                break
            slot = cycle.slot(day)
            if slot is not None and slot.workout_id is not None:
                _, created = WorkoutSchedule.objects.get_or_create(
                    owner=owner,
                    workout_id=slot.workout_id,
                    scheduled_date=day,
                    defaults={"source_cycle": cycle},
                )
                if created:
                    written += 1
            day += timedelta(days=1)

        if through >= (cycle.materialized_through or cycle.effective_from):
            cycle.materialized_through = through
            cycle.save(update_fields=["materialized_through", "updated_at"])
        return written
