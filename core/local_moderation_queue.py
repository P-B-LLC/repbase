"""Short DB transactions surrounding (never containing) expensive screening.

Shadow evaluation only. Activation/publication integration is a separate gate.
Inference receives a post ID and must read immutable content; a fingerprint is
checked again before accepting results. No retries cache an allow decision.
"""
import hashlib
import json
import uuid
from datetime import timedelta

from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from .local_moderation_models import LocalModerationJob, LocalModerationCase
from .local_moderation_policy import POLICY_VERSION, Decision, Outcome, evaluate
from .models import Post

MAX_ATTEMPTS = 3
LEASE_SECONDS = 120


def revision(post):
    video = getattr(post, 'video', None)
    data = [post.pk, post.caption, str(post.updated_at), post.image.name,
            post.visibility, post.is_hidden, post.repost_of_id]
    if video:
        data.extend([video.pk, video.file.name, video.processing_state,
                     video.status, video.variants, video.thumbnail_key])
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


@transaction.atomic
def enqueue_shadow(post_id):
    post = Post.objects.select_for_update().get(pk=post_id)
    digest = revision(post)
    job, created = LocalModerationJob.objects.get_or_create(post=post, defaults={
        'revision': digest, 'policy_version': POLICY_VERSION, 'available_at': timezone.now()})
    if not created and (job.revision != digest or job.policy_version != POLICY_VERSION):
        # Previous worker's generation and lease can never commit this result.
        job.revision = digest
        job.policy_version = POLICY_VERSION
        job.generation = uuid.uuid4()
        job.state, job.attempts = 'queued', 0
        job.lease = job.lease_until = None
        job.outcome, job.evidence, job.error_code = '', [], ''
        job.available_at = timezone.now()
        job.save()
        LocalModerationCase.objects.filter(post=post, status='pending').update(status='superseded')
    return job


@transaction.atomic
def claim():
    now = timezone.now()
    candidates = LocalModerationJob.objects.filter(
        Q(state='queued', available_at__lte=now) | Q(state='processing', lease_until__lte=now))
    locked = candidates.select_for_update(skip_locked=connection.features.has_select_for_update_skip_locked)
    job = locked.order_by('available_at', 'pk').first()
    if job is None:
        return None
    if job.attempts >= MAX_ATTEMPTS:
        job.state, job.error_code = 'failed', 'attempts_exhausted'
        job.lease = job.lease_until = None
        job.save()
        return None
    job.state = 'processing'
    job.attempts += 1
    job.lease = uuid.uuid4()
    job.lease_until = now + timedelta(seconds=LEASE_SECONDS)
    job.save()
    return job


@transaction.atomic
def finish(claimed, decision):
    if not isinstance(decision, Decision) or decision != evaluate(
            list(decision.reasons), screening_complete=True):
        raise ValueError('Unvalidated policy result')
    # Lock order matches enqueue: post, then job. Never undo human visibility.
    post = Post.objects.select_for_update().filter(pk=claimed.post_id).first()
    if post is None:
        return False
    job = LocalModerationJob.objects.select_for_update().filter(
        pk=claimed.pk, state='processing', generation=claimed.generation,
        lease=claimed.lease, lease_until__gt=timezone.now()).first()
    if job is None:
        return False
    if job.revision != revision(post) or job.policy_version != POLICY_VERSION:
        job.state, job.error_code = 'stale', 'content_changed'
        job.lease = job.lease_until = None
        job.save()
        return False
    evidence = [{'category': s.category.value, 'finding': s.finding.value,
                 'detector_version': s.detector_version, 'rule_id': s.rule_id}
                for s in decision.reasons]
    job.state, job.outcome, job.evidence = 'complete', decision.outcome.value, evidence
    job.lease = job.lease_until = None
    job.error_code = ''
    job.save()
    if decision.outcome != Outcome.ALLOW:
        LocalModerationCase.objects.get_or_create(generation=job.generation, defaults={
            'post': post, 'revision': job.revision, 'policy_version': job.policy_version,
            'outcome': job.outcome, 'evidence': evidence})
    return True


def fail(claimed):
    # No exception strings, URLs, media or captions in logs/DB error fields.
    return LocalModerationJob.objects.filter(
        pk=claimed.pk, state='processing', generation=claimed.generation,
        lease=claimed.lease).update(
            state='failed' if claimed.attempts >= MAX_ATTEMPTS else 'queued',
            error_code='screening_unavailable', lease=None, lease_until=None,
            available_at=timezone.now() + timedelta(seconds=30 * 2 ** (claimed.attempts - 1)))


def process_one(detector):
    if connection.in_atomic_block:
        raise RuntimeError('Screening must not run inside a database transaction')
    job = claim()
    if job is None:
        return False
    try:
        decision = detector(job.post_id)
        finish(job, decision)
    except Exception:
        fail(job)
    return True
