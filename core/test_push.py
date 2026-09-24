from datetime import timedelta
from unittest.mock import Mock
from unittest import mock
import tempfile
from pathlib import Path

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.db import transaction
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from .models import Block, Notification, Post, PushDelivery, PushDevice, notify
from .push import APNsClient, claim_delivery, deliver
from .tests import RepbaseAPITestMixin


@override_settings(APNS_ENABLED=True, APNS_ENVIRONMENT="production")
class PushTests(RepbaseAPITestMixin, APITestCase):
    def setUp(self):
        _, self.recipient, self.token = self.create_account("recipient")
        _, self.actor, self.actor_token = self.create_account("actor")
        self.authenticate(self.token)
        self.registration = {"token": "ab" * 32, "environment": "production", "community_enabled": True}
        self.url = "/api/v1/push/devices/"

    def register(self, **changes):
        return self.client.post(self.url, self.registration | changes, format="json")

    def queue(self):
        self.assertEqual(self.register().status_code, 204)
        return notify(self.recipient, self.actor, Notification.Kind.FOLLOW)

    def test_requires_auth_and_valid_token(self):
        self.client.credentials()
        self.assertEqual(self.register().status_code, 401)
        self.authenticate(self.token)
        for token in ("xyz", "a" * 33, "a" * 514):
            self.assertEqual(self.register(token=token).status_code, 400)
        self.assertEqual(self.register(environment="other").status_code, 400)

    def test_repeat_registration_preserves_pending_delivery(self):
        self.queue()
        generation = PushDevice.objects.get().generation
        self.assertEqual(self.register(token=("AB" * 32)).status_code, 204)
        self.assertEqual(PushDevice.objects.get().generation, generation)
        provider = Mock()
        provider.send.return_value = httpx.Response(200)
        deliver(claim_delivery(), provider)
        self.assertEqual(PushDelivery.objects.get().status, "sent")
        provider.send.assert_called_once()

    def test_opt_out_cancels_queued_delivery(self):
        self.queue()
        self.register(community_enabled=False)
        provider = Mock()
        deliver(claim_delivery(), provider)
        provider.send.assert_not_called()
        self.assertEqual(PushDelivery.objects.get().status, "cancelled")

    def test_account_transfer_does_not_deliver_old_account_alert(self):
        self.queue()
        self.authenticate(self.actor_token)
        self.register()
        provider = Mock()
        deliver(claim_delivery(), provider)
        provider.send.assert_not_called()

    def test_logout_revocation_cascades(self):
        self.queue()
        self.assertEqual(self.client.post('/api/v1/auth/logout/').status_code, 204)
        self.assertFalse(PushDevice.objects.exists())
        self.assertFalse(PushDelivery.objects.exists())

    def test_moderated_post_is_suppressed_after_enqueue(self):
        self.register()
        post = Post.objects.create(author=self.recipient, kind=Post.Kind.MEAL)
        notify(self.recipient, self.actor, Notification.Kind.LIKE, post=post)
        post.is_hidden = True
        post.save()
        provider = Mock()
        deliver(claim_delivery(), provider)
        provider.send.assert_not_called()

    def test_expired_notification_is_not_sent(self):
        self.queue()
        Notification.objects.update(created_at=timezone.now() - timedelta(days=2))
        provider = Mock()
        deliver(claim_delivery(), provider)
        provider.send.assert_not_called()

    def test_provider_throttling_retries_even_with_malformed_error_body(self):
        self.queue()
        provider = Mock()
        provider.send.return_value = httpx.Response(429, json=[])
        deliver(claim_delivery(), provider)
        self.assertEqual(PushDelivery.objects.get().status, 'queued')

    def test_self_and_duplicate_notifications_do_not_enqueue_twice(self):
        self.queue()
        notify(self.recipient, self.actor, Notification.Kind.FOLLOW)
        self.assertIsNone(notify(self.recipient, self.recipient, Notification.Kind.FOLLOW))
        self.assertEqual(PushDelivery.objects.count(), 1)

    @override_settings(APNS_ENABLED=False)
    def test_disabled_does_not_enqueue(self):
        self.queue()
        self.assertFalse(PushDelivery.objects.exists())

    def test_sandbox_device_not_sent_to_production(self):
        self.register(environment="sandbox")
        notify(self.recipient, self.actor, Notification.Kind.FOLLOW)
        self.assertFalse(PushDelivery.objects.exists())

    def test_read_notification_suppressed(self):
        notification = self.queue()
        notification.read_at = timezone.now()
        notification.save()
        provider = Mock()
        deliver(claim_delivery(), provider)
        provider.send.assert_not_called()

    def test_block_after_enqueue_suppresses_push(self):
        self.queue()
        Block.objects.create(blocker=self.recipient, blocked=self.actor)
        provider = Mock()
        deliver(claim_delivery(), provider)
        provider.send.assert_not_called()

    def test_retry_is_bounded_and_claim_is_exclusive(self):
        self.queue()
        provider = Mock()
        provider.send.side_effect = httpx.ConnectError("not logged")
        for attempt in range(1, 6):
            claim = claim_delivery()
            self.assertIsNone(claim_delivery())
            deliver(claim, provider)
            row = PushDelivery.objects.get()
            self.assertEqual(row.attempts, attempt)
            self.assertEqual(row.status, "queued" if attempt < 5 else "failed")
            PushDelivery.objects.update(next_attempt=timezone.now() - timedelta(seconds=1))
        self.assertIsNone(claim_delivery())

    def test_permanent_provider_failure_not_retried(self):
        self.queue()
        provider = Mock()
        provider.send.return_value = httpx.Response(403, json={"reason": "InvalidProviderToken"})
        deliver(claim_delivery(), provider)
        self.assertEqual(PushDelivery.objects.get().status, "failed")
        self.assertTrue(PushDevice.objects.get().community_enabled)

    def test_unregistered_timestamp_does_not_invalidate_new_registration(self):
        self.queue()
        provider = Mock()
        provider.send.return_value = httpx.Response(410, json={"reason": "Unregistered", "timestamp": 1})
        deliver(claim_delivery(), provider)
        self.assertTrue(PushDevice.objects.get().community_enabled)

    def test_unregistered_disables_invalid_device(self):
        self.queue()
        provider = Mock()
        provider.send.return_value = httpx.Response(410, json={
            "reason": "Unregistered", "timestamp": timezone.now().timestamp() * 1000 + 1000,
        })
        deliver(claim_delivery(), provider)
        self.assertFalse(PushDevice.objects.get().community_enabled)

    def test_bad_device_token_is_disabled(self):
        self.queue()
        provider = Mock()
        provider.send.return_value = httpx.Response(400, json={"reason": "BadDeviceToken"})
        deliver(claim_delivery(), provider)
        self.assertFalse(PushDevice.objects.get().community_enabled)

    def test_disabled_account_does_not_receive_queued_alert(self):
        self.queue()
        self.recipient.user.is_active = False
        self.recipient.user.save(update_fields=['is_active'])
        provider = Mock()
        deliver(claim_delivery(), provider)
        provider.send.assert_not_called()

    def test_expired_lease_cannot_overwrite_new_worker_result(self):
        self.queue()
        first = claim_delivery()
        PushDelivery.objects.update(next_attempt=timezone.now() - timedelta(seconds=1))
        second = claim_delivery()
        provider = Mock()
        provider.send.return_value = httpx.Response(200)
        deliver(second, provider)
        deliver(first, provider)
        self.assertEqual(PushDelivery.objects.get().status, "sent")
        provider.send.assert_called_once()

    def test_outbox_rolls_back_with_notification(self):
        self.register()
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                notify(self.recipient, self.actor, Notification.Kind.FOLLOW)
                raise RuntimeError("rollback")
        self.assertFalse(Notification.objects.exists())
        self.assertFalse(PushDelivery.objects.exists())

    def test_worker_crash_on_last_attempt_eventually_fails(self):
        self.queue()
        PushDelivery.objects.update(status="sending", attempts=5, next_attempt=timezone.now())
        self.assertIsNone(claim_delivery())
        self.assertEqual(PushDelivery.objects.get().status, "failed")

    def test_payload_is_generic_and_jwt_is_valid(self):
        self.queue()
        claim = claim_delivery()
        key = ec.generate_private_key(ec.SECP256R1())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.p8"
            path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            with override_settings(APNS_KEY_PATH=str(path), APNS_TEAM_ID="TESTTEAM",
                    APNS_KEY_ID="TESTKEY", APNS_TOPIC="com.example.test"):
                with mock.patch("core.push.httpx.Client") as transport:
                    transport.return_value.post.return_value = httpx.Response(200)
                    provider = APNsClient()
                    deliver(claim, provider)
                    kwargs = transport.return_value.post.call_args.kwargs
                    payload = kwargs["json"]
                    self.assertEqual(payload["accountID"], self.recipient.pk)
                    self.assertEqual(payload["route"], "community")
                    self.assertNotIn(self.actor.user.username, str(payload))
                    self.assertEqual(kwargs["headers"]["apns-push-type"], "alert")
                    self.assertEqual(kwargs["headers"]["apns-topic"], "com.example.test")
                    token = kwargs["headers"]["authorization"].removeprefix("bearer ")
                    claims = jwt.decode(token, key.public_key(), algorithms=["ES256"], issuer="TESTTEAM")
                    self.assertIn("iat", claims)
                    provider.close()
