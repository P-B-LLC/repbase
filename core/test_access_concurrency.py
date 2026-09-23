"""Run against disposable PostgreSQL, never infer row-lock safety from SQLite."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from unittest import skipUnless

from django.core.management import call_command
from django.db import close_old_connections, connection, transaction
from django.test import override_settings
from io import StringIO
from rest_framework.exceptions import APIException
from rest_framework.test import APITransactionTestCase

from .access import assign_roles
from .access_models import AccountAccess, AccessAudit, AccessPolicyLock
from .tests import RepbaseAPITestMixin


@skipUnless(connection.vendor == 'postgresql', 'Requires real PostgreSQL row locks')
@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class AccessConcurrencyTests(RepbaseAPITestMixin, APITransactionTestCase):
    def setUp(self):
        AccessPolicyLock.objects.get_or_create(pk=1)
        self.top, _, _ = self.create_account('top')
        AccountAccess.objects.create(user=self.top, superowner=True)
        self.owner, _, _ = self.create_account('owner')
        AccountAccess.objects.create(user=self.owner, owner=True)
        self.member, _, _ = self.create_account('member')

    def run_write(self, actor, target, roles, started=None, barrier=None):
        close_old_connections()
        try:
            if barrier:
                barrier.wait(timeout=10)
            if started:
                started.set()
            try:
                assign_roles(actor, target, roles, 0, 'Concurrency test')
                return 200
            except APIException as error:
                return error.status_code
        finally:
            close_old_connections()

    def test_two_writes_with_same_version_cannot_overwrite_each_other(self):
        barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.run_write, self.top, self.member, [role], barrier=barrier)
                       for role in ('analytics', 'moderator')]
            statuses = sorted(future.result(timeout=20) for future in futures)
        self.assertEqual(statuses, [200, 409])
        self.assertEqual(AccessAudit.objects.filter(action='roles_changed').count(), 1)
        self.assertEqual(AccountAccess.objects.get(user=self.member).version, 1)

    def test_actor_waiting_on_policy_lock_is_rechecked_after_revocation(self):
        started = Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                AccessPolicyLock.objects.select_for_update().get(pk=1)
                future = pool.submit(self.run_write, self.owner, self.member, ['analytics'], started)
                self.assertTrue(started.wait(timeout=10))
                assign_roles(self.top, self.owner, [], 0, 'Revoked before waiting request')
            self.assertEqual(future.result(timeout=20), 403)
        self.assertFalse(AccountAccess.objects.filter(user=self.member).exists())

    def test_concurrent_superowner_bootstrap_has_exactly_one_winner(self):
        from django.core.management.base import CommandError
        AccountAccess.objects.filter(user=self.top).update(superowner=False)
        barrier = Barrier(2)

        def bootstrap(user):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                try:
                    call_command('grant_app_superowner', username=user.username, email=user.email,
                                 user_id=user.pk, confirm_account_ownership=True, stdout=StringIO())
                    return True
                except CommandError:
                    return False
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(bootstrap, user) for user in (self.top, self.member)]
            self.assertEqual(sum(future.result(timeout=20) for future in futures), 1)
        self.assertEqual(AccountAccess.objects.filter(superowner=True).count(), 1)
        self.assertEqual(AccessAudit.objects.filter(action='superowner_bootstrap').count(), 1)
