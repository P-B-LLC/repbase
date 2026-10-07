from django.test import Client, override_settings
from rest_framework.test import APITestCase
from rest_framework.authtoken.models import Token
from .tests import RepbaseAPITestMixin
from .access_models import AccountAccess, AccessAudit


@override_settings(DEBUG=True, ANALYTICS_ENABLED=False)
class AccountControlTests(RepbaseAPITestMixin, APITestCase):
    def setUp(self):
        self.mod, self.mod_profile, self.mod_token = self.create_account('moderator')
        AccountAccess.objects.create(user=self.mod, moderator=True)
        self.member, self.profile, self.token = self.create_account('member')
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.mod_token.key}')

    def update(self, active=False, version=0, profile=None, **overrides):
        profile = profile or self.profile
        data = dict(active=active, version=version, reason='Community policy violation', confirm_username=profile.user.username)
        data.update(overrides)
        return self.client.put(f'/api/v1/administration/users/{profile.pk}/status/', data, format='json')

    def test_disable_restore_revokes_tokens_preserves_data_and_audits(self):
        response = self.update()
        self.assertEqual(response.status_code, 200, response.data)
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_active)
        self.assertFalse(Token.objects.filter(pk=self.token.pk).exists())
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.user_id, self.member.pk)
        self.assertEqual(self.update(active=True, version=1).status_code, 200)
        self.member.refresh_from_db()
        self.assertTrue(self.member.is_active)
        self.assertFalse(Token.objects.filter(user=self.member).exists())
        self.assertEqual(AccessAudit.objects.count(), 2)

    def test_disabled_accounts_remain_searchable(self):
        self.update()
        response = self.client.get('/api/v1/administration/users/?search=member')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['results'][0]['active'])

    def test_moderator_cannot_control_privileged_or_self(self):
        for role in ('owner', 'moderator', 'analytics', 'superowner'):
            AccountAccess.objects.filter(user=self.member).delete()
            AccountAccess.objects.create(user=self.member, **{role: True})
            self.assertEqual(self.update().status_code, 403, role)
        self.assertEqual(self.update(profile=self.mod_profile).status_code, 403)

    def test_owner_controls_moderator_but_not_peer_superowner_or_staff(self):
        AccountAccess.objects.filter(user=self.mod).update(owner=True)
        for role, expected in [('moderator', 200), ('owner', 403), ('superowner', 403)]:
            AccountAccess.objects.filter(user=self.member).delete()
            self.member.is_active = True
            self.member.save()
            AccountAccess.objects.create(user=self.member, **{role: True})
            self.assertEqual(self.update().status_code, expected)
        self.member.is_staff = True
        self.member.save()
        self.assertEqual(self.update().status_code, 403)

    def test_superowner_controls_owner_not_self(self):
        AccountAccess.objects.filter(user=self.mod).update(superowner=True)
        AccountAccess.objects.create(user=self.member, owner=True)
        self.assertEqual(self.update().status_code, 200)
        self.assertEqual(self.update(active=True, version=1).status_code, 200)
        self.assertEqual(self.update(profile=self.mod_profile).status_code, 403)

    def test_requires_reason_confirmation_version_and_current_authority(self):
        self.assertEqual(self.update(reason=' ').status_code, 400)
        self.assertEqual(self.update(confirm_username='wrong').status_code, 400)
        self.assertEqual(self.update(version=8).status_code, 409)
        AccountAccess.objects.filter(user=self.mod).update(moderator=False)
        self.assertEqual(self.update().status_code, 403)
        self.assertFalse(AccessAudit.objects.exists())

    def test_cannot_restore_external_disable(self):
        self.member.is_active = False
        self.member.save()
        self.assertEqual(self.update(active=True).status_code, 403)

    def test_cookie_cannot_revive_after_restore_and_new_login_works(self):
        browser = Client()
        browser.force_login(self.member)
        self.update()
        self.update(active=True, version=1)
        self.assertEqual(browser.get('/api/v1/me/').status_code, 401)
        browser.force_login(self.member)
        self.assertEqual(browser.get('/api/v1/me/').status_code, 200)

    def test_portal_csrf_and_disabled_token(self):
        browser = Client(enforce_csrf_checks=True)
        browser.force_login(self.mod)
        path = f'/access/users/{self.profile.pk}/status/'
        self.assertEqual(browser.get(path).status_code, 200)
        self.assertEqual(browser.post(path, {}).status_code, 403)
        self.update()
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        self.assertEqual(self.client.get('/api/v1/me/').status_code, 401)
