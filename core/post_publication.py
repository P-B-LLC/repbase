"""Stage media before publishing; durable cleanup survives a failed request."""
from contextlib import contextmanager
import uuid

from django.core.files.base import ContentFile, File
from django.core.files.storage import default_storage
from django.db import transaction

from .media_cleanup import delete_pending_media, queue_media_deletion
from .models import PendingMediaDeletion, RepbaseUser, profile_photo_path
from .photos import feed_variant


class PublicationMediaError(Exception):
    pass


@contextmanager
def publication_media(decoded, extension):
    files = []
    if decoded is not None:
        if not callable(getattr(default_storage, 'save_reserved', None)):
            # Fail before uploading anything. Guessing the final filename
            # after upload cannot recover from a crash inside storage.save().
            raise PublicationMediaError('Storage does not support reserved upload names.')
        files.append(('image', f'post-photos/{uuid.uuid4().hex}{extension}', decoded))
        smaller = feed_variant(decoded)
        if smaller is not None:
            files.append(('feed_image', f'post-photos/feed/{uuid.uuid4().hex}.jpg', smaller))
    with staged_files(files) as fields:
        yield fields


@contextmanager
def staged_files(files):
    """Write server-named files so that no crash can leave one untracked.

    `files` is a list of (field, name, content), where content is bytes or an
    open file. Shared by photos and clips: the ordering below is the whole
    guarantee, and two copies of it would be two chances to get it wrong.
    """
    if files and not callable(getattr(default_storage, 'save_reserved', None)):
        raise PublicationMediaError('Storage does not support reserved upload names.')
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
                    content = data if isinstance(data, File) else ContentFile(data)
                    actual = default_storage.save_reserved(name, content)
                    written.append(actual)
                    if actual != name:
                        raise PublicationMediaError('Storage violated the reserved-name contract.')
                    fields[field] = actual
                except Exception as error:
                    raise PublicationMediaError('Media storage is unavailable.') from error
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


def replace_profile_photo(profile, decoded, extension):
    """Make `decoded` the profile's photo, and queue the old one for deletion.

    Staged like a post's photo, under a lock on the profile. It used to delete
    the old file and save the new one with no lock, so uploads sent at the same
    time each wrote a file and only the last was kept on the row: ten at once
    left nine files on disk that no row, and so no cleanup, would ever reach.
    """
    name = profile_photo_path(profile, f'photo{extension}')
    with staged_files([('profile_photo', name, decoded)]) as stored:
        locked = RepbaseUser.objects.select_for_update().get(pk=profile.pk)
        previous = locked.profile_photo.name if locked.profile_photo else None
        locked.profile_photo = stored['profile_photo']
        locked.save(update_fields=['profile_photo', 'updated_at'])
        if previous:
            queue_media_deletion(previous)
    return locked


def remove_profile_photo(profile):
    """Clear the profile's photo; the file goes once that has committed."""
    with transaction.atomic():
        locked = RepbaseUser.objects.select_for_update().get(pk=profile.pk)
        previous = locked.profile_photo.name if locked.profile_photo else None
        if previous:
            locked.profile_photo = None
            locked.save(update_fields=['profile_photo', 'updated_at'])
            queue_media_deletion(previous)
    return locked
