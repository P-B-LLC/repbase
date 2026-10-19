"""Durable image/video screening. Network and decoding run outside row locks."""
from datetime import timedelta
import math
from pathlib import Path
import tempfile
import uuid

from django.conf import settings
from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import MediaScreeningJob, Post
from .media_publication import mark_pending, decide
from .openai_media_moderation import screen_image, MediaScreeningUnavailable, MediaSafetyHold


class Superseded(Exception):
    pass


def submission_consent(request):
    from .moderation import consent_given, ModerationConsentRequired
    if not consent_given(request):
        raise ModerationConsentRequired()
    return settings.MODERATION_CONSENT_VERSION


@transaction.atomic
def enqueue(post_id, *, consent_version, threshold):
    if consent_version != settings.MODERATION_CONSENT_VERSION:
        raise ValueError('Current moderation consent is required')
    if type(threshold) not in (float, int) or not math.isfinite(threshold) or not 0 < threshold <= 1:
        raise ValueError('Configure a sexual-content threshold in (0, 1]')
    generation = mark_pending(post_id)
    from .models import PostVideo
    waiting = PostVideo.objects.filter(post_id=post_id, file='',
        processing_state__in=['uploading', 'processing']).exists()
    job, _ = MediaScreeningJob.objects.update_or_create(post_id=post_id, defaults=dict(
        generation=generation, consent_version=consent_version, threshold=threshold,
        state='waiting' if waiting else 'queued', attempts=0, lease=None, lease_until=None, error_code='', evidence={},
        next_attempt_at=timezone.now()))
    return job


@transaction.atomic
def claim():
    now = timezone.now()
    job = (MediaScreeningJob.objects.select_for_update(skip_locked=True)
           .filter(Q(state='queued', next_attempt_at__lte=now)
                   | Q(state='processing', lease_until__lt=now))
           .order_by('next_attempt_at', 'pk').first())
    if job is None:
        return None
    if job.attempts >= 5:
        job.state, job.error_code = 'failed', 'attempts_exhausted'
        job.save(update_fields=['state', 'error_code'])
        return None
    job.state, job.lease = 'processing', uuid.uuid4()
    job.lease_until = now + timedelta(minutes=15)
    job.attempts += 1
    job.save(update_fields=['state', 'lease', 'lease_until', 'attempts'])
    return job


def current_post(job):
    # Check again before each external request, not just at claim time.
    live = MediaScreeningJob.objects.filter(pk=job.pk, generation=job.generation,
        lease=job.lease, state='processing', lease_until__gt=timezone.now())
    if not live.update(lease_until=timezone.now() + timedelta(minutes=15)):
        raise Superseded()
    post = Post.objects.select_related('author').get(pk=job.post_id)
    if post.moderation_generation != job.generation or post.moderation_status != 'pending':
        raise Superseded()
    if post.moderation_child_hold:
        raise MediaSafetyHold()
    if job.consent_version != settings.MODERATION_CONSENT_VERSION:
        raise MediaScreeningUnavailable()
    from .models import ModerationConsent
    choice = ModerationConsent.objects.filter(user_id=post.author.user_id).first()
    if choice is not None and (not choice.granted or
            choice.version != settings.MODERATION_ACCOUNT_CONSENT_VERSION):
        raise Superseded()
    return post


def process(job):
    peak, count, model = 0.0, 0, ''

    def check(data):
        nonlocal peak, count, model
        post = current_post(job)
        result = screen_image(data, threshold=job.threshold, consent_confirmed=True,
                              child_safety_hold=post.moderation_child_hold)
        peak, count, model = max(peak, result.sexual_score), count + 1, result.model
        return result.flagged

    try:
        post = current_post(job)
        flagged = False
        if post.image:
            with default_storage.open(post.image.name, 'rb') as source:
                flagged = check(source.read(20 * 1024 * 1024 + 1))
        video = getattr(post, 'video', None)
        if video and not flagged:
            from .moderation_frames import sampled_frames
            from . import video_store
            with tempfile.TemporaryDirectory(prefix='rytivo-screen-') as folder:
                source = Path(folder) / 'source.mp4'
                if video.variants:
                    if video.processing_state != 'ready':
                        raise MediaScreeningUnavailable()
                    variant = video.variants[0]
                    head = video_store.head(variant['key'])
                    video_store.download(variant['key'], head['ETag'], source,
                                         settings.VIDEO_DIRECT_MAX_BYTES)
                elif video.file:
                    total = 0
                    with default_storage.open(video.file.name, 'rb') as incoming, source.open('wb') as outgoing:
                        while chunk := incoming.read(1024 * 1024):
                            total += len(chunk)
                            if total > settings.VIDEO_DIRECT_MAX_BYTES:
                                raise ValueError('video_too_large')
                            outgoing.write(chunk)
                else:
                    raise MediaScreeningUnavailable()
                with sampled_frames(source) as frames:
                    for frame in frames:
                        if check(frame.read_bytes()):
                            flagged = True
                            break
        if not count:
            raise MediaScreeningUnavailable()
        with transaction.atomic():
            # Match enqueue's post -> job lock order; expired workers cannot publish.
            Post.objects.select_for_update().get(pk=job.post_id)
            live = MediaScreeningJob.objects.select_for_update().filter(pk=job.pk,
                generation=job.generation, lease=job.lease, state='processing',
                lease_until__gt=timezone.now()).first()
            if live is None:
                raise Superseded()
            decide(job.post_id, job.generation, 'flagged' if flagged else 'approved')
            live.state = 'done'
            live.error_code = ''
            live.evidence = dict(max_sexual_score=peak, checked_images=count, model=model,
                                 threshold=job.threshold)
            live.save(update_fields=['state', 'error_code', 'evidence'])
        return True
    except (Superseded, Post.DoesNotExist):
        MediaScreeningJob.objects.filter(pk=job.pk, generation=job.generation,
            lease=job.lease, state='processing').update(state='cancelled', error_code='superseded')
        return False
    except Exception as error:
        hold = isinstance(error, MediaSafetyHold)
        retry = isinstance(error, MediaScreeningUnavailable)
        delay = max(60 * 2 ** (job.attempts - 1), getattr(error, 'retry_after', 60))
        MediaScreeningJob.objects.filter(pk=job.pk, generation=job.generation,
            lease=job.lease, state='processing').update(
                state='queued' if retry and job.attempts < 5 else 'failed',
                error_code='safety_hold' if hold else 'screening_unavailable',
                next_attempt_at=timezone.now() + timedelta(seconds=min(delay, 3600)))
        return False  # Never publish on malformed input, timeout, or exhausted retries.
