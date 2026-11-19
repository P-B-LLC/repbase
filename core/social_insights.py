"""Private figures about the social pages: whether they are useful, and what
they cost to run.

Three rules hold for everything here.

- **Aggregates, over a bounded window.** `roll_up_social_insights` works the
  figures out once a day for the 7 and 30 days just ended and writes them to
  `SocialInsightSnapshot`. The page at `/insights/social/` reads the snapshot
  and never the rows behind it, so nobody with access to the page is given a
  way to look at what one account did, and opening it costs one indexed read.
- **Small groups are left out.** A figure with fewer than `SMALL_COHORT`
  people behind it is not written at all. Five readers and one save is not a
  20% save rate; it is one person's evening, readable by anybody who knows
  who was in the beta.
- **Nothing private goes in.** Every figure is a count of social rows --
  views, likes, saves of a post, takes, "I tried this" -- and of API presence.
  No caption, token, planner text, health reading or media address is read to
  make one, and none is logged.

Broken playback is reported beside the watch figures, never folded into them:
a clip that would not start is not a clip somebody disliked.

What this cannot see is said on the page instead of being left blank:
database CPU, per-request SQL time and media bandwidth are measured by the
database host, the load-test harness and the storage provider, not from
inside the application.
"""

import logging
from collections import defaultdict
from datetime import datetime, time as day_time, timedelta, timezone as datetime_timezone

from django.core.cache import cache, caches
from django.db import connection, transaction
from django.db.models import Count, Exists, F, Max, Min, OuterRef, Q, Sum
from django.utils import timezone

from .analytics_models import (
    DailyActiveAccount,
    DailyApiMetric,
    MetricFlushReceipt,
    SocialDailyCounter,
    SocialInsightSnapshot,
)

logger = logging.getLogger(__name__)

#: Fewer people than this behind a figure and it is not recorded.
SMALL_COHORT = 5
#: The windows a snapshot is kept for, in days.
WINDOWS = (7, 30)
#: How long the daily operational counters are kept.
COUNTER_DAYS = 90

#: What `count` accepts. A fixed list, so a typo is an error and not a new
#: series nobody reads, and so a name can never carry anything about a person.
COUNTERS = (
    # Attention batches that arrived, and the ones that had arrived before.
    "attention_batches",
    "attention_duplicate_batches",
    # Summaries sent, and the ones not recorded: too old, too short, or of a
    # post the reader may not see.
    "attention_events",
    "attention_events_ignored",
    # Bodies refused for size before they were read.
    "attention_oversize",
    # Batches set aside because measurement is switched off for the account.
    "attention_batches_switched_off",
)

_COUNTER_TTL = 8 * 24 * 3600


def _day(now=None):
    return (now or timezone.now()).astimezone(datetime_timezone.utc).date()


def _counter_key(day, name):
    return f"social-count:{day.isoformat()}:{name}"


def count(name, amount=1, now=None):
    """Add to today's count of `name`. One cache increment, and a failure
    changes nothing for the request that was being counted."""
    if name not in COUNTERS:
        raise ValueError(f"{name!r} is not a social counter.")
    if amount <= 0:
        return
    key = _counter_key(_day(now), name)
    try:
        cache.add(key, 0, _COUNTER_TTL)
        try:
            cache.incr(key, amount)
        except ValueError:
            cache.set(key, amount, _COUNTER_TTL)
    except Exception:  # noqa: BLE001
        logger.warning("A social counter could not be recorded; the request was not affected.")


def flush_counters(now=None):
    """Move what the cache has counted into `SocialDailyCounter`.

    Taken out of the cache before it is added to the table, so a run that
    dies in between loses a few counts and can never add them twice. These
    are operational estimates, and losing some is the cheaper mistake.
    """
    today = _day(now)
    moved = 0
    for day in (today - timedelta(days=1), today):
        for name in COUNTERS:
            key = _counter_key(day, name)
            value = cache.get(key) or 0
            if value <= 0:
                continue
            try:
                cache.decr(key, value)
            except ValueError:
                continue
            with transaction.atomic():
                row, _ = SocialDailyCounter.objects.get_or_create(day=day, name=name)
                SocialDailyCounter.objects.filter(pk=row.pk).update(value=F("value") + value)
            moved += value
    return moved


