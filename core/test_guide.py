"""The welcome guide: opens by itself once per app, for new accounts only."""
from importlib import import_module

from django.apps import apps
from django.contrib.auth.models import User
from rest_framework.test import APITestCase

from .models import GuideCompletion
from .tests import RepbaseAPITestMixin


class GuideTests(RepbaseAPITestMixin, APITestCase):
    IOS = '/api/v1/me/guide/ios/'
    WEB = '/api/v1/me/guide/web/'

    def setUp(self):
        self.user, self.profile, self.token = self.create_account('newcomer')
        self.authenticate(self.token)

    def test_a_new_account_has_not_been_shown_round(self):
        response = self.client.get(self.IOS)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {'surface': 'ios', 'completed': False})
        self.assertIn('no-store', response['Cache-Control'])

    def test_finishing_is_remembered_and_repeatable(self):
        for _ in range(2):
            response = self.client.put(self.IOS, {'completed': True}, format='json')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, {'surface': 'ios', 'completed': True})
        self.assertTrue(self.client.get(self.IOS).data['completed'])
        self.assertEqual(GuideCompletion.objects.filter(user=self.user).count(), 1)

    def test_each_app_has_its_own_guide(self):
        self.client.put(self.IOS, {'completed': True}, format='json')
        self.assertFalse(self.client.get(self.WEB).data['completed'])

    def test_it_can_be_asked_to_open_again(self):
        self.client.put(self.WEB, {'completed': True}, format='json')
        response = self.client.put(self.WEB, {'completed': False}, format='json')
        self.assertEqual(response.data, {'surface': 'web', 'completed': False})
        self.assertFalse(self.client.get(self.WEB).data['completed'])

    def test_it_belongs_to_the_account_asking(self):
        self.client.put(self.IOS, {'completed': True}, format='json')
        _, _, other = self.create_account('someone_else')
        self.authenticate(other)
        self.assertFalse(self.client.get(self.IOS).data['completed'])

    def test_unknown_apps_bad_bodies_and_anonymous_requests_are_refused(self):
        self.assertEqual(self.client.get('/api/v1/me/guide/android/').status_code, 404)
        self.assertEqual(
            self.client.put('/api/v1/me/guide/android/', {'completed': True}, format='json').status_code, 404)
        self.assertEqual(self.client.put(self.IOS, {}, format='json').status_code, 400)
        self.assertEqual(self.client.put(self.IOS, {'completed': 'later'}, format='json').status_code, 400)
        self.client.credentials()
        self.assertEqual(self.client.get(self.IOS).status_code, 401)
        self.assertEqual(self.client.put(self.IOS, {'completed': True}, format='json').status_code, 401)
        self.assertFalse(GuideCompletion.objects.exists())

    def test_deleting_the_account_takes_its_guide_rows_with_it(self):
        self.client.put(self.IOS, {'completed': True}, format='json')
        self.user.delete()
        self.assertFalse(GuideCompletion.objects.exists())


class GuideBackfillTests(RepbaseAPITestMixin, APITestCase):
    """The migration's step that spares existing accounts the tour."""

    def test_accounts_that_already_exist_are_marked_shown_round_in_both_apps(self):
        first, _, _ = self.create_account('longtime')
        second, _, _ = self.create_account('regular')
        # One app already done must not trip the uniqueness constraint.
        GuideCompletion.objects.create(user=second, surface='ios')
        backfill = import_module('core.migrations.0082_guide_completion')
        backfill.existing_accounts_have_been_shown_round(apps, None)
        for user in (first, second):
            self.assertEqual(
                set(GuideCompletion.objects.filter(user=user).values_list('surface', flat=True)),
                {'ios', 'web'},
            )

    def test_an_account_registered_afterwards_is_shown_round(self):
        response = self.client.post('/api/v1/auth/register/', {
            'username': 'brand_new', 'email': 'brand_new@example.com',
            'password': 'StrongPass!234', 'first_name': 'Brand', 'last_name': 'New',
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {response.data['token']}")
        self.assertFalse(self.client.get('/api/v1/me/guide/ios/').data['completed'])
        self.assertFalse(GuideCompletion.objects.filter(user=User.objects.get(username='brand_new')).exists())
