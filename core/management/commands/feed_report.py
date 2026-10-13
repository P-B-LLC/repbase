"""How the For You page is doing: speed, and whether people like what it shows.

Counts and rates only. Nothing here names a person or quotes a post, so the
output is safe to paste into a channel or keep in a scheduler's log.

Run it ad hoc after changing FOR_YOU_WEIGHTS, or daily, and compare: the
numbers worth watching are the engagement rate (are recommendations being
acted on), the skip and not-interested rates (are they being rejected),
coverage (is the page spreading attention across creators or concentrating
it), and whether people come back.

What each section answers:

- the top-level rates: is the page, as a whole, getting better or worse;
- `actions`: which kind of engagement moved -- a like is cheap, a save is a
  plan to do the thing;
- `watch`: are clips being watched, and how far;
- `by_reason`: is each kind of slot earning its place -- whether discovery,
  similar taste and new creators are acted on as often as interest;
- `diversity`: is attention spread or concentrated -- how much of it the ten
  most-shown authors take, how much goes to people the reader does not
  follow and to new creators, and how often a post was put in front of the
  same person again;
- `retention`: of the people who started using For You in the window, how
  many were back the next day and within a week (needs ANALYTICS_ENABLED,
  which is what records a day's activity);
- `by_variant`: the same figures per arm, when FOR_YOU_EXPERIMENT is set.
"""

import json
from collections import defaultdict
from datetime import timedelta, timezone as datetime_timezone

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Avg, Count, Exists, F, FloatField, Min, OuterRef, Q, Sum
from django.db.models.functions import Cast
from django.utils import timezone

from core.analytics_models import DailyActiveAccount
from core.models import Post, PostComment, PostFeedback, PostLike, PostView, SavedFoodMeal, WorkoutTemplate
from core.models import Follow
from core.recommendations import NEWCOMER_DAYS, experiment_config, experiment_variant, latency_summary

ENGAGED = Q(liked=True) | Q(commented=True) | Q(shared=True) | Q(saved=True)


def engaged(queryset):
    """Annotate each view row with whether its viewer acted on its post."""
    same_post = {"post": OuterRef("post")}
    return queryset.annotate(
        liked=Exists(PostLike.objects.filter(user=OuterRef("viewer"), **same_post)),
        commented=Exists(PostComment.objects.filter(author=OuterRef("viewer"), **same_post)),
        shared=Exists(Post.objects.filter(author=OuterRef("viewer"), repost_of=OuterRef("post"))),
        saved=Exists(WorkoutTemplate.objects.filter(owner=OuterRef("viewer"), source_post=OuterRef("post")))
        | Exists(SavedFoodMeal.objects.filter(owner=OuterRef("viewer"), source_post=OuterRef("post"))),
        dismissed=Exists(PostFeedback.objects.filter(viewer=OuterRef("viewer"), **same_post)),
    )


def rate(part, whole):
    return round(part / whole, 4) if whole else None


def _by(queryset, key):
    """{value of key: count} for a queryset, in one query."""
    return {row[key]: row["n"] for row in queryset.order_by().values(key).annotate(n=Count("pk"))}


def by_reason(rows, annotated):
    """Impressions and rates per For You reason. Rows from before reasons
    were recorded, or first seen elsewhere, are left out."""
    rows = rows.exclude(reason="")
    totals = {
        row["reason"]: row
        for row in rows.order_by().values("reason").annotate(
            impressions=Count("pk"), skipped=Count("pk", filter=Q(skipped=True))
        )
    }
    acted = _by(annotated.exclude(reason="").filter(ENGAGED), "reason")
    dismissed = _by(annotated.exclude(reason="").filter(dismissed=True), "reason")
    return {
        reason: {
            "impressions": row["impressions"],
            "engagement_rate": rate(acted.get(reason, 0), row["impressions"]),
            "skip_rate": rate(row["skipped"], row["impressions"]),
            "not_interested_rate": rate(dismissed.get(reason, 0), row["impressions"]),
        }
        for reason, row in sorted(totals.items())
    }