# ------------------------------------------------------------------ figures

def figure(value, of=None, people=None):
    """One figure as it is stored: a count, or `value` out of `of`, with how
    many people are behind it -- or nothing but the fact that it was withheld
    when that is too few to be anybody's business."""
    if people is not None and 0 < people < SMALL_COHORT:
        return {"value": None, "of": None, "rate": None, "people": None, "withheld": True}
    rate = round(value / of, 4) if of else None
    return {"value": value, "of": of, "rate": rate, "people": people, "withheld": False}


def _bounds(day, days):
    """[start, end) of the `days` UTC days ending with `day`."""
    end = datetime.combine(day + timedelta(days=1), day_time.min, tzinfo=datetime_timezone.utc)
    return end - timedelta(days=days), end


def _members(field=""):
    """Everything below leaves out staff and closed accounts: the people who
    run the app are not the people it is being measured for. `field` is the
    path from the row to its `RepbaseUser`."""
    prefix = f"{field}__" if field else ""
    return Q(**{f"{prefix}user__is_staff": False, f"{prefix}user__is_active": True})


def product_figures(day, days):
    from .models import (
        Post, PostComment, PostFeedback, PostLike, PostMeal, PostTried, PostView, PostWorkout,
        RepbaseUser, SavedFoodMeal, WorkoutTemplate,
    )
    from .views import PUBLIC_AND_STANDING

    start, end = _bounds(day, days)
    member = _members("viewer")
    saved = (
        Exists(WorkoutTemplate.objects.filter(owner=OuterRef("viewer"), source_post=OuterRef("post")))
        | Exists(SavedFoodMeal.objects.filter(owner=OuterRef("viewer"), source_post=OuterRef("post")))
    )
    saveable = (
        Exists(PostWorkout.objects.filter(post=OuterRef("post")))
        | Exists(PostMeal.objects.filter(post=OuterRef("post")))
    )
    views = PostView.objects.filter(member, first_seen_at__gte=start, first_seen_at__lt=end)
    qualified = views.filter(qualified_views__gt=0)

    # Qualified exposure -> save. Only posts with something to save are in
    # the denominator: a gym clip has no recipe to fail to keep.
    offered = qualified.annotate(saveable=saveable, saved=saved).filter(saveable=True)
    offered_totals = offered.aggregate(pairs=Count("pk"), people=Count("viewer", distinct=True))
    # Its own query: a filtered aggregate over a subquery annotation is not
    # something every database counts the same way.
    offered_totals["saved"] = offered.filter(saved=True).count()
    exposure = qualified.aggregate(
        pairs=Count("pk"), people=Count("viewer", distinct=True),
        authors=Count("post__author", distinct=True),
    )

    # Saved -> said they tried it. Counted from the saves made in the window.
    tried_of = lambda owner, post: Exists(  # noqa: E731
        PostTried.objects.filter(user=OuterRef(owner), post=OuterRef(post))
    )
    saves = savers = tried_saves = 0
    saver_ids = set()
    for model in (WorkoutTemplate, SavedFoodMeal):
        rows = model.objects.filter(
            Q(owner__user__is_staff=False, owner__user__is_active=True),
            source_post__isnull=False, created_at__gte=start, created_at__lt=end,
        ).annotate(tried=tried_of("owner", "source_post"))
        saves += rows.count()
        tried_saves += rows.filter(tried=True).count()
        saver_ids.update(rows.values_list("owner_id", flat=True).distinct())
    savers = len(saver_ids)

    takes = Post.objects.filter(
        Q(author__user__is_staff=False), is_take=True, published_at__gte=start, published_at__lt=end,
    ).aggregate(posts=Count("pk"), people=Count("author", distinct=True))

    tried = PostTried.objects.filter(
        _members("user"), created_at__gte=start, created_at__lt=end
    ).aggregate(rows=Count("pk"), people=Count("user", distinct=True))

    # Creator coverage: of the people who published something anybody could
    # read in the window, how many had it properly seen by anybody at all.
    creators = set(
        Post.objects.filter(PUBLIC_AND_STANDING, published_at__gte=start, published_at__lt=end)
        .filter(author__user__is_staff=False).values_list("author_id", flat=True).distinct()
    )
    reached = set(
        qualified.filter(post__published_at__gte=start, post__author_id__in=creators)
        .values_list("post__author_id", flat=True).distinct()
    ) if creators else set()

    # What readers said, in the words the page gives them.
    feedback = PostFeedback.objects.filter(member, created_at__gte=start, created_at__lt=end).aggregate(
        rows=Count("pk"), people=Count("viewer", distinct=True),
        more=Count("pk", filter=Q(kind=PostFeedback.Kind.MORE)),
    )

    # Playback, beside its failures.
    clips = views.filter(last_observed_at__isnull=False, playback_ms__isnull=False).aggregate(
        pairs=Count("pk"), people=Count("viewer", distinct=True),
        through=Count("pk", filter=Q(watched_through=True)),
        failed=Count("pk", filter=Q(playback_failures__gt=0)),
        failures=Sum("playback_failures"),
        played=Count("pk", filter=Q(playback_failures=0)),
        through_played=Count("pk", filter=Q(watched_through=True, playback_failures=0)),
    )

    # Coming back. Presence is recorded only while ANALYTICS_ENABLED is on.
    def returned(offset):
        cohort_end = end - timedelta(days=offset)
        cohort = dict(
            RepbaseUser.objects.filter(_members(), created_at__gte=start, created_at__lt=cohort_end)
            .values_list("user_id", "created_at")
        )
        if not cohort:
            return 0, 0
        present = set(
            DailyActiveAccount.objects.filter(user_id__in=cohort, day__gte=start.date())
            .values_list("user_id", "day")
        )
        back = sum(
            1 for user_id, joined in cohort.items()
            if (user_id, joined.astimezone(datetime_timezone.utc).date() + timedelta(days=offset)) in present
        )
        return back, len(cohort)

    d1_back, d1_cohort = returned(1)
    d7_back, d7_cohort = returned(7)

    # Return visits that did something: an account that liked, commented,
    # saved, said it tried something or published on two or more days.
    active_days = defaultdict(set)

    def mark(rows):
        for account, when in rows:
            if account and when:
                active_days[account].add(when.astimezone(datetime_timezone.utc).date())

    window = dict(created_at__gte=start, created_at__lt=end)
    mark(PostLike.objects.filter(_members("user"), **window).values_list("user_id", "created_at"))
    mark(PostComment.objects.filter(
        author__user__is_staff=False, author__user__is_active=True, **window
    ).values_list("author_id", "created_at"))
    mark(PostTried.objects.filter(_members("user"), **window).values_list("user_id", "created_at"))
    for model in (WorkoutTemplate, SavedFoodMeal):
        mark(model.objects.filter(
            owner__user__is_staff=False, source_post__isnull=False, **window
        ).values_list("owner_id", "created_at"))
    mark(Post.objects.filter(
        author__user__is_staff=False, published_at__gte=start, published_at__lt=end
    ).values_list("author_id", "published_at"))
    socially_active = len(active_days)
    came_back = sum(1 for days_seen in active_days.values() if len(days_seen) >= 2)

    return {
        "qualified_exposures": figure(exposure["pairs"], people=exposure["people"]),
        "exposure_to_save": figure(
            offered_totals["saved"], offered_totals["pairs"], offered_totals["people"]
        ),
        "save_to_tried": figure(tried_saves, saves, savers),
        "tried": figure(tried["rows"], people=tried["people"]),
        "takes_created": figure(takes["posts"], people=takes["people"]),
        "creator_coverage": figure(len(reached), len(creators), len(creators)),
        "satisfaction": figure(feedback["more"], feedback["rows"], feedback["people"]),
        "d1_return": figure(d1_back, d1_cohort, d1_cohort),
        "d7_return": figure(d7_back, d7_cohort, d7_cohort),
        "meaningful_return": figure(came_back, socially_active, socially_active),
        "clip_exposures": figure(clips["pairs"], people=clips["people"]),
        "playback_failure": figure(clips["failed"], clips["pairs"], clips["people"]),
        "playback_failures": figure(clips["failures"] or 0, people=clips["people"]),
        "watched_through": figure(clips["through"], clips["pairs"], clips["people"]),
        # The same rate over the exposures whose clip played at all: the
        # honest reading of whether people watched what they could watch.
        "watched_through_when_played": figure(clips["through_played"], clips["played"], clips["people"]),
    }


