"""Explicit hosted settings; no SQLite or development-secret fallback."""
import os

from .settings import *  # noqa: F403
from .environment import boolean, database, integer, production_values

# settings.py chooses its mail backend at import time. Require DEBUG=false
# before importing this module via the deployment entry points.
DEBUG = False
MODERATION_ENABLED = True
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

# Enable ONLY when ingress strips untrusted X-Forwarded-Proto and direct
# access to the application port is blocked. Default: trust no proxy headers.
if boolean(os.environ, 'DJANGO_TRUST_PROXY_HTTPS'):
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# Global settings may have selected console email when DJANGO_DEBUG was absent.
# Production must never inherit that development-only mailer.
if os.environ.get('DJANGO_DEBUG', '').lower() not in {'false', '0'}:
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured('Set DJANGO_DEBUG=false explicitly for production.')
