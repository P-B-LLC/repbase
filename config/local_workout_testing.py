"""Explicit, durable, loopback-only development workspace. Never deploy this.

Start with Scripts/run-local-workout-testing.py, not a hosted environment file.
Only the separate marked directory below can hold this workspace's database.
"""
import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

if os.environ.get("RYTIVO_LOCAL_WORKOUT_TESTING") != "1":
    raise ImproperlyConfigured("Use Scripts/run-local-workout-testing.py for local workout testing.")
if any(os.environ.get(key) for key in (
    "DATABASE_URL", "REDIS_URL", "SMTP_HOST", "SMTP_PASSWORD", "MODERATION_API_KEY",
    "OPENAI_API_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "APNS_KEY_PATH",
    "VIDEO_STORAGE_ACCESS_KEY", "VIDEO_STORAGE_SECRET_KEY",
)):
    raise ImproperlyConfigured("Local workout testing refuses hosted service configuration.")

_repo = Path(__file__).resolve().parent.parent
LOCAL_WORKOUT_STATE = _repo / ".local-workout-testing"
_marker = LOCAL_WORKOUT_STATE / ".rytivo-local-only"
if (LOCAL_WORKOUT_STATE.is_symlink() or not _marker.is_file()
        or _marker.is_symlink() or _marker.read_text() != "rytivo-local-workout-testing-v1\n"):
    raise ImproperlyConfigured("Local workout testing needs its own marked state directory.")
for _name in ("db.sqlite3", "media", "private-videos"):
    if (LOCAL_WORKOUT_STATE / _name).is_symlink():
        raise ImproperlyConfigured("Local workout testing refuses linked state paths.")

from .settings import *  # noqa: E402,F403

DEBUG = True
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "[::1]"]
SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]
DATABASES = {"default": {
    "ENGINE": "django.db.backends.sqlite3",
    "NAME": LOCAL_WORKOUT_STATE / "db.sqlite3",
    "OPTIONS": {"timeout": 20},
}}
MEDIA_ROOT = LOCAL_WORKOUT_STATE / "media"
VIDEO_LOCAL_ROOT = LOCAL_WORKOUT_STATE / "private-videos"
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
MAILERS = {"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
SECURE_SSL_REDIRECT = False
SECURE_HSTS_SECONDS = 0
SECURE_PROXY_SSL_HEADER = None
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
ANALYTICS_ENABLED = False
APNS_ENABLED = False
MODERATION_ENABLED = False
MODERATION_API_KEY = ""
MEDIA_SCREENING_ENABLED = False
VIDEO_PIPELINE_ENABLED = False
VIDEO_MODERATION_BYPASS = False
VIDEO_STORAGE_BACKEND = "local"
ROOT_URLCONF = "config.local_workout_urls"
MIDDLEWARE = ["config.local_workout_urls.LoopbackOnlyMiddleware", *MIDDLEWARE]  # noqa: F405
