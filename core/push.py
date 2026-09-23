"""Transactional outbox for generic, account-bound community alerts.

Delivery is at least once: an ambiguous network failure may follow acceptance
by Apple. A stable collapse ID limits duplicates; no exact-once claim is made.
"""
import time
import uuid
from datetime import timedelta
from pathlib import Path

import httpx
import jwt
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from .models import Block, PushDelivery, PushDevice

MAX_ATTEMPTS = 5


def enqueue_notification(notification):
    if not settings.APNS_ENABLED:
        return
    devices = PushDevice.objects.filter(
        recipient_id=notification.recipient_id, community_enabled=True,
        auth_token__user__is_active=True,
        environment=settings.APNS_ENVIRONMENT,
    )
    PushDelivery.objects.bulk_create([
        PushDelivery(notification=notification, device=device, device_generation=device.generation)
        for device in devices
    ], ignore_conflicts=True)


class APNsClient:
    def __init__(self):
        if settings.APNS_ENVIRONMENT not in ("sandbox", "production") or not all((
            settings.APNS_TEAM_ID, settings.APNS_KEY_ID, settings.APNS_TOPIC, settings.APNS_KEY_PATH,
        )):
            raise ImproperlyConfigured("APNs configuration is incomplete.")
        try:
            path = Path(settings.APNS_KEY_PATH)
            if not path.is_absolute():
                raise ValueError()
            self.key = path.read_bytes()
            self._sign()
        except (OSError, ValueError, TypeError, jwt.PyJWTError):
            raise ImproperlyConfigured("APNs signing key cannot be loaded.") from None
        self.host = "https://api.push.apple.com" if settings.APNS_ENVIRONMENT == "production" else "https://api.sandbox.push.apple.com"
        self.client = httpx.Client(http2=True, timeout=10, trust_env=False)

    def close(self):
        self.client.close()

    def _sign(self):
        self.signed_at = int(time.time())
        self.authorization = jwt.encode(
            {"iss": settings.APNS_TEAM_ID, "iat": self.signed_at}, self.key,
            algorithm="ES256", headers={"kid": settings.APNS_KEY_ID},
        )

    def send(self, delivery):
        if time.time() - self.signed_at >= 2400:
            self._sign()
        return self.client.post(
            f"{self.host}/3/device/{delivery.device.token}",
            headers={
                "authorization": f"bearer {self.authorization}",
                "apns-topic": settings.APNS_TOPIC,
                "apns-push-type": "alert", "apns-priority": "10",
                "apns-id": str(delivery.id),
                "apns-collapse-id": f"community-{delivery.notification_id}",
                "apns-expiration": str(int(time.time()) + 3600),
            },
            json={
                "aps": {"alert": {"title": "Rytivo", "body": "You have new community activity."}, "sound": "default"},
                "accountID": delivery.notification.recipient_id,
                "route": "community", "notificationID": delivery.notification_id,
            },
        )


@transaction.atomic
def claim_delivery():
    now = timezone.now()
    rows = PushDelivery.objects.filter(status__in=["queued", "sending"], next_attempt__lte=now)
    # Recover a worker that died during its last permitted attempt.
    rows.filter(attempts__gte=MAX_ATTEMPTS).update(status="failed", last_error="AttemptsExhausted")
    rows = rows.filter(attempts__lt=MAX_ATTEMPTS).order_by("next_attempt", "id")
    rows = rows.select_for_update(skip_locked=True) if connection.features.has_select_for_update_skip_locked else rows.select_for_update()
    delivery = rows.first()
    if delivery:
        delivery.status = "sending"
        delivery.lease = uuid.uuid4()
        delivery.attempts += 1
        delivery.next_attempt = now + timedelta(minutes=2)
        delivery.save(update_fields=["status", "lease", "attempts", "next_attempt"])
    return delivery


def deliver(claim, provider):
    # Re-read after claim: preferences, ownership, deletion, and moderation can change.
    delivery = PushDelivery.objects.select_related("device", "notification").filter(
        pk=claim.pk, lease=claim.lease, status="sending",
    ).first()
    if not delivery:
        return
    current = PushDelivery.objects.filter(pk=delivery.pk, lease=claim.lease, status="sending")
    device, notification = delivery.device, delivery.notification
    blocked = Block.objects.filter(
        Q(blocker_id=notification.actor_id, blocked_id=notification.recipient_id)
        | Q(blocker_id=notification.recipient_id, blocked_id=notification.actor_id)
    ).exists()
    hidden = PushDelivery.objects.filter(pk=delivery.pk).filter(
        Q(notification__post__is_hidden=True) | Q(notification__comment__is_hidden=True)
        | Q(notification__comment__parent__is_hidden=True)
        | Q(notification__recipient__user__is_active=False)
    ).exists()
    if (not settings.APNS_ENABLED or not device.community_enabled
        or device.environment != settings.APNS_ENVIRONMENT
        or device.generation != delivery.device_generation
        or device.recipient_id != notification.recipient_id or notification.read_at
        or notification.created_at < timezone.now() - timedelta(hours=24) or blocked or hidden):
        current.update(status="cancelled", last_error="NoLongerEligible")
        return
    reason, retry = "TransportFailure", True
    try:
        response = provider.send(delivery)
        if response.status_code == 200:
            current.update(status="sent", last_error="")
            return
        # Store only Apple's documented code, never response content or device tokens.
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        reason = f"HTTP{response.status_code}"
        if response.status_code == 400 and body.get("reason") == "BadDeviceToken":
            PushDevice.objects.filter(pk=device.pk, generation=delivery.device_generation,
                registered_at=device.registered_at).update(community_enabled=False, generation=uuid.uuid4())
        if response.status_code == 410 and body.get("reason") == "Unregistered":
            timestamp = body.get("timestamp")
            if isinstance(timestamp, (int, float)) and timestamp >= device.registered_at.timestamp() * 1000:
                PushDevice.objects.filter(pk=device.pk, generation=delivery.device_generation,
                    registered_at=device.registered_at).update(community_enabled=False, generation=uuid.uuid4())
        retry = response.status_code in (429, 500, 503)
    except httpx.TransportError:
        pass
    if retry and delivery.attempts < MAX_ATTEMPTS:
        current.update(status="queued", last_error=reason,
            next_attempt=timezone.now() + timedelta(seconds=30 * 2 ** delivery.attempts))
    else:
        current.update(status="failed", last_error=reason)
