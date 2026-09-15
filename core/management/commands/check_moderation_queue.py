"""Run hourly; alert on nonzero exit. No report content in scheduler logs."""
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.models import PostReport, CommentReport


class Command(BaseCommand):
    help = 'Report overdue moderation work; does not resolve reports or email user content.'

    def add_arguments(self, parser):
        parser.add_argument('--max-age-hours', type=int, default=24)

    def handle(self, *args, **options):
        hours = options['max_age_hours']
        if not 1 <= hours <= 168:
            raise CommandError('Use a review deadline between 1 and 168 hours.')
        cutoff = timezone.now() - timedelta(hours=hours)
        queues = [model.objects.filter(reviewed_at__isnull=True) for model in (PostReport, CommentReport)]
        overdue = sum(queue.filter(created_at__lt=cutoff).count() for queue in queues)
        self.stdout.write(f'Open reports: {sum(queue.count() for queue in queues)}; overdue: {overdue}.')
        if overdue:
            raise CommandError('Moderation review deadline exceeded. A human must review the admin queue.')
