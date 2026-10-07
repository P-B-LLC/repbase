from datetime import timedelta, timezone as datetime_timezone
from django.core.management.base import BaseCommand
from django.utils import timezone
from core.analytics_models import DailyActiveAccount, DailyApiMetric, RecentApiMetric


class Command(BaseCommand):
    help = 'Delete account activity older than 30 days, aggregate metrics older than 90 days, and live buckets older than 3 days.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        now = timezone.now()
        today = now.astimezone(datetime_timezone.utc).date()
        activity = DailyActiveAccount.objects.filter(day__lt=today - timedelta(days=29))
        metrics = DailyApiMetric.objects.filter(day__lt=today - timedelta(days=89))
        # Five-minute buckets are for answering "now", and the page never
        # looks back further than a few hours. Keeping three days is slack for
        # a missed run, not a second history: the daily table is the history.
        buckets = RecentApiMetric.objects.filter(bucket__lt=now - timedelta(days=3))
        if options['dry_run']:
            self.stdout.write(
                f'Would delete {activity.count()} account-day rows, {metrics.count()} route-day rows '
                f'and {buckets.count()} live bucket rows.')
            return
        activity.delete()
        metrics.delete()
        buckets.delete()
        self.stdout.write('Expired analytics removed; operational app records were not changed.')