# -------------------------------------------------------------- reliability

#: The tables that grow with use of the social pages.
WATCHED_TABLES = (
    "core_postview", "core_impressionbatchreceipt", "core_postfeedback", "core_post",
    "core_postlike", "core_postcomment", "core_postengagementstats", "core_posthashtag",
    "core_posttried", "core_dailyactiveaccount", "core_recentapimetric",
)


def _age_seconds(moment, now):
    return None if moment is None else max(0, round((now - moment).total_seconds()))


def _queues(now):
    """How long the oldest piece of waiting work has waited, per queue."""
    from .models import MediaScreeningJob, PendingMediaDeletion, Sound, VideoJob

    waiting = {
        "video_processing": VideoJob.objects.filter(finished_at__isnull=True).exclude(state="uploading"),
        "media_screening": MediaScreeningJob.objects.filter(state="queued"),
        "sound_preparation": Sound.objects.filter(status=Sound.Status.ACTIVE, audio_key=""),
        "media_deletion": PendingMediaDeletion.objects.all(),
    }
    found = {}
    for name, rows in waiting.items():
        summary = rows.aggregate(n=Count("pk"), oldest=Min("created_at"))
        found[name] = {"waiting": summary["n"], "oldest_seconds": _age_seconds(summary["oldest"], now)}
    return found


