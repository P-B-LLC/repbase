"""Isolated PostgreSQL integration-test settings; never production settings."""
import os

from .settings import *  # noqa: F403

DATABASES = {"default": {
    "ENGINE": "django.db.backends.postgresql",
    "NAME": "rytivo_test",
    "USER": "postgres",
    "PASSWORD": os.environ["TEST_POSTGRES_PASSWORD"],
    "HOST": "127.0.0.1",
    "PORT": os.environ.get("TEST_POSTGRES_PORT", "5432"),
}}
