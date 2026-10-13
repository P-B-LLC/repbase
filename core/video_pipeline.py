import logging
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
from django.conf import settings
from django.db import transaction, DatabaseError, connection
from django.db.models import F, Q, Sum
from django.utils import timezone
from .models import Post, PostVideo
from .video_jobs_models import VideoJob, VideoStoredObject, VideoMetric, VideoDailyUpload
from . import video_store as store
from .video_transcode import transcode, InvalidVideo

logger = logging.getLogger(__name__)


def fail_upload(job):
    if job is not None:
        VideoJob.objects.filter(pk=job.pk, state='uploading').update(state='failed', error_code='upload_failed', finished_at=timezone.now())
        PostVideo.objects.filter(pk=job.video_id, processing_state='uploading').update(processing_state='failed', processing_error='upload_failed')
        VideoStoredObject.objects.filter(job=job, retained=False).update(expires_at=timezone.now())


def queue_local(post, received):
    """Store a bounded raw upload. Encoding only happens in the worker."""
    from rest_framework.exceptions import ValidationError
    from .post_publication import PublicationMediaError
    job = None
    try:
        job, _ = begin(post, received.size, received.declared_type)
        metric('upload_started')
        store.write(job.source_key, received.file)
        complete(job.pk, post.author.user_id)
        return job
    except ValueError as error:
        fail_upload(job)
        metric('upload_rejected')
        raise ValidationError({'video': str(error)})
    except Exception as error:
        fail_upload(job)
        metric('upload_failed')
        raise PublicationMediaError('Video storage unavailable.') from error
    finally:
        received.file.close()


def metric(event, milliseconds=0, size=0):
    try:
        with transaction.atomic():
            row, _ = VideoMetric.objects.get_or_create(day=timezone.now().date(), event=event)
            VideoMetric.objects.filter(pk=row.pk).update(count=F('count') + 1,
                total_ms=F('total_ms') + max(0, milliseconds), total_bytes=F('total_bytes') + max(0, size))
    except DatabaseError:
        logger.warning('Video metric write failed.')


@transaction.atomic
def begin(post, size, content_type):
    if not 0 < size <= settings.VIDEO_DIRECT_MAX_BYTES:
        raise ValueError('The clip exceeds the video size limit.')
    if settings.VIDEO_STORAGE_BACKEND == 'local':
        if connection.vendor == 'postgresql':
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_xact_lock(%s)', [742681905])
        if VideoJob.objects.filter(state__in=['uploading', 'queued', 'processing']).count() >= 10:
            raise ValueError('Video processing is busy. Please try again later.')
        stored = VideoStoredObject.objects.aggregate(total=Sum('size_bytes'))['total'] or 0
        pending = VideoJob.objects.filter(state__in=['uploading', 'queued', 'processing']).count()
        reserved = 2 * settings.VIDEO_DIRECT_MAX_BYTES + 1024 * 1024
        if stored + (pending + 1) * reserved > settings.VIDEO_LOCAL_STORAGE_LIMIT or store.available() < settings.VIDEO_LOCAL_MIN_FREE + (pending + 1) * reserved:
            raise ValueError('Video storage is full. Please try again later.')
    # Serialize per author, so concurrent requests cannot overrun pending quota.
    from django.contrib.auth import get_user_model
    get_user_model().objects.select_for_update().get(pk=post.author.user_id)
    post = Post.objects.select_for_update().get(pk=post.pk)
    if post.kind == Post.Kind.REPOST or PostVideo.objects.filter(post=post).exists():
        raise ValueError('Remove the existing clip before adding another.')
    active = VideoJob.objects.filter(video__post__author=post.author,
        state__in=['uploading', 'queued', 'processing']).count()
    if active >= 3:
        raise ValueError('Finish your pending uploads before starting another.')
    if settings.VIDEO_STORAGE_BACKEND == 'local':
        if active >= 1:
            raise ValueError('Finish your pending video before uploading another.')
        budget, _ = VideoDailyUpload.objects.get_or_create(owner=post.author, day=timezone.now().date())
        if budget.count >= 5:
            raise ValueError('The beta allows five video uploads per day.')
        budget.count += 1
        budget.save(update_fields=['count'])
    job_id = uuid.uuid4()
    source = f'video-incoming/{job_id}/source'
    video = PostVideo.objects.create(post=post, file='', content_type='video/mp4', codec='avc1',
        duration_ms=0, width=0, height=0, size_bytes=0, processing_state='uploading')
    job = VideoJob.objects.create(id=job_id, video=video, source_key=source,
        expected_bytes=size, declared_type=content_type)
    VideoStoredObject.objects.create(job=job, key=source, size_bytes=size, expires_at=timezone.now() + timedelta(days=1))
    policy = store.upload_policy(source, size, content_type)
    return job, policy


def complete(job_id, owner_id):
    job = VideoJob.objects.get(pk=job_id, video__post__author__user_id=owner_id)
    if job.state != 'uploading':
        return job
    details = store.head(job.source_key)
    if details['ContentLength'] != job.expected_bytes or (settings.VIDEO_STORAGE_BACKEND == 's3' and details.get('ContentType') != job.declared_type):
        metric('upload_rejected')
        raise ValueError('Uploaded file does not match its upload policy.')
    with transaction.atomic():
        job = VideoJob.objects.select_for_update().get(pk=job.pk)
        if job.state == 'uploading':
            job.etag = details['ETag']
            job.state = 'queued'
            job.save(update_fields=['etag', 'state'])
            PostVideo.objects.filter(pk=job.video_id).update(processing_state='processing')
            VideoStoredObject.objects.filter(key=job.source_key).update(size_bytes=job.expected_bytes)
            metric('upload_completed', size=job.expected_bytes)
    return job


