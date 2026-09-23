from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.models import PushDelivery
from core.push import APNsClient, claim_delivery, deliver


class Command(BaseCommand):
    help = "Deliver a bounded batch of queued APNs alerts. Disabled unless APNS_ENABLED=true."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        if not 1 <= options["limit"] <= 1000:
            raise CommandError("limit must be between 1 and 1000")
        if not settings.APNS_ENABLED:
            self.stdout.write("APNs disabled; no alerts sent.")
            return
        try:
            provider = APNsClient()
        except ImproperlyConfigured as exc:
            raise CommandError(str(exc)) from None
        count = 0
        try:
            for _ in range(options["limit"]):
                claim = claim_delivery()
                if not claim:
                    break
                deliver(claim, provider)
                count += 1
        finally:
            provider.close()
        PushDelivery.objects.filter(created_at__lt=timezone.now() - timedelta(days=7)).delete()
        self.stdout.write(f"Processed {count} push deliveries.")
