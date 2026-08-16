from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    BodyWeightEntryViewSet,
    ExerciseProgressView,
    ExerciseViewSet,
    GymViewSet,
    LoginView,
    LogoutView,
    MePhotoView,
    MeView,
    RegisterView,
    RepbaseUserViewSet,
    RotateTokenView,
    SessionExerciseViewSet,
    SetEntryViewSet,
    PlannerEntryViewSet,
    WorkoutExerciseViewSet,
    WorkoutRecurrenceViewSet,
    WorkoutScheduleViewSet,
    WorkoutSessionViewSet,
    WorkoutTemplateViewSet,
)


router = DefaultRouter()
router.register("users", RepbaseUserViewSet)
router.register("exercises", ExerciseViewSet, basename="exercise")
router.register("workouts", WorkoutTemplateViewSet)
router.register("workout-exercises", WorkoutExerciseViewSet)
router.register("schedules", WorkoutScheduleViewSet)
router.register("recurrences", WorkoutRecurrenceViewSet)
router.register("planner", PlannerEntryViewSet)
router.register("gyms", GymViewSet)
router.register("sessions", WorkoutSessionViewSet)
router.register("session-exercises", SessionExerciseViewSet)
router.register("set-entries", SetEntryViewSet)
router.register("body-weight", BodyWeightEntryViewSet)

urlpatterns = [
    path("auth/register/", RegisterView.as_view(), name="register"),
    path("auth/login/", LoginView.as_view(), name="login"),
    path("auth/rotate-token/", RotateTokenView.as_view(), name="rotate-token"),
    path("auth/logout/", LogoutView.as_view(), name="logout"),
    path("me/", MeView.as_view(), name="me"),
    path("me/photo/", MePhotoView.as_view(), name="me-photo"),
    path(
        "progress/exercises/<int:exercise_id>/",
        ExerciseProgressView.as_view(),
        name="exercise-progress",
    ),
] + router.urls
