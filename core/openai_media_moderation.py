"""OpenAI-only image/frame classifier. No publication mutations or raw logging.

The durable worker owns retries; this client performs one bounded request and
never converts a network failure or incomplete result into approval.
"""
import base64
from dataclasses import dataclass
import json
import math
import urllib.error
import urllib.request

from django.conf import settings

from .moderation import NoRedirects, review_copy

MODEL = 'omni-moderation-latest'
ENDPOINT = 'https://api.openai.com/v1/moderations'


class MediaScreeningUnavailable(Exception):
    def __init__(self, retry_after=60):
        super().__init__('Media screening unavailable')
        self.retry_after = retry_after


class MediaSafetyHold(Exception):
    """Known/suspected child abuse must never be transmitted to this API."""


@dataclass(frozen=True)
class MediaVerdict:
    flagged: bool
    sexual_score: float
    sexual_category: bool
    model: str


def parse_result(payload, threshold):
    if type(threshold) not in (int, float) or not math.isfinite(threshold) or not 0 < threshold <= 1:
        raise ValueError('Configure a sexual-content threshold in (0, 1]')
    try:
        rows = payload['results']
        if not isinstance(rows, list) or len(rows) != 1:
            raise ValueError
        row = rows[0]
        score = row['category_scores']['sexual']
        category = row['categories']['sexual']
        applied = row['category_applied_input_types']['sexual']
        model = payload['model']
        if (type(score) not in (int, float) or not math.isfinite(score)
                or not 0 <= score <= 1 or type(category) is not bool
                or not isinstance(applied, list) or 'image' not in applied
                or not isinstance(model, str) or not 1 <= len(model) <= 100):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise MediaScreeningUnavailable() from None
    # Deliberately ignore aggregate `flagged` and all unrelated categories.
    # The threshold, rather than a hardcoded model boolean, is the app policy.
    return MediaVerdict(score >= threshold, float(score), category, model)


def screen_image(image, *, threshold, consent_confirmed, child_safety_hold):
    # Explicit server-owned safety/consent arguments, never direct request fields.
    if child_safety_hold is not False:
        raise MediaSafetyHold()
    if consent_confirmed is not True or not settings.MODERATION_DISCLOSURE_CONFIRMED:
        raise MediaScreeningUnavailable()
    key = settings.MODERATION_API_KEY
    if not key or not isinstance(image, bytes) or not 0 < len(image) <= 20 * 1024 * 1024:
        raise MediaScreeningUnavailable()
    if type(threshold) not in (int, float) or not math.isfinite(threshold) or not 0 < threshold <= 1:
        raise ValueError('Invalid sexual-content threshold')
    clean = review_copy(image)
    body = json.dumps({'model': MODEL, 'input': [{'type': 'image_url', 'image_url': {
        'url': 'data:image/jpeg;base64,' + base64.b64encode(clean).decode('ascii')}}]}).encode()
    outbound = urllib.request.Request(ENDPOINT, data=body, method='POST', headers={
        'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    try:
        with urllib.request.build_opener(NoRedirects()).open(
                outbound, timeout=settings.MODERATION_TIMEOUT_SECONDS) as response:
            raw = response.read(262145)
            if len(raw) > 262144:
                raise ValueError
            return parse_result(json.loads(raw), threshold)
    except urllib.error.HTTPError as error:
        try:
            delay = max(1, min(3600, int(error.headers.get('Retry-After', '60'))))
        except (AttributeError, ValueError, TypeError):
            delay = 60
        raise MediaScreeningUnavailable(delay) from None
    except Exception:
        raise MediaScreeningUnavailable() from None
