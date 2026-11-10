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

from django.test.runner import DiscoverRunner, ParallelTestSuite, _init_worker
from django.test.utils import override_settings


def _start_from_no_rollout_rows():
    # A test database has no rollout rows until a test makes one, so each
    # test process starts from that instead of asking: the first request of
    # whichever test ran first would otherwise cost one query more than the
    # same request a moment later, and tests that count queries would fail
    # by the order they happened to run in.
    from core import rollout

    rollout.assume_no_rows()


def _init_worker_without_rollout_rows(*args, **kwargs):
    _init_worker(*args, **kwargs)
    _start_from_no_rollout_rows()


class RepbaseParallelTestSuite(ParallelTestSuite):
    init_worker = _init_worker_without_rollout_rows


class RepbaseTestRunner(DiscoverRunner):
    parallel_test_suite = RepbaseParallelTestSuite

    def setup_databases(self, **kwargs):
        with override_settings(EXERCISE_LIBRARY_SYNC_ON_MIGRATE=False):
            databases = super().setup_databases(**kwargs)
        _start_from_no_rollout_rows()
        return databases
