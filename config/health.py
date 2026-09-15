"""Infrastructure probes: no credentials, exception strings, or user data."""
import uuid

from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe


@require_safe
@never_cache
def live(request):
    return JsonResponse({'status': 'ok'})


def dependencies_available():
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
            if cursor.fetchone() != (1,):
                return False
        key = 'readiness:' + uuid.uuid4().hex
        try:
            cache.set(key, 'ok', timeout=10)
            return cache.get(key) == 'ok'
        finally:
            cache.delete(key)
    except Exception:
        # Health responses must not expose database/cache connection details.
        return False


@require_safe
@never_cache
def ready(request):
    ok = dependencies_available()
    return JsonResponse({'status': 'ok' if ok else 'unavailable'}, status=200 if ok else 503)
