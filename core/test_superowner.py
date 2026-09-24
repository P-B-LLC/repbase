from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import override_settings
from rest_framework.test import APITestCase, APIClient

from .access import capabilities
from .access_models import AccountAccess, AccessAudit
from .tests import RepbaseAPITestMixin


@override_settings(DEBUG=True, ANALYTICS_ENABLED=False)
class SuperownerTests(RepbaseAPITestMixin, APITestCase):
    def setUp(self):
        self.official, self.official_profile, self.official_token = self.create_account('Rytivo_Official')
        self.official.email = 'admin@rytivo.app'
        self.official.save()
        self.owner, self.owner_profile, self.owner_token = self.create_account('team-owner')
        AccountAccess.objects.create(user=self.owner, owner=True)
        self.member, self.member_profile, self.member_token = self.create_account('member')

    def bootstrap(self, **overrides):
        options = dict(username='Rytivo_Official', email='admin@rytivo.app', user_id=self.official.pk,
                       confirm_account_ownership=True, stdout=StringIO())
        options.update(overrides)
        call_command('grant_app_superowner', **options)

    def update(self, token, profile, roles, version=0):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')
        return self.client.put(f'/api/v1/administration/users/{profile.pk}/access/',
                               dict(roles=roles, version=version), format='json')

    def test_exact_bootstrap_is_audited_idempotent_and_not_django_superuser(self):
        self.bootstrap()
        row = AccountAccess.objects.get(user=self.official)
        self.assertTrue(row.superowner)
        self.assertEqual(row.version, 1)
        self.assertEqual(AccessAudit.objects.get().action, 'superowner_bootstrap')
        self.bootstrap()
        self.assertEqual(AccessAudit.objects.count(), 1)
        self.official.refresh_from_db()
        self.assertFalse(self.official.is_superuser)
        self.assertFalse(self.official.is_staff)
        self.assertEqual(capabilities(self.official), dict(manage_roles=True, manage_owners=True,
                                                          view_analytics=True, moderate=True))

    def test_dry_run_verifies_without_granting(self):
        self.bootstrap(dry_run=True, confirm_account_ownership=False)
        self.assertFalse(AccountAccess.objects.filter(user=self.official).exists())
        self.assertFalse(AccessAudit.objects.exists())

    def test_identity_mismatch_and_missing_confirmation_fail_closed(self):
        for options in [dict(user_id=self.member.pk), dict(username='rytivo_official'),
                        dict(email='wrong@example.com'), dict(confirm_account_ownership=False)]:
            with self.assertRaises(CommandError):
                self.bootstrap(**options)
        self.assertFalse(AccountAccess.objects.filter(user=self.official).exists())

    def test_admin_email_or_username_alone_grants_nothing(self):
        self.assertFalse(any(capabilities(self.official).values()))
        self.assertEqual(self.update(self.official_token, self.member_profile, ['owner']).status_code, 403)

    def test_owner_cannot_promote_owner_or_superowner_or_change_peer(self):
        self.bootstrap()
        for role in ['owner', 'superowner']:
            response = self.update(self.owner_token, self.member_profile, [role])
            self.assertIn(response.status_code, [400, 403])
        peer, profile, _ = self.create_account('other-owner')
        AccountAccess.objects.create(user=peer, owner=True)
        self.assertEqual(self.update(self.owner_token, profile, []).status_code, 403)
        self.assertTrue(AccountAccess.objects.get(user=peer).owner)

    def test_superowner_can_promote_demote_owner_and_assign_combined_roles(self):
        self.bootstrap()
        response = self.update(self.official_token, self.member_profile, ['owner', 'analytics'])
        self.assertEqual(response.status_code, 200, response.data)
        response = self.update(self.official_token, self.member_profile, [], version=1)
        self.assertEqual(response.status_code, 200, response.data)
        response = self.update(self.official_token, self.owner_profile, ['moderator'])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(capabilities(self.owner)['manage_roles'])

    def test_superowner_is_protected_from_owner_and_server_admin_via_api(self):
        self.bootstrap()
        self.assertEqual(self.update(self.owner_token, self.official_profile, [], version=1).status_code, 403)
        self.owner.is_superuser = True
        self.owner.save()
        self.assertEqual(self.update(self.owner_token, self.official_profile, [], version=1).status_code, 403)
        self.assertEqual(self.update(self.owner_token, self.member_profile, ['owner']).status_code, 403)
        self.assertEqual(self.update(self.official_token, self.official_profile, [], version=1).status_code, 400)

    def test_ui_readonly_and_owner_choices_match_backend(self):
        self.bootstrap()
        browser = APIClient()
        browser.force_login(self.owner)
        response = browser.get(f'/access/users/{self.member_profile.pk}/')
        self.assertNotContains(response, 'value="owner"')
        response = browser.post(f'/access/users/{self.member_profile.pk}/', dict(roles=['owner'], version=0))
        self.assertEqual(response.status_code, 200)  # Form validation, no write.
        self.assertFalse(AccountAccess.objects.filter(user=self.member).exists())
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.owner_token.key}')
        response = self.client.get(f'/api/v1/administration/users/{self.official_profile.pk}/access/')
        self.assertFalse(response.data['editable'])
        self.assertEqual(response.data['roles'], ['superowner'])

    def test_second_superowner_rejected_in_command_and_database(self):
        self.bootstrap()
        with self.assertRaises(CommandError):
            self.bootstrap(username=self.member.username, email=self.member.email, user_id=self.member.pk)
        with self.assertRaises(IntegrityError), transaction.atomic():
            AccountAccess.objects.create(user=self.member, superowner=True)
        self.assertEqual(AccountAccess.objects.filter(superowner=True).count(), 1)

    def test_legacy_owner_bootstrap_cannot_bypass_configured_superowner(self):
        self.bootstrap()
        with self.assertRaises(CommandError):
            call_command('grant_app_owner', username=self.member.username,
                         confirm_account_ownership=True, stdout=StringIO())

    def test_superowner_still_cannot_assign_protected_role_over_http(self):
        self.bootstrap()
        self.assertEqual(self.update(self.official_token, self.member_profile, ['superowner']).status_code, 400)

    def test_revoked_owner_loses_authority_on_next_request(self):
        self.bootstrap()
        self.assertEqual(self.update(self.owner_token, self.member_profile, ['analytics']).status_code, 200)
        self.assertEqual(self.update(self.official_token, self.owner_profile, []).status_code, 200)
        self.assertEqual(self.update(self.owner_token, self.member_profile, [], version=1).status_code, 403)
