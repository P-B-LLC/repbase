"""Run hourly; alert on nonzero exit. No report content in scheduler logs."""
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.models import CommentReport, PostReport, PostVideo


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
        # Clips wait for a person before anybody but their author sees them,
        # so an unreviewed one is somebody's post sitting half-published.
        clips = PostVideo.objects.filter(status=PostVideo.Status.PENDING)
        overdue = (
            sum(queue.filter(created_at__lt=cutoff).count() for queue in queues)
            + clips.filter(created_at__lt=cutoff).count()
        )
        self.stdout.write(
            f'Open reports: {sum(queue.count() for queue in queues)}; '
            f'clips awaiting review: {clips.count()}; overdue: {overdue}.'
        )
        if overdue:
            raise CommandError('Moderation review deadline exceeded. A human must review the admin queue.')
