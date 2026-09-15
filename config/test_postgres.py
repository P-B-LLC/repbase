"""Isolated PostgreSQL integration-test settings; never production settings."""
import os
from urllib.parse import quote

from .settings import *  # noqa: F403

from .environment import database

# Exercise the same parser/options as hosted PostgreSQL, with plaintext allowed
# only for this disposable loopback test service. Never target a real database.
DATABASES = {'default': database({
    'DATABASE_URL': 'postgresql://postgres:{}@127.0.0.1:{}/rytivo_test'.format(
        quote(os.environ['TEST_POSTGRES_PASSWORD'], safe=''),
        os.environ.get('TEST_POSTGRES_PORT', '5432')),
    'DATABASE_SSLMODE': 'disable',
}, BASE_DIR)}  # noqa: F405
