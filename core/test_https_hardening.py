"""Regression coverage for HTTPS proxy handling and edge header inheritance."""
import importlib.util
from pathlib import Path

from django.http import HttpResponse
from django.middleware.security import SecurityMiddleware
from django.test import RequestFactory, SimpleTestCase, override_settings

spec = importlib.util.spec_from_file_location(
    'configure_https', Path(__file__).resolve().parents[1] / 'deploy/configure_https.py')
rollout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rollout)


@override_settings(SECURE_SSL_REDIRECT=True, SECURE_HSTS_SECONDS=3600,
                   SECURE_HSTS_INCLUDE_SUBDOMAINS=False, SECURE_HSTS_PRELOAD=False,
                   SECURE_PROXY_SSL_HEADER=('HTTP_X_FORWARDED_PROTO', 'https'))
class HTTPSMiddlewareTests(SimpleTestCase):
    def test_trusted_https_is_not_redirected_and_has_narrow_hsts(self):
        request = RequestFactory().get('/api/v1/', HTTP_X_FORWARDED_PROTO='https')
        response = SecurityMiddleware(lambda request: HttpResponse(status=401))(request)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response['Strict-Transport-Security'], 'max-age=3600')

    def test_http_fallback_preserves_path_and_query(self):
        request = RequestFactory().get('/access/reports/?kind=post')
        response = SecurityMiddleware(lambda request: HttpResponse())(request)
        self.assertEqual(response.status_code, 301)
        self.assertEqual(response['Location'], 'https://testserver/access/reports/?kind=post')


class HTTPSRolloutTests(SimpleTestCase):
    def site(self):
        return ('server {\n ssl_certificate_key /key;\n}\n' * 2
                + 'location /assets/ {\n add_header Cache-Control "public, immutable";\n}\n'
                + 'proxy_pass http://127.0.0.1:8001;\n'
                  'proxy_set_header X-Forwarded-Proto https;\n' * 4)

    def test_web_static_and_api_policy_idempotent(self):
        rendered = rollout.site_config(self.site())
        self.assertEqual(rendered.count(rollout.HSTS), 3)
        self.assertEqual(rendered.count('proxy_hide_header Strict-Transport-Security;'), 2)
        self.assertEqual(rollout.site_config(rendered), rendered)
        self.assertIn('public, immutable', rendered)

    def test_private_video_headers_preserved(self):
        text = ('location /_private_video/ {\n internal;\n'
                ' add_header X-Content-Type-Options nosniff always;\n'
                ' add_header Cache-Control "private, no-store" always;\n}\n'
                + 'proxy_pass http://127.0.0.1:8001;\n'
                  'proxy_set_header X-Forwarded-Proto https;\n' * 2)
        rendered = rollout.video_config(text)
        self.assertEqual(rendered.count(rollout.HSTS), 1)
        self.assertIn('private, no-store', rendered)
        self.assertIn('internal;', rendered)
        self.assertEqual(rollout.video_config(rendered), rendered)

    def test_unexpected_header_or_proxy_policy_refused(self):
        for text in (self.site() + '\n' + rollout.HSTS,
                     self.site().replace('X-Forwarded-Proto https', 'X-Forwarded-Proto $http_x_forwarded_proto')):
            with self.assertRaises(ValueError):
                rollout.site_config(text)

    def test_environment_preserves_unrelated_values_and_is_idempotent(self):
        original = 'SECRET="test-only"\nMODERATION_ENABLED=false\nDJANGO_SECURE_HSTS_SECONDS=0\n'
        updated = rollout.environment(original)
        self.assertIn('SECRET="test-only"\nMODERATION_ENABLED=false\n', updated)
        self.assertEqual(updated.count('DJANGO_SECURE_HSTS_SECONDS='), 1)
        self.assertIn('DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS=false', updated)
        self.assertEqual(rollout.environment(updated), updated)
