"""Bounded shadow evaluation; disabled unless an operator configures a detector."""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils.module_loading import import_string

from core.local_moderation_queue import enqueue_shadow, process_one


class Command(BaseCommand):
    help = 'Run at most 10 shadow scans; never changes post visibility.'

    def add_arguments(self, parser):
        parser.add_argument('--post', type=int, action='append', default=[])
        parser.add_argument('--limit', type=int, default=1)

    def handle(self, *args, **options):
        if not getattr(settings, 'LOCAL_MODERATION_SHADOW_ENABLED', False):
            raise CommandError('Shadow moderation is disabled; no work was enqueued.')
        if not 1 <= options['limit'] <= 10 or len(options['post']) > 10:
            raise CommandError('Use a limit and explicit post batch of at most 10.')
        factory_path = getattr(settings, 'LOCAL_MODERATION_DETECTOR_FACTORY', '')
        if not factory_path:
            raise CommandError('No validated detector configured; nothing was scanned.')
        # Initialize once per bounded batch, not for every post or web request.
        detector = import_string(factory_path)()
        if not callable(detector):
            raise CommandError('Detector factory did not return a callable.')
        for post_id in set(options['post']):
            enqueue_shadow(post_id)
        count = 0
        for _ in range(options['limit']):
            if not process_one(detector):
                break
            count += 1
        self.stdout.write(f'Shadow jobs attempted: {count}. No publication decisions applied.')
