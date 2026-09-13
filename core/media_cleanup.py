"""Delete owned media after commit; retain failed jobs for operational retry."""

import logging

from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import Q
from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import PendingMediaDeletion, Post, RepbaseUser

logger = logging.getLogger(__name__)


def delete_pending_media(job_id, using='default'):
    with transaction.atomic(using=using):
        return _delete_pending_media(job_id, using)


def _delete_pending_media(job_id, using):
    job = PendingMediaDeletion.objects.using(using).select_for_update().filter(pk=job_id).first()
    if job is None:
        return True
    # Never remove a file that another surviving row still references.
    if (RepbaseUser.objects.using(using).filter(profile_photo=job.name).exists()
            or Post.objects.using(using).filter(Q(image=job.name) | Q(feed_image=job.name)).exists()):
        job.delete(using=using)
        return True
    try:
        default_storage.delete(job.name)
    except Exception:
        logger.exception('Media cleanup failed for job %s; retained for retry', job_id)
        return False
    job.delete(using=using)
    return True


@receiver(post_delete, sender=Post)
@receiver(post_delete, sender=RepbaseUser)
def queue_deleted_media(sender, instance, using, **kwargs):
    fields = ('image', 'feed_image') if sender is Post else ('profile_photo',)
    for field in fields:
        uploaded = getattr(instance, field)
        if not uploaded:
            continue
        name = uploaded.name
        uploaded.close()
        job, _ = PendingMediaDeletion.objects.using(using).get_or_create(name=name)
        transaction.on_commit(
            lambda job_id=job.pk: delete_pending_media(job_id, using),
            using=using,
            robust=True,
        )