def claim():
    now = timezone.now()
    with transaction.atomic():
        jobs = VideoJob.objects.select_for_update(skip_locked=True).filter(
            Q(state='queued', next_attempt_at__isnull=True) | Q(state='queued', next_attempt_at__lte=now)
            | Q(state='processing', lease_until__lt=now)).order_by('created_at')
        job = jobs.first()
        if job is None:
            return None
        if job.attempts >= settings.VIDEO_MAX_ATTEMPTS:
            job.state = 'failed'
            job.error_code = 'attempts_exhausted'
            job.finished_at = now
            job.save(update_fields=['state', 'error_code', 'finished_at'])
            PostVideo.objects.filter(pk=job.video_id).update(processing_state='failed', processing_error=job.error_code)
            return None
        job.state = 'processing'
        job.attempts += 1
        job.lease = uuid.uuid4()
        job.lease_until = now + timedelta(seconds=settings.VIDEO_LEASE_SECONDS)
        job.started_at = now
        job.save(update_fields=['state', 'attempts', 'lease', 'lease_until', 'started_at'])
        return job


def process(job):
    lease = job.lease
    started = timezone.now()
    try:
        with tempfile.TemporaryDirectory(prefix='rytivo-video-') as directory:
            source = Path(directory) / 'source.mp4'
            size = store.download(job.source_key, job.etag, source, settings.VIDEO_DIRECT_MAX_BYTES)
            if size != job.expected_bytes:
                raise InvalidVideo('source_changed')
            outputs, thumbnail = transcode(source, directory)
            variants = []
            for output in outputs:
                key = f'video-ready/{job.pk}/{lease}/{output["path"].name}'
                VideoStoredObject.objects.create(job=job, key=key, expires_at=timezone.now() + timedelta(days=1), size_bytes=output['size_bytes'])
                store.upload(key, output['path'], 'video/mp4')
                variants.append({k: output[k] for k in ('width', 'height', 'size_bytes') } | {'key': key})
            thumb_key = f'video-ready/{job.pk}/{lease}/thumbnail.jpg'
            VideoStoredObject.objects.create(job=job, key=thumb_key, expires_at=timezone.now() + timedelta(days=1), size_bytes=thumbnail.stat().st_size)
            store.upload(thumb_key, thumbnail, 'image/jpeg')
            with transaction.atomic():
                current = VideoJob.objects.select_for_update().filter(pk=job.pk, state='processing', lease=lease).first()
                if current is None:
                    return False  # Deleted or reclaimed; reserved outputs get cleaned.
                approved = settings.VIDEO_MODERATION_BYPASS
                video = current.video
                video.processing_state = 'ready'
                video.processing_error = ''
                video.status = 'approved' if approved else 'pending'
                video.variants = variants
                video.thumbnail_key = thumb_key
                video.width, video.height = outputs[0]['width'], outputs[0]['height']
                video.duration_ms, video.has_audio = outputs[0]['duration_ms'], outputs[0]['has_audio']
                video.size_bytes = sum(v['size_bytes'] for v in variants)
                video.save()
                current.state = 'ready'
                current.finished_at = timezone.now()
                current.error_code = ''
                current.save(update_fields=['state', 'finished_at', 'error_code'])
                VideoStoredObject.objects.filter(job=current, key__in=[v['key'] for v in variants] + [thumb_key]).update(retained=True)
            elapsed = round((timezone.now() - started).total_seconds() * 1000)
            metric('processing_ready', elapsed, video.size_bytes)
            if approved:
                metric('moderation_bypassed')
            return True
    except Exception as error:
        permanent = isinstance(error, (InvalidVideo, ValueError))
        error_code = str(error)[:40] if isinstance(error, InvalidVideo) else 'processing_failed'
        with transaction.atomic():
            current = VideoJob.objects.select_for_update().filter(pk=job.pk, state='processing', lease=lease).first()
            if current is not None:
                current.state = 'failed' if permanent or current.attempts >= settings.VIDEO_MAX_ATTEMPTS else 'queued'
                current.error_code = error_code
                current.next_attempt_at = timezone.now() + timedelta(seconds=60 * current.attempts)
                current.finished_at = timezone.now() if current.state == 'failed' else None
                current.save(update_fields=['state', 'error_code', 'next_attempt_at', 'finished_at'])
                PostVideo.objects.filter(pk=current.video_id).update(
                    processing_state='failed' if current.state == 'failed' else 'processing', processing_error=error_code)
        metric('processing_failed', round((timezone.now() - started).total_seconds() * 1000))
        logger.warning('Video processing attempt failed: job=%s category=%s', job.pk, error_code)
        return False


def cleanup(limit=100):
    now = timezone.now()
    expired = VideoJob.objects.filter(state='uploading', created_at__lt=now - timedelta(days=1))
    PostVideo.objects.filter(job__in=expired).update(processing_state='failed', processing_error='upload_expired')
    expired.update(state='failed', error_code='upload_expired', finished_at=now)
    VideoMetric.objects.filter(day__lt=(now - timedelta(days=90)).date()).delete()
    VideoDailyUpload.objects.filter(day__lt=(now - timedelta(days=2)).date()).delete()
    count = 0
    for pk in VideoStoredObject.objects.filter(expires_at__lt=now).filter(
        Q(job__isnull=True) | Q(retained=False)).values_list('pk', flat=True)[:limit]:
        with transaction.atomic():
            obj = VideoStoredObject.objects.select_for_update().filter(pk=pk).first()
            if obj is None or (obj.retained and obj.job_id):
                continue
            try:
                store.delete(obj.key)
            except Exception:
                metric('cleanup_failed')
                continue
            obj.delete()
            count += 1
    return count
