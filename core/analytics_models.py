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


class ApiAlert(models.Model):
    """One ongoing problem with one route, from opened to resolved.

    A row rather than a cache key with a cooldown, for two reasons. An alert
    that is only a timestamp can tell you something broke and never that it
    stopped, which leaves somebody checking by hand -- the thing alerting is
    supposed to remove. And an open row is the natural guard against sending
    the same thing every five minutes: the notification happens on the edge,
    when the row opens or closes, not while it sits there.

    Holds route names and counters only, the same as the metrics it is built
    from. Nothing here identifies an account.
    """

    class Kind(models.TextChoices):
        FAILING = 'failing', 'Server errors'
        SLOW = 'slow', 'Slow responses'

    endpoint = models.CharField(max_length=160)
    method = models.CharField(max_length=8)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    opened_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()
    resolved_at = models.DateTimeField(null=True, blank=True, db_index=True)
    #: What it looked like when it last breached, for the resolution note.
    detail = models.CharField(max_length=200, default='')

    class Meta:
        constraints = [
            # One open alert per route and kind. A second breach while the
            # first is unresolved is the same incident continuing.
            models.UniqueConstraint(
                fields=['endpoint', 'method', 'kind'], condition=models.Q(resolved_at__isnull=True),
                name='one_open_alert_per_route_and_kind'),
        ]


class DailyActiveAccount(models.Model):
    day = models.DateField(db_index=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['day', 'user'], name='unique_daily_active_account')]
