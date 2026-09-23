"""Private aggregate insights; no raw URLs, request bodies or device identifiers."""
import logging
import time
from datetime import timedelta, timezone as datetime_timezone

from django.conf import settings
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.views import LoginView, LogoutView
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction
from django.db.models import Count, F, Max, Sum
from django.db.models.functions import Greatest
from django.http import HttpResponseForbidden, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.views.decorators.cache import never_cache

from .analytics_models import AnalyticsAccess, DailyActiveAccount, DailyApiMetric
from .models import RepbaseUser, WorkoutSession, PlannerEntry, FoodMeal

ADMIN_EMAIL = 'admin@rytivo.app'
logger = logging.getLogger(__name__)


def allowed(user):
    from .access import capabilities
    return capabilities(user)['view_analytics']


class InsightsLoginForm(AuthenticationForm):
    def confirm_login_allowed(self, user):
        if not allowed(user):
            raise ValidationError(self.error_messages['invalid_login'], code='invalid_login',
                                  params={'username': self.username_field.verbose_name})


class InsightsLoginView(LoginView):
    authentication_form = InsightsLoginForm
    template_name = 'insights/login.html'
    next_page = '/insights/'
    redirect_authenticated_user = False

    def post(self, request, *args, **kwargs):
        # Per-source rate limit; never store a raw IP or submitted username.
        key = 'insights-login:' + salted_hmac('insights-login', request.META.get('REMOTE_ADDR', '')).hexdigest()
        try:
            cache.add(key, 0, 900)
            attempts = cache.incr(key)
        except Exception:
            # Do not fail open if the shared rate limiter is unavailable.
            return HttpResponse('Sign-in temporarily unavailable.', status=503)
        if attempts > 5:
            return HttpResponse('Too many attempts. Try again in 15 minutes.', status=429)
        response = super().post(request, *args, **kwargs)
        if response.status_code == 302:
            request.session.set_expiry(1800)
        return response


class InsightsLogoutView(LogoutView):
    next_page = '/insights/login/'


class AnalyticsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        private_workspace = request.path.startswith(('/insights/', '/access/'))
        if private_workspace and not settings.DEBUG and not settings.ANALYTICS_LOGIN_SHARED_LIMIT_CONFIRMED:
            response = HttpResponse('Private insights requires a configured shared login rate limit.', status=503)
            response['Cache-Control'] = 'no-store'
            return response
        if private_workspace and not settings.DEBUG and not (
            settings.SESSION_COOKIE_SECURE and settings.CSRF_COOKIE_SECURE
        ):
            response = HttpResponse('Private insights requires secure session and CSRF cookies.', status=503)
            response['Cache-Control'] = 'no-store'
            return response
        start = time.perf_counter()
        response = self.get_response(request)
        if private_workspace:
            response['Cache-Control'] = 'no-store, private'
            response['X-Robots-Tag'] = 'noindex, nofollow'
            # HTTPS forms without an Origin header need a same-origin Referer
            # for Django's CSRF fallback. Still omit it for external sites.
            response['Referrer-Policy'] = 'same-origin'
            response['Content-Security-Policy'] = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
            return response
        if not settings.ANALYTICS_ENABLED or not request.path.startswith('/api/v1/'):
            return response
        match = request.resolver_match
        # Fixed route names, never a user-supplied URL/ID/query string.
        endpoint = (match.view_name if match else 'unmatched')[:160]
        method = request.method if request.method in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS') else 'OTHER'
        elapsed = max(0, min(round((time.perf_counter() - start) * 1000), 86400000))
        day = timezone.now().astimezone(datetime_timezone.utc).date()
        try:
            # Savepoint prevents a metrics failure poisoning an outer transaction.
            with transaction.atomic():
                row, _ = DailyApiMetric.objects.get_or_create(day=day, endpoint=endpoint, method=method)
                DailyApiMetric.objects.filter(pk=row.pk).update(
                    requests=F('requests') + 1,
                    client_errors=F('client_errors') + int(400 <= response.status_code < 500),
                    server_errors=F('server_errors') + int(response.status_code >= 500),
                    total_ms=F('total_ms') + elapsed, max_ms=Greatest(F('max_ms'), elapsed))
                user = getattr(request, 'user', None)
                if user and user.is_authenticated and not user.is_staff and response.status_code < 400:
                    DailyActiveAccount.objects.get_or_create(day=day, user=user)
        except DatabaseError:
            # Observability must never change the result of a user's save.
            logger.warning('Analytics write failed; application response preserved.')
        return response


