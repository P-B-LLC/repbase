"""Pre-publication moderation. No raw content, credentials or scores in logs.

The remote classifier supplements human reports; it is not an Apple approval
guarantee. Only explicitly public-facing text and uploaded photos are sent.
"""
import base64
import io
import json
import urllib.request

from django.conf import settings
from PIL import Image, ImageOps
from rest_framework.exceptions import APIException, ValidationError


class ModerationUnavailable(APIException):
    status_code = 503
    default_detail = 'Safety checks are temporarily unavailable. Nothing was published; please try again.'
    default_code = 'moderation_unavailable'


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a provider credential or user content to another host.
        return None


PUBLIC_TEXT_FIELDS = {'caption', 'body', 'name', 'title', 'description', 'bio',
                      'first_name', 'last_name', 'username', 'answer', 'cooking_instructions'}


def public_text(value):
    """Extract an allowlist, not arbitrary account/health/credential fields."""
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in PUBLIC_TEXT_FIELDS and isinstance(item, str) and item.strip():
                found.append(item.strip())
            elif isinstance(item, (dict, list, tuple)):
                found.extend(public_text(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(public_text(item))
    return found


def check_public_content(value=None, *, image=None):
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
        with Image.open(io.BytesIO(image)) as source:
            if getattr(source, 'n_frames', 1) != 1:
                raise ValidationError('Please use a still image, not an animated image.')
            # Remove EXIF/location metadata before sending the review copy.
            clean = ImageOps.exif_transpose(source).convert('RGB')
            clean.thumbnail((2048, 2048))
            output = io.BytesIO()
            clean.save(output, format='JPEG', quality=90)
        inputs.append({'type': 'image_url', 'image_url': {
            'url': 'data:image/jpeg;base64,' + base64.b64encode(output.getvalue()).decode('ascii')}})
    if not inputs:
        return
    if not settings.MODERATION_DISCLOSURE_CONFIRMED:
        # Operator release gate, not a substitute for per-submission permission.
        raise ModerationUnavailable()
    key = settings.MODERATION_API_KEY
    if not key:
        raise ModerationUnavailable()
    body = json.dumps({'model': 'omni-moderation-2024-09-26', 'input': inputs}).encode()
    request = urllib.request.Request('https://api.openai.com/v1/moderations', data=body,
        headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.build_opener(NoRedirects()).open(request, timeout=8) as response:
            result = json.loads(response.read(262145))
        results = result['results']
        if not isinstance(results, list) or not results or any(
                not isinstance(row, dict) or type(row.get('flagged')) is not bool for row in results):
            raise ValueError('Malformed moderation response')
    except Exception:
        # Includes timeouts, rate limits and malformed responses. No fail-open.
        raise ModerationUnavailable() from None
    if any(row['flagged'] for row in results):
        raise ValidationError('This content could not be published under our community standards. '
                              'Please revise it, or appeal to ' + settings.MODERATION_CONTACT_EMAIL + '.')
