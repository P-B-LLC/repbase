from django.conf import settings
from django.test import TestCase, override_settings
from .serializers import RegisterSerializer
from .models import AccountAgreementAcceptance, ModerationConsent


@override_settings(MODERATION_ENABLED=False,
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class SignupAgreementTests(TestCase):
    def register(self, **extra):
        serializer = RegisterSerializer(data=dict(username='agreement_test',
            email='agreement@example.org', first_name='Example', last_name='Person',
            password='test-only-UniqueP4ss!937', **extra))
        serializer.is_valid(raise_exception=True)
        return serializer.save()

    def test_acceptance_and_screening_preference_are_saved_together(self):
        profile = self.register(agreement_version=settings.ACCOUNT_AGREEMENT_VERSION)
        self.assertTrue(AccountAgreementAcceptance.objects.filter(user=profile.user,
            version=settings.ACCOUNT_AGREEMENT_VERSION).exists())
        self.assertTrue(ModerationConsent.objects.get(user=profile.user).granted)

    def test_legacy_signup_does_not_invent_acceptance(self):
        self.register()
        self.assertFalse(AccountAgreementAcceptance.objects.exists())
        self.assertFalse(ModerationConsent.objects.exists())

    def test_old_agreement_is_rejected(self):
        from rest_framework.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            self.register(agreement_version='old')
        self.assertFalse(AccountAgreementAcceptance.objects.exists())

    def test_existing_account_acceptance_is_private_and_idempotent(self):
        from rest_framework.test import APIClient
        profile = self.register()
        client = APIClient()
        client.force_authenticate(profile.user)
        url = '/api/v1/me/agreements/'
        response = client.get(url)
        self.assertFalse(response.data['accepted'])
        self.assertIn('no-store', response['Cache-Control'])
        body = dict(version=settings.ACCOUNT_AGREEMENT_VERSION, accepted=True)
        self.assertEqual(client.put(url, body, format='json').status_code, 200)
        self.assertTrue(client.get(url).data['accepted'])
        ModerationConsent.objects.filter(user=profile.user).update(granted=False)
        self.assertEqual(client.put(url, body, format='json').status_code, 200)
        self.assertFalse(ModerationConsent.objects.get(user=profile.user).granted)
        self.assertEqual(AccountAgreementAcceptance.objects.count(), 1)

    def test_anonymous_and_invalid_acceptance_are_rejected(self):
        from rest_framework.test import APIClient
        client = APIClient()
        url = '/api/v1/me/agreements/'
        self.assertEqual(client.get(url).status_code, 401)
        self.assertEqual(client.put(url, {}, format='json').status_code, 401)
        profile = self.register()
        client.force_authenticate(profile.user)
        for body in ({}, {'version': 'old', 'accepted': True},
                     {'version': settings.ACCOUNT_AGREEMENT_VERSION, 'accepted': False}):
            self.assertEqual(client.put(url, body, format='json').status_code, 400)
        self.assertFalse(AccountAgreementAcceptance.objects.exists())

    def test_browser_acceptance_requires_csrf(self):
        from rest_framework.test import APIClient
        profile = self.register()
        client = APIClient(enforce_csrf_checks=True)
        client.force_login(profile.user)
        self.assertEqual(client.put('/api/v1/me/agreements/', dict(
            version=settings.ACCOUNT_AGREEMENT_VERSION, accepted=True), format='json').status_code, 403)

    def test_acceptance_does_not_apply_to_another_account(self):
        from rest_framework.test import APIClient
        from .fixtures_for_tests import make_account
        profile = self.register(agreement_version=settings.ACCOUNT_AGREEMENT_VERSION)
        other, _, _ = make_account('other_agreement')
        client = APIClient()
        client.force_authenticate(other)
        self.assertFalse(client.get('/api/v1/me/agreements/').data['accepted'])
        self.assertTrue(AccountAgreementAcceptance.objects.filter(user=profile.user).exists())

    def test_failed_signup_does_not_leave_acceptance_or_user(self):
        from unittest.mock import patch
        from django.contrib.auth import get_user_model
        with patch('core.serializers.RepbaseUser.objects.create', side_effect=RuntimeError('fixture failure')):
            with self.assertRaises(RuntimeError):
                self.register(agreement_version=settings.ACCOUNT_AGREEMENT_VERSION)
        self.assertFalse(get_user_model().objects.filter(username='agreement_test').exists())
        self.assertFalse(AccountAgreementAcceptance.objects.exists())
        self.assertFalse(ModerationConsent.objects.exists())
