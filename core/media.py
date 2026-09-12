"""Serving uploaded photos: with the app's key, and without its database.

Two problems were solved together here, because solving either one alone
makes the other worse.

The first is that photos were served by `static()` under `if settings.DEBUG`,
so with DEBUG off -- which production requires -- every avatar, post photo and
feed image answered 404. Measured against a DEBUG=False server: the API
answered 401, media answered 404.

The second is that they had no access control at all. Anyone holding a URL
could fetch any photo for as long as the file existed, including one belonging
to a private account, and nothing expired.

The obvious fix for the second -- authenticate every image request -- is the
expensive one. It puts a database read and a permission check in front of
every thumbnail in a scrolling feed, and it requires the client to attach
credentials to image loads, which `RemoteImage` does not do.

So: the URL carries its own proof. A keyed signature over the file's name and
an expiry, checked with an HMAC and nothing else. No query, no session, no
per-image authorisation, and a URL that stops working on its own.

This is what S3 presigned URLs and CloudFront signed URLs do, for the same
reasons.

## Why the expiry is rounded

A signature over `now + ttl` would produce a different URL every time a post
is serialised. The app caches decoded images under their URL and the system
caches responses under theirs, so a URL that changes on every read turns every
feed refresh into a fresh download and a fresh decode of every photo on
screen. That is a large efficiency regression bought with no extra safety.

Rounding the expiry up to a window boundary makes the URL identical for
everyone for the whole window, so it caches, revalidates as a 304, and could
sit in front of a CDN later. The cost is that the lifetime varies between
`ttl` and `ttl + window` rather than being exact, which for "this link stops
working in about a week" is not a cost at all.

## What this does not do

It does not decide who may see a post. That is the feed's job, and it is done
where the post is served -- someone who cannot see the post never receives the
URL. What this bounds is how long a URL keeps working once handed out, and it
makes one unguessable rather than merely long.
"""

import hashlib
import posixpath
import time

from django.conf import settings
from django.core.files.storage import default_storage
from django.http import FileResponse, HttpResponse, HttpResponseForbidden, HttpResponseNotModified
from django.utils.crypto import constant_time_compare, salted_hmac
from django.utils.http import urlencode

#: Rounded up to the next window, so the real lifetime is TTL..TTL+WINDOW.
MEDIA_URL_TTL = int(getattr(settings, "MEDIA_URL_TTL", 7 * 24 * 60 * 60))
#: How often a URL is allowed to change. One day: long enough that a photo is
#: fetched once and then cached, short enough that a leaked link is not
#: interesting for long.
MEDIA_URL_WINDOW = int(getattr(settings, "MEDIA_URL_WINDOW", 24 * 60 * 60))

_KEY_SALT = "core.media.signed-url"


def _signature(name, expires):
    """A keyed digest of exactly the two things the URL promises."""
    return salted_hmac(
        _KEY_SALT, f"{name}:{expires}", algorithm="sha256"
    ).hexdigest()[:32]


def _expiry_for(now):
    """The next window boundary at least TTL away.

    Everyone serialising the same file within one window gets the same
    answer, which is the whole point -- see the note above on caching.
    """
    return ((int(now) + MEDIA_URL_TTL) // MEDIA_URL_WINDOW + 1) * MEDIA_URL_WINDOW


def signed_media_path(name, now=None):
    """`/media/<name>?e=...&s=...` for a stored file name."""

    expires = _expiry_for(time.time() if now is None else now)
    query = urlencode({"e": expires, "s": _signature(name, expires)})
    return f"{settings.MEDIA_URL}{name}?{query}"


def signed_media_url(uploaded, request=None):
    """The absolute, signed URL of an uploaded file, or None when there is none.

    Absolute because the app talks to the API from a different origin than the
    one serving the file, and a relative path would resolve against the app.
    """
    if not uploaded:
        return None
    path = signed_media_path(uploaded.name)
    return request.build_absolute_uri(path) if request else path


def verify(name, expires, signature, now=None):
    """Whether this signature was issued for this file and is still live."""

    if not expires or not signature:
        return False
    try:
        expires_at = int(expires)
    except (TypeError, ValueError):
        return False
    if expires_at < (time.time() if now is None else now):
        return False
    return constant_time_compare(signature, _signature(name, expires_at))


def serve_media(request, path):
    """A signed photo.

    Deliberately not `login_required` and deliberately without a database
    query: the signature is the authorisation, and this sits in front of every
    thumbnail in a feed.
    """
    # Normalised before anything is checked, so that a signature cannot be
    # made for one name and spent on another spelling of a different file.
    name = posixpath.normpath(path).lstrip("/")
    if name.startswith("../") or name != path.strip("/"):
        return HttpResponseForbidden("Bad media path.")

    if not verify(name, request.GET.get("e"), request.GET.get("s")):
        return HttpResponseForbidden("This photo link has expired.")

    accel = getattr(settings, "MEDIA_ACCEL_REDIRECT_ROOT", "")
    if accel:
        # The proxy sends the file; this process sends a header and moves on.
        # No image bytes pass through Python, which is the whole reason to run
        # something in front of it. Storage is not touched at all here, not
        # even to check the file is there -- that would be a stat per
        # thumbnail to save the proxy from answering its own 404.
        response = HttpResponse(status=200)
        response["X-Accel-Redirect"] = f"{accel.rstrip('/')}/{name}"
        # Cleared so the proxy fills it in from the file it actually serves.
        del response["Content-Type"]
        return _cached(response, request)

    # Everything below goes through `default_storage` rather than reading
    # MEDIA_ROOT off the disk. The first version of this called
    # django.views.static.serve, which only knows about the filesystem -- so
    # it would have kept working right up until media moved to S3 and then
    # served nothing, which is the move this project is heading for.
    if not default_storage.exists(name):
        # Checked after the signature, so this cannot be used to find out
        # which names exist.
        return HttpResponse("Not found.", status=404)

    # A stored name carries a UUID and its bytes never change, so the name is
    # a complete identity for the content. That makes a revalidation cheap
    # and, with `immutable` below, rare.
    etag = '"%s"' % hashlib.sha256(name.encode()).hexdigest()[:32]
    if request.headers.get("If-None-Match") == etag:
        return _cached(HttpResponseNotModified(), request)

    response = FileResponse(default_storage.open(name, "rb"))
    response["ETag"] = etag
    return _cached(response, request)


def _cached(response, request):
    """Tell the client to keep it for as long as the link is good for.

    Without this the app asks about every photo on every launch and is told
    304 every time -- a round trip each, on a screen showing a dozen. The
    lifetime is the link's own remaining validity, so nothing is ever cached
    past the point where it would stop working.
    """
    try:
        remaining = max(int(request.GET.get("e", 0)) - int(time.time()), 0)
    except (TypeError, ValueError):
        remaining = 0
    response["Cache-Control"] = f"private, max-age={remaining}, immutable"
    return response
