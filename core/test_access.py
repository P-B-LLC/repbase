from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase, APIClient

from .access import assign_roles
from .access_models import AccountAccess, AccessAudit
from .analytics import allowed
from .analytics_models import AnalyticsAccess
from .models import Post, PostReport, PostComment, CommentReport
from .tests import RepbaseAPITestMixin


@override_settings(DEBUG=True, ANALYTICS_ENABLED=False)
class AccessTests(RepbaseAPITestMixin, APITestCase):
    def setUp(self):
        self.admin, self.admin_profile, self.admin_token = self.create_account('operator')
        self.admin.is_superuser = True
        self.admin.save()
        self.member, self.profile, self.token = self.create_account('member')
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.admin_token.key}')

    def url(self, profile=None):
        return f'/api/v1/administration/users/{(profile or self.profile).pk}/access/'

    def change(self, roles, version=0, profile=None):
        return self.client.put(self.url(profile), dict(roles=roles, version=version, reason='Approved'), format='json')

    def as_member(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')

    def test_owner_grant_multiple_roles_persists_and_audits(self):
        response = self.change(['analytics', 'moderator'])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['roles'], ['analytics', 'moderator'])
        self.assertEqual(response.data['version'], 1)
        self.assertFalse(self.member.is_staff)
        self.assertEqual(AccessAudit.objects.get().actor, self.admin)
        self.as_member()
        self.assertEqual(self.client.get('/api/v1/me/access/').data['capabilities'],
                         dict(manage_roles=False, manage_owners=False, view_analytics=True, moderate=True))

    def test_regular_staff_and_moderator_cannot_assign_roles(self):
        for staff, role in [(False, None), (True, None), (False, 'moderator'), (False, 'analytics')]:
            self.member.is_staff = staff
            self.member.save()
            AccountAccess.objects.filter(user=self.member).delete()
            if role:
                AccountAccess.objects.create(user=self.member, **{role: True})
            self.as_member()
            self.assertEqual(self.change(['owner']).status_code, 403)
            self.assertEqual(self.client.get('/api/v1/administration/users/', {'search': 'member'}).status_code, 403)
        self.assertFalse(AccessAudit.objects.exists())

    def test_owner_inherits_capabilities_and_can_assign_others(self):
        self.assertEqual(self.change(['owner']).status_code, 200)
        other, profile, _ = self.create_account('other')
        self.as_member()
        self.assertEqual(self.change(['moderator'], profile=profile).status_code, 200)
        self.assertTrue(allowed(self.member))
        self.assertTrue(AccountAccess.objects.get(user=other).moderator)

    def test_self_and_superuser_changes_rejected(self):
        self.assertEqual(self.change([], profile=self.admin_profile).status_code, 400)
        self.change(['owner'])
        self.as_member()
        self.assertEqual(self.change([], profile=self.admin_profile).status_code, 400)
        self.assertEqual(self.change([], version=1).status_code, 400)

    def test_stale_save_conflicts_and_does_not_overwrite(self):
        self.change(['moderator'])
        self.assertEqual(self.change(['owner']).status_code, 409)
        self.assertEqual(AccessAudit.objects.count(), 1)
        self.assertFalse(AccountAccess.objects.get(user=self.member).owner)

    def test_unchanged_retry_does_not_write_audit(self):
        self.assertEqual(self.change([]).status_code, 200)
        self.assertFalse(AccountAccess.objects.exists())

    def test_invalid_roles_and_missing_version_rejected(self):
        for data in [dict(roles=['superuser'], version=0), dict(roles=['owner', 'owner'], version=0), dict(roles=['owner'])]:
            self.assertEqual(self.client.put(self.url(), data, format='json').status_code, 400)
        self.assertFalse(AccountAccess.objects.exists())

    def test_revocation_applies_to_same_token_and_browser_session(self):
        self.change(['analytics', 'moderator'])
        browser = APIClient()
        browser.force_login(self.member)
        self.assertEqual(browser.get('/insights/').status_code, 200)
        self.assertEqual(self.change([], 1).status_code, 200)
        self.assertEqual(browser.get('/insights/').status_code, 403)
        self.as_member()
        self.assertEqual(self.client.get('/api/v1/administration/reports/?kind=post').status_code, 403)

    def test_legacy_analytics_removal_cannot_fall_back_to_old_grant(self):
        self.member.email = 'admin@rytivo.app'
        self.member.is_staff = True
        self.member.save()
        AnalyticsAccess.objects.create(user=self.member, enabled=True, verified_email='admin@rytivo.app', verified_at=timezone.now())
        self.assertTrue(allowed(self.member))
        self.assertEqual(self.change(['analytics']).status_code, 200)
        self.assertTrue(allowed(self.member))
        self.assertEqual(self.change([]).status_code, 200)
        self.assertFalse(allowed(self.member))

    def test_role_endpoint_cannot_make_user_staff(self):
        response = self.client.put(self.url(), dict(roles=['moderator'], version=0, is_staff=True, is_superuser=True), format='json')
        self.assertEqual(response.status_code, 200)
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_staff)
        self.assertFalse(self.member.is_superuser)

    def test_anonymous_denied_and_search_is_bounded(self):
        self.assertEqual(self.client.get('/api/v1/administration/users/').status_code, 400)
        response = self.client.get('/api/v1/administration/users/?search=member')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('email', response.data['results'][0])
        self.assertIn('no-store', response['Cache-Control'])
        self.client.credentials()
        self.assertEqual(self.client.get('/api/v1/me/access/').status_code, 401)

    def test_moderator_can_hide_reported_posts_and_comments_only(self):
        self.change(['moderator'])
        post = Post.objects.create(author=self.admin_profile, kind='text', caption='Reported text')
        report = PostReport.objects.create(post=post, reporter=self.profile, reason='spam')
        comment = PostComment.objects.create(post=post, author=self.admin_profile, body='Reported reply')
        comment_report = CommentReport.objects.create(comment=comment, reporter=self.profile, reason='spam')
        self.as_member()
        for kind, row, content in [('post', report, post), ('comment', comment_report, comment)]:
            response = self.client.get(f'/api/v1/administration/reports/?kind={kind}')
            self.assertEqual(response.status_code, 200, response.data)
            self.assertNotIn('reporter', response.data['results'][0])
            url = f'/api/v1/administration/reports/{kind}/{row.pk}/decision/'
            self.assertEqual(self.client.post(url, dict(decision='hide', reason='Spam confirmed'), format='json').status_code, 200)
            self.assertEqual(self.client.post(url, dict(decision='hide', reason='Retry'), format='json').status_code, 409)
            content.refresh_from_db()
            self.assertTrue(content.is_hidden)
        self.assertEqual(AccessAudit.objects.filter(action='moderation_hide').count(), 2)

    def test_permission_rechecked_inside_service(self):
        self.change(['owner'])
        actor = self.member
        AccountAccess.objects.filter(user=actor).update(owner=False)
        from rest_framework.exceptions import PermissionDenied
        with self.assertRaises(PermissionDenied):
            assign_roles(actor, self.admin, ['owner'], 0, '')

    def test_inactive_grant_denied(self):
        self.member.is_active = False
        self.member.save()
        self.assertEqual(self.change(['owner']).status_code, 400)

    def test_portal_requires_role_and_csrf(self):
        browser = APIClient(enforce_csrf_checks=True)
        self.assertEqual(browser.get('/access/').status_code, 302)
        browser.force_login(self.member)
        self.assertEqual(browser.get('/access/').status_code, 403)
        self.assertEqual(browser.get(f'/access/users/{self.profile.pk}/').status_code, 403)
        browser.force_login(self.admin)
        response = browser.get('/access/?search=member')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Manage access')
        self.assertIn('no-store', response['Cache-Control'])
        self.assertIn("frame-ancestors 'none'", response['Content-Security-Policy'])
        self.assertEqual(browser.post(f'/access/users/{self.profile.pk}/', dict(roles=['owner'], version=0)).status_code, 403)
        self.assertFalse(AccountAccess.objects.exists())

    def test_portal_and_api_share_role_service(self):
        browser = APIClient()
        browser.force_login(self.admin)
        response = browser.post(f'/access/users/{self.profile.pk}/', dict(roles=['analytics'], version=0))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(allowed(self.member))
        self.assertEqual(self.client.get(self.url()).data['roles'], ['analytics'])
        self.assertEqual(AccessAudit.objects.count(), 1)
        stale = browser.post(f'/access/users/{self.profile.pk}/', dict(roles=['owner'], version=0))
        self.assertEqual(stale.status_code, 409)
        self.assertContains(stale, 'Reload', status_code=409)

    def test_moderator_portal_does_not_show_role_management(self):
        self.change(['moderator'])
        browser = APIClient()
        browser.force_login(self.member)
        response = browser.get('/access/')
        self.assertContains(response, 'Review reports')
        self.assertNotContains(response, 'People &amp; access')
        self.assertEqual(browser.get('/access/reports/').status_code, 200)
        self.assertEqual(browser.get('/access/reports/?kind=invalid').status_code, 400)

    def test_access_login_limits_and_revocation(self):
        from django.core.cache import cache
        browser = APIClient()
        cache.clear()
        response = browser.post('/access/login/', dict(username='member', password='StrongPass!234'))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('_auth_user_id', browser.session)
        self.change(['owner'])
        response = browser.post('/access/login/', dict(username='member', password='StrongPass!234'))
        self.assertEqual(response.status_code, 302)
        self.assertLessEqual(browser.session.get_expiry_age(), 1800)
        self.change([], 1)
        self.assertEqual(browser.get('/access/').status_code, 403)

    def test_bootstrap_is_explicit_and_does_not_elevate_django_access(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError
        from io import StringIO
        with self.assertRaises(CommandError):
            call_command('grant_app_owner', username='member')
        call_command('grant_app_owner', username='member', confirm_account_ownership=True, stdout=StringIO())
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_staff)
        self.assertFalse(self.member.is_superuser)
        self.assertTrue(AccountAccess.objects.get(user=self.member).owner)
        self.assertEqual(AccessAudit.objects.get().action, 'owner_bootstrap')

    def test_profile_edit_cannot_grant_roles(self):
        self.as_member()
        self.client.patch('/api/v1/me/', dict(roles=['owner'], owner=True,
                          is_staff=True, is_superuser=True, can_view_insights=True), format='json')
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_staff)
        self.assertFalse(self.member.is_superuser)
        self.assertFalse(AccountAccess.objects.exists())
        self.assertFalse(self.client.get('/api/v1/me/access/').data['capabilities']['manage_roles'])

    def test_no_action_preserves_content_and_anonymous_decision_denied(self):
        post = Post.objects.create(author=self.admin_profile, kind='text', caption='Allowed text')
        report = PostReport.objects.create(post=post, reporter=self.profile, reason='other')
        url = f'/api/v1/administration/reports/post/{report.pk}/decision/'
        self.client.credentials()
        self.assertEqual(self.client.post(url, dict(decision='hide', reason='Anything'), format='json').status_code, 401)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.admin_token.key}')
        self.assertEqual(self.client.post(url, dict(decision='no_action', reason='No violation'), format='json').status_code, 200)
        post.refresh_from_db()
        self.assertFalse(post.is_hidden)

    @override_settings(DEBUG=False, ANALYTICS_LOGIN_SHARED_LIMIT_CONFIRMED=False, SECURE_SSL_REDIRECT=False)
    def test_portal_fails_closed_without_shared_login_limit(self):
        self.assertEqual(self.client.get('/access/login/').status_code, 503)

    def test_role_responses_match_committed_contract(self):
        import json
        from pathlib import Path
        import yaml
        from .contract_check import response_schema, to_json_schema, violations
        spec = to_json_schema(yaml.safe_load((Path(__file__).resolve().parent.parent / 'openapi.yaml').read_text(encoding='utf-8')))
        post = Post.objects.create(author=self.admin_profile, kind='text', caption='Reported content')
        PostReport.objects.create(post=post, reporter=self.profile, reason='spam')
        for path, url in [
            ('/api/v1/me/access/', '/api/v1/me/access/'),
            ('/api/v1/administration/users/', '/api/v1/administration/users/?search=member'),
            ('/api/v1/administration/users/{profile_id}/access/', self.url()),
            ('/api/v1/administration/reports/', '/api/v1/administration/reports/?kind=post'),
        ]:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(violations(response_schema(spec, path), json.loads(response.content)), [], path)