# --- Reading the numbers ------------------------------------------------
#
# The first version of this page answered "how much?" and stopped. Every
# figure was a bare total with nothing beside it, which is a report rather
# than an instrument: 14,820 requests is neither good nor bad, and nobody can
# act on it. What follows adds the three things that make a number worth
# looking at -- what it was last period, what it implies, and which specific
# thing to open next.


def _change(current, previous):
    """Percent change, or None when there is nothing honest to compare to.

    A previous period of zero is not a 100% rise; it is a period with no
    observations, and dividing by it produces an impressive number that means
    nothing. Those read "no baseline" on the page instead.
    """
    if not previous:
        return None
    return round((current - previous) / previous * 100)


def _delta(current, previous, days, rising_is_good=True):
    percent = _change(current, previous)
    if percent is None:
        return {'text': 'no baseline', 'tone': 'flat'}
    if percent == 0:
        return {'text': 'level vs previous %d days' % days, 'tone': 'flat'}
    good = percent > 0 if rising_is_good else percent < 0
    return {
        'text': '%+d%% vs previous %d days' % (percent, days),
        'tone': 'good' if good else 'bad',
    }


def _totals(metrics):
    totals = metrics.aggregate(requests=Sum('requests'), errors=Sum('server_errors'),
                               rejected=Sum('client_errors'), total_ms=Sum('total_ms'))
    return {key: value or 0 for key, value in totals.items()}


def _chart(trend, height=170, width=760):
    """Geometry for the request curve.

    Computed here because the page runs under `default-src 'none'` and cannot
    execute a charting library, or any script at all. Inline SVG is part of
    the document rather than a fetched resource, so the shape is drawn from
    numbers worked out on the server.
    """
    if not trend:
        return None
    peak = max(row['count'] for row in trend) or 1
    step = width / max(len(trend) - 1, 1)
    points = []
    for index, row in enumerate(trend):
        x = round(index * step, 1)
        y = round(height - (row['count'] / peak) * (height - 14), 1)
        points.append('%s,%s' % (x, y))
        row['x'] = x
        row['y'] = y
    return {
        'line': ' '.join(points),
        'area': '0,%d ' % height + ' '.join(points) + ' %d,%d' % (width, height),
        'peak': peak, 'height': height, 'width': width, 'points': trend,
    }


def _attention(rows, slow_ms):
    """Routes worth opening, each with the reason and the next move stated.

    Ordered the way somebody would actually work: anything returning 5xx
    first, then anything slow enough to be felt. A route appears once, under
    its worse problem, so the list stays short enough to finish.
    """
    items = []
    for row in rows:
        if row['errors']:
            share = round(row['errors'] / row['count'] * 100, 1) if row['count'] else 0
            items.append({
                'endpoint': row['endpoint'], 'method': row['method'], 'severity': 'error',
                'headline': '%d server error%s' % (row['errors'], '' if row['errors'] == 1 else 's'),
                'detail': '%s%% of %s requests failed outright.' % (share, row['count']),
                'action': 'Read this route’s traceback before anything else here.',
            })
        elif row['max_ms'] >= slow_ms:
            items.append({
                'endpoint': row['endpoint'], 'method': row['method'], 'severity': 'slow',
                'headline': '%s ms worst case' % row['max_ms'],
                'detail': 'Averages %s ms, so the slow path is rare rather than usual.' % row['average_ms'],
                'action': 'Look for an unbounded query or a call made without a timeout.',
            })
    return items[:8]


