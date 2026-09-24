"""Tell somebody when a route breaks, without anybody watching a page.

The dashboard answers "is it broken" in five minutes *if someone is looking*.
Nothing was looking at 3am. This runs on a schedule, reads the same
five-minute buckets, and sends one email when a route starts failing and one
when it stops.

Deliberately edge-triggered. An alert that repeats every run trains people to
filter it, and the filtered alert is the one that matters. An open `ApiAlert`
row is what stops the repeat and what lets the all-clear be sent at all.
"""
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.db.models import Max, Q, Sum
from django.utils import timezone

from core.analytics import BUCKET_MINUTES, bucket_for
from core.analytics_models import AnalyticsAccess, ApiAlert, RecentApiMetric

#: How far back a run looks. Wider than the schedule it runs on, so a blip
#: between two runs is still seen by one of them.
WINDOW_MINUTES = 15

#: A single 500 is worth knowing about; this is infrastructure, not a metric
#: to be smoothed. Slowness needs the same ceiling the dashboard uses.
ERROR_THRESHOLD = 1
SLOW_MS = 3000


def recipients():
    """Who hears about it: the accounts already trusted with this data.

    `ANALYTICS_ALERT_EMAILS` overrides, for a pager address that is not a
    person's inbox.
    """
    configured = getattr(settings, 'ANALYTICS_ALERT_EMAILS', '')
    if configured:
        return [address.strip() for address in configured.split(',') if address.strip()]
    return list(AnalyticsAccess.objects.filter(enabled=True)
                .values_list('verified_email', flat=True).distinct())


def breaches(now):
    """Routes currently in trouble, as {(endpoint, method, kind): detail}."""
    since = bucket_for(now) - timedelta(minutes=WINDOW_MINUTES)
    rows = RecentApiMetric.objects.filter(bucket__gte=since).values('endpoint', 'method').annotate(
        count=Sum('requests'), errors=Sum('server_errors'), worst_ms=Max('max_ms'))
    found = {}
    for row in rows:
        if row['errors'] >= ERROR_THRESHOLD:
            found[(row['endpoint'], row['method'], ApiAlert.Kind.FAILING)] = (
                '%d server error%s in %d requests over the last %d minutes.'
                % (row['errors'], '' if row['errors'] == 1 else 's', row['count'], WINDOW_MINUTES))
        elif row['worst_ms'] >= SLOW_MS:
            found[(row['endpoint'], row['method'], ApiAlert.Kind.SLOW)] = (
                'Slowest request %d ms over the last %d minutes.'
                % (row['worst_ms'], WINDOW_MINUTES))
    return found


class Command(BaseCommand):
    help = 'Open, close and notify on API health alerts from the live metric buckets.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Report what would be opened or resolved; send nothing, write nothing.')

    def handle(self, *args, **options):
        if not settings.ANALYTICS_ENABLED:
            # Nothing is being recorded, so an all-clear here would be a lie
            # about a system nobody is watching.
            self.stdout.write('API collection is off; no health signal to check.')
            return

        now = timezone.now()
        current = breaches(now)
        open_alerts = {
            (alert.endpoint, alert.method, alert.kind): alert
            for alert in ApiAlert.objects.filter(resolved_at__isnull=True)
        }
        opened, resolved = [], []

        for key, detail in current.items():
            alert = open_alerts.get(key)
            if alert is None:
                opened.append((key, detail))
            elif not options['dry_run']:
                # Still broken. Keep the row current and stay quiet.
                ApiAlert.objects.filter(pk=alert.pk).update(last_seen_at=now, detail=detail)

        for key, alert in open_alerts.items():
            if key not in current:
                resolved.append(alert)

        if options['dry_run']:
            for (endpoint, method, kind), detail in opened:
                self.stdout.write(f'Would open {kind} for {method} {endpoint}: {detail}')
            for alert in resolved:
                self.stdout.write(f'Would resolve {alert.kind} for {alert.method} {alert.endpoint}')
            if not opened and not resolved:
                self.stdout.write('No change.')
            return

        for (endpoint, method, kind), detail in opened:
            ApiAlert.objects.create(endpoint=endpoint, method=method, kind=kind,
                                    opened_at=now, last_seen_at=now, detail=detail)
            self.notify(
                subject='[Rytivo] %s %s is %s' % (
                    method, endpoint, 'failing' if kind == ApiAlert.Kind.FAILING else 'slow'),
                body='%s\n\nSeen in the last %d minutes of five-minute buckets.\n'
                     'Open the private insights dashboard for the route table.\n'
                     % (detail, WINDOW_MINUTES))
        for alert in resolved:
            ApiAlert.objects.filter(pk=alert.pk).update(resolved_at=now)
            lasted = int((now - alert.opened_at).total_seconds() // 60)
            self.notify(
                subject='[Rytivo] %s %s recovered' % (alert.method, alert.endpoint),
                body='No breach in the last %d minutes. It was first seen %d minute%s ago.\n\n'
                     'Last observed problem: %s\n'
                     % (WINDOW_MINUTES, lasted, '' if lasted == 1 else 's', alert.detail or 'unknown'))

        self.stdout.write('Opened %d, resolved %d, %d still open.'
                          % (len(opened), len(resolved),
                             ApiAlert.objects.filter(resolved_at__isnull=True).count()))

    def notify(self, subject, body):
        """Send, and never let a mail failure stop the rest of the run.

        A provider being down must not leave an alert unrecorded or block the
        all-clear for a different route; the rows are the durable part and
        they are already written.
        """
        addresses = recipients()
        if not addresses:
            self.stderr.write('No alert recipients configured; recorded but not sent: ' + subject)
            return
        try:
            send_mail(subject=subject, message=body,
                      from_email=settings.DEFAULT_FROM_EMAIL,
                      recipient_list=addresses, fail_silently=False)
        except Exception as error:  # noqa: BLE001 - any provider failure
            self.stderr.write('Alert mail failed (%s): %s' % (error.__class__.__name__, subject))
