"""What an uploaded file is allowed to be, decided from its bytes.

Everything here starts from the same position: a filename, an extension and a
Content-Type are things the client chose, so none of them is evidence of what
was sent. The bytes are opened, their structure is walked, and the upload is
judged on what that structure turns out to be.

## Photos: what is kept of an original

A phone writes a great deal into a photo besides the picture. EXIF carries the
GPS fix of where it was taken, the camera's serial number and the owner's name
when one is set; XMP repeats much of that and adds editing history; IPTC
carries captions and names; a JPEG comment can say anything. Every one of
those was stored with the original and served to whoever could see the post.
For a workout photo taken at home, the GPS fix is a home address -- the exact
disclosure this codebase already refuses for route points.

So the original is rewritten with only what the picture needs to display
correctly: the pixels, the colour profile, and the orientation. Nothing is
re-encoded. The compressed image data is copied byte for byte, so the photo
somebody posted is the photo that is served, just without the dossier.

## Videos: what a clip has to be

MP4 and QuickTime, the two containers every phone records into and every
browser and AVPlayer can play. Both are ISO base media files, a tree of
length-prefixed boxes, so one walker reads both. A clip is accepted when:

- the first box is `ftyp` naming an MP4 or QuickTime brand -- HEIC photos and
  AVIF images are ISO media files too, and are not videos;
- there is a picture track coded as H.264 or HEVC, and any sound is in a
  format players decode;
- it is no longer than the configured limit by *every* clock in the file.
  The movie header's duration is one number the file asserts about itself;
  each track's header, its media header, its sample table and its edit list
  are four more, and a clip is as long as the longest of them says;
- its picture is within the dimension limits, by both the track header and
  the sample description;
- it is a whole file: fragmented MP4 is refused, because its length is only
  knowable by reading every fragment, and a truncated file is refused because
  its boxes run past the end.

The walk is bounded throughout -- how deep, how many boxes, how large the
`moov` that is read into memory -- so a hostile file costs a bounded amount
to refuse.

## Videos: what is kept

The same principle as photos. A phone writes the recording location into
`moov/meta` (Apple's `com.apple.quicktime.location.ISO6709`) or `udta/©xyz`
(Android), next to the device model and software version. So the stored file
is rebuilt from the boxes a player needs and nothing else: `ftyp`, `moov`
without its `udta`, `meta` and `uuid` children, and the media data. The media
bytes are copied, not re-encoded, and the chunk offsets in `stco`/`co64` are
moved to match where the media data now sits.
"""

import io
import os
import shutil
import struct
import tempfile
from dataclasses import dataclass

from rest_framework import serializers

from .moderation import MAX_IMAGE_PIXELS


class MetadataError(ValueError):
    """The file's structure could not be walked safely."""


# --------------------------------------------------------------------- images

def check_image_dimensions(width, height, field="image_base64"):
    """Refuse a pixel count that would exhaust memory once decoded.

    Asked of the header alone, before anything is allocated. The moderation
    review copy already refused these, but only when moderation is on -- and
    every derived copy decodes the whole picture regardless.
    """
    if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
        raise serializers.ValidationError(
            {field: "That image is too large. Please use a smaller one."}
        )


#: JPEG markers worth keeping as they are: the Adobe colour-transform flag
#: (APP14, without which some CMYK files decode with the wrong colours), and
#: every structural table. APP0 and APP2 are decided separately: APP0 because
#: JFIF can carry a thumbnail, and APP2 because it holds both the ICC profile
#: (kept) and the multi-picture index (dropped, along with the depth and gain
#: maps it points at past EOI).
_JPEG_KEEP = {
    0xEE,
    0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
    0xC4, 0xCC, 0xDB, 0xDC, 0xDD, 0xDE, 0xDF,
}


def _jfif_without_thumbnail(payload):
    """A JFIF APP0 segment with its embedded thumbnail removed, or None.

    JFIF may carry a small uncompressed preview, and a preview is made from
    the picture before it was cropped. A cropped-out face or street sign that
    survives in the thumbnail is a well-known way photos leak.
    """
    if not payload.startswith(b"JFIF\x00") or len(payload) < 12:
        return None
    rebuilt = payload[:12] + b"\x00\x00"
    return b"\xff\xe0" + struct.pack(">H", len(rebuilt) + 2) + rebuilt


