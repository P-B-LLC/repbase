"""Delete rows that exist only until they stop being useful.

Two tables here grow with use and never shrink on their own, and neither is
user data: a cache of what FoodData Central said, and receipts that let a
retried save replay instead of duplicating. Both are worth keeping for a
while and worth deleting afterwards, and until something calls this, "a
while" means forever.

Run it on a schedule -- daily is plenty, nothing here is urgent. It is safe
to run twice, safe to run on an empty database, and safe to interrupt.
"""

from django.core.management.base import BaseCommand

from core.models import FoodSearchCache, SaveReceipt


class Command(BaseCommand):
    help = "Delete expired food-search cache entries and save receipts."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be deleted without deleting it.",
        )

    def handle(self, *args, **options):
        from django.utils import timezone

        if options["dry_run"]:
            now = timezone.now()
            counts = {
                "food search cache entries": FoodSearchCache.objects.filter(
                    fetched_at__lt=now - FoodSearchCache.RETENTION
                ).count(),
                "save receipts": SaveReceipt.objects.filter(
                    created_at__lt=now - SaveReceipt.RETENTION
                ).count(),
            }
            for label, count in counts.items():
                self.stdout.write(f"would delete {count} {label}")
            return

        for label, deleted in (
            ("food search cache entries", FoodSearchCache.prune()),
            ("save receipts", SaveReceipt.prune()),
        ):
            self.stdout.write(f"deleted {deleted} {label}")
        self.stdout.write(self.style.SUCCESS("Expired rows pruned."))
