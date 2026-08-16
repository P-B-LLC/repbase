from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiParameter,
    extend_schema,
    extend_schema_view,
)
from rest_framework import mixins, status, viewsets
from rest_framework.authtoken.models import Token
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.generics import RetrieveUpdateAPIView

from .models import (
    BodyWeightEntry,
    Exercise,
    RepbaseUser,
    SessionExercise,
    SessionRoutePoint,
    SetEntry,
    WorkoutExercise,
    PlannerCategory,
    PlannerEntry,
    WorkoutRecurrence,
    WorkoutSchedule,
    WorkoutSession,
    WorkoutTemplate,
    plan_recurring_week,
    week_start_for,
)
from .permissions import IsCustomExerciseOwnerOrAdmin
from .serializers import (
    AuthResponseSerializer,
    BodyWeightEntrySerializer,
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
    PlanWeekSerializer,
    PlannerEntrySerializer,
    WorkoutExerciseSerializer,
    WorkoutRecurrenceSerializer,
    WorkoutScheduleSerializer,
    WorkoutSessionSerializer,
    WorkoutTemplateSerializer,
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


def profile_for(user):
    profile, _ = RepbaseUser.objects.get_or_create(user=user)
    return profile


class RegisterView(APIView):
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

    @extend_schema(request=LoginSerializer, responses={200: AuthResponseSerializer})
    def post(self, request):
        serializer = LoginSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]
        profile = profile_for(user)
        token, _ = Token.objects.get_or_create(user=user)
        response = AuthResponseSerializer({"token": token.key, "user": profile})
        return Response(response.data)


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


class MeView(RetrieveUpdateAPIView):
    serializer_class = RepbaseUserSerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):
        return profile_for(self.request.user)


class RepbaseUserViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = RepbaseUser.objects.select_related("user").order_by("-created_at")
    serializer_class = PublicRepbaseUserSerializer
    permission_classes = [IsAuthenticated]


class ExerciseViewSet(viewsets.ModelViewSet):
    queryset = Exercise.objects.all()
    serializer_class = ExerciseSerializer
    permission_classes = [IsAuthenticated, IsCustomExerciseOwnerOrAdmin]

    def get_queryset(self):
        queryset = super().get_queryset().select_related("created_by")
        if self.request.user.is_superuser:
            return queryset
        profile = profile_for(self.request.user)
        return queryset.filter(Q(created_by__isnull=True) | Q(created_by=profile))

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
        plan_recurring_week(owner, week_start, timezone.localdate())
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
        today = timezone.localdate()
        recurrence = serializer.save(
            owner=self.owner_profile(),
            effective_from=week_start_for(today),
        )
        # Claim the current week straight away. The workout is already on the
        # calendar for the day the user turned this on, so nothing new appears
        # now; it stops the rule re-adding it if they remove it this week.
        plan_recurring_week(recurrence.owner, week_start_for(today), today)

    def perform_destroy(self, instance):
        current_week = week_start_for(timezone.localdate())
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
    queryset = WorkoutSession.objects.select_related(
        "repbase_user", "workout"
    ).prefetch_related("route_points")
    serializer_class = WorkoutSessionSerializer
    permission_classes = [IsAuthenticated]
    owner_lookup = "repbase_user"

    def get_queryset(self):
        queryset = self.scope_to_owner(super().get_queryset())
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
        # 200, matching the documented contract and the start/end actions.
        return Response(self.get_serializer(session).data)


class SessionExerciseViewSet(OwnedViewSetMixin, viewsets.ModelViewSet):
    queryset = SessionExercise.objects.select_related("session", "exercise")
    serializer_class = SessionExerciseSerializer
    permission_classes = [IsAuthenticated]
    owner_lookup = "session__repbase_user"

    def get_queryset(self):
        queryset = self.scope_to_owner(super().get_queryset())
        session = self.request.query_params.get("session")
        return queryset.filter(session_id=session) if session else queryset


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
