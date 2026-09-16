"""Pre-publication moderation. No raw content, credentials or scores in logs.

The remote classifier supplements human reports; it is not an Apple approval
guarantee. Only explicitly public-facing text and uploaded photos are sent.

Consent is checked here rather than trusted from the client's behaviour. The
app asks per submission, but a build from last month, a script, or curl does
not -- and "the app shows a dialog" is not a property the server can rely on.
So a submission has to carry the consent version this server requires, and a
request without it is refused before anything leaves the machine. Bumping
MODERATION_CONSENT_VERSION invalidates every older client at once, which is
the point of versioning it: if what is disclosed changes, agreement to the
previous wording stops counting.
"""
import base64
import io
import json
import urllib.request

from django.conf import settings
from PIL import Image, ImageFile, ImageOps, UnidentifiedImageError
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError

#: A truncated upload should be a validation error, not a 500 halfway through
#: preprocessing. Pillow is told to refuse rather than improvise.
ImageFile.LOAD_TRUNCATED_IMAGES = False

#: Refuse a decompression bomb before allocating for it. Well above anything a
#: phone camera produces and far below what exhausts the machine.
MAX_IMAGE_PIXELS = 50_000_000


class ModerationUnavailable(APIException):
    status_code = 503
    # A dict, not a string, because DRF renders exc.detail as the body and a
    # bare string gives {"detail": ...} with no code. The client keys off the
    # code to tell an outage from a refusal -- one is worth a retry button and
    # the other is not -- and default_code never reaches the wire.
    default_detail = {
        'detail': 'Safety checks are temporarily unavailable. Nothing was published; please try again.',
        'code': 'moderation_unavailable',
    }
    default_code = 'moderation_unavailable'


class ModerationConsentRequired(PermissionDenied):
    default_detail = {
        'detail': (
            'This submission needs permission for safety review before it can be sent. '
            'Update the app, then try again.'
        ),
        'code': 'moderation_consent_required',
    }
    default_code = 'moderation_consent_required'

CONSENT_HEADER = 'HTTP_X_MODERATION_CONSENT'


def consent_given(request):
    """Whether this request carries agreement to the current disclosure."""
    if request is None:
        return False
    supplied = (request.META.get(CONSENT_HEADER) or '').strip()
    return bool(supplied) and supplied == settings.MODERATION_CONSENT_VERSION


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a provider credential or user content to another host.
        return None


PUBLIC_TEXT_FIELDS = {'caption', 'body', 'name', 'title', 'description', 'bio',
                      'first_name', 'last_name', 'username', 'answer', 'cooking_instructions',
                      'city', 'country', 'handle'}


