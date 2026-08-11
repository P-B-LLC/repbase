from django.contrib import admin

from .models import (
    BodyWeightEntry,
    Exercise,
    RepbaseUser,
    SessionExercise,
    SetEntry,
    WorkoutExercise,
    WorkoutSchedule,
    WorkoutSession,
    WorkoutTemplate,
)


@admin.register(RepbaseUser)
class RepbaseUserAdmin(admin.ModelAdmin):
    list_display = (
        "username",
        "first_name",
        "last_name",
        "email",
        "height_cm",
        "weight_kg",
        "created_at",
    )
    search_fields = (
        "user__username",
        "user__first_name",
        "user__last_name",
        "user__email",
    )
    list_select_related = ("user",)
    ordering = ("-created_at",)


class WorkoutExerciseInline(admin.TabularInline):
    model = WorkoutExercise
    extra = 0


@admin.register(WorkoutTemplate)
class WorkoutTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "updated_at")
    search_fields = ("name", "owner__user__username")
    inlines = (WorkoutExerciseInline,)


@admin.register(Exercise)
class ExerciseAdmin(admin.ModelAdmin):
    list_display = ("name", "muscle_group", "created_by")
    search_fields = ("name", "muscle_group")


class SessionExerciseInline(admin.TabularInline):
    model = SessionExercise
    extra = 0


@admin.register(WorkoutSession)
class WorkoutSessionAdmin(admin.ModelAdmin):
    list_display = ("id", "repbase_user", "workout", "status", "started_at", "ended_at")
    list_filter = ("status",)
    inlines = (SessionExerciseInline,)


@admin.register(SessionExercise)
class SessionExerciseAdmin(admin.ModelAdmin):
    list_display = ("session", "exercise", "order")


@admin.register(SetEntry)
class SetEntryAdmin(admin.ModelAdmin):
    list_display = ("session_exercise", "set_number", "weight_kg", "reps", "completed_at")


@admin.register(WorkoutSchedule)
class WorkoutScheduleAdmin(admin.ModelAdmin):
    list_display = ("scheduled_date", "owner", "workout")
    list_filter = ("scheduled_date",)


@admin.register(BodyWeightEntry)
class BodyWeightEntryAdmin(admin.ModelAdmin):
    list_display = ("owner", "weight_kg", "recorded_at")
