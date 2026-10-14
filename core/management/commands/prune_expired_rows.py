"""Prune cache rows, compact expired replay payloads, and forget old viewing.

Used save keys are safety data, not disposable cache entries: their compact
tombstones remain until account deletion so an old retry cannot create a duplicate.

Viewing history and "not interested" are kept for as long as the For You page
reads them back -- RECOMMENDATION_HISTORY_DAYS -- and no longer. Past that
they change nothing about anyone's page and are only a record of what
somebody once looked at, which is not a record worth keeping. Dismissed
suggestions go after SUGGESTION_DISMISSAL_DAYS.

Run it on a schedule -- daily is plenty, nothing here is urgent. It is safe
to run twice, safe to run on an empty database, and safe to interrupt.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.core.management import call_command

from core.models import (
    RECOMMENDATION_HISTORY_DAYS,
    SUGGESTION_DISMISSAL_DAYS,
    FoodSearchCache,
    PostFeedback,
    PostView,
    SaveReceipt,
    SuggestionDismissal,
)


def recommendation_history(now):
    """What has aged out of the For You page's reach, by label."""
    history = now - timedelta(days=RECOMMENDATION_HISTORY_DAYS)
    return {
        "viewing-history rows": PostView.objects.filter(last_seen_at__lt=history),
        "not-interested rows": PostFeedback.objects.filter(created_at__lt=history),
        "dismissed suggestions": SuggestionDismissal.objects.filter(
            created_at__lt=now - timedelta(days=SUGGESTION_DISMISSAL_DAYS)
        ),
    }


class Command(BaseCommand):
    help = (
        "Delete expired food-search cache entries and old viewing history, and compact "
        "save receipt payloads without deleting used keys."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be deleted or compacted without changing it.",
        )

    def handle(self, *args, **options):
        from django.utils import timezone

        call_command('prune_analytics', dry_run=options['dry_run'], stdout=self.stdout)

        if options["dry_run"]:
            now = timezone.now()
            counts = {
                "food search cache entries": FoodSearchCache.objects.filter(
                    fetched_at__lt=now - FoodSearchCache.RETENTION
                ).count(),
                "save receipts": SaveReceipt.objects.filter(
                    created_at__lt=now - SaveReceipt.RETENTION, status_code__lt=300
                ).count(),
            }
            counts.update({label: rows.count() for label, rows in recommendation_history(now).items()})
            for label, count in counts.items():
                action = 'compact' if label == 'save receipts' else 'delete'
                self.stdout.write(f"would {action} {count} {label}")
            return

        for label, deleted in (
            ("food search cache entries", FoodSearchCache.prune()),
            ("save receipts", SaveReceipt.prune()),
        ):
            action = 'compacted' if label == 'save receipts' else 'deleted'
            self.stdout.write(f"{action} {deleted} {label}")
        # One DELETE each: nothing hangs off these rows, so Django deletes
        # them without reading them first.
        for label, rows in recommendation_history(timezone.now()).items():
            deleted, _ = rows.delete()
            self.stdout.write(f"deleted {deleted} {label}")
        self.stdout.write(self.style.SUCCESS("Expired rows pruned."))
