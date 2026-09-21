"""Application access is separate from Django staff and model permissions."""
from django.conf import settings
from django.db import models


class AccountAccess(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    owner = models.BooleanField(default=False)
    analytics = models.BooleanField(default=False)
    moderator = models.BooleanField(default=False)
    version = models.PositiveIntegerField(default=0)


class AccessPolicyLock(models.Model):
    """One seeded row serializes grants/revocations and moderation decisions."""
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)


class AccessAudit(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
                              related_name='+')
    target = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
                               related_name='+')
    action = models.CharField(max_length=40)
    subject = models.CharField(max_length=100)
    before = models.JSONField(default=dict)
    after = models.JSONField(default=dict)
    reason = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-id']
