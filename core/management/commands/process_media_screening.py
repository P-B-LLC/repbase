from django.core.management.base import BaseCommand, CommandError
from django.conf import settings
from core.media_screening_jobs import claim, process


class Command(BaseCommand):
    help = 'Process a bounded batch of private media screening jobs.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=10)

    def handle(self, *args, **options):
        if not settings.MEDIA_SCREENING_ENABLED:
            raise CommandError('MEDIA_SCREENING_ENABLED is disabled; no media was sent.')
        if not 1 <= options['limit'] <= 100:
            raise CommandError('limit must be between 1 and 100')
        processed = 0
        for _ in range(options['limit']):
            job = claim()
            if job is None:
                break
            process(job)
            processed += 1
        self.stdout.write(f'Processed {processed} media screening attempts.')
