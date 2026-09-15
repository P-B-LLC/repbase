"""Run hourly; alert on nonzero exit. No report content in scheduler logs."""
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.models import PostReport


class Command(BaseCommand):
    help = 'Report overdue moderation work; does not resolve reports or email user content.'

    def add_arguments(self, parser):
        parser.add_argument('--max-age-hours', type=int, default=24)

    def handle(self, *args, **options):
        hours = options['max_age_hours']
        if not 1 <= hours <= 168:
            raise CommandError('Use a review deadline between 1 and 168 hours.')
        pending = PostReport.objects.filter(reviewed_at__isnull=True)
        overdue = pending.filter(created_at__lt=timezone.now() - timedelta(hours=hours)).count()
        self.stdout.write(f'Open reports: {pending.count()}; overdue: {overdue}.')
        if overdue:
            raise CommandError('Moderation review deadline exceeded. A human must review the admin queue.')
