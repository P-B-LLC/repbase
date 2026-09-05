from django.core.management.base import BaseCommand, CommandError

from core.media_cleanup import delete_pending_media
from core.models import PendingMediaDeletion


class Command(BaseCommand):
    help = 'Retry committed account/post media deletions that failed in storage.'

    def handle(self, *args, **options):
        failed = 0
        for job_id in PendingMediaDeletion.objects.values_list('pk', flat=True).iterator():
            if not delete_pending_media(job_id):
                failed += 1
        if failed:
            raise CommandError(f'{failed} media deletion job(s) still need retry.')
        self.stdout.write(self.style.SUCCESS('Pending media deletions processed.'))