def _exif_orientation(tiff):
    """The Orientation tag from an EXIF TIFF block, or None.

    Read by hand rather than with Pillow because only one tag is wanted, and
    a parser that reads one fixed-size entry cannot be talked into walking
    anything else.
    """
    if len(tiff) < 8 or tiff[:2] not in (b"II", b"MM"):
        return None
    order = "<" if tiff[:2] == b"II" else ">"
    if struct.unpack(order + "H", tiff[2:4])[0] != 42:
        return None
    offset = struct.unpack(order + "I", tiff[4:8])[0]
    if offset + 2 > len(tiff):
        return None
    count = struct.unpack(order + "H", tiff[offset:offset + 2])[0]
    for index in range(min(count, 512)):
        entry = offset + 2 + 12 * index
        if entry + 12 > len(tiff):
            break
        tag, kind = struct.unpack(order + "HH", tiff[entry:entry + 4])
        if tag == 0x0112 and kind == 3:
            value = struct.unpack(order + "H", tiff[entry + 8:entry + 10])[0]
            return value if 1 <= value <= 8 else None
    return None


def _orientation_segment(orientation):
    """An APP1 segment carrying the Orientation tag and nothing else."""
    tiff = (
        b"MM\x00\x2a" + struct.pack(">I", 8)
        + struct.pack(">H", 1)
        + struct.pack(">HHIHH", 0x0112, 3, 1, orientation, 0)
        + struct.pack(">I", 0)
    )
    payload = b"Exif\x00\x00" + tiff
    return b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload


def _next_marker(data, position):
    """Where the next real marker starts, past entropy-coded scan data.

    Inside a scan, 0xFF is only ever followed by 0x00 (a stuffed byte) or a
    restart marker, so the next 0xFF followed by anything else is a marker.
    `find` jumps between candidates rather than walking every byte, which
    matters on a five-megabyte photo.
    """
    while True:
        found = data.find(b"\xff", position)
        if found < 0 or found + 1 >= len(data):
            raise MetadataError("Image data ends before the image does.")
        following = data[found + 1]
        if following == 0x00 or 0xD0 <= following <= 0xD7 or following == 0xFF:
            position = found + 1 if following == 0xFF else found + 2
            continue
        return found


def strip_jpeg_metadata(data):
    """The same JPEG with only what display needs.

    Kept: JFIF, the Adobe flag, the ICC profile, every table and every scan.
    Replaced: EXIF, by a block holding the orientation alone. Dropped: XMP,
    IPTC, comments, unknown application segments, the multi-picture index,
    and anything after the end-of-image marker -- which is where a phone puts
    the extra images that index describes, each with metadata of its own.
    """
    if not data.startswith(b"\xff\xd8"):
        raise MetadataError("Not a JPEG.")
    kept = []
    orientation = None
    position = 2
    seen_scan = False
    while True:
        if position >= len(data) or data[position] != 0xFF:
            raise MetadataError("Expected a marker.")
        while position < len(data) and data[position] == 0xFF:
            position += 1
        if position >= len(data):
            raise MetadataError("Truncated marker.")
        marker = data[position]
        position += 1

        if marker == 0xD9:
            if not seen_scan:
                raise MetadataError("No image data.")
            kept.append(b"\xff\xd9")
            break
        if 0xD0 <= marker <= 0xD7 or marker in (0x01, 0xD8):
            # Standalone markers have no length. A second SOI is malformed.
            if marker == 0xD8:
                raise MetadataError("Nested start of image.")
            kept.append(bytes((0xFF, marker)))
            continue

        if position + 2 > len(data):
            raise MetadataError("Truncated segment.")
        length = struct.unpack(">H", data[position:position + 2])[0]
        if length < 2 or position + length > len(data):
            raise MetadataError("Segment runs past the file.")
        payload = data[position + 2:position + length]
        segment = b"\xff" + bytes((marker,)) + data[position:position + length]
        position += length

        if marker == 0xDA:
            seen_scan = True
            boundary = _next_marker(data, position)
            kept.append(segment + data[position:boundary])
            position = boundary
            continue
        if marker == 0xE1 and payload.startswith(b"Exif\x00\x00"):
            found = _exif_orientation(payload[6:])
            orientation = found if found is not None else orientation
            continue
        if marker == 0xE0:
            jfif = _jfif_without_thumbnail(payload)
            if jfif is not None:
                kept.append(jfif)
            continue
        if marker == 0xE2 and payload.startswith(b"ICC_PROFILE\x00"):
            kept.append(segment)
            continue
        if marker in _JPEG_KEEP:
            kept.append(segment)
            continue
        # Everything else -- XMP, IPTC, COM, MPF, vendor APPn -- is dropped.

    header = b"\xff\xd8"
    if orientation and orientation != 1:
        exif = _orientation_segment(orientation)
        # After JFIF when there is one, which is where readers expect it.
        if kept and kept[0].startswith(b"\xff\xe0"):
            kept.insert(1, exif)
        else:
            kept.insert(0, exif)
    return header + b"".join(kept)


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

