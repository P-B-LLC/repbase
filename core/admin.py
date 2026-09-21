from django.contrib import admin
from django.db import transaction
from django.db.models import Count
from django.utils import timezone
from .access_models import AccessAudit


@admin.register(AccessAudit)
class AccessAuditAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'actor', 'target', 'action', 'subject')
    list_filter = ('action',)
    readonly_fields = ('actor', 'target', 'action', 'subject', 'before', 'after', 'reason', 'created_at')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_superuser

from .models import (
    BodyWeightEntry,
    Block,
    Exercise,
    Post,
    PostComment,
    CommentReport,
    PostReport,
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


# ---------------------------------------------------------------------------
# Moderation
#
# Reports and blocks were being written and read by nobody. Apple's Guideline
# 1.2 asks for evidence that reports are acted on, and "acted on" needs a
# person with somewhere to look and something to press.
# ---------------------------------------------------------------------------


class OpenReportFilter(admin.SimpleListFilter):
    """Open, handled, or everything.

    Defaults to open, because a queue that opens on its own history is a list
    rather than a queue.
    """

    title = "review state"
    parameter_name = "state"

    def lookups(self, request, model_admin):
        return (("open", "Open"), ("handled", "Handled"))

    def queryset(self, request, queryset):
        if self.value() == "handled":
            return queryset.filter(reviewed_at__isnull=False)
        if self.value() == "open":
            return queryset.filter(reviewed_at__isnull=True)
        return queryset

    def value(self):
        # Open unless asked otherwise, including on first load.
        return super().value() or ("open" if "state" not in self.used_parameters else None)


@admin.register(PostReport)
class PostReportAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "reason",
        "post_author",
        "reporter",
        "post_caption",
        "review_state",
    )
    list_filter = (OpenReportFilter, "reason", "resolution")
    search_fields = (
        "post__caption",
        "post__author__user__username",
        "reporter__user__username",
        "detail",
    )
    list_select_related = ("post__author__user", "reporter__user")
    readonly_fields = ("post", "reporter", "reason", "detail", "created_at")
    ordering = ("created_at",)
    actions = ("mark_no_action", "hide_reported_posts", "unhide_reported_posts")

    def has_moderate_posts_permission(self, request):
        return request.user.has_perms(('core.change_postreport', 'core.change_post'))

    @admin.display(description="author", ordering="post__author")
    def post_author(self, report):
        return report.post.author.user.username

    @admin.display(description="post")
    def post_caption(self, report):
        caption = (report.post.caption or "").strip()
        if not caption:
            return f"({report.post.kind}, no caption)"
        return caption[:60] + ("…" if len(caption) > 60 else "")

    @admin.display(description="state")
    def review_state(self, report):
        if report.reviewed_at is None:
            return "open"
        return report.get_resolution_display() or "handled"

    def _resolve(self, request, queryset, resolution):
        return queryset.update(
            reviewed_at=timezone.now(),
            reviewed_by=request.user,
            resolution=resolution,
        )

    @admin.action(description="Looked — no action needed", permissions=['change'])
    def mark_no_action(self, request, queryset):
        count = self._resolve(request, queryset, PostReport.Resolution.NO_ACTION)
        self.message_user(request, f"{count} report(s) closed with no action.")

    @admin.action(description="Hide the reported post", permissions=['moderate_posts'])
    @transaction.atomic
    def hide_reported_posts(self, request, queryset):
        posts = Post.objects.filter(reports__in=queryset).distinct()
        hidden = posts.update(is_hidden=True, hidden_at=timezone.now())
        # Every report against a post just hidden is settled by that, not only
        # the rows that happened to be selected.
        count = PostReport.objects.filter(post__in=posts).update(
            reviewed_at=timezone.now(),
            reviewed_by=request.user,
            resolution=PostReport.Resolution.HIDDEN,
        )
        self.message_user(
            request, f"{hidden} post(s) hidden, {count} report(s) closed."
        )

    @admin.action(description="Unhide the reported post", permissions=['moderate_posts'])
    @transaction.atomic
    def unhide_reported_posts(self, request, queryset):
        posts = Post.objects.filter(reports__in=queryset).distinct()
        shown = posts.update(is_hidden=False, hidden_at=None)
        count = self._resolve(request, queryset, PostReport.Resolution.NO_ACTION)
        self.message_user(
            request, f"{shown} post(s) restored, {count} report(s) closed."
        )