def returns(rows, today):
    """{viewer id: (came back next day, came back within a week)} for the
    people whose first For You view in the window was long enough ago to
    tell; None for one too recent to say. Days are UTC, as DailyActiveAccount
    keeps them."""
    firsts = list(rows.order_by().values("viewer_id", "viewer__user_id").annotate(first=Min("first_seen_at")))
    if not firsts:
        return {}
    entry = {
        row["viewer_id"]: (row["viewer__user_id"], row["first"].astimezone(datetime_timezone.utc).date())
        for row in firsts
    }
    start = min(day for _, day in entry.values())
    active = set()
    users = sorted({user for user, _ in entry.values()})
    for index in range(0, len(users), 1000):
        active.update(
            DailyActiveAccount.objects.filter(
                user_id__in=users[index:index + 1000], day__gt=start, day__lte=today
            ).values_list("user_id", "day")
        )
    outcome = {}
    for viewer, (user, day) in entry.items():
        next_day = (user, day + timedelta(days=1)) in active if day + timedelta(days=1) <= today else None
        week = (
            any((user, day + timedelta(days=offset)) in active for offset in range(1, 8))
            if day + timedelta(days=7) <= today else None
        )
        outcome[viewer] = (next_day, week)
    return outcome


def summarise_returns(outcomes):
    next_day = [came for came, _ in outcomes if came is not None]
    week = [came for _, came in outcomes if came is not None]
    return {
        "cohort": len(outcomes),
        "next_day_return_rate": rate(sum(next_day), len(next_day)),
        "seven_day_return_rate": rate(sum(week), len(week)),
    }


def by_variant(rows, annotated, config, comebacks):
    """The core figures per experiment arm, summed from per-reader counts.

    Readers are assigned by the same hash the ranking uses, so nothing about
    who was in which arm is stored anywhere."""
    per_viewer = defaultdict(lambda: defaultdict(int))
    for row in rows.order_by().values("viewer_id").annotate(
        impressions=Count("pk"),
        skipped=Count("pk", filter=Q(skipped=True)),
        with_clip=Count("pk", filter=Q(post__video__isnull=False)),
        completed=Count("pk", filter=Q(completed=True, post__video__isnull=False)),
    ):
        per_viewer[row["viewer_id"]].update(row)
    for viewer, n in _by(annotated.filter(ENGAGED), "viewer_id").items():
        per_viewer[viewer]["engaged"] = n
    for viewer, n in _by(annotated.filter(dismissed=True), "viewer_id").items():
        per_viewer[viewer]["dismissed"] = n

    arms = defaultdict(lambda: defaultdict(int))
    arm_returns = defaultdict(list)
    for viewer, counts in per_viewer.items():
        variant, _ = experiment_variant(viewer, config)
        arm = arms[variant]
        arm["viewers"] += 1
        for name in ("impressions", "skipped", "with_clip", "completed", "engaged", "dismissed"):
            arm[name] += counts.get(name, 0)
        if viewer in comebacks:
            arm_returns[variant].append(comebacks[viewer])
    return {
        variant: {
            "viewers": arm["viewers"],
            "impressions": arm["impressions"],
            "engagement_rate": rate(arm["engaged"], arm["impressions"]),
            "skip_rate": rate(arm["skipped"], arm["impressions"]),
            "not_interested_rate": rate(arm["dismissed"], arm["impressions"]),
            "clip_completion_rate": rate(arm["completed"], arm["with_clip"]),
            **({"retention": summarise_returns(arm_returns[variant])} if comebacks else {}),
        }
        for variant, arm in sorted(arms.items())
    }


def diversity(rows, impressions):
    """Where attention went: counts and shares only, nobody named."""
    if not impressions:
        return {
            "top_ten_author_share": None, "unconnected_share": None,
            "new_creator_share": None, "repeat_impression_rate": None,
        }
    per_author = sorted(
        (row["n"] for row in rows.order_by().values("post__author").annotate(n=Count("pk"))),
        reverse=True,
    )
    unconnected = rows.annotate(
        follows=Exists(Follow.objects.filter(follower=OuterRef("viewer"), following=OuterRef("post__author")))
    ).filter(follows=False).count()
    new_creator = rows.filter(
        post__author__created_at__gte=F("first_seen_at") - timedelta(days=NEWCOMER_DAYS)
    ).count()
    return {
        # How much of For You the ten most-shown authors took. Rising means
        # the page is concentrating on fewer creators.
        "top_ten_author_share": rate(sum(per_author[:10]), impressions),
        # Shown somebody the reader does not follow: what For You is for.
        "unconnected_share": rate(unconnected, impressions),
        # Shown somebody who had joined within NEWCOMER_DAYS of being seen.
        "new_creator_share": rate(new_creator, impressions),
        # A post put in front of the same reader more than once. Paging never
        # repeats a post; this is across visits.
        "repeat_impression_rate": rate(rows.filter(view_count__gt=1).count(), impressions),
    }


