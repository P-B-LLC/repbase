from datetime import timedelta, timezone as datetime_timezone
from django.core.management.base import BaseCommand
from django.utils import timezone
from core.analytics_models import DailyActiveAccount, DailyApiMetric


class Command(BaseCommand):
    help = 'Delete account activity older than 30 days and aggregate metrics older than 90 days.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        today = timezone.now().astimezone(datetime_timezone.utc).date()
        activity = DailyActiveAccount.objects.filter(day__lt=today - timedelta(days=29))
        metrics = DailyApiMetric.objects.filter(day__lt=today - timedelta(days=89))
        if options['dry_run']:
            self.stdout.write(f'Would delete {activity.count()} account-day rows and {metrics.count()} route-day rows.')
            return
        activity.delete()
        metrics.delete()
        self.stdout.write('Expired analytics removed; operational app records were not changed.')