#: Chunks that change how the picture looks, and so are kept. Text chunks,
#: eXIf, tIME and anything private are not on the list. Both spellings of the
#: HDR chunks, because the third edition of the PNG spec renamed them.
_PNG_KEEP = {
    b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"gAMA", b"cHRM", b"sRGB",
    b"iCCP", b"sBIT", b"bKGD", b"pHYs", b"cICP", b"mDCV", b"cLLI", b"mDCv",
    b"cLLi", b"acTL", b"fcTL", b"fdAT",
}


def strip_png_metadata(data):
    """The same PNG with only the chunks that affect display."""
    if not data.startswith(_PNG_SIGNATURE):
        raise MetadataError("Not a PNG.")
    kept = [_PNG_SIGNATURE]
    position = len(_PNG_SIGNATURE)
    while True:
        if position + 12 > len(data):
            raise MetadataError("Truncated chunk.")
        length = struct.unpack(">I", data[position:position + 4])[0]
        kind = data[position + 4:position + 8]
        end = position + 12 + length
        if end > len(data):
            raise MetadataError("Chunk runs past the file.")
        if kind in _PNG_KEEP:
            kept.append(data[position:end])
        position = end
        if kind == b"IEND":
            return b"".join(kept)


#: WebP chunks that make up the picture. EXIF and XMP are left behind.
_WEBP_KEEP = {b"VP8 ", b"VP8L", b"VP8X", b"ALPH", b"ANIM", b"ANMF", b"ICCP"}


