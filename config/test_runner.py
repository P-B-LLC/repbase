"""The test runner: Django's, with test databases built from code alone.

Migration 0085 loads the shared exercise library from a JSON file in the
repository. A test database built that way would hold whatever the library
holds on the day the suite runs, so a test counting the catalogue, or making
its own "Bench Press", would pass or fail on the library's contents rather than
on the code. Worse, the transactional tests flush every table when they finish,
so the catalogue would be there for some tests and gone for the ones after
them, depending on order.

So test databases are migrated with the sync switched off, and the suites that
test the library (`core.test_activity_tracking`) hand it fixtures of their own.
"""

from django.test.runner import DiscoverRunner
from django.test.utils import override_settings


class RepbaseTestRunner(DiscoverRunner):
    def setup_databases(self, **kwargs):
        with override_settings(EXERCISE_LIBRARY_SYNC_ON_MIGRATE=False):
            return super().setup_databases(**kwargs)