def _cache_stats():
    """Redis's own running totals of hits and misses, or None. Cumulative
    since the server started: the page shows the rate between two snapshots."""
    run = getattr(caches["default"], "run", None)
    if run is None:
        return None
    try:
        stats = run(lambda client: client.info("stats"), miss=None)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(stats, dict) or "keyspace_hits" not in stats:
        return None
    return {"hits": int(stats["keyspace_hits"]), "misses": int(stats["keyspace_misses"])}


def _database():
    """Table sizes, dead rows, vacuum times and lock pressure. PostgreSQL
    only; anything else answers None and the page says so."""
    if connection.vendor != "postgresql":
        return None
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relname, pg_total_relation_size(relid), n_live_tup, n_dead_tup, "
            "GREATEST(last_autovacuum, last_vacuum) FROM pg_stat_user_tables "
            "WHERE relname = ANY(%s)",
            [list(WATCHED_TABLES)],
        )
        tables = {
            name: {
                "bytes": size, "live_rows": live, "dead_rows": dead,
                "last_vacuum": vacuumed.isoformat() if vacuumed else None,
            }
            for name, size, live, dead, vacuumed in cursor.fetchall()
        }
        cursor.execute(
            "SELECT pg_database_size(current_database()), "
            "(SELECT deadlocks FROM pg_stat_database WHERE datname = current_database()), "
            "(SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
            " AND wait_event_type = 'Lock')"
        )
        size, deadlocks, waiting = cursor.fetchone()
    return {
        "bytes": size, "deadlocks_since_reset": deadlocks, "sessions_waiting_on_locks": waiting,
        "tables": tables,
    }


def reliability_figures(day, days, now=None):
    from . import feed_stats
    from .models import PostVideo
    from .recommendations import latency_summary

    now = now or timezone.now()
    start, end = _bounds(day, days)
    counters = dict(
        SocialDailyCounter.objects.filter(day__gte=start.date(), day__lte=day)
        .values("name").annotate(total=Sum("value")).values_list("name", "total")
    )
    feed_routes = DailyApiMetric.objects.filter(
        day__gte=start.date(), day__lte=day, endpoint__in=("for-you", "impressions", "post-list"),
    ).values("endpoint").annotate(
        requests=Sum("requests"), server_errors=Sum("server_errors"),
        total_ms=Sum("total_ms"), max_ms=Max("max_ms"),
    )
    lost = MetricFlushReceipt.objects.filter(
        applied_at__gte=start, applied_at__lt=end
    ).aggregate(lost=Sum("lost_observations"))["lost"] or 0
    videos = PostVideo.objects.aggregate(
        total=Sum("size_bytes"),
        added=Sum("size_bytes", filter=Q(post__created_at__gte=start, post__created_at__lt=end)),
    )
    stats_age = feed_stats.staleness(now)
    return {
        "ranking_latency": {
            # From the ranking's own histogram, kept for the last day.
            "first_page": latency_summary(hours=24, kind="first"),
            "next_page": latency_summary(hours=24, kind="next"),
        },
        "routes": {
            row["endpoint"]: {
                "requests": row["requests"], "server_errors": row["server_errors"],
                "mean_ms": round(row["total_ms"] / row["requests"]) if row["requests"] else None,
                "max_ms": row["max_ms"],
            }
            for row in feed_routes
        },
        "stats_age_seconds": None if stats_age is None else round(stats_age),
        "stats_stale_after_seconds": feed_stats.stale_seconds(),
        "queues": _queues(now),
        "telemetry": {
            **{name: counters.get(name, 0) for name in COUNTERS},
            "api_observations_lost": lost,
        },
        "cache": _cache_stats(),
        "database": _database(),
        "storage": {"clip_bytes": videos["total"] or 0, "clip_bytes_added": videos["added"] or 0},
    }


