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


class RecentApiMetric(models.Model):
    """The same counters as `DailyApiMetric`, in five-minute buckets.

    A daily row cannot answer "is it broken now". Two 500s in one row might
    be from this minute or from twenty hours ago, and `max_ms` is a running
    maximum for the whole day, so one bad request at 03:00 keeps a route
    looking slow until midnight. That is fine for a trend and useless for
    the thing somebody opens this page at 2am to find out.

    So the same writes land here at a granularity worth alerting on, and are
    kept only long enough to answer "now" -- see `prune_analytics`. The daily
    table stays exactly as it was: it is what the 7- and 30-day views read,
    and it holds the history this one deliberately does not.
    """

    bucket = models.DateTimeField(db_index=True)
    endpoint = models.CharField(max_length=160)
    method = models.CharField(max_length=8)
    requests = models.PositiveBigIntegerField(default=0)
    client_errors = models.PositiveBigIntegerField(default=0)
    server_errors = models.PositiveBigIntegerField(default=0)
    total_ms = models.PositiveBigIntegerField(default=0)
    max_ms = models.PositiveBigIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['bucket', 'endpoint', 'method'], name='unique_recent_api_metric')]


class DailyActiveAccount(models.Model):
    day = models.DateField(db_index=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['day', 'user'], name='unique_daily_active_account')]
