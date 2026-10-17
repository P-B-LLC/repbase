from unittest.mock import patch
from django.conf import settings
from django.test import TestCase, override_settings
from rest_framework.test import APIClient, APIRequestFactory
from .fixtures_for_tests import make_account, workout_post
from .models import ModerationConsent
from .moderation import consent_given
from .media_screening_jobs import enqueue, claim, process


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class SafetyConsentTests(TestCase):
    def setUp(self):
        self.user, self.profile, _ = make_account('consent_user')
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.url = '/api/v1/me/safety-consent/'

    def choose(self, granted, version=None):
        return self.client.put(self.url, {'version': version or settings.MODERATION_ACCOUNT_CONSENT_VERSION,
                                         'granted': granted}, format='json')

    def test_private_default_and_persisted_grant(self):
        response = self.client.get(self.url)
        self.assertFalse(response.data['granted'])
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(self.choose(True).status_code, 200)
        self.assertTrue(self.client.get(self.url).data['granted'])
        request = APIRequestFactory().post('/')
        request.user = self.user
        self.assertTrue(consent_given(request))

    def test_other_account_does_not_inherit(self):
        self.choose(True)
        other, _, _ = make_account('other_consent')
        self.client.force_authenticate(other)
        self.assertFalse(self.client.get(self.url).data['granted'])

    def test_anonymous_cannot_read_or_write(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(self.url).status_code, 401)
        self.assertEqual(self.choose(True).status_code, 401)

    def test_stale_or_invalid_consent_is_not_saved(self):
        self.assertEqual(self.choose(True, 'old').status_code, 400)
        self.assertEqual(self.client.put(self.url, {'granted': True}, format='json').status_code, 400)
        self.assertFalse(ModerationConsent.objects.exists())

    def test_withdrawal_overrides_legacy_header(self):
        self.choose(False)
        request = APIRequestFactory().post('/', HTTP_X_MODERATION_CONSENT=settings.MODERATION_CONSENT_VERSION)
        request.user = self.user
        self.assertFalse(consent_given(request))

    def test_version_change_requires_new_grant(self):
        self.choose(True)
        with override_settings(MODERATION_ACCOUNT_CONSENT_VERSION='next'):
            self.assertFalse(self.client.get(self.url).data['granted'])

    def test_withdrawal_cancels_claimed_job_even_after_regrant(self):
        self.choose(True)
        post = workout_post(self.profile)
        job = enqueue(post.pk, consent_version=settings.MODERATION_CONSENT_VERSION, threshold=.5)
        claimed = claim()
        self.choose(False)
        self.choose(True)
        with patch('core.media_screening_jobs.screen_image') as provider:
            self.assertFalse(process(claimed))
        provider.assert_not_called()
        job.refresh_from_db()
        post.refresh_from_db()
        self.assertEqual(job.state, 'cancelled')
        self.assertIsNone(post.published_at)

    def test_session_write_requires_csrf(self):
        browser = APIClient(enforce_csrf_checks=True)
        browser.force_login(self.user)
        self.assertEqual(browser.put(self.url, {'version': settings.MODERATION_ACCOUNT_CONSENT_VERSION,
            'granted': True}, format='json').status_code, 403)

    def test_withdrawal_during_provider_call_blocks_publication(self):
        from io import BytesIO
        from .openai_media_moderation import MediaVerdict
        self.choose(True)
        post = workout_post(self.profile)
        post.image = 'post-photos/synthetic-placeholder.jpg'
        post.save(update_fields=['image'])
        enqueue(post.pk, consent_version=settings.MODERATION_CONSENT_VERSION, threshold=.5)
        def withdraw(*args, **kwargs):
            self.assertEqual(self.choose(False).status_code, 200)
            return MediaVerdict(False, .1, False, 'test')
        with patch('core.media_screening_jobs.default_storage.open', return_value=BytesIO(b'synthetic')), \
             patch('core.media_screening_jobs.screen_image', side_effect=withdraw):
            self.assertFalse(process(claim()))
        post.refresh_from_db()
        self.assertIsNone(post.published_at)

    def test_legacy_submission_does_not_silently_grant_account_consent(self):
        request = APIRequestFactory().post('/', HTTP_X_MODERATION_CONSENT=settings.MODERATION_CONSENT_VERSION)
        request.user = self.user
        self.assertTrue(consent_given(request))
        self.assertFalse(ModerationConsent.objects.exists())
