"""Prune cache rows and compact expired replay payloads.

Used save keys are safety data, not disposable cache entries: their compact
tombstones remain until account deletion so an old retry cannot create a duplicate.

Run it on a schedule -- daily is plenty, nothing here is urgent. It is safe
to run twice, safe to run on an empty database, and safe to interrupt.
"""

from django.core.management.base import BaseCommand
from django.core.management import call_command

from core.models import FoodSearchCache, SaveReceipt


class Command(BaseCommand):
    help = "Delete expired food-search cache entries and compact save receipt payloads without deleting used keys."

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
        self.stdout.write(self.style.SUCCESS("Expired rows pruned."))
