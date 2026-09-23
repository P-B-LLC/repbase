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


@override_settings(DEBUG=True, ANALYTICS_ENABLED=True)
class DashboardAnswersSoWhatTests(RepbaseAPITestMixin, APITestCase):
    """Every figure has to survive the question a total cannot answer.

    The page used to report "14,820 requests" and stop, which is a report
    rather than an instrument: the number is neither good nor bad on its own
    and nobody can act on it. These pin the parts that make it actionable --
    the comparison, the two ratios, and the list of what to open first.
    """

    def setUp(self):
        self.admin, self.profile, self.token = self.create_account('so-what-owner')
        self.admin.email = 'admin@rytivo.app'
        self.admin.is_staff = True
        self.admin.save()
        AnalyticsAccess.objects.create(user=self.admin, verified_email='admin@rytivo.app',
                                       verified_at=timezone.now(), enabled=True)
        self.client.force_login(self.admin)
        self.today = timezone.now().date()

    def metric(self, day, requests=1, server_errors=0, total_ms=100, max_ms=100, endpoint='planner-detail'):
        return DailyApiMetric.objects.create(day=day, endpoint=endpoint, method='GET',
            requests=requests, server_errors=server_errors, total_ms=total_ms, max_ms=max_ms)

    def dashboard(self, days=7):
        return self.client.get(reverse('insights-dashboard'), {'days': days})

    def test_a_total_is_compared_with_the_period_before_it(self):
        self.metric(self.today, requests=100, total_ms=100)
        self.metric(self.today - timedelta(days=8), requests=50, total_ms=50)

        deltas = self.dashboard().context['deltas']
        self.assertEqual(deltas['requests']['text'], '+100% vs previous 7 days')
        self.assertEqual(deltas['requests']['tone'], 'good')

    def test_a_rise_in_errors_is_not_good_news(self):
        # Direction alone is not the signal. More requests is a rise worth
        # having; more failures is the same arithmetic meaning the opposite.
        self.metric(self.today, requests=100, server_errors=10, total_ms=100)
        self.metric(self.today - timedelta(days=8), requests=100, server_errors=1, total_ms=100)

        self.assertEqual(self.dashboard().context['deltas']['error_rate']['tone'], 'bad')

    def test_an_empty_previous_period_says_so_rather_than_inventing_a_rise(self):
        self.metric(self.today, requests=100, total_ms=100)

        delta = self.dashboard().context['deltas']['requests']
        self.assertEqual(delta['text'], 'no baseline')
        self.assertEqual(delta['tone'], 'flat')

    def test_a_failing_route_is_listed_first_with_something_to_do(self):
        self.metric(self.today, requests=200, total_ms=2000, max_ms=20, endpoint='quiet-route')
        self.metric(self.today, requests=10, server_errors=3, total_ms=100, max_ms=50, endpoint='breaking-route')

        attention = self.dashboard().context['attention']
        self.assertEqual(attention[0]['endpoint'], 'breaking-route')
        self.assertEqual(attention[0]['severity'], 'error')
        self.assertTrue(attention[0]['action'])

    def test_a_slow_route_is_caught_by_its_worst_case_not_its_average(self):
        # The whole point of keeping max_ms. A route averaging 60ms with one
        # 9-second request is a real problem that the average hides, and the
        # average was all this page had ever shown.
        self.metric(self.today, requests=300, total_ms=18000, max_ms=9000, endpoint='occasionally-awful')

        attention = self.dashboard().context['attention']
        self.assertEqual(attention[0]['severity'], 'slow')
        self.assertIn('9000', attention[0]['headline'])

    def test_a_healthy_route_is_not_listed(self):
        self.metric(self.today, requests=500, total_ms=25000, max_ms=90, endpoint='fine-route')
        self.assertEqual(self.dashboard().context['attention'], [])

    def test_returning_and_lapsed_accounts_are_counted_separately(self):
        stayed, _, _ = self.create_account('stayed')
        left, _, _ = self.create_account('left')
        arrived, _, _ = self.create_account('arrived')
        for user in (stayed, left):
            DailyActiveAccount.objects.create(day=self.today - timedelta(days=8), user=user)
        for user in (stayed, arrived):
            DailyActiveAccount.objects.create(day=self.today, user=user)

        context = self.dashboard().context
        self.assertEqual(context['returning'], 1)
        self.assertEqual(context['lapsed'], 1)
        self.assertEqual(context['new_to_api'], 1)
        self.assertEqual(context['return_rate'], 50)

    def test_a_new_account_that_did_nothing_counts_against_activation(self):
        from .models import WorkoutSession
        started, started_profile, _ = self.create_account('started')
        self.create_account('never-started')
        WorkoutSession.objects.create(repbase_user=started_profile, status='completed',
                                      started_at=timezone.now(), ended_at=timezone.now())

        context = self.dashboard().context
        # Three accounts were created today: the two here and setUp's owner,
        # which is staff and so excluded.
        self.assertEqual(context['signups'], 2)
        self.assertEqual(context['activated'], 1)
        self.assertEqual(context['activation_rate'], 50)
