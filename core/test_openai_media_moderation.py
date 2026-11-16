import io
import json
from unittest.mock import MagicMock, patch
import urllib.error

from django.test import SimpleTestCase, override_settings
from PIL import Image

from .openai_media_moderation import (
    MODEL, MediaSafetyHold, MediaScreeningUnavailable, parse_result, screen_image,
)


@override_settings(MODERATION_API_KEY='test-only', MODERATION_DISCLOSURE_CONFIRMED=True,
                   MODERATION_TIMEOUT_SECONDS=8)
class OpenAIMediaTests(SimpleTestCase):
    def payload(self, score=0.02, category=False):
        return {'model': MODEL, 'results': [{'flagged': True,
            'categories': {'sexual': category, 'violence': True},
            'category_scores': {'sexual': score, 'violence': 0.99},
            'category_applied_input_types': {'sexual': ['image']}}]}

    def image(self):
        buffer = io.BytesIO()
        Image.new('RGB', (8, 8), 'white').save(buffer, format='JPEG')
        return buffer.getvalue()

    def test_unrelated_categories_do_not_flag(self):
        self.assertFalse(parse_result(self.payload(), 0.5).flagged)

    def test_sexual_score_and_configured_boundary(self):
        self.assertTrue(parse_result(self.payload(0.5, True), 0.5).flagged)
        self.assertFalse(parse_result(self.payload(0.49, True), 0.5).flagged)

    def test_malformed_or_missing_results_never_pass(self):
        for payload in ({}, {'results': []}, None, self.payload(float('nan')),
                        self.payload(True), self.payload(1.1)):
            with self.assertRaises(MediaScreeningUnavailable):
                parse_result(payload, 0.5)

    def test_missing_image_coverage_never_passes(self):
        payload = self.payload()
        payload['results'][0]['category_applied_input_types']['sexual'] = []
        with self.assertRaises(MediaScreeningUnavailable):
            parse_result(payload, 0.5)

    @patch('core.openai_media_moderation.urllib.request.build_opener')
    def test_safety_hold_never_sends(self, opener):
        for hold in (True, None, 0):
            with self.assertRaises(MediaSafetyHold):
                screen_image(self.image(), threshold=0.5, consent_confirmed=True, child_safety_hold=hold)
        opener.assert_not_called()

    @patch('core.openai_media_moderation.urllib.request.build_opener')
    def test_consent_required_before_network(self, opener):
        with self.assertRaises(MediaScreeningUnavailable):
            screen_image(self.image(), threshold=0.5, consent_confirmed=False, child_safety_hold=False)
        opener.assert_not_called()

    @patch('core.openai_media_moderation.urllib.request.build_opener')
    def test_image_request_uses_exact_model_and_private_bytes(self, opener):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(self.payload()).encode()
        opener.return_value.open.return_value = response
        verdict = screen_image(self.image(), threshold=0.5, consent_confirmed=True, child_safety_hold=False)
        self.assertFalse(verdict.flagged)
        request = opener.return_value.open.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(body['model'], 'omni-moderation-latest')
        self.assertTrue(body['input'][0]['image_url']['url'].startswith('data:image/jpeg;base64,'))

    @patch('core.openai_media_moderation.urllib.request.build_opener')
    def test_timeout_and_rate_limit_remain_unavailable(self, opener):
        for error in (TimeoutError(), urllib.error.HTTPError('https://api.openai.com', 429,
                       'Rate limited', {'Retry-After': '120'}, None)):
            opener.return_value.open.side_effect = error
            with self.assertRaises(MediaScreeningUnavailable) as caught:
                screen_image(self.image(), threshold=0.5, consent_confirmed=True, child_safety_hold=False)
            self.assertIn(caught.exception.retry_after, (60, 120))
