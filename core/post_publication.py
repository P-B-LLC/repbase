"""Stage media before publishing; durable cleanup survives a failed request."""
from contextlib import contextmanager
import uuid

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction

from .media_cleanup import delete_pending_media
from .models import PendingMediaDeletion
from .photos import feed_variant


class PublicationMediaError(Exception):
    pass


@contextmanager
def publication_media(decoded, extension):
    files = []
    if decoded is not None:
        files.append(('image', f'post-photos/{uuid.uuid4().hex}{extension}', decoded))
        smaller = feed_variant(decoded)
        if smaller is not None:
            files.append(('feed_image', f'post-photos/feed/{uuid.uuid4().hex}.jpg', smaller))
    # Committed BEFORE any storage write. A process crash leaves a discoverable
    # cleanup job, not an untracked upload. Names are server-generated UUIDs.
    jobs = [PendingMediaDeletion.objects.create(name=name) for _, name, _ in files]
    written = []
    try:
        with transaction.atomic():
            # Cleanup workers use the same locks. They cannot delete a file
            # during publication; after success these jobs no longer exist.
            locked = list(PendingMediaDeletion.objects.select_for_update().filter(
                pk__in=[job.pk for job in jobs]).order_by('pk'))
            if len(locked) != len(jobs):
                raise PublicationMediaError('Staging expired before upload started.')
            fields = {}
            for field, name, data in files:
                try:
                    actual = default_storage.save(name, ContentFile(data))
                    written.append(actual)
                    # Storage backends may choose another available filename.
                    PendingMediaDeletion.objects.filter(name=name).update(name=actual)
                    fields[field] = actual
                except Exception as error:
                    raise PublicationMediaError('Photo storage is unavailable.') from error
            yield fields
            PendingMediaDeletion.objects.filter(pk__in=[job.pk for job in jobs]).delete()
    except Exception:
        # This runs AFTER rollback. Keep retry records if immediate cleanup
        # fails, including names changed by the storage backend.
        for name in written:
            PendingMediaDeletion.objects.get_or_create(name=name)
        for name in set([job.name for job in jobs] + written):
            job = PendingMediaDeletion.objects.filter(name=name).first()
            if job is not None:
                delete_pending_media(job.pk)
        raise
