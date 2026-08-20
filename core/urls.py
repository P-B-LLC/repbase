from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    BlockViewSet,
    BodyWeightEntryViewSet,
    DailyStepCountViewSet,
    FeedViewSet,
    ExerciseProgressView,
    ExerciseViewSet,
    GearViewSet,
    HealthWorkoutImportView,
    TrainingStatsView,
    PostViewSet,
    FoodEntryViewSet,
    FoodMealViewSet,
    GymViewSet,
    NutritionGoalView,
    SavedFoodMealViewSet,
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
router.register("food/meals", FoodMealViewSet)
router.register("food/entries", FoodEntryViewSet)
router.register("food/saved-meals", SavedFoodMealViewSet)
router.register("social/posts", PostViewSet)
# An explicit basename: this viewset is a second view over Post, and the
# router would otherwise derive "post" for both and register two url names
# that shadow each other.
router.register("social/feed", FeedViewSet, basename="feed")
router.register("social/blocks", BlockViewSet)
router.register("sessions", WorkoutSessionViewSet)
router.register("session-exercises", SessionExerciseViewSet)
router.register("set-entries", SetEntryViewSet)
router.register("gear", GearViewSet, basename="gear")
router.register("body-weight", BodyWeightEntryViewSet)
router.register("step-counts", DailyStepCountViewSet, basename="step-count")

urlpatterns = [
    path("auth/register/", RegisterView.as_view(), name="register"),
    path("auth/login/", LoginView.as_view(), name="login"),
    path("auth/rotate-token/", RotateTokenView.as_view(), name="rotate-token"),
    path("auth/logout/", LogoutView.as_view(), name="logout"),
    path("me/", MeView.as_view(), name="me"),
    path("me/photo/", MePhotoView.as_view(), name="me-photo"),
    path("food/goals/", NutritionGoalView.as_view(), name="nutrition-goals"),
    path(
        "sessions/training-stats/",
        TrainingStatsView.as_view(),
        name="training-stats",
    ),
    path(
        "sessions/import-health/",
        HealthWorkoutImportView.as_view(),
        name="import-health",
    ),
    path(
        "progress/exercises/<int:exercise_id>/",
        ExerciseProgressView.as_view(),
        name="exercise-progress",
    ),
] + router.urls