def public_text(value):
    """Extract an allowlist, not arbitrary account/health/credential fields.

    The shape matters as much as the key. Recursion loses the key on the way
    down, so a bare string inside a list is anonymous by the time it is looked
    at -- there is no way to tell `{'bio': ['...']}` from `{'weight_kg': [...]}`
    once the list is entered. That is why a list under an allowlisted key is
    read here, where the key is still in hand, rather than in the list branch
    below.

    Nothing currently sends that shape: `public_text_for_source` builds
    `[{'name': ...}]`, dicts all the way down, which the recursion already
    reaches. This is for the next caller, because the failure mode is silence
    -- text would be published having been checked by nobody, and no test
    anywhere would go red.
    """
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            allowed = key in PUBLIC_TEXT_FIELDS
            if allowed and isinstance(item, str) and item.strip():
                found.append(item.strip())
            elif isinstance(item, (dict, list, tuple)):
                if allowed and isinstance(item, (list, tuple)):
                    # Plain strings under a public key are as public as the
                    # same text written directly under it. Lists only: a dict
                    # would iterate its keys, which are field names.
                    found.extend(entry.strip() for entry in item
                                 if isinstance(entry, str) and entry.strip())
                found.extend(public_text(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(public_text(item))
    return found


def review_copy(image):
    """A clean JPEG of an upload, or a validation error explaining why not.

    Everything here is a decision about untrusted bytes. The file already
    passed a type sniff; that says what it claims to be, not that it decodes.
    A truncated JPEG sniffs clean and then raises partway through
    preprocessing, which is a 500 for what is really a bad upload -- and it
    was reproduced, not imagined.

    So the image is fully decoded before anything is done with it, the pixel
    count is refused before it is allocated for, and every way Pillow can fail
    becomes a message the person can act on.
    """
    try:
        with Image.open(io.BytesIO(image)) as probe:
            width, height = probe.size
            if width * height > MAX_IMAGE_PIXELS:
                raise ValidationError('This image is too large to check. Please use a smaller one.')
            if getattr(probe, 'n_frames', 1) != 1:
                raise ValidationError('Please use a still image, not an animated image.')
            # verify() invalidates the object, so the work happens on a second
            # open. That is Pillow's documented shape, not superstition.
            probe.verify()
        with Image.open(io.BytesIO(image)) as source:
            # Force the whole thing through the decoder now. Anything broken
            # raises here, where it can be answered, rather than inside the
            # publication transaction.
            source.load()
            clean = ImageOps.exif_transpose(source).convert('RGB')
            clean.thumbnail((2048, 2048))
            output = io.BytesIO()
            clean.save(output, format='JPEG', quality=90)
    except ValidationError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError, SyntaxError):
        raise ValidationError(
            'This image could not be read. It may be damaged or incomplete; '
            'try exporting it again.'
        ) from None
    return output.getvalue()


def check_public_content(value=None, *, image=None, request=None):
    if not settings.MODERATION_ENABLED:
        # Development only. Production deployment checks refuse this state.
        return
    inputs = []
    text = '\n'.join(public_text(value or {}))
    if text:
        # Never truncate and then publish text the classifier did not inspect.
        if len(text) > 24000:
            raise ValidationError('This shared content is too long to check. Please shorten it.')
        inputs.append({'type': 'text', 'text': text})
    if image is not None:
        inputs.append({'type': 'image_url', 'image_url': {
            'url': 'data:image/jpeg;base64,' + base64.b64encode(review_copy(image)).decode('ascii')}})
    if not inputs:
        return
    # Nothing has left the machine yet. Consent is checked here, at the last
    # moment before transmission and after the cheap refusals, so a damaged
    # image is still answered as a damaged image rather than as a consent
    # problem.
    if not consent_given(request):
        raise ModerationConsentRequired()
    if not settings.MODERATION_DISCLOSURE_CONFIRMED:
        # Operator release gate, not a substitute for per-submission permission.
        raise ModerationUnavailable()
    key = settings.MODERATION_API_KEY
    if not key:
        raise ModerationUnavailable()
    body = json.dumps({'model': 'omni-moderation-2024-09-26', 'input': inputs}).encode()
    # Named for what it is: `request` is the caller's HTTP request, which the
    # consent check above needs, and reusing that name here cost a confusing
    # minute once already.
    outbound = urllib.request.Request('https://api.openai.com/v1/moderations', data=body,
        headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.build_opener(NoRedirects()).open(
                outbound, timeout=settings.MODERATION_TIMEOUT_SECONDS) as response:
            result = json.loads(response.read(262145))
        results = result['results']
        if not isinstance(results, list) or not results or any(
                not isinstance(row, dict) or type(row.get('flagged')) is not bool for row in results):
            raise ValueError('Malformed moderation response')
    except Exception:
        # Includes timeouts, rate limits and malformed responses. No fail-open.
        raise ModerationUnavailable() from None
    if any(row['flagged'] for row in results):
        # Coded, because the app has to tell a refusal apart from an outage to
        # know whether retrying is worth offering, and a human-readable string
        # is not something a client should be matching on.
        raise ValidationError({
            'detail': (
                'This content could not be published under our community standards. '
                'Please revise it, or appeal to ' + settings.MODERATION_CONTACT_EMAIL + '.'
            ),
            'code': 'moderation_rejected',
        })
