"""Original sounds: a creator's own audio, published once and referenced.

A sound is made by its owner, from one of their own posts, on purpose.
Nothing here turns a clip into a reusable sound because it happens to have
audio: reuse is something a person agrees to, with a permission they choose.

**Stored once.** The audio is extracted from the clip into one file, by a
worker and never during a request (`prepare_pending`, run by the
`prepare_sounds` command). Every post that uses the sound holds a reference
to the row. Nothing is copied per use.

**Taken down in one place.** Removing a sound is a status on the one row. The
posts that used it keep their reference and lose the credit, the audio file
stops being served, nobody new can use it, and those posts leave For You
(`recommendations.eligible_posts`). Reversed, all of that comes back. The
owner can remove and restore their own; what a moderator removed only a
moderator restores.

A commercial music catalogue is a different thing with different rights, and
is not this.
"""

import logging
import subprocess
import tempfile
import uuid
from pathlib import Path

from django.conf import settings
from django.core.files import File
from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import Post, PostVideo, Sound
from .post_publication import staged_files

logger = logging.getLogger(__name__)

#: How many times a sound's audio is tried before it is left alone.
MAX_PREPARE_ATTEMPTS = 3
TITLE_LENGTH = 120


def default_title(author):
    return f"Original sound - @{author.user.username}"[:TITLE_LENGTH]


def publish_sound(author, post, title="", reuse_permission=Post.Reuse.EVERYONE):
    """Make the audio of the author's own post a sound others may use.

    The post has to be theirs, carry a clip that has been approved and has
    audio in it, and not already be set to somebody else's sound -- a clip
    over another person's audio is not this author's sound to publish.
    Asking twice hands back the sound already made.
    """
    if post.author_id != author.pk or post.kind == Post.Kind.REPOST:
        raise ValidationError({"post": "Only your own post can become a sound."})
    video = PostVideo.objects.filter(post=post).first()
    if video is None or video.status != PostVideo.Status.APPROVED or video.processing_state != "ready":
        raise ValidationError({"post": "That post has no approved video to take a sound from."})
    if not video.has_audio:
        raise ValidationError({"post": "That video has no audio."})
    with transaction.atomic():
        locked = Post.objects.select_for_update().get(pk=post.pk)
        existing = Sound.objects.filter(origin_post=locked).first()
        if existing is not None:
            return existing, False
        if locked.sound_id is not None:
            raise ValidationError({"post": "That post is set to somebody else's sound."})
        sound = Sound.objects.create(
            owner=author, origin_post=locked,
            title=(title or "").strip()[:TITLE_LENGTH] or default_title(author),
            duration_ms=video.duration_ms, reuse_permission=reuse_permission,
        )
        locked.sound = sound
        locked.save(update_fields=["sound"])
    return sound, True


def set_removed(sound, removed, *, by_moderator):
    """Take a sound down, or put it back. Returns whether anything changed.

    An owner cannot restore what a moderator removed.
    """
    with transaction.atomic():
        locked = Sound.objects.select_for_update().get(pk=sound.pk)
        if removed:
            if locked.status == Sound.Status.REMOVED and (locked.removed_by_moderator or not by_moderator):
                return False
            locked.status = Sound.Status.REMOVED
            locked.removed_at = locked.removed_at or timezone.now()
            locked.removed_by_moderator = by_moderator
        else:
            if locked.status == Sound.Status.ACTIVE:
                return False
            if locked.removed_by_moderator and not by_moderator:
                raise ValidationError({"sound": "A moderator removed this sound."})
            locked.status = Sound.Status.ACTIVE
            locked.removed_at = None
            locked.removed_by_moderator = False
        locked.save(update_fields=["status", "removed_at", "removed_by_moderator"])
    return True


def extract_audio(source, destination):
    """Write `source`'s audio track to `destination` as AAC, and nothing else
    of it: no picture, no location, no device metadata."""
    subprocess.run(
        [settings.VIDEO_FFMPEG, "-nostdin", "-v", "error", "-y", "-threads", "1",
         "-protocol_whitelist", "file", "-i", str(source),
         "-vn", "-map", "0:a:0", "-map_metadata", "-1", "-map_chapters", "-1",
         "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-movflags", "+faststart", str(destination)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=settings.VIDEO_PROCESS_TIMEOUT, check=True,
    )


def prepare_pending(limit=10):
    """Extract the audio for sounds that are waiting for it. Returns
    (prepared, failed). Worker code: never called from a request."""
    prepared = failed = 0
    waiting = list(
        Sound.objects.filter(
            audio_key="", status=Sound.Status.ACTIVE, prepare_attempts__lt=MAX_PREPARE_ATTEMPTS
        ).order_by("created_at")[:limit].values_list("pk", flat=True)
    )
    for pk in waiting:
        if _prepare(pk):
            prepared += 1
        else:
            failed += 1
    return prepared, failed


def _prepare(pk):
    # The attempt is counted before the work, in its own statement, so a
    # worker killed part way has still used one of the sound's tries.
    Sound.objects.filter(pk=pk).update(prepare_attempts=F("prepare_attempts") + 1)
    sound = Sound.objects.select_related("origin_post").filter(pk=pk, audio_key="").first()
    video = PostVideo.objects.filter(post_id=sound.origin_post_id).first() if sound else None
    if sound is None or video is None or not video.file:
        return False
    name = f"sounds/{uuid.uuid4().hex}.m4a"
    try:
        with tempfile.TemporaryDirectory(prefix="rytivo-sound-") as directory:
            source = Path(directory) / "clip"
            with default_storage.open(video.file.name, "rb") as stored, open(source, "wb") as local:
                for chunk in iter(lambda: stored.read(1024 * 1024), b""):
                    local.write(chunk)
            target = Path(directory) / "sound.m4a"
            extract_audio(source, target)
            with open(target, "rb") as audio, staged_files([("audio", name, File(audio, name=name))]) as stored:
                # Only if nobody prepared or removed it meanwhile; otherwise
                # staging rolls back and the file is cleaned up.
                changed = Sound.objects.filter(pk=pk, audio_key="", status=Sound.Status.ACTIVE).update(
                    audio_key=stored["audio"]
                )
                if not changed:
                    raise RuntimeError("sound changed while its audio was being prepared")
        return True
    except Exception:
        logger.warning("sound %s: audio could not be prepared", pk, exc_info=True)
        return False
