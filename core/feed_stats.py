"""Per-post counts for the For You page, kept up to date by a job.

`core.recommendations.engagement_for` used to work every candidate's counts
out during somebody's request, sharing them through the cache for ten
minutes. That is the slow part of a ranking whose cache is cold, and at a
community of hundreds of thousands of posts a month it is the part that does
not scale. `refresh_feed_stats` (`python manage.py refresh_feed_stats`, on
the `repbase-feed-stats` timer every five minutes) works them out ahead of
time instead and writes them to `PostEngagementStats`:

- every post that was acted on since the last run, at once -- and, among
  posts younger than VIEWS_DAYS, every one that was seen, because early
  views are what a new post's first test is judged on;
- every other post inside the recommendation window whose row is older than
  REFRESH_SECONDS, oldest first, enough a run that the whole window comes
  round in that time -- which is what catches unlikes, deleted comments,
  views of older posts and accounts that aged past their first days, none
  of which leave a row to notice cheaply.

Each batch is its own short transaction, so a run never holds locks for
long, and a post deleted while its batch was being counted is left out
rather than failing the run. A run that finishes records when it started;
the next run looks for activity from then, so a run that was stopped half
way leaves nothing behind. Without that record (a cache that was emptied)
it looks back further, at the cost of counting a little more.

The ranking reads a row only while it is younger than FRESH_SECONDS and was
written by the current way of counting (STATS_VERSION); anything else it
works out itself, as before. A stopped job makes For You slower, never wrong,
and the deploy check `core.W009` says when it has stopped.
"""

import logging
import math
import time
from datetime import timedelta

from django.core.cache import cache
from django.db import IntegrityError, transaction
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
#: How often the job runs (deploy/repbase-feed-stats.timer). Each run
#: refreshes enough old rows that the window comes round in REFRESH_SECONDS.
RUN_EVERY_SECONDS = 5 * 60
#: At least this many rows refreshed for age in one run, oldest first. Posts
#: with new activity are always refreshed.
REFRESH_LIMIT = 5000
#: Views count as activity only for posts this young: finding the posts seen
#: since the last run means reading views, the biggest table, and it is only
#: done through the newest posts. Older posts' views are caught by age.
VIEWS_DAYS = 2
#: Attempts at writing a batch while posts in it are being deleted.
WRITE_ATTEMPTS = 3
#: When the last run that finished started.
_FINISHED_KEY = "feed-stats:finished-run"
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


def _ids(queryset, field="post_id"):
    return queryset.order_by().values_list(field, flat=True).distinct()


def _active_since(since, window_start, now):
    """Posts inside the window that anything happened to since `since`."""
    in_window = {"post__published_at__gte": window_start}
    young = {"post__published_at__gte": max(window_start, now - timedelta(days=VIEWS_DAYS))}
    found = set()
    found.update(_ids(PostLike.objects.filter(created_at__gte=since, **in_window)))
    found.update(_ids(PostComment.objects.filter(created_at__gte=since, **in_window)))
    found.update(_ids(PostView.objects.filter(last_seen_at__gte=since, **young)))
    found.update(_ids(PostFeedback.objects.filter(created_at__gte=since, **in_window)))
    found.update(_ids(
        Post.objects.filter(
            published_at__gte=since, repost_of__isnull=False, repost_of__published_at__gte=window_start,
        ),
        "repost_of_id",
    ))
    for model in (WorkoutTemplate, SavedFoodMeal):
        found.update(_ids(
            model.objects.filter(created_at__gte=since, source_post__published_at__gte=window_start),
            "source_post_id",
        ))
    # New posts have no row yet.
    found.update(_ids(Post.objects.filter(published_at__gte=since).exclude(kind=Post.Kind.REPOST), "pk"))
    found.discard(None)
    return found


def refresh(now=None, full=False, limit=REFRESH_LIMIT, batch=BATCH):
    """Bring the table up to date. Returns what it did, as counts."""
    from .recommendations import WINDOW_DAYS

    started = time.perf_counter()
    now = now or timezone.now()
    window_start = now - timedelta(days=WINDOW_DAYS)
    window = Post.objects.filter(published_at__gte=window_start).exclude(kind=Post.Kind.REPOST)

    last_run = PostEngagementStats.objects.filter(version=STATS_VERSION).aggregate(at=Max("computed_at"))["at"]
    full = bool(full or last_run is None)
    if full:
        due = set(window.values_list("pk", flat=True))
        active = len(due)
    else:
        finished = cache.get(_FINISHED_KEY)
        if finished is None or finished > last_run:
            # Not known when the last finished run started: look back as far
            # as a row may go unrefreshed, which covers it.
            finished = last_run - timedelta(seconds=REFRESH_SECONDS)
        # A little overlap, so a write that landed while the last run was
        # counting is not missed.
        active_posts = _active_since(finished - timedelta(minutes=2), window_start, now)
        active = len(active_posts)
        # Enough that every row comes round within REFRESH_SECONDS, however
        # big the window has grown.
        limit = max(limit, math.ceil(window.count() * RUN_EVERY_SECONDS / REFRESH_SECONDS))
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
    written = gone = 0
    for start in range(0, len(ordered), batch):
        done, lost = _write(ordered[start:start + batch], now)
        written += done
        gone += lost

    pruned, _ = PostEngagementStats.objects.filter(
        post__published_at__lt=window_start - timedelta(days=PRUNE_GRACE_DAYS)
    ).delete()
    cache.set(_FINISHED_KEY, now, None)
    summary = {
        "written": written,
        "active": active,
        "deleted_while_counting": gone,
        "pruned": pruned,
        "full": full,
        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
    }
    logger.info(
        "feed_stats refreshed written=%(written)d active=%(active)d pruned=%(pruned)d "
        "deleted_while_counting=%(deleted_while_counting)d full=%(full)s duration_ms=%(duration_ms).1f",
        summary,
    )
    return summary


def _write(chunk, now):
    """Count and write one batch, in a transaction of its own. Returns (rows
    written, posts deleted while it was being counted)."""
    from .recommendations import _count_engagement

    counted = _count_engagement(chunk, now)
    rows = [
        PostEngagementStats(
            post_id=pk, version=STATS_VERSION, computed_at=now,
            **{name: getattr(counts, name) for name in _FIELDS},
        )
        for pk, counts in counted.items()
    ]
    for attempt in range(WRITE_ATTEMPTS):
        try:
            # A post deleted under the batch shows up as a broken foreign
            # key. Those are checked when a transaction commits, which is not
            # here when a caller already opened one, so they are checked here
            # too; then the batch is written again without it.
            with transaction.atomic():
                PostEngagementStats.objects.bulk_create(
                    rows,
                    update_conflicts=True,
                    unique_fields=["post"],
                    update_fields=[*_FIELDS, "version", "computed_at"],
                )
                transaction.get_connection().check_constraints(
                    table_names=[PostEngagementStats._meta.db_table]
                )
            return len(rows), len(counted) - len(rows)
        except IntegrityError:
            if attempt == WRITE_ATTEMPTS - 1:
                raise
            alive = set(Post.objects.filter(pk__in=[row.post_id for row in rows]).values_list("pk", flat=True))
            rows = [row for row in rows if row.post_id in alive]
    return len(rows), len(counted) - len(rows)  # pragma: no cover


def staleness(now=None):
    """Seconds since the newest row was written, or None with no rows."""
    newest = PostEngagementStats.objects.aggregate(at=Max("computed_at"))["at"]
    if newest is None:
        return None
    return ((now or timezone.now()) - newest).total_seconds()
