from datetime import timedelta
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError
from django.http import HttpResponse
from django.test import RequestFactory, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase, APIClient

from .analytics import AnalyticsMiddleware
from .analytics_models import AnalyticsAccess, DailyActiveAccount, DailyApiMetric
from .tests import RepbaseAPITestMixin


@override_settings(DEBUG=True, ANALYTICS_ENABLED=False)
class AnalyticsTests(RepbaseAPITestMixin, APITestCase):
    def setUp(self):
        self.admin, self.profile, self.token = self.create_account('insights-owner')
        self.admin.email = 'admin@rytivo.app'
        self.admin.is_staff = True
        self.admin.save()

    def approve(self):
        return AnalyticsAccess.objects.create(user=self.admin, verified_email='admin@rytivo.app',
            verified_at=timezone.now(), enabled=True)

    def request_metric(self, status=200, user=None, method='GET'):
        request = RequestFactory().generic(method, '/api/v1/planner/123/?secret=never-store-this')
        request.resolver_match = SimpleNamespace(view_name='planner-detail')
        request.user = user or self.admin
        return AnalyticsMiddleware(lambda request: HttpResponse(status=status))(request)

    def test_anonymous_redirect_and_uncacheable(self):
        response = self.client.get('/insights/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/insights/login/', response.url)
        self.assertIn('no-store', response['Cache-Control'])

    def test_matching_email_without_grant_is_denied(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get('/insights/').status_code, 403)

    def test_server_superuser_can_access_without_email_matching(self):
        self.approve()
        self.admin.email = 'other@example.com'
        self.admin.is_superuser = True
        self.admin.save()
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get('/insights/').status_code, 200)

    def test_approved_dashboard_renders_and_has_security_headers(self):
        self.approve()
        self.client.force_login(self.admin)
        response = self.client.get('/insights/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'See what')
        self.assertContains(response, 'API collection is off')
        self.assertNotContains(response, 'insights-owner@example.com')
        self.assertEqual(response['X-Robots-Tag'], 'noindex, nofollow')
        self.assertIn("frame-ancestors 'none'", response['Content-Security-Policy'])

    def test_revocation_applies_to_existing_session(self):
        grant = self.approve()
        self.client.force_login(self.admin)
        grant.enabled = False
        grant.save()
        self.assertEqual(self.client.get('/insights/').status_code, 403)

    def test_invalid_and_unbounded_ranges_rejected(self):
        self.approve()
        self.client.force_login(self.admin)
        for days in ('0', '999999', 'bad', '-1'):
            self.assertEqual(self.client.get('/insights/', {'days': days}).status_code, 400)

    def test_login_requires_grant_even_with_correct_password(self):
        response = self.client.post('/insights/login/', {'username': self.admin.username, 'password': 'StrongPass!234'})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_approved_login_and_post_only_logout(self):
        self.approve()
        response = self.client.post('/insights/login/', {'username': self.admin.username, 'password': 'StrongPass!234'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/insights/')
        self.assertLessEqual(self.client.session.get_expiry_age(), 1800)
        self.assertEqual(self.client.get('/insights/logout/').status_code, 405)
        self.assertEqual(self.client.post('/insights/logout/').status_code, 302)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_login_csrf_enforced(self):
        client = APIClient(enforce_csrf_checks=True)
        self.assertEqual(client.post('/insights/login/', {'username': 'anything'}).status_code, 403)

    def test_https_login_without_origin_accepts_same_origin_referer(self):
        self.approve()
        client = APIClient(enforce_csrf_checks=True)
        page = client.get('/insights/login/', secure=True)
        self.assertEqual(page['Referrer-Policy'], 'same-origin')
        response = client.post('/insights/login/', {
            'username': self.admin.username, 'password': 'StrongPass!234',
            'csrfmiddlewaretoken': client.cookies['csrftoken'].value,
        }, secure=True, HTTP_REFERER='https://testserver/insights/login/')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/insights/')

    def test_https_login_rejects_external_referer_even_with_valid_token(self):
        client = APIClient(enforce_csrf_checks=True)
        client.get('/insights/login/', secure=True)
        response = client.post('/insights/login/', {
            'csrfmiddlewaretoken': client.cookies['csrftoken'].value,
        }, secure=True, HTTP_REFERER='https://untrusted.example/')
        self.assertEqual(response.status_code, 403)

    def test_login_rate_limit(self):
        for _ in range(5):
            self.client.post('/insights/login/', {'username': 'unknown', 'password': 'wrong'})
        self.assertEqual(self.client.post('/insights/login/', {}).status_code, 429)

    def test_rate_limit_failure_denies_login(self):
        with patch('core.analytics.cache.add', side_effect=RuntimeError('offline')):
            self.assertEqual(self.client.post('/insights/login/', {}).status_code, 503)

    @override_settings(DEBUG=False, SESSION_COOKIE_SECURE=False, ANALYTICS_LOGIN_SHARED_LIMIT_CONFIRMED=True)
    def test_production_refuses_insecure_session_configuration(self):
        self.assertEqual(self.client.get('/insights/login/').status_code, 503)

    @override_settings(DEBUG=False, SESSION_COOKIE_SECURE=True, CSRF_COOKIE_SECURE=True,
                       ANALYTICS_LOGIN_SHARED_LIMIT_CONFIRMED=False)
    def test_production_requires_shared_rate_limit_confirmation(self):
        self.assertEqual(self.client.get('/insights/login/').status_code, 503)

    def test_future_verification_is_not_accepted(self):
        grant = self.approve()
        grant.verified_at = timezone.now() + timedelta(days=1)
        grant.save()
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get('/insights/').status_code, 403)

    def test_dashboard_uses_weighted_average_and_bounds_dates(self):
        self.approve()
        self.client.force_login(self.admin)
        today = timezone.now().date()
        DailyApiMetric.objects.create(day=today, endpoint='one', method='GET', requests=2, total_ms=200)
        DailyApiMetric.objects.create(day=today, endpoint='two', method='GET', requests=1, total_ms=400, server_errors=1)
        DailyApiMetric.objects.create(day=today - timedelta(days=7), endpoint='old', method='GET', requests=99)
        response = self.client.get('/insights/')
        self.assertEqual(response.context['requests'], 3)
        self.assertEqual(response.context['average_ms'], 200)
        self.assertEqual(response.context['error_rate'], 33.33)

    def test_retention_dry_run_preserves_rows(self):
        DailyApiMetric.objects.create(day=timezone.now().date() - timedelta(days=100), endpoint='old', method='GET')
        call_command('prune_analytics', dry_run=True, stdout=StringIO())
        self.assertEqual(DailyApiMetric.objects.count(), 1)

    def test_collection_off_writes_nothing(self):
        self.request_metric()
        self.assertFalse(DailyApiMetric.objects.exists())

    @override_settings(ANALYTICS_ENABLED=True)
    def test_metrics_aggregate_without_raw_urls_or_staff_presence(self):
        self.request_metric(status=500)
        self.request_metric(status=400)
        row = DailyApiMetric.objects.get()
        self.assertEqual((row.requests, row.server_errors, row.client_errors), (2, 1, 1))
        self.assertEqual(row.endpoint, 'planner-detail')
        self.assertFalse(DailyActiveAccount.objects.exists())

    @override_settings(ANALYTICS_ENABLED=True)
    def test_activity_deduplicates_and_cascades_on_account_deletion(self):
        self.admin.is_staff = False
        self.admin.save()
        self.request_metric()
        self.request_metric()
        self.assertEqual(DailyActiveAccount.objects.count(), 1)
        self.admin.delete()
        self.assertFalse(DailyActiveAccount.objects.exists())

    @override_settings(ANALYTICS_ENABLED=True)
    def test_metrics_failure_never_fails_original_save(self):
        with patch('core.analytics.DailyApiMetric.objects.get_or_create', side_effect=DatabaseError('failure')):
            self.assertEqual(self.request_metric(status=201).status_code, 201)

    def test_grant_requires_explicit_ownership_verification(self):
        with self.assertRaises(CommandError):
            call_command('grant_analytics_access', user_id=self.admin.pk, stdout=StringIO())
        self.assertFalse(AnalyticsAccess.objects.exists())
        call_command('grant_analytics_access', user_id=self.admin.pk, confirm_email_ownership=True, stdout=StringIO())
        self.assertTrue(AnalyticsAccess.objects.get().enabled)
        self.admin.refresh_from_db()
        self.assertFalse(self.admin.is_superuser)

    def test_grant_never_approves_other_email(self):
        self.admin.email = 'not-admin@example.com'
        self.admin.save()
        with self.assertRaises(CommandError):
            call_command('grant_analytics_access', user_id=self.admin.pk, confirm_email_ownership=True)

    def test_retention_removes_only_expired_metrics(self):
        today = timezone.now().date()
        DailyActiveAccount.objects.create(day=today - timedelta(days=30), user=self.admin)
        DailyActiveAccount.objects.create(day=today, user=self.admin)
        DailyApiMetric.objects.create(day=today - timedelta(days=90), endpoint='old', method='GET')
        DailyApiMetric.objects.create(day=today, endpoint='current', method='GET')
        call_command('prune_analytics', stdout=StringIO())
        self.assertEqual(DailyActiveAccount.objects.count(), 1)
        self.assertEqual(DailyApiMetric.objects.get().endpoint, 'current')
