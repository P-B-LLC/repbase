"""Explicit hosted settings; no SQLite or development-secret fallback."""
import os

from .settings import *  # noqa: F403
from .environment import boolean, database, integer, production_values

# settings.py chooses its mail backend at import time. Require DEBUG=false
# before importing this module via the deployment entry points.
DEBUG = False
MODERATION_ENABLED = True
# The classifier reads text and photos, not video, so a clip is seen by a
# person before anybody else sees it. Not configurable here for the same
# reason moderation itself is not.
VIDEO_REVIEW_REQUIRED = True
globals().update(production_values(os.environ))
DATABASES = {'default': database(os.environ, BASE_DIR, production=True)}  # noqa: F405
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = integer(os.environ, 'DJANGO_SECURE_HSTS_SECONDS', 3600, 0, 31536000)
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
STATIC_ROOT = BASE_DIR / 'staticfiles'  # noqa: F405
MIDDLEWARE = [MIDDLEWARE[0], 'whitenoise.middleware.WhiteNoiseMiddleware', *MIDDLEWARE[1:]]  # noqa: F405
STORAGES = {**STORAGES, 'staticfiles': {  # noqa: F405
    'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage',
}}
CSRF_TRUSTED_ORIGINS = ['https://' + host for host in ALLOWED_HOSTS]  # noqa: F405

# One proxy -- nginx on the same machine -- unless the deployment says
# otherwise. Without a count, a client-supplied X-Forwarded-For becomes the
# throttle identity and every anonymous rate limit can be stepped around. See
# the note beside NUM_PROXIES in settings. This relies on the application port
# not being reachable except through that proxy, which is the same condition
# DJANGO_TRUST_PROXY_HTTPS already states.
REST_FRAMEWORK = {  # noqa: F405
    **REST_FRAMEWORK,  # noqa: F405
    'NUM_PROXIES': integer(os.environ, 'REPBASE_NUM_PROXIES', 1, 0, 5),
}

# The schema and its docs page describe every endpoint, parameter and error
# this API has. Nothing ships that fetches them at run time -- both clients
# are generated from the committed openapi.yaml -- so on a hosted server they
# are for staff, who can sign in to the admin and read them there. Set
# REPBASE_PUBLIC_API_DOCS=true to publish them.
if not boolean(os.environ, 'REPBASE_PUBLIC_API_DOCS'):
    SPECTACULAR_SETTINGS = {  # noqa: F405
        **SPECTACULAR_SETTINGS,  # noqa: F405
        'SERVE_PERMISSIONS': ['rest_framework.permissions.IsAdminUser'],
    }

# Enable ONLY when ingress strips untrusted X-Forwarded-Proto and direct
# access to the application port is blocked. Default: trust no proxy headers.
if boolean(os.environ, 'DJANGO_TRUST_PROXY_HTTPS'):
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# Global settings may have selected console email when DJANGO_DEBUG was absent.
# Production must never inherit that development-only mailer.
if os.environ.get('DJANGO_DEBUG', '').lower() not in {'false', '0'}:
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured('Set DJANGO_DEBUG=false explicitly for production.')