# ----------------------------------------------------------------- snapshots

def roll_up(day=None, now=None, windows=WINDOWS):
    """Write the snapshots for the windows ending on `day` (yesterday, the
    last whole UTC day, when not given). Safe to run again: it replaces."""
    now = now or timezone.now()
    day = day or _day(now) - timedelta(days=1)
    flush_counters(now)
    written = []
    for days in windows:
        figures = {
            "product": product_figures(day, days),
            "reliability": reliability_figures(day, days, now),
        }
        snapshot, _ = SocialInsightSnapshot.objects.update_or_create(
            day=day, window_days=days, defaults={"figures": figures}
        )
        written.append(snapshot)
    return written


def prune(now=None):
    """Old operational counters. Snapshots are kept: a few numbers a day,
    about nobody."""
    deleted, _ = SocialDailyCounter.objects.filter(
        day__lt=_day(now) - timedelta(days=COUNTER_DAYS)
    ).delete()
    return deleted


# -------------------------------------------------------------------- page

PRODUCT_ROWS = (
    ("exposure_to_save", "Qualified exposure → save",
     "Of the times a post with a workout or recipe was properly seen, how often the reader kept it."),
    ("save_to_tried", "Save → “I tried this”",
     "Of the saves made, how many the saver later said they tried. Only ever their own statement."),
    ("takes_created", "Your take posts", "Takes published, and by how many people."),
    ("creator_coverage", "Creator coverage",
     "Of the people who published in the window, how many were properly seen by anybody."),
    ("satisfaction", "Reader-reported satisfaction",
     "Of the times a reader told the page what they thought, how often it was “more like this”."),
    ("d1_return", "Back the next day", "New accounts seen again the day after they joined."),
    ("d7_return", "Back on day seven", "New accounts seen again seven days after they joined."),
    ("meaningful_return", "Returned and did something",
     "Of accounts with any social activity, those active on two or more days."),
)
PLAYBACK_ROWS = (
    ("clip_exposures", "Clip exposures measured", "Reader and clip pairs with measured playback."),
    ("playback_failure", "Clips that failed to start or stalled out",
     "Read this before the two below: a clip that would not play was not disliked."),
    ("watched_through", "Watched through", "Of every measured clip exposure."),
    ("watched_through_when_played", "Watched through, when it played",
     "The same, leaving out exposures where playback failed."),
)

NOT_MEASURED = (
    "Database CPU: read it from the database host's own monitoring.",
    "SQL time and statement count per request: measured by the load-test harness (loadtest/), "
    "not in production.",
    "Media bandwidth: read it from the storage or CDN provider.",
    "Batches a device gave up on before sending: the app does not report them.",
)


def _shown(entry, previous=None):
    """A stored figure as the page prints it."""
    if entry is None:
        return {"text": "—", "note": "not recorded", "withheld": False}
    if entry.get("withheld"):
        return {
            "text": f"fewer than {SMALL_COHORT}", "withheld": True,
            "note": "Withheld: too few people for this to be a rate and not somebody's activity.",
        }
    if entry.get("of") is not None:
        if not entry["of"]:
            return {"text": "—", "note": "nothing to measure yet", "withheld": False}
        text = f"{entry['rate'] * 100:.1f}%"
        note = f"{entry['value']} of {entry['of']}"
    else:
        text, note = f"{entry['value']}", ""
    if entry.get("people") is not None:
        note = (note + " · " if note else "") + f"{entry['people']} people"
    change = None
    if previous and not previous.get("withheld") and previous.get("rate") is not None and entry.get("rate") is not None:
        change = round((entry["rate"] - previous["rate"]) * 100, 1)
    return {"text": text, "note": note, "withheld": False, "change": change}


