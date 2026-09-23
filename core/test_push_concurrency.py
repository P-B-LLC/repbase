"""Real row-lock tests; SQLite cannot prove registration/revocation ordering."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless

from django.db import close_old_connections, connection
from django.test import override_settings
from rest_framework.test import APIClient, APITransactionTestCase

from .models import Notification, PushDelivery, PushDevice, notify
from .push import claim_delivery
from .tests import RepbaseAPITestMixin


@skipUnless(connection.vendor == 'postgresql', 'Requires PostgreSQL row locks')
@override_settings(APNS_ENABLED=True, APNS_ENVIRONMENT='production')
class PushConcurrencyTests(RepbaseAPITestMixin, APITransactionTestCase):
    def setUp(self):
        _, self.profile, self.token = self.create_account('push-race')
        _, self.actor, _ = self.create_account('push-actor')

    def race(self, operations):
        barrier = Barrier(len(operations))

        def run(operation):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return operation()
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=len(operations)) as pool:
            pending = [pool.submit(run, operation) for operation in operations]
            return [result.result(timeout=20) for result in pending]

    def register(self):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        return client.post('/api/v1/push/devices/', {
            'token': 'ab' * 32, 'environment': 'production', 'community_enabled': True,
        }, format='json').status_code

    def test_logout_racing_new_registration_leaves_no_device(self):
        def logout():
            client = APIClient()
            client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
            return client.post('/api/v1/auth/logout/').status_code

        registered, logged_out = self.race([self.register, logout])
        self.assertIn(registered, (204, 401))
        self.assertEqual(logged_out, 204)
        self.assertFalse(PushDevice.objects.exists())

    def test_two_workers_cannot_claim_same_delivery(self):
        self.assertEqual(self.register(), 204)
        notify(self.profile, self.actor, Notification.Kind.FOLLOW)
        claims = self.race([claim_delivery, claim_delivery])
        self.assertEqual(sum(claim is not None for claim in claims), 1)
        self.assertEqual(PushDelivery.objects.get().attempts, 1)
