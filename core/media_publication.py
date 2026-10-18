"""One locked decision path for automated and human media approval.

Upload/worker integration must use these operations rather than editing fields.
This module alone does not enable asynchronous moderation on existing uploads.
"""
import uuid
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from .access import AccessConflict, require
from .access_models import AccessAudit, AccessPolicyLock
from .models import Post, PostVideo, Notification


@transaction.atomic
def mark_pending(post_id):
    post = Post.objects.select_for_update().get(pk=post_id)
    post.moderation_generation = uuid.uuid4()
    post.moderation_status = 'pending'
    post.published_at = post.reviewed_at = None
    post.save(update_fields=['moderation_generation', 'moderation_status', 'published_at', 'reviewed_at'])
    return post.moderation_generation


@transaction.atomic
def decide(post_id, generation, decision, *, actor=None, reason=''):
    if decision not in ('approved', 'flagged', 'rejected'):
        raise ValidationError('Invalid media moderation decision')
    if actor is not None:
        AccessPolicyLock.objects.select_for_update().get(pk=1)
        actor = get_user_model().objects.get(pk=actor.pk)
        require(actor, 'moderate')
        if not actor.is_active or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
            raise PermissionDenied('An active reviewer and audit reason are required')
    post = Post.objects.select_for_update().get(pk=post_id)
    if actor is not None and post.author.user_id == actor.pk:
        raise PermissionDenied('Another moderator must review your post')
    if str(post.moderation_generation) != str(generation):
        raise AccessConflict('This content has changed; review its current version')
    if post.moderation_child_hold:
        raise PermissionDenied('Child-safety holds require the separate safety workflow')
    if post.moderation_status == decision:
        return post  # Duplicate approval cannot refresh age or repeat audit/event.
    if post.moderation_status not in ('pending', 'flagged'):
        raise AccessConflict('This submission already has a final decision')
    if actor is None and post.moderation_status != 'pending':
        raise AccessConflict('Flagged content requires a moderator')
    if decision == 'approved' and post.is_hidden:
        raise PermissionDenied('Approval cannot undo a moderator takedown')
    video = getattr(post, 'video', None)
    if decision == 'approved' and video and not video.file and video.processing_state != 'ready':
        raise AccessConflict('The video is not ready for review')
    before = {'moderation_status': post.moderation_status}
    post.moderation_status = decision
    post.published_at = timezone.now() if decision == 'approved' else None
    if actor is not None:
        post.reviewed_at = timezone.now()
    post.save(update_fields=['moderation_status', 'published_at', 'reviewed_at'])
    if decision in ('approved', 'rejected'):
        PostVideo.objects.filter(post=post).update(status=decision,
            reviewed_at=post.reviewed_at, reviewed_by=actor)
    if decision == 'rejected':
        # A system-kind message anchored to the recipient, never the reviewer's
        # identity or confidential decision notes. The post lock deduplicates it.
        notice = Notification.objects.create(recipient=post.author, actor=post.author,
            kind=Notification.Kind.MODERATION_REJECTED, post=post)
        from .push import enqueue_notification
        enqueue_notification(notice)
    AccessAudit.objects.create(actor=actor, target=post.author.user,
        action='media_' + decision, subject=f'post:{post.pk}', before=before,
        after={'moderation_status': decision, 'generation': str(generation),
               'published_at': post.published_at.isoformat() if post.published_at else None},
        reason=reason[:500] if actor else 'Automated sexual-content screening')
    return post
