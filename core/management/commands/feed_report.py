"""How the For You page is doing: speed, and whether people like what it shows.

Counts and rates only. Nothing here names a person or quotes a post, so the
output is safe to paste into a channel or keep in a scheduler's log.

Run it ad hoc after changing FOR_YOU_WEIGHTS, or daily, and compare: the
numbers worth watching are the engagement rate (are recommendations being
acted on), the skip and not-interested rates (are they being rejected), and
coverage (is the page spreading attention across creators or concentrating
it).
"""

import json
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Avg, Count, Exists, F, OuterRef, Q
from django.utils import timezone

from core.models import Post, PostComment, PostFeedback, PostLike, PostView, SavedFoodMeal, WorkoutTemplate
from core.recommendations import latency_summary


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
        since = timezone.now() - timedelta(days=days)
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
        acted = {
            "engaged": annotated.filter(
                Q(liked=True) | Q(commented=True) | Q(shared=True) | Q(saved=True)
            ).count(),
            "dismissed": annotated.filter(dismissed=True).count(),
        }

        def rate(part, whole):
            return round(part / whole, 4) if whole else None

        report = {
            "window_days": days,
            "impressions": totals["impressions"],
            "viewers": totals["viewers"],
            "distinct_posts": totals["posts"],
            "distinct_authors": totals["authors"],
            "engagement_rate": rate(acted["engaged"], totals["impressions"]),
            "skip_rate": rate(totals["skipped"], totals["impressions"]),
            "not_interested_rate": rate(acted["dismissed"], totals["impressions"]),
            "clip_completion_rate": rate(totals["completed"], totals["with_clip"]),
            "average_dwell_ms": round(totals["dwell"]) if totals["dwell"] is not None else None,
            "latency": latency_summary(hours=hours),
        }
        if options["json"]:
            self.stdout.write(json.dumps(report, sort_keys=True))
            return
        for key, value in report.items():
            if key == "latency":
                self.stdout.write(
                    f"latency (last {hours}h): rankings={value['count']} "
                    f"p50<={value['p50_ms_at_most']}ms p95<={value['p95_ms_at_most']}ms"
                )
            else:
                self.stdout.write(f"{key}: {value}")