@never_cache
@login_required(login_url='/insights/login/')
def dashboard(request):
    if not allowed(request.user):
        return HttpResponseForbidden('Access denied.')
    period = request.GET.get('days', '7')
    if period not in ('7', '30'):
        return HttpResponse('Choose a 7- or 30-day period.', status=400)
    days = int(period)
    today = timezone.now().astimezone(datetime_timezone.utc).date()
    since = today - timedelta(days=days - 1)
    # The same length again, immediately before, so a comparison is like for
    # like rather than this week against an arbitrary stretch of history.
    prior_end = since - timedelta(days=1)
    prior_start = prior_end - timedelta(days=days - 1)

    metrics = DailyApiMetric.objects.filter(day__gte=since, day__lte=today)
    prior_metrics = DailyApiMetric.objects.filter(day__gte=prior_start, day__lte=prior_end)
    totals = _totals(metrics)
    prior = _totals(prior_metrics)
    total = totals['requests']

    rows = list(metrics.values('endpoint', 'method').annotate(
        count=Sum('requests'), errors=Sum('server_errors'), rejected=Sum('client_errors'),
        total_ms=Sum('total_ms'), max_ms=Max('max_ms'),
    ).order_by('-errors', '-count', 'endpoint', 'method')[:25])
    for row in rows:
        row['average_ms'] = round(row['total_ms'] / row['count']) if row['count'] else 0

    trend = list(metrics.values('day').annotate(
        count=Sum('requests'), errors=Sum('server_errors')).order_by('day'))
    active_by_day = {
        row['day']: row['people']
        for row in DailyActiveAccount.objects.filter(day__gte=since, day__lte=today)
        .values('day').annotate(people=Count('user_id', distinct=True))
    }
    peak = max((row['count'] for row in trend), default=1) or 1
    for row in trend:
        row['people'] = active_by_day.get(row['day'], 0)
        row['width'] = round(row['count'] / peak * 100)

    # Who came back. `DailyActiveAccount` records an API presence, so this
    # counts accounts whose device reached the server -- background sync
    # included -- not people who opened the app. It is still the only
    # returning/lapsed signal available without dedicated client events, and
    # the page says so rather than letting it pass as engagement.
    now_active = set(DailyActiveAccount.objects.filter(day__gte=since, day__lte=today)
                     .values_list('user_id', flat=True).distinct())
    was_active = set(DailyActiveAccount.objects.filter(day__gte=prior_start, day__lte=prior_end)
                     .values_list('user_id', flat=True).distinct())
    returning = now_active & was_active
    lapsed = was_active - now_active

    # Did the people who just signed up do anything at all? An account that
    # never reaches a first workout, meal or finished task is an onboarding
    # failure, and nothing else on this page would show it.
    # ponytail: bounded by one window's signups; count with a join instead of
    # an __in if that ever reaches tens of thousands.
    new_ids = set(RepbaseUser.objects.filter(
        created_at__date__gte=since, created_at__date__lte=today,
        user__is_staff=False).values_list('id', flat=True))
    activated = set()
    if new_ids:
        activated |= set(WorkoutSession.objects.filter(
            repbase_user_id__in=new_ids, status='completed'
        ).values_list('repbase_user_id', flat=True))
        activated |= set(FoodMeal.objects.filter(
            owner_id__in=new_ids, entries__isnull=False).values_list('owner_id', flat=True))
        activated |= set(PlannerEntry.objects.filter(
            owner_id__in=new_ids, completed_at__isnull=False).values_list('owner_id', flat=True))

    active_count = len(now_active)
    signups = len(new_ids)
    error_rate = round(totals['errors'] / total * 100, 2) if total else None
    prior_rate = round(prior['errors'] / prior['requests'] * 100, 2) if prior['requests'] else None
    average_ms = round(totals['total_ms'] / total) if total else None
    prior_average = round(prior['total_ms'] / prior['requests']) if prior['requests'] else None

    def finished(start, end):
        return WorkoutSession.objects.filter(
            status='completed', ended_at__date__gte=start, ended_at__date__lte=end,
            health_external_id__isnull=True, repbase_user__user__is_staff=False).count()

    workouts = finished(since, today)
    slow_ms = 3000
    return render(request, 'insights/dashboard.html', {
        'days': days, 'since': since, 'today': today, 'enabled': settings.ANALYTICS_ENABLED,
        'prior_start': prior_start, 'prior_end': prior_end, 'slow_ms': slow_ms,
        'requests': total, 'errors': totals['errors'], 'rejected': totals['rejected'],
        'error_rate': error_rate, 'average_ms': average_ms,
        'active': active_count, 'signups': signups, 'workouts': workouts,
        'deltas': {
            'requests': _delta(total, prior['requests'], days),
            'active': _delta(active_count, len(was_active), days),
            'workouts': _delta(workouts, finished(prior_start, prior_end), days),
            'error_rate': _delta(error_rate or 0, prior_rate, days, rising_is_good=False),
            'average_ms': _delta(average_ms or 0, prior_average, days, rising_is_good=False),
        },
        'returning': len(returning), 'lapsed': len(lapsed),
        'new_to_api': active_count - len(returning),
        'return_rate': round(len(returning) / len(was_active) * 100) if was_active else None,
        'activated': len(activated),
        'activation_rate': round(len(activated) / signups * 100) if signups else None,
        'attention': _attention(rows, slow_ms),
        'chart': _chart(trend),
        'tasks': PlannerEntry.objects.filter(kind='task', completed_at__date__gte=since,
            completed_at__date__lte=today, owner__user__is_staff=False).count(),
        'meals': FoodMeal.objects.filter(date__gte=since, date__lte=today, entries__isnull=False,
            owner__user__is_staff=False).distinct().count(),
        'rows': rows, 'trend': trend,
    })
