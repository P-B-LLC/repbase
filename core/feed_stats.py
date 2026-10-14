"""Per-post counts for the For You page, kept up to date by a job.

`core.recommendations.engagement_for` used to work every candidate's counts
out during somebody's request, sharing them through the cache for ten
minutes. That is the slow part of a ranking whose cache is cold, and at a
community of hundreds of thousands of posts a month it is the part that does
not scale. `refresh_feed_stats` (`python manage.py refresh_feed_stats`, on
the `repbase-feed-stats` timer every five minutes) works them out ahead of
time instead and writes them to `PostEngagementStats`:

- every post that was acted on or seen since the last run, at once;
- every other post inside the recommendation window whose row is older than
  REFRESH_SECONDS, oldest first, at most REFRESH_LIMIT a run -- which is what
  catches unlikes, deleted comments and accounts that aged past their first
  days, none of which leave a row to notice.

The ranking reads a row only while it is younger than FRESH_SECONDS and was
written by the current way of counting (STATS_VERSION); anything else it
works out itself, as before. A stopped job makes For You slower, never wrong,
and the deploy check `core.W009` says when it has stopped.
"""

import logging
import time
from datetime import timedelta

from django.db.models import Max
from django.utils import timezone

from .models import (
    Post,
    PostComment,
    PostEngagementStats,
    PostFeedback,
    PostLike,
    PostView,
    SavedFoodMeal,
    WorkoutTemplate,
)

logger = logging.getLogger(__name__)

#: The way of counting that wrote a row. Raise it whenever what
#: `_count_engagement` means by a count changes, so old rows are recomputed
#: rather than read.
STATS_VERSION = 2
#: A row older than this is not read: the ranking works the figures out itself.
FRESH_SECONDS = 30 * 60
#: Every row in the window is rewritten at least this often.
REFRESH_SECONDS = 15 * 60
#: At most this many rows refreshed for age in one run, oldest first, so one
#: run never takes long whatever the size of the community. Posts with new
#: activity are always refreshed.
REFRESH_LIMIT = 5000
#: Posts counted per batch.
BATCH = 500
#: Rows of posts this far past the recommendation window are deleted: they
#: can never be candidates again.
PRUNE_GRACE_DAYS = 1

_FIELDS = (
    "likes", "comments", "shares", "saves", "impressions", "long_views", "completions",
    "trusted_likes", "trusted_comments", "trusted_shares", "trusted_saves", "skips", "dismissals",
)


def read_fresh(post_ids, now=None):
    """{post id: Engagement} for the posts that have a fresh row. One query."""
    from .recommendations import Engagement

    if not post_ids:
        return {}
    now = now or timezone.now()
    return {
        row["post_id"]: Engagement(**{name: row[name] for name in _FIELDS})
        for row in PostEngagementStats.objects.filter(
            post_id__in=list(post_ids), version=STATS_VERSION,
            computed_at__gte=now - timedelta(seconds=FRESH_SECONDS),
        ).values("post_id", *_FIELDS)
    }


def _active_since(since, window_start):
    """Posts inside the window that anything happened to since `since`."""
    in_window = {"post__created_at__gte": window_start}
    found = set()
    found.update(PostLike.objects.filter(created_at__gte=since, **in_window).values_list("post_id", flat=True))
    found.update(PostComment.objects.filter(created_at__gte=since, **in_window).values_list("post_id", flat=True))
    found.update(PostView.objects.filter(last_seen_at__gte=since, **in_window).values_list("post_id", flat=True))
    found.update(PostFeedback.objects.filter(created_at__gte=since, **in_window).values_list("post_id", flat=True))
    found.update(
        Post.objects.filter(
            created_at__gte=since, repost_of__isnull=False, repost_of__created_at__gte=window_start,
        ).values_list("repost_of_id", flat=True)
    )
    for model in (WorkoutTemplate, SavedFoodMeal):
        found.update(
            model.objects.filter(created_at__gte=since, source_post__created_at__gte=window_start)
            .values_list("source_post_id", flat=True)
        )
    # New posts have no row yet.
    found.update(
        Post.objects.filter(created_at__gte=since).exclude(kind=Post.Kind.REPOST).values_list("pk", flat=True)
    )
    found.discard(None)
    return found


def refresh(now=None, full=False, limit=REFRESH_LIMIT, batch=BATCH):
    """Bring the table up to date. Returns what it did, as counts."""
    from .recommendations import WINDOW_DAYS, _count_engagement

    started = time.perf_counter()
    now = now or timezone.now()
    window_start = now - timedelta(days=WINDOW_DAYS)
    window = Post.objects.filter(created_at__gte=window_start).exclude(kind=Post.Kind.REPOST)

    last_run = PostEngagementStats.objects.filter(version=STATS_VERSION).aggregate(at=Max("computed_at"))["at"]
    if full or last_run is None:
        due = set(window.values_list("pk", flat=True))
        active = len(due)
    else:
        # A little overlap, so a write that landed while the last run was
        # counting is not missed.
        active_posts = _active_since(last_run - timedelta(minutes=2), window_start)
        active = len(active_posts)
        missing = set(
            window.exclude(engagement_stats__version=STATS_VERSION).values_list("pk", flat=True)[:limit]
        )
        old = set(
            window.filter(
                engagement_stats__version=STATS_VERSION,
                engagement_stats__computed_at__lt=now - timedelta(seconds=REFRESH_SECONDS),
            ).order_by("engagement_stats__computed_at").values_list("pk", flat=True)[:limit]
        )
        due = active_posts | missing | old

    ordered = sorted(due)
    written = 0
    for start in range(0, len(ordered), batch):
        chunk = ordered[start:start + batch]
        counted = _count_engagement(chunk, now)
        PostEngagementStats.objects.bulk_create(
            [
                PostEngagementStats(
                    post_id=pk, version=STATS_VERSION, computed_at=now,
                    **{name: getattr(counts, name) for name in _FIELDS},
                )
                for pk, counts in counted.items()
            ],
            update_conflicts=True,
            unique_fields=["post"],
            update_fields=[*_FIELDS, "version", "computed_at"],
        )
        written += len(counted)

    pruned, _ = PostEngagementStats.objects.filter(
        post__created_at__lt=window_start - timedelta(days=PRUNE_GRACE_DAYS)
    ).delete()
    summary = {
        "written": written,
        "active": active,
        "pruned": pruned,
        "full": bool(full or last_run is None),
        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
    }
    logger.info(
        "feed_stats refreshed written=%(written)d active=%(active)d pruned=%(pruned)d "
        "full=%(full)s duration_ms=%(duration_ms).1f", summary,
    )
    return summary


def staleness(now=None):
    """Seconds since the newest row was written, or None with no rows."""
    newest = PostEngagementStats.objects.aggregate(at=Max("computed_at"))["at"]
    if newest is None:
        return None
    return ((now or timezone.now()) - newest).total_seconds()
