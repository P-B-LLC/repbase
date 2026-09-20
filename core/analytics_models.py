from django.conf import settings
from django.db import models


class AnalyticsAccess(models.Model):
    """Explicit operator approval after verifying ownership outside registration."""
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    verified_email = models.EmailField()
    verified_at = models.DateTimeField()
    enabled = models.BooleanField(default=False)


class DailyApiMetric(models.Model):
    day = models.DateField(db_index=True)
    endpoint = models.CharField(max_length=160)
    method = models.CharField(max_length=8)
    requests = models.PositiveBigIntegerField(default=0)
    client_errors = models.PositiveBigIntegerField(default=0)
    server_errors = models.PositiveBigIntegerField(default=0)
    total_ms = models.PositiveBigIntegerField(default=0)
    max_ms = models.PositiveBigIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['day', 'endpoint', 'method'], name='unique_daily_api_metric')]


class DailyActiveAccount(models.Model):
    day = models.DateField(db_index=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['day', 'user'], name='unique_daily_active_account')]
