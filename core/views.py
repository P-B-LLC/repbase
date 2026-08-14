from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from drf_spectacular.utils import (
    OpenApiParameter,
    extend_schema,
    extend_schema_view,
)
from rest_framework import status, viewsets
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
    WorkoutSchedule,
    WorkoutSession,
    WorkoutTemplate,
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
    SessionRoutePointSerializer,
    SessionRouteUploadSerializer,
    SetEntrySerializer,
    WorkoutExerciseSerializer,
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
        if session_status:
            queryset = queryset.filter(status=session_status)
        if workout:
            queryset = queryset.filter(workout_id=workout)
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

    @extend_schema(responses=ExerciseProgressPointSerializer(many=True))
    def get(self, request, exercise_id):
        profile = profile_for(request.user)
        entries = SetEntry.objects.filter(
            session_exercise__session__repbase_user=profile,
            session_exercise__exercise_id=exercise_id,
            completed_at__isnull=False,
            weight_kg__isnull=False,
            reps__isnull=False,
        ).order_by("completed_at")
        points = [
            {
                "completed_at": entry.completed_at,
                "weight_kg": entry.weight_kg,
                "reps": entry.reps,
                "volume_kg": entry.weight_kg * Decimal(entry.reps),
            }
            for entry in entries
        ]
        return Response(ExerciseProgressPointSerializer(points, many=True).data)
