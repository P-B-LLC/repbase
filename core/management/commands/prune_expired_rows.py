"""Prune cache rows, compact expired replay payloads, and forget old viewing.

Used save keys are safety data, not disposable cache entries: their compact
tombstones remain until account deletion so an old retry cannot create a duplicate.

Viewing history and "not interested" are kept for as long as the For You page
reads them back -- RECOMMENDATION_HISTORY_DAYS, or the shorter policy set
with the setting of that name -- and no longer. Past that
they change nothing about anyone's page and are only a record of what
somebody once looked at, which is not a record worth keeping. Dismissed
suggestions go after SUGGESTION_DISMISSAL_DAYS. What the ranking knew about
each post it served -- the per-exposure training sample -- is cleared from
its viewing row after TRAINING_SAMPLE_DAYS, long before the row itself goes.

Run it on a schedule -- daily is plenty, nothing here is urgent. It is safe
to run twice, safe to run on an empty database, and safe to interrupt.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.core.management import call_command

from core.models import (
    SUGGESTION_DISMISSAL_DAYS,
    TRAINING_SAMPLE_DAYS,
    FoodSearchCache,
    IMPRESSION_RECEIPT_DAYS,
    ImpressionBatchReceipt,
    PostFeedback,
    PostView,
    SaveReceipt,
    SuggestionDismissal,
    recommendation_history_days,
)

#: Rows touched per statement. Small enough that one statement holds its
#: locks for milliseconds and leaves the table to vacuum between them, so a
#: sweep of a million rows never stands in front of a reader's impression.
BATCH = 2000


def recommendation_history(now):
    """What has aged out of the For You page's reach, by label, each with
    the indexed column the sweep walks."""
    history = now - timedelta(days=recommendation_history_days())
    return {
        "viewing-history rows": PostView.objects.filter(last_seen_at__lt=history),
        "not-interested rows": PostFeedback.objects.filter(created_at__lt=history),
        # Past any retry a device will make; see ImpressionBatchReceipt.
        "impression batch receipts": ImpressionBatchReceipt.objects.filter(
            received_at__lt=now - timedelta(days=IMPRESSION_RECEIPT_DAYS)
        ),
        "dismissed suggestions": SuggestionDismissal.objects.filter(
            created_at__lt=now - timedelta(days=SUGGESTION_DISMISSAL_DAYS)
        ),
    }


def training_samples(now):
    """Viewing rows still carrying what the ranking knew when it served
    them, past the time anything trains on it."""
    return PostView.objects.filter(
        surface=PostView.Surface.FOR_YOU,
        first_seen_at__lt=now - timedelta(days=TRAINING_SAMPLE_DAYS),
        features__isnull=False,
    )


def in_batches(rows, change, batch=BATCH):
    """Apply `change` to `rows` a batch of primary keys at a time, each in
    its own short transaction. Returns how many rows it reached.

    Safe to interrupt: whatever was done stays done and the next run carries
    on. `change` must take the rows it is given out of `rows`, or this would
    never end; a batch that changes nothing stops it regardless.
    """
    total = 0
    while True:
        ids = list(rows.order_by("pk").values_list("pk", flat=True)[:batch])
        if not ids:
            return total
        done = change(rows.model.objects.filter(pk__in=ids))
        total += done
        if not done or len(ids) < batch:
            return total


def _delete(rows):
    return rows.delete()[0]


def _forget_sample(rows):
    return rows.update(features=None, served=None)


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
            counts["ranking training samples"] = training_samples(now).count()
            for label, count in counts.items():
                action = {'save receipts': 'compact', 'ranking training samples': 'clear'}.get(label, 'delete')
                self.stdout.write(f"would {action} {count} {label}")
            return

        for label, deleted in (
            ("food search cache entries", FoodSearchCache.prune()),
            ("save receipts", SaveReceipt.prune()),
        ):
            action = 'compacted' if label == 'save receipts' else 'deleted'
            self.stdout.write(f"{action} {deleted} {label}")
        # In batches by primary key, each its own statement: nothing hangs
        # off these rows, so Django deletes without reading them first, and
        # no one statement holds the table for long.
        now = timezone.now()
        for label, rows in recommendation_history(now).items():
            self.stdout.write(f"deleted {in_batches(rows, _delete)} {label}")
        self.stdout.write(
            f"cleared {in_batches(training_samples(now), _forget_sample)} ranking training samples"
        )
        self.stdout.write(self.style.SUCCESS("Expired rows pruned."))
