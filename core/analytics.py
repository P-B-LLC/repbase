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
from django.db.models import F, Sum
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
    return bool(user.is_authenticated and user.is_active and user.is_staff
        and user.email.casefold() == ADMIN_EMAIL
        and AnalyticsAccess.objects.filter(user=user, enabled=True,
            verified_email=ADMIN_EMAIL, verified_at__lte=timezone.now()).exists())


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
        if request.path.startswith('/insights/') and not settings.DEBUG and not settings.ANALYTICS_LOGIN_SHARED_LIMIT_CONFIRMED:
            response = HttpResponse('Private insights requires a configured shared login rate limit.', status=503)
            response['Cache-Control'] = 'no-store'
            return response
        if request.path.startswith('/insights/') and not settings.DEBUG and not (
            settings.SESSION_COOKIE_SECURE and settings.CSRF_COOKIE_SECURE
        ):
            response = HttpResponse('Private insights requires secure session and CSRF cookies.', status=503)
            response['Cache-Control'] = 'no-store'
            return response
        start = time.perf_counter()
        response = self.get_response(request)
        if request.path.startswith('/insights/'):
            response['Cache-Control'] = 'no-store, private'
            response['X-Robots-Tag'] = 'noindex, nofollow'
            # HTTPS forms without an Origin header need a same-origin Referer
            # for Django's CSRF fallback. Still omit it for external sites.
            response['Referrer-Policy'] = 'same-origin'
            response['Content-Security-Policy'] = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
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
    metrics = DailyApiMetric.objects.filter(day__gte=since, day__lte=today)
    totals = metrics.aggregate(requests=Sum('requests'), errors=Sum('server_errors'),
                               rejected=Sum('client_errors'), total_ms=Sum('total_ms'))
    total = totals['requests'] or 0
    active = DailyActiveAccount.objects.filter(day__gte=since, day__lte=today)
    rows = list(metrics.values('endpoint', 'method').annotate(
        count=Sum('requests'), errors=Sum('server_errors'), rejected=Sum('client_errors'),
        total_ms=Sum('total_ms')).order_by('-errors', '-count', 'endpoint', 'method')[:25])
    for row in rows:
        row['average_ms'] = round(row['total_ms'] / row['count']) if row['count'] else 0
    trend = list(metrics.values('day').annotate(count=Sum('requests'), errors=Sum('server_errors')).order_by('day'))
    peak = max((row['count'] for row in trend), default=1)
    for row in trend:
        row['width'] = round(row['count'] / peak * 100) if peak else 0
    return render(request, 'insights/dashboard.html', {
        'days': days, 'since': since, 'today': today, 'enabled': settings.ANALYTICS_ENABLED,
        'requests': total, 'errors': totals['errors'] or 0, 'rejected': totals['rejected'] or 0,
        'error_rate': round((totals['errors'] or 0) / total * 100, 2) if total else None,
        'average_ms': round((totals['total_ms'] or 0) / total) if total else None,
        'active': active.values('user_id').distinct().count(),
        'signups': RepbaseUser.objects.filter(created_at__date__gte=since, created_at__date__lte=today, user__is_staff=False).count(),
        'workouts': WorkoutSession.objects.filter(status='completed', ended_at__date__gte=since, ended_at__date__lte=today,
            health_external_id__isnull=True, repbase_user__user__is_staff=False).count(),
        'tasks': PlannerEntry.objects.filter(kind='task', completed_at__date__gte=since,
            completed_at__date__lte=today, owner__user__is_staff=False).count(),
        'meals': FoodMeal.objects.filter(date__gte=since, date__lte=today, entries__isnull=False,
            owner__user__is_staff=False).distinct().count(),
        'rows': rows, 'trend': trend,
    })
