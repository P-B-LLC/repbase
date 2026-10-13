import json
from contextlib import contextmanager
from pathlib import Path
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Sum
from core.video_pipeline import claim, process, cleanup
from core.video_jobs_models import VideoJob, VideoStoredObject, VideoMetric


@contextmanager
def worker_lock():
    if settings.VIDEO_STORAGE_BACKEND != 'local':
        yield True
        return
    root = Path(settings.VIDEO_LOCAL_ROOT)
    root.mkdir(parents=True, exist_ok=True, mode=0o750)
    with (root / '.worker.lock').open('a+b') as handle:
        import os
        if os.name == 'nt':
            import msvcrt
            if handle.tell() == 0:
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                yield False
                return
            try:
                yield True
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


class Command(BaseCommand):
    help = 'Process a bounded batch of video jobs on a dedicated worker.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=1)
        parser.add_argument('--cleanup', action='store_true')
        parser.add_argument('--stats', action='store_true')

    def handle(self, *args, **options):
        with worker_lock() as acquired:
            if not acquired:
                self.stdout.write('Another video worker is running; no jobs claimed.')
                return
            return self.run_batch(options)

    def run_batch(self, options):
        if options['stats']:
            states = {state: VideoJob.objects.filter(state=state).count() for state in ('uploading', 'queued', 'processing', 'ready', 'failed')}
            self.stdout.write(json.dumps({'states': states,
                'tracked_storage_bytes': VideoStoredObject.objects.aggregate(total=Sum('size_bytes'))['total'] or 0,
                'daily_metrics': list(VideoMetric.objects.order_by('-day')[:100].values())}, default=str))
            return
        if not settings.VIDEO_PIPELINE_ENABLED or (settings.VIDEO_STORAGE_BACKEND == 's3' and not settings.VIDEO_STORAGE_BUCKET):
            raise CommandError('Configure video storage and enable the pipeline first.')
        if not 1 <= options['limit'] <= 20:
            raise CommandError('Choose a batch limit between 1 and 20.')
        if options['cleanup']:
            self.stdout.write(f'Cleaned {cleanup()} expired objects.')
        for _ in range(options['limit']):
            job = claim()
            if job is None:
                break
            self.stdout.write(f'Job {job.pk}: {"ready" if process(job) else "retry/failed"}')
