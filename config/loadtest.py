"""Settings for the load-test harness (`loadtest/`); never for anything else.

The target is checked before anything else happens, so this module cannot be
imported against a real database. Everything that shapes a request -- the
middleware, DRF, the cache backend, gunicorn's worker model -- is what production
runs. What differs is listed here, and each difference is either a measuring
instrument or something that would otherwise stop the load being applied.
"""
import os

from loadtest.safety import check_target

check_target(os.environ)
os.environ['DJANGO_DEBUG'] = 'false'
os.environ['DJANGO_SECURE_SSL_REDIRECT'] = 'false'

from .settings import *  # noqa: E402,F403

LOADTEST = True
SECRET_KEY = 'load-test-only-never-a-real-secret-0123456789-abcdefghijklmnop'
ALLOWED_HOSTS = ['127.0.0.1', 'localhost']

# The measuring instrument: outermost, so it sees what AnalyticsMiddleware does.
INSTALLED_APPS = [*INSTALLED_APPS, 'loadtest']  # noqa: F405
MIDDLEWARE = ['loadtest.middleware.RequestCostMiddleware', *MIDDLEWARE]  # noqa: F405

# A few accounts generate all the traffic, so limits sized for one person would
# measure the limiter. The throttles still read and write the cache as usual.
REST_FRAMEWORK = {
    **REST_FRAMEWORK,  # noqa: F405
    'DEFAULT_THROTTLE_RATES': {
        scope: '100000000/hour' for scope in REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']  # noqa: F405
    },
}

# Seeded search terms answer from the database. Anything else must fail here
# rather than send a load test to somebody else's service.
OPEN_FOOD_FACTS_ORIGIN = 'http://127.0.0.1:9'

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {'stderr': {'class': 'logging.StreamHandler'}},
    'root': {'handlers': ['stderr'], 'level': 'WARNING'},
    'loggers': {'django': {'handlers': ['stderr'], 'level': 'ERROR', 'propagate': False}},
}