def build_report(days=7, hours=24, now=None):
    now = now or timezone.now()
    since = now - timedelta(days=days)
    today = now.astimezone(datetime_timezone.utc).date()
    rows = PostView.objects.filter(surface=PostView.Surface.FOR_YOU, first_seen_at__gte=since)

    totals = rows.aggregate(
        impressions=Count("pk"),
        viewers=Count("viewer", distinct=True),
        authors=Count("post__author", distinct=True),
        posts=Count("post", distinct=True),
        skipped=Count("pk", filter=Q(skipped=True)),
        with_clip=Count("pk", filter=Q(post__video__isnull=False)),
        completed=Count("pk", filter=Q(completed=True, post__video__isnull=False)),
        dwell=Avg(F("total_dwell_ms") / F("view_count"), filter=Q(view_count__gt=0)),
    )
    # Filtered and counted rather than aggregated with filter=: PostgreSQL
    # refuses an aggregate that names an Exists annotation by its alias,
    # where SQLite lets it through.
    annotated = engaged(rows)
    impressions = totals["impressions"]
    acted = {
        "engaged": annotated.filter(ENGAGED).count(),
        "dismissed": annotated.filter(dismissed=True).count(),
        **{name: annotated.filter(**{name: True}).count() for name in ("liked", "commented", "saved", "shared")},
    }
    clips = rows.filter(post__video__isnull=False, max_watch_ms__isnull=False, post__video__duration_ms__gt=0)
    watched = clips.aggregate(
        fraction=Avg(Cast("max_watch_ms", FloatField()) / Cast("post__video__duration_ms", FloatField())),
        total=Sum("max_watch_ms"),
    )

    comebacks = returns(rows, today) if settings.ANALYTICS_ENABLED else {}
    report = {
        "window_days": days,
        "impressions": impressions,
        "viewers": totals["viewers"],
        "distinct_posts": totals["posts"],
        "distinct_authors": totals["authors"],
        "engagement_rate": rate(acted["engaged"], impressions),
        "skip_rate": rate(totals["skipped"], impressions),
        "not_interested_rate": rate(acted["dismissed"], impressions),
        "clip_completion_rate": rate(totals["completed"], totals["with_clip"]),
        "average_dwell_ms": round(totals["dwell"]) if totals["dwell"] is not None else None,
        "actions": {
            "like_rate": rate(acted["liked"], impressions),
            "comment_rate": rate(acted["commented"], impressions),
            "save_rate": rate(acted["saved"], impressions),
            "share_rate": rate(acted["shared"], impressions),
        },
        "watch": {
            "clip_impressions": totals["with_clip"],
            # How far into a clip people got, on average, as a share of it.
            "average_watch_fraction": (
                round(min(watched["fraction"], 1.0), 4) if watched["fraction"] is not None else None
            ),
            "watch_time_ms": watched["total"] or 0,
        },
        "by_reason": by_reason(rows, annotated),
        "diversity": diversity(rows, impressions),
        # None when analytics is off: without daily activity there is no
        # honest way to say who came back.
        "retention": summarise_returns(list(comebacks.values())) if settings.ANALYTICS_ENABLED else None,
        "latency": latency_summary(hours=hours, kind="first"),
        "latency_next_page": latency_summary(hours=hours, kind="next"),
    }
    config = experiment_config()
    if config:
        report["experiment"] = config["name"]
        report["by_variant"] = by_variant(rows, annotated, config, comebacks)
    return report


class Command(BaseCommand):
    help = "Report For You latency and recommendation quality, as counts and rates."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=7, help="Window for quality figures.")
        parser.add_argument("--hours", type=int, default=24, help="Window for latency figures.")
        parser.add_argument("--json", action="store_true", help="Print one JSON object.")

    def handle(self, *args, **options):
        days, hours = options["days"], options["hours"]
        if not 1 <= days <= 90 or not 1 <= hours <= 24 * 7:
            raise CommandError("Use --days between 1 and 90 and --hours between 1 and 168.")
        report = build_report(days=days, hours=hours)
        if options["json"]:
            self.stdout.write(json.dumps(report, sort_keys=True))
            return
        for key, value in report.items():
            if key in ("latency", "latency_next_page"):
                label = "latency" if key == "latency" else "next-page latency"
                self.stdout.write(
                    f"{label} (last {hours}h): rankings={value['count']} "
                    f"p50<={value['p50_ms_at_most']}ms p95<={value['p95_ms_at_most']}ms"
                )
            elif isinstance(value, dict):
                self.stdout.write(f"{key}:")
                for name, inner in value.items():
                    self.stdout.write(f"  {name}: {inner}")
            else:
                self.stdout.write(f"{key}: {value}")
