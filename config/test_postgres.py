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

# Tests do not get persistent connections.
#
# The parser defaults CONN_MAX_AGE to 60, which is right for a server and
# wrong here: the concurrency tests open connections on worker threads, those
# connections stay alive for the next minute, and Django then cannot drop the
# test database at the end of the run. The failure is not the tests -- they
# report OK -- it is the teardown afterwards, so it reads as a green suite and
# a red job, and it arrives only once the suite runs long enough for the
# timing to line up.
DATABASES['default']['CONN_MAX_AGE'] = 0  # noqa: F405