class PostReportInline(admin.TabularInline):
    model = PostReport
    extra = 0
    readonly_fields = ("reporter", "reason", "detail", "created_at")
    can_delete = False


@admin.register(Post)
class PostAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "author",
        "kind",
        "short_caption",
        "visibility",
        "is_hidden",
        "report_count",
    )
    list_filter = ("is_hidden", "kind", "visibility")
    search_fields = ("caption", "author__user__username")
    list_select_related = ("author__user",)
    ordering = ("-created_at",)
    inlines = (PostReportInline,)
    actions = ("hide_posts", "unhide_posts")

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .annotate(_reports=Count("reports", distinct=True))
        )

    @admin.display(description="caption")
    def short_caption(self, post):
        caption = (post.caption or "").strip()
        return caption[:60] + ("…" if len(caption) > 60 else "") if caption else "—"

    @admin.display(description="reports", ordering="_reports")
    def report_count(self, post):
        return post._reports

    @admin.action(description="Hide")
    def hide_posts(self, request, queryset):
        count = queryset.update(is_hidden=True, hidden_at=timezone.now())
        self.message_user(request, f"{count} post(s) hidden.")

    @admin.action(description="Unhide")
    def unhide_posts(self, request, queryset):
        count = queryset.update(is_hidden=False, hidden_at=None)
        self.message_user(request, f"{count} post(s) restored.")


@admin.register(PostComment)
class PostCommentAdmin(admin.ModelAdmin):
    list_display = ("id", "created_at", "author", "short_body", "post", "is_reply", "is_hidden")
    search_fields = ("=id", "body", "author__user__username")
    list_select_related = ("author__user", "post")
    ordering = ("-created_at",)

    @admin.display(description="comment")
    def short_body(self, comment):
        return comment.body[:70] + ("…" if len(comment.body) > 70 else "")

    @admin.display(boolean=True, description="reply")
    def is_reply(self, comment):
        return comment.parent_id is not None


@admin.register(CommentReport)
class CommentReportAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'comment', 'reporter', 'reason', 'reviewed_at', 'resolution')
    list_filter = (OpenReportFilter, 'reason', 'resolution')
    readonly_fields = ('comment', 'reporter', 'reason', 'detail', 'created_at', 'reviewed_at', 'reviewed_by', 'resolution')
    list_select_related = ('comment', 'reporter')
    ordering = ('created_at',)
    actions = ('hide_comments', 'no_action')

    def has_moderate_comments_permission(self, request):
        return request.user.has_perms(('core.change_commentreport', 'core.change_postcomment'))

    @admin.action(description='Hide reported comments and close their reports', permissions=['moderate_comments'])
    @transaction.atomic
    def hide_comments(self, request, queryset):
        reports = list(queryset)
        ids = [report.comment_id for report in reports]
        PostComment.objects.filter(pk__in=ids).update(is_hidden=True)
        CommentReport.objects.filter(comment_id__in=ids).update(reviewed_at=timezone.now(),
            reviewed_by=request.user, resolution=PostReport.Resolution.HIDDEN)
        for report in reports:
            self.log_change(request, report, 'Hid reported comment and closed reports.')

    @admin.action(description='Reviewed: no action required', permissions=['change'])
    def no_action(self, request, queryset):
        queryset.update(reviewed_at=timezone.now(), reviewed_by=request.user, resolution=PostReport.Resolution.NO_ACTION)


@admin.register(Block)
class BlockAdmin(admin.ModelAdmin):
    """Read-only. A block is one person's decision, not a moderator's to undo."""

    list_display = ("created_at", "blocker", "blocked")
    search_fields = ("blocker__user__username", "blocked__user__username")
    list_select_related = ("blocker__user", "blocked__user")
    ordering = ("-created_at",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
