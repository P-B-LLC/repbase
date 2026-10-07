from django.conf import settings
from django.test import override_settings
from rest_framework.test import APITestCase, APIClient
from rest_framework.authtoken.models import Token
from .tests import RepbaseAPITestMixin


@override_settings(DEBUG=True, MODERATION_ENABLED=False)
class BrowserAuthTests(RepbaseAPITestMixin, APITestCase):
    def setUp(self):
        self.user, self.profile, self.token = self.create_account('browseruser')
        self.client = APIClient(enforce_csrf_checks=True)

    def bootstrap(self):
        response = self.client.get('/api/v1/web/session/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('no-store', response['Cache-Control'])
        return response.data['csrf_token']

    def login(self):
        csrf = self.bootstrap()
        return self.client.post('/api/v1/web/login/', {'username': 'browseruser', 'password': 'StrongPass!234'}, format='json', HTTP_X_CSRFTOKEN=csrf)

    def test_login_requires_csrf_even_when_anonymous(self):
        response = self.client.post('/api/v1/web/login/', {'username': 'browseruser', 'password': 'StrongPass!234'}, format='json')
        self.assertEqual(response.status_code, 403)

    def test_cookie_login_no_token_leak_and_logout_does_not_revoke_native(self):
        response = self.login()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn('token', response.data)
        self.assertTrue(response.cookies[settings.SESSION_COOKIE_NAME]['httponly'])
        self.assertEqual(response.cookies[settings.SESSION_COOKIE_NAME]['samesite'], 'Lax')
        self.assertEqual(self.client.get('/api/v1/me/').status_code, 200)
        self.assertEqual(self.client.post('/api/v1/web/logout/').status_code, 403)
        response = self.client.post('/api/v1/web/logout/', HTTP_X_CSRFTOKEN=response.data['csrf_token'])
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.client.get('/api/v1/web/session/').data['user'])
        self.assertTrue(Token.objects.filter(pk=self.token.pk).exists())

    def test_mutations_reject_missing_csrf_and_accept_rotated_nonce(self):
        response = self.login()
        self.assertEqual(self.client.patch('/api/v1/me/', {'first_name': 'Edited'}, format='json').status_code, 403)
        self.assertEqual(self.client.patch('/api/v1/me/', {'first_name': 'Edited'}, format='json', HTTP_X_CSRFTOKEN=response.data['csrf_token']).status_code, 200)

    def test_cross_origin_login_rejected(self):
        csrf = self.bootstrap()
        response = self.client.post('/api/v1/web/login/', {'username': 'browseruser', 'password': 'StrongPass!234'}, format='json', HTTP_X_CSRFTOKEN=csrf, HTTP_ORIGIN='https://attacker.invalid')
        self.assertEqual(response.status_code, 403)

    def test_disabled_account_cannot_login(self):
        self.user.is_active = False
        self.user.save()
        self.assertEqual(self.login().status_code, 400)

    def test_register_uses_cookie_without_creating_token(self):
        csrf = self.bootstrap()
        response = self.client.post('/api/v1/web/register/', dict(username='newbrowser', email='new@example.com', first_name='New', last_name='Browser', password='UniquePass!234'), format='json', HTTP_X_CSRFTOKEN=csrf)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertNotIn('token', response.data)
        self.assertFalse(Token.objects.filter(user__username='newbrowser').exists())
        self.assertEqual(self.client.get('/api/v1/me/').status_code, 200)

    @override_settings(SESSION_COOKIE_SECURE=True, CSRF_COOKIE_SECURE=True)
    def test_production_cookie_flags_and_session_rotation(self):
        self.bootstrap()
        session = self.client.session
        session['pre_login'] = True
        session.save()
        previous_key = session.session_key
        response = self.login()
        cookie = response.cookies[settings.SESSION_COOKIE_NAME]
        self.assertTrue(cookie['secure'])
        self.assertTrue(cookie['httponly'])
        self.assertTrue(response.cookies[settings.CSRF_COOKIE_NAME]['secure'])
        self.assertTrue(response.cookies[settings.CSRF_COOKIE_NAME]['httponly'])
        self.assertNotEqual(previous_key, cookie.value)

    def test_local_proxy_preserved_host_allows_same_origin_csrf(self):
        csrf = self.bootstrap()
        response = self.client.post('/api/v1/web/login/', {'username': 'browseruser', 'password': 'StrongPass!234'}, format='json', HTTP_X_CSRFTOKEN=csrf, HTTP_HOST='localhost:5173', HTTP_ORIGIN='http://localhost:5173')
        self.assertEqual(response.status_code, 200)