def strip_webp_metadata(data):
    """The same WebP without its EXIF and XMP chunks."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise MetadataError("Not a WebP.")
    declared = struct.unpack("<I", data[4:8])[0]
    end = min(len(data), 8 + declared)
    kept = []
    position = 12
    while position + 8 <= end:
        kind = data[position:position + 4]
        size = struct.unpack("<I", data[position + 4:position + 8])[0]
        body_end = position + 8 + size
        if body_end > end:
            raise MetadataError("Chunk runs past the file.")
        # Chunks are padded to an even length; the pad belongs to the chunk.
        padded_end = min(body_end + (size & 1), end)
        if kind in _WEBP_KEEP:
            chunk = bytearray(data[position:padded_end])
            if kind == b"VP8X" and len(chunk) > 8:
                # The extended header announces which optional chunks follow.
                # Leaving the EXIF and XMP bits set after removing the chunks
                # would describe a file that is not there.
                chunk[8] &= ~(0x08 | 0x04) & 0xFF
            kept.append(bytes(chunk))
        position = padded_end
    if not kept:
        raise MetadataError("No image chunks.")
    body = b"WEBP" + b"".join(kept)
    return b"RIFF" + struct.pack("<I", len(body)) + body


_STRIPPERS = {
    ".jpg": strip_jpeg_metadata,
    ".png": strip_png_metadata,
    ".webp": strip_webp_metadata,
}


def _reencoded(data):
    """A clean copy made by decoding and encoding again.

    Only reached when the structure could not be walked even though Pillow
    could read the picture. Lossy for a JPEG, which is why it is the fallback
    and not the method: an unreadable structure is not a reason to refuse the
    photo, and it is certainly not a reason to keep its metadata.
    """
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(data)) as source:
        source.load()
        detected = source.format
        clean = ImageOps.exif_transpose(source)
        output = io.BytesIO()
        if detected == "PNG":
            clean.save(output, format="PNG")
        elif detected == "WEBP":
            clean.save(output, format="WEBP", quality=95)
        else:
            if clean.mode not in ("RGB", "L"):
                clean = clean.convert("RGB")
            clean.save(output, format="JPEG", quality=95)
    return output.getvalue()


def _same_picture(original, cleaned):
    """Whether the cleaned bytes still decode to a picture of the same size."""
    from PIL import Image

    try:
        with Image.open(io.BytesIO(original)) as before:
            expected = (before.format, before.size)
        with Image.open(io.BytesIO(cleaned)) as after:
            after.load()
            return (after.format, after.size) == expected
    except Exception:
        return False


def without_metadata(data, extension, field="image_base64"):
    """The upload with its identifying metadata removed.

    The result is checked by decoding it. A stripper that produced something
    unreadable is a bug here, and the person uploading should not be the one
    who pays for it -- so that case falls back to re-encoding, and only a
    picture that cannot be read even then is refused.
    """
    stripper = _STRIPPERS.get(extension)
    if stripper is None:
        return data
    try:
        cleaned = stripper(data)
        if _same_picture(data, cleaned):
            return cleaned
    except MetadataError:
        pass
    try:
        return _reencoded(data)
    except Exception:
        raise serializers.ValidationError(
            {field: "That image could not be processed. Try exporting it again."}
        ) from None


# --------------------------------------------------------------------- videos

class VideoRejected(serializers.ValidationError):
    """A clip that cannot be accepted, with a sentence the person can act on."""

    def __init__(self, message):
        super().__init__({"video": message})


#: Brands that mean "this is an MP4". The ISO family, the MPEG-4 versions,
#: Apple's M4V and the H.264 brand. Deliberately not `mif1`, `heic`, `avif`
#: and friends: those are still images in the same container.
MP4_BRANDS = frozenset({
    b"isom", b"iso2", b"iso3", b"iso4", b"iso5", b"iso6", b"iso7", b"iso8",
    b"iso9", b"mp41", b"mp42", b"avc1", b"M4V ", b"M4VH", b"M4VP", b"MSNV",
})
QUICKTIME_BRAND = b"qt  "

#: Brands of still images and image sequences in the same container. A file
#: naming any of them, anywhere in `ftyp`, is a photo however much else of
#: it looks like video, and is refused as one.
IMAGE_BRANDS = frozenset({
    b"mif1", b"msf1", b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx",
    b"hevm", b"hevs", b"avif", b"avis", b"avio",
})

#: How the picture may be coded: H.264 and HEVC, in both of their sample
#: entry spellings. Everything that records video today writes one of these,
#: and they are the two every client this API serves can decode. A decoder is
#: attack surface, so the list is short on purpose.
VIDEO_CODECS = frozenset({b"avc1", b"avc3", b"hvc1", b"hev1"})

#: Sound formats players decode: AAC, Apple Lossless, Opus, FLAC, Dolby, and
#: the plain PCM spellings cameras put in QuickTime files.
AUDIO_CODECS = frozenset({
    b"mp4a", b"alac", b"Opus", b"fLaC", b"ac-3", b"ec-3",
    b"lpcm", b"sowt", b"twos", b"raw ", b"in24", b"in32", b"fl32",
})

CONTAINER_TYPES = {"mp4": ("video/mp4", ".mp4"), "mov": ("video/quicktime", ".mov")}

#: Boxes walked into. Anything else is a leaf and is read, if at all, as data.
_CONTAINERS = frozenset({b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"mvex"})

#: Children of `moov` and `trak` that are metadata rather than media. These
#: are where location, device and software details live.
_METADATA_BOXES = frozenset({b"udta", b"meta", b"uuid"})

#: Top-level boxes that survive the rebuild. `free`, `skip`, `wide`, `uuid`,
#: `udta` and `meta` at the top level are padding or metadata.
_KEPT_TOP_LEVEL = frozenset({b"ftyp", b"moov", b"mdat"})

#: Bounds on the walk itself.
MAX_MOOV_BYTES = 16 * 1024 * 1024
_MAX_TOP_LEVEL_BOXES = 256
_MAX_BOXES = 20_000
_MAX_DEPTH = 8

#: A clip shorter than this is not a clip.
MIN_VIDEO_SECONDS = 0.5
DURATION_GRACE_SECONDS = 0.5


@dataclass(frozen=True)
class VideoInfo:
    container: str
    codec: str
    duration_ms: int
    width: int
    height: int
    has_audio: bool

    @property
    def content_type(self):
        return CONTAINER_TYPES[self.container][0]

    @property
    def extension(self):
        return CONTAINER_TYPES[self.container][1]


@dataclass
class _Box:
    kind: bytes
    start: int        # where the header begins
    body: int         # where the payload begins
    end: int          # one past the last byte


def _box_header(read, position, limit):
    """Read one box header at `position`; `read(offset, n)` returns bytes."""
    header = read(position, 8)
    if len(header) < 8:
        raise VideoRejected("The video file is incomplete. Try exporting it again.")
    size, kind = struct.unpack(">I4s", header)
    body = position + 8
    if size == 1:
        large = read(position + 8, 8)
        if len(large) < 8:
            raise VideoRejected("The video file is incomplete. Try exporting it again.")
        size = struct.unpack(">Q", large)[0]
        body = position + 16
    elif size == 0:
        size = limit - position
    if size < body - position:
        raise VideoRejected("That file is not a video we can read.")
    end = position + size
    if end > limit:
        raise VideoRejected("The video file is incomplete. Try exporting it again.")
    return _Box(kind, position, body, end)


def _children(data, start, end, counter):
    """The boxes directly inside data[start:end]."""
    boxes = []
    position = start
    read = lambda offset, n: data[offset:offset + n]  # noqa: E731
    while position < end:
        if end - position < 8:
            # Trailing padding inside a container is legal and harmless.
            break
        counter[0] += 1
        if counter[0] > _MAX_BOXES:
            raise VideoRejected("That file is not a video we can read.")
        box = _box_header(read, position, end)
        boxes.append(box)
        position = box.end
    return boxes


def _full_box_version(data, box):
    if box.end - box.body < 4:
        raise VideoRejected("That file is not a video we can read.")
    return data[box.body]


#: Field positions inside mvhd and mdhd, per version: (timescale, duration,
#: duration width). Both boxes share the layout up to the duration.
_HEADER_LAYOUT = {0: (12, 16, 4), 1: (20, 24, 8)}


def _timescaled(data, box):
    """(timescale, duration) from an mvhd- or mdhd-shaped full box."""
    layout = _HEADER_LAYOUT.get(_full_box_version(data, box))
    if layout is None:
        raise VideoRejected("That file is not a video we can read.")
    timescale_offset, duration_offset, duration_size = layout
    if box.body + duration_offset + duration_size > box.end:
        raise VideoRejected("That file is not a video we can read.")
    timescale = struct.unpack(">I", data[box.body + timescale_offset:box.body + timescale_offset + 4])[0]
    raw = data[box.body + duration_offset:box.body + duration_offset + duration_size]
    duration = struct.unpack(">I" if duration_size == 4 else ">Q", raw)[0]
    return timescale, duration


def _seconds(duration, timescale):
    # 0xFFFFFFFF (or the 64-bit equivalent) means "unknown" in these headers.
    if not timescale or duration in (0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF):
        return 0.0
    return duration / timescale


def _track_facts(data, trak, movie_timescale, counter):
    """What one track claims: kind, codec, size and every duration it states."""
    facts = {"handler": None, "codec": None, "width": 0, "height": 0, "durations": []}
    media_timescale = 0
    stts_total = None

    def visit(box, depth):
        nonlocal media_timescale, stts_total
        if depth > _MAX_DEPTH:
            raise VideoRejected("That file is not a video we can read.")
        for child in _children(data, box.body, box.end, counter):
            kind = child.kind
            if kind == b"tkhd":
                version = _full_box_version(data, child)
                duration_at = 20 if version == 0 else 28
                width_at = child.end - 8
                if child.body + duration_at + 8 > child.end or width_at < child.body:
                    raise VideoRejected("That file is not a video we can read.")
                size = 4 if version == 0 else 8
                raw = data[child.body + duration_at:child.body + duration_at + size]
                facts["durations"].append(
                    _seconds(struct.unpack(">I" if size == 4 else ">Q", raw)[0], movie_timescale)
                )
                width, height = struct.unpack(">II", data[width_at:child.end])
                facts["width"] = max(facts["width"], width >> 16)
                facts["height"] = max(facts["height"], height >> 16)
            elif kind == b"mdhd":
                media_timescale, duration = _timescaled(data, child)
                facts["durations"].append(_seconds(duration, media_timescale))
            elif kind == b"hdlr" and box.kind == b"mdia":
                # Only the media-level handler names the track. QuickTime puts a
                # second hdlr inside minf for the data reference, saying "url ".
                if child.body + 12 > child.end:
                    raise VideoRejected("That file is not a video we can read.")
                facts["handler"] = data[child.body + 8:child.body + 12]
            elif kind == b"elst":
                version = _full_box_version(data, child)
                count = struct.unpack(">I", data[child.body + 4:child.body + 8])[0]
                entry = 12 if version == 0 else 20
                if child.body + 8 + count * entry > child.end:
                    raise VideoRejected("That file is not a video we can read.")
                total = 0
                for index in range(min(count, 10_000)):
                    at = child.body + 8 + index * entry
                    raw = data[at:at + (4 if version == 0 else 8)]
                    total += struct.unpack(">I" if version == 0 else ">Q", raw)[0]
                facts["durations"].append(_seconds(total, movie_timescale))
            elif kind == b"stts":
                count = struct.unpack(">I", data[child.body + 4:child.body + 8])[0]
                if child.body + 8 + count * 8 > child.end:
                    raise VideoRejected("That file is not a video we can read.")
                total = 0
                for index in range(min(count, 1_000_000)):
                    at = child.body + 8 + index * 8
                    samples, delta = struct.unpack(">II", data[at:at + 8])
                    total += samples * delta
                stts_total = total
            elif kind == b"stsd":
                if child.body + 16 > child.end:
                    raise VideoRejected("That file is not a video we can read.")
                entries = struct.unpack(">I", data[child.body + 4:child.body + 8])[0]
                if entries < 1:
                    raise VideoRejected("That file is not a video we can read.")
                facts["codec"] = data[child.body + 12:child.body + 16]
                # A visual sample entry states its own size too, 24 bytes into
                # the entry after the 8-byte entry header.
                entry_at = child.body + 8
                if child.body + 8 + 36 <= child.end and facts["handler"] in (b"vide", None):
                    width, height = struct.unpack(">HH", data[entry_at + 32:entry_at + 36])
                    facts["width"] = max(facts["width"], width)
                    facts["height"] = max(facts["height"], height)
            elif kind in _CONTAINERS:
                visit(child, depth + 1)

    visit(trak, 1)
    if stts_total is not None:
        facts["durations"].append(_seconds(stts_total, media_timescale))
    return facts


def _scan_top_level(fileobj, size):
    """The top-level boxes of a file, read by seeking past their contents."""
    def read(offset, n):
        fileobj.seek(offset)
        return fileobj.read(n)

    boxes = []
    position = 0
    while position < size:
        if len(boxes) >= _MAX_TOP_LEVEL_BOXES:
            raise VideoRejected("That file is not a video we can read.")
        if size - position < 8:
            raise VideoRejected("The video file is incomplete. Try exporting it again.")
        box = _box_header(read, position, size)
        boxes.append(box)
        position = box.end
    return boxes


def _parse_video(fileobj, size, max_seconds, max_dimension, max_pixels):
    """Validate a clip and return (VideoInfo, top-level boxes, moov bytes)."""
    if size < 16:
        raise VideoRejected("Send an MP4 or MOV video.")
    fileobj.seek(0)
    head = fileobj.read(12)
    if head[4:8] != b"ftyp":
        raise VideoRejected("Send an MP4 or MOV video.")

    boxes = _scan_top_level(fileobj, size)
    ftyp = boxes[0]
    fileobj.seek(ftyp.body)
    brands_raw = fileobj.read(min(ftyp.end - ftyp.body, 1024))
    if len(brands_raw) < 8:
        raise VideoRejected("Send an MP4 or MOV video.")
    major = brands_raw[:4]
    compatible = {brands_raw[i:i + 4] for i in range(8, len(brands_raw) - 3, 4)}
    if major in IMAGE_BRANDS or compatible & IMAGE_BRANDS:
        raise VideoRejected("Send an MP4 or MOV video.")
    if major == QUICKTIME_BRAND:
        container = "mov"
    elif major in MP4_BRANDS or compatible & MP4_BRANDS:
        container = "mp4"
    else:
        raise VideoRejected("Send an MP4 or MOV video.")

    kinds = [box.kind for box in boxes]
    if b"moof" in kinds or b"mfra" in kinds or b"sidx" in kinds or b"styp" in kinds:
        raise VideoRejected(
            "This video is in a streaming format we cannot check. "
            "Export it as a regular MP4 or MOV."
        )
    moovs = [box for box in boxes if box.kind == b"moov"]
    if len(moovs) != 1 or b"mdat" not in kinds:
        raise VideoRejected("That file is not a video we can read.")
    moov = moovs[0]
    if moov.end - moov.start > MAX_MOOV_BYTES:
        raise VideoRejected("That file is not a video we can read.")
    fileobj.seek(moov.start)
    data = fileobj.read(moov.end - moov.start)
    if len(data) != moov.end - moov.start:
        raise VideoRejected("The video file is incomplete. Try exporting it again.")

    counter = [0]
    # Re-read the moov box from the start of its own buffer.
    root = _box_header(lambda offset, n: data[offset:offset + n], 0, len(data))
    children = _children(data, root.body, root.end, counter)
    if any(child.kind == b"mvex" for child in children):
        raise VideoRejected(
            "This video is in a streaming format we cannot check. "
            "Export it as a regular MP4 or MOV."
        )
    mvhd = next((child for child in children if child.kind == b"mvhd"), None)
    if mvhd is None:
        raise VideoRejected("That file is not a video we can read.")
    movie_timescale, movie_duration = _timescaled(data, mvhd)
    if not movie_timescale:
        raise VideoRejected("That file is not a video we can read.")
    durations = [_seconds(movie_duration, movie_timescale)]

    video = None
    has_audio = False
    for trak in (child for child in children if child.kind == b"trak"):
        facts = _track_facts(data, trak, movie_timescale, counter)
        durations.extend(facts["durations"])
        if facts["handler"] == b"vide":
            if facts["codec"] not in VIDEO_CODECS:
                raise VideoRejected(
                    "This video uses a format we cannot play back. "
                    "Export it as H.264 or HEVC."
                )
            video = video or facts
            # Every picture track is held to the size limit, not only the
            # first, since a player may show any of them.
            if facts["width"] > max_dimension or facts["height"] > max_dimension or (
                facts["width"] * facts["height"] > max_pixels
            ):
                raise VideoRejected(
                    f"Videos can be at most {max_dimension} pixels on a side."
                )
        elif facts["handler"] == b"soun":
            if facts["codec"] not in AUDIO_CODECS:
                raise VideoRejected(
                    "This video's sound is in a format we cannot play back."
                )
            has_audio = True
    if video is None:
        raise VideoRejected("This file has no picture in it.")
    if not video["width"] or not video["height"]:
        raise VideoRejected("That file is not a video we can read.")

    longest = max(durations)
    # A little grace: a clip stopped at exactly the limit can run a few
    # hundredths over once its sound track's encoder priming is counted.
    if longest > max_seconds + DURATION_GRACE_SECONDS:
        raise VideoRejected(f"Videos can be at most {int(max_seconds)} seconds long.")
    if longest < MIN_VIDEO_SECONDS:
        raise VideoRejected("That video is too short.")

    info = VideoInfo(
        container=container,
        codec=video["codec"].decode("latin-1"),
        duration_ms=int(round(longest * 1000)),
        width=video["width"],
        height=video["height"],
        has_audio=has_audio,
    )
    return info, boxes, data


def _rebuilt(data, box, counter, depth=0, remap=None):
    """A container box with its metadata children dropped, re-sized.

    `remap` moves an absolute chunk offset to where that byte will sit in the
    rebuilt file; it is applied to every stco and co64 entry on the way down.
    """
    if depth > _MAX_DEPTH:
        raise VideoRejected("That file is not a video we can read.")
    parts = []
    for child in _children(data, box.body, box.end, counter):
        if child.kind in _METADATA_BOXES and box.kind in (b"moov", b"trak"):
            continue
        if child.kind in _CONTAINERS:
            parts.append(_rebuilt(data, child, counter, depth + 1, remap))
        elif child.kind in (b"stco", b"co64") and remap is not None:
            parts.append(_remapped_offsets(data, child, remap))
        else:
            parts.append(data[child.start:child.end])
    payload = b"".join(parts)
    # Container boxes carry no version or flags, so the header is the box.
    size = len(payload) + 8
    if size > 0xFFFFFFFF:
        return struct.pack(">I4sQ", 1, box.kind, size + 8) + payload
    return struct.pack(">I4s", size, box.kind) + payload


def _remapped_offsets(data, box, remap):
    wide = box.kind == b"co64"
    width = 8 if wide else 4
    count = struct.unpack(">I", data[box.body + 4:box.body + 8])[0]
    if box.body + 8 + count * width > box.end:
        raise VideoRejected("That file is not a video we can read.")
    fmt = ">Q" if wide else ">I"
    out = bytearray(data[box.start:box.end])
    base = box.body - box.start + 8
    for index in range(count):
        at = base + index * width
        moved = remap(struct.unpack(fmt, out[at:at + width])[0])
        if not wide and moved > 0xFFFFFFFF:
            raise VideoRejected("That file is not a video we can read.")
        out[at:at + width] = struct.pack(fmt, moved)
    return bytes(out)


def normalise_video(fileobj, size, *, max_seconds, max_dimension, max_pixels):
    """Validate a clip, and write a copy without its metadata.

    Returns (VideoInfo, temporary file positioned at 0). The caller owns the
    file and closes it. Raises VideoRejected for anything that is not an
    acceptable clip.
    """
    info, boxes, moov_data = _parse_video(
        fileobj, size, max_seconds, max_dimension, max_pixels
    )
    kept = [box for box in boxes if box.kind in _KEPT_TOP_LEVEL]
    moov_index = next(index for index, box in enumerate(kept) if box.kind == b"moov")

    # The rebuilt moov's size does not depend on the offsets written into it,
    # so it is built once to measure, the layout is worked out, and it is
    # built again with the offsets that layout implies.
    counter = [0]
    root = _box_header(lambda offset, n: moov_data[offset:offset + n], 0, len(moov_data))
    measured = len(_rebuilt(moov_data, root, counter, remap=lambda offset: offset))

    layout = []
    cursor = 0
    for box in kept:
        length = measured if box.kind == b"moov" else box.end - box.start
        layout.append((box, cursor))
        cursor += length

    regions = [(box.start, box.end, new_start) for box, new_start in layout if box.kind == b"mdat"]

    def remap(offset):
        for old_start, old_end, new_start in regions:
            if old_start <= offset < old_end:
                return new_start + (offset - old_start)
        # A chunk that is not inside any media data box points at something
        # this rebuild drops, and the copy would play garbage.
        raise VideoRejected("That file is not a video we can read.")

    rebuilt_moov = _rebuilt(moov_data, root, [0], remap=remap)
    if len(rebuilt_moov) != measured:  # pragma: no cover - defensive
        raise VideoRejected("That file is not a video we can read.")

    output = tempfile.TemporaryFile()
    try:
        for index, (box, _) in enumerate(layout):
            if index == moov_index:
                output.write(rebuilt_moov)
                continue
            fileobj.seek(box.start)
            remaining = box.end - box.start
            while remaining:
                chunk = fileobj.read(min(remaining, 1024 * 1024))
                if not chunk:
                    raise VideoRejected("The video file is incomplete. Try exporting it again.")
                output.write(chunk)
                remaining -= len(chunk)
        output.flush()
        output.seek(0)
        # The copy is held to the same rules as the original. A rebuild that
        # produced something the parser would refuse is a bug here, and it
        # must not reach storage.
        _parse_video(output, os.fstat(output.fileno()).st_size, max_seconds, max_dimension, max_pixels)
        output.seek(0)
    except BaseException:
        output.close()
        raise
    return info, output