def _megabytes(value):
    return None if value is None else round(value / 1_048_576, 1)


def page_context(days, today=None):
    """What `/insights/social/` shows for the chosen window."""
    latest = SocialInsightSnapshot.objects.filter(window_days=days).first()
    if latest is None:
        return {"days": days, "snapshot": None, "small_cohort": SMALL_COHORT}
    earlier = SocialInsightSnapshot.objects.filter(
        window_days=days, day__lte=latest.day - timedelta(days=days)
    ).first()
    product = latest.figures.get("product", {})
    before = (earlier.figures.get("product", {}) if earlier else {})
    reliability = latest.figures.get("reliability", {})
    was = earlier.figures.get("reliability", {}) if earlier else {}

    def rows(spec):
        return [
            {"label": label, "help": text, **_shown(product.get(key), before.get(key))}
            for key, label, text in spec
        ]

    telemetry = reliability.get("telemetry", {})
    batches = telemetry.get("attention_batches") or 0
    events = telemetry.get("attention_events") or 0
    cache_now, cache_was = reliability.get("cache"), was.get("cache")
    hit_rate = None
    if cache_now and cache_was:
        hits = cache_now["hits"] - cache_was["hits"]
        total = hits + cache_now["misses"] - cache_was["misses"]
        # A restarted Redis counts from nought again; that is not a rate.
        if hits >= 0 and total > 0:
            hit_rate = round(hits / total * 100, 1)
    database = reliability.get("database")
    tables = []
    if database:
        earlier_tables = (was.get("database") or {}).get("tables", {})
        for name, row in sorted(database["tables"].items(), key=lambda item: -item[1]["bytes"]):
            previous = earlier_tables.get(name)
            total = row["live_rows"] + row["dead_rows"]
            tables.append({
                "name": name, "megabytes": _megabytes(row["bytes"]), "rows": row["live_rows"],
                "grew_megabytes": _megabytes(row["bytes"] - previous["bytes"]) if previous else None,
                "dead_share": round(row["dead_rows"] / total * 100, 1) if total else 0,
                "last_vacuum": row["last_vacuum"],
            })
    stats_age = reliability.get("stats_age_seconds")
    latency = reliability.get("ranking_latency", {})
    return {
        "days": days, "snapshot": latest, "compared_with": earlier, "small_cohort": SMALL_COHORT,
        "since": latest.day - timedelta(days=days - 1),
        "product": rows(PRODUCT_ROWS), "playback": rows(PLAYBACK_ROWS),
        "exposures": _shown(product.get("qualified_exposures")),
        "latency": [
            {"label": label, **(latency.get(key) or {})}
            for key, label in (("first_page", "First page"), ("next_page", "Next page"))
        ],
        "routes": sorted(reliability.get("routes", {}).items()),
        "stats_age_minutes": None if stats_age is None else round(stats_age / 60),
        "stats_stale": (
            stats_age is not None and stats_age > (reliability.get("stats_stale_after_seconds") or 0)
        ),
        "queues": [
            {
                "name": name.replace("_", " "), "waiting": row["waiting"],
                "oldest_minutes": None if row["oldest_seconds"] is None else round(row["oldest_seconds"] / 60),
            }
            for name, row in sorted(reliability.get("queues", {}).items())
        ],
        "telemetry": telemetry,
        "duplicate_share": round(telemetry.get("attention_duplicate_batches", 0) / batches * 100, 1) if batches else None,
        "ignored_share": round(telemetry.get("attention_events_ignored", 0) / events * 100, 1) if events else None,
        "cache_hit_rate": hit_rate, "cache_measured": cache_now is not None,
        "database": database, "database_megabytes": _megabytes(database["bytes"]) if database else None,
        "tables": tables,
        "clip_megabytes": _megabytes((reliability.get("storage") or {}).get("clip_bytes")),
        "clip_megabytes_added": _megabytes((reliability.get("storage") or {}).get("clip_bytes_added")),
        "not_measured": NOT_MEASURED,
    }
