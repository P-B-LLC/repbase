"""Settings for the cross-repository integration test; never for anything else.

The web client's integration suite (rytivo-web/tests/integration) starts a
real server with these settings and drives it through the real client code.
The database and media directory must both be named in the environment, and
there is no fallback, so this cannot be pointed at the development database
by accident -- the one thing worse than a flaky test is a test that edits
somebody's real data.
"""
import os

from .settings import *  # noqa: F403

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': os.environ['RYTIVO_E2E_DATABASE'],
    }
}
MEDIA_ROOT = os.environ['RYTIVO_E2E_MEDIA_ROOT']

# One process, one run: limits sized for a person would only make the suite
# depend on how many requests it happens to make.
REST_FRAMEWORK = {
    **REST_FRAMEWORK,  # noqa: F405
    'DEFAULT_THROTTLE_RATES': {
        scope: '10000/hour'
        for scope in REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']  # noqa: F405
    },
}
# Fast hashing: the suite makes accounts and tests nothing about hashing.
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
