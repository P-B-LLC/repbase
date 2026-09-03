"""A feed-sized copy of a photo somebody posted.

The feed asked for originals. They are two to four megabytes each, sent whole
to draw a card a few hundred points tall, so a scroll through Discover spent a
tester's cellular data and felt slow in a way that reads as the app being bad
rather than the pictures being big.

The variant is made once, on upload, and stored beside the original. The
original stays exactly as it was: the detail view opens it, and a photo
somebody posted is theirs, not something to quietly degrade.

Nothing here is allowed to fail a post. Every route out of `feed_variant`
returns bytes or None, and None means "serve the original", which is what the
app did before this existed.
"""

from __future__ import annotations

import io

#: The box a feed photo is fitted inside, in pixels.
#:
#: 1080 wide because the widest phone this runs on is 1206 physical pixels and
#: the difference is invisible on a photograph; 1350 tall because 4:5 is the
#: tallest shape a card draws, and anything taller is letterboxed anyway. The
#: aspect ratio is preserved -- this is a bounding box, not a crop.
FEED_PHOTO_BOX = (1080, 1350)

#: JPEG quality. 82 is the usual place where further reduction starts showing
#: on skin and sky before it saves much.
FEED_PHOTO_QUALITY = 82

#: How much smaller a variant has to be to be worth keeping.
#:
#: A photo already smaller than the box re-encodes to roughly its own size, or
#: larger. Keeping that would mean storing and serving a second copy for no
#: gain, so anything that does not clear this bar is thrown away and the
#: original is served instead.
FEED_PHOTO_WORTH_KEEPING = 0.9


def feed_variant(original: bytes) -> bytes | None:
    """A smaller JPEG of the same picture, or None to serve the original.

    None rather than an exception on every failure path, deliberately. This
    runs inside the request that creates a post: an unreadable file, a format
    Pillow was not built with, or a decompression bomb are all reasons to skip
    the optimisation, and none of them are reasons to refuse somebody's post.
    """
    try:
        from PIL import Image, ImageOps
    except ImportError:  # pragma: no cover - Pillow is a hard dependency
        return None

    try:
        with Image.open(io.BytesIO(original)) as image:
            # Before resizing, not after. A phone records orientation in EXIF
            # rather than in the pixels, and JPEG metadata does not survive
            # what follows -- so a photo taken sideways would come back
            # sideways in the feed and upright in the detail view.
            image = ImageOps.exif_transpose(image)

            image.thumbnail(FEED_PHOTO_BOX, Image.Resampling.LANCZOS)

            # JPEG has no alpha channel and cannot store a palette. Converting
            # is what lets a PNG screenshot through; without it the save below
            # raises and the variant is silently skipped for a whole format.
            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")

            buffer = io.BytesIO()
            image.save(
                buffer,
                format="JPEG",
                quality=FEED_PHOTO_QUALITY,
                optimize=True,
                progressive=True,
            )
    except Exception:
        # Broad on purpose. Pillow raises OSError, ValueError, SyntaxError and
        # DecompressionBombError for different bad inputs, and the answer to
        # every one of them here is the same.
        return None

    smaller = buffer.getvalue()
    if len(smaller) >= len(original) * FEED_PHOTO_WORTH_KEEPING:
        return None
    return smaller
