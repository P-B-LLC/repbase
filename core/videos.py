"""Attaching a clip to a post: receiving it, checking it, storing it.

The clip arrives as the raw request body with a video Content-Type, not as
base64 inside JSON the way photos do. A photo is a few megabytes and base64
costs a third on top; a clip is tens of megabytes, and a third on top of that,
held in memory to be decoded, is a cost per upload this server does not need
to pay. The body is streamed to a temporary file as it arrives, and the limit
is enforced while reading -- an oversized upload is refused at the byte that
crosses the line, not after the whole thing has been accepted.

What is accepted is decided by `core.uploads.normalise_video` from the bytes.
The Content-Type only has to say "a video"; whether it is MP4 or QuickTime,
and so how it is stored and served, is read from the file.
"""

import tempfile
import uuid

from django.conf import settings
from django.core.files.base import File
from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import APIException, UnsupportedMediaType
from rest_framework.parsers import BaseParser

from .models import PostVideo
from .post_publication import staged_files
from .uploads import normalise_video

#: What a client may say it is sending. Anything else is refused before a
#: byte of the body is read. `video/x-m4v` is what some browsers call an
#: Apple-exported MP4; the bytes decide what it is either way.
DECLARED_VIDEO_TYPES = frozenset({"video/mp4", "video/quicktime", "video/x-m4v"})

#: Read in pieces this size. Large enough that a 25 MB upload is a hundred
#: reads, small enough that crossing the limit is noticed within a quarter
#: megabyte of it.
_CHUNK = 256 * 1024


class VideoTooLarge(APIException):
    status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
    default_code = "video_too_large"

    def __init__(self):
        megabytes = settings.VIDEO_MAX_BYTES // (1024 * 1024)
        super().__init__({"video": f"That video is larger than {megabytes} MB."})


class ReceivedVideo:
    """The body of an upload, spooled to a temporary file."""

    def __init__(self, file, size, declared_type):
        self.file = file
        self.size = size
        self.declared_type = declared_type


class VideoUploadParser(BaseParser):
    """Stream a video body to a temporary file, refusing it past the limit."""

    media_type = "video/*"

    def parse(self, stream, media_type=None, parser_context=None):
        declared = (media_type or "").split(";")[0].strip().lower()
        if declared not in DECLARED_VIDEO_TYPES:
            raise UnsupportedMediaType(media_type)

        limit = settings.VIDEO_MAX_BYTES
        request = parser_context["request"]
        try:
            announced = int(request.META.get("CONTENT_LENGTH") or 0)
        except (TypeError, ValueError):
            announced = 0
        if announced > limit:
            # Refused on the header alone. Reading a body already known to be
            # too large would spend the bandwidth the limit exists to save.
            raise VideoTooLarge()

        spooled = tempfile.SpooledTemporaryFile(max_size=4 * 1024 * 1024)
        received = 0
        try:
            while True:
                chunk = stream.read(_CHUNK)
                if not chunk:
                    break
                received += len(chunk)
                if received > limit:
                    raise VideoTooLarge()
                spooled.write(chunk)
        except BaseException:
            spooled.close()
            raise
        spooled.seek(0)
        return ReceivedVideo(spooled, received, declared)


def review_required():
    """Whether a new clip waits for a person before anyone else sees it.

    Follows moderation unless set explicitly: the classifier that checks
    photos and text cannot read video, so wherever moderation is on, a clip
    has nobody checking it except a human.
    """
    configured = getattr(settings, "VIDEO_REVIEW_REQUIRED", None)
    return settings.MODERATION_ENABLED if configured is None else bool(configured)


def attach_video(post, received):
    """Check a received clip and make it the post's video.

    Replaces any clip already there. The old file is queued for deletion only
    once the new row has committed, so a failure part way leaves the post with
    the clip it had rather than with none.
    """
    try:
        info, clean = normalise_video(
            received.file,
            received.size,
            max_seconds=settings.VIDEO_MAX_SECONDS,
            max_dimension=settings.VIDEO_MAX_DIMENSION,
            max_pixels=settings.VIDEO_MAX_PIXELS,
        )
    finally:
        received.file.close()

    approved = not review_required()
    name = f"post-videos/{uuid.uuid4().hex}{info.extension}"
    try:
        size = clean.seek(0, 2)
        clean.seek(0)
        with staged_files([("file", name, File(clean, name=name))]) as stored:
            with transaction.atomic():
                previous = PostVideo.objects.select_for_update().filter(post=post).first()
                if previous is not None:
                    # Deleting the row queues its file through the same
                    # post_delete receiver every other removal uses.
                    previous.delete()
                video = PostVideo.objects.create(
                    post=post,
                    file=stored["file"],
                    content_type=info.content_type,
                    codec=info.codec,
                    duration_ms=info.duration_ms,
                    width=info.width,
                    height=info.height,
                    size_bytes=size,
                    has_audio=info.has_audio,
                    status=PostVideo.Status.APPROVED if approved else PostVideo.Status.PENDING,
                    reviewed_at=timezone.now() if approved else None,
                )
    finally:
        clean.close()
    return video


def remove_video(post):
    """Take the clip off a post. Nothing to remove is not an error."""
    PostVideo.objects.filter(post=post).delete()


def set_review(videos, status_value, reviewer):
    """Record a moderator's decision on clips, for the admin actions."""
    return videos.update(
        status=status_value, reviewed_at=timezone.now(), reviewed_by=reviewer
    )

