"""The cleanup jobs have nothing calling them, and silence is the failure.

Two tables only grow and storage keeps files no row points at, and the first
symptom is a disk bill or a photo that outlived the account that uploaded it.
The check asks about the outcome -- is anything left that a daily run should
already have taken -- rather than whether a job ran, because a job that runs
and does nothing would pass the second question and fail the first.
"""

from datetime import timedelta

from django.core.files.base import ContentFile
from django.test import TestCase
from django.utils import timezone

from .checks import scheduled_cleanup_is_running
from .models import FoodSearchCache, PendingMediaDeletion, SaveReceipt


def age(model, **fields):
    instance = model.objects.create(**fields)
    return instance


class TheCleanupCheckTests(TestCase):
    def test_a_clean_database_says_nothing(self):
        self.assertEqual(scheduled_cleanup_is_running(None), [])

    def test_a_fresh_cache_entry_is_not_overdue(self):
        FoodSearchCache.objects.create(term='oats', payload=[])
        self.assertEqual(scheduled_cleanup_is_running(None), [])

    def test_an_expired_cache_entry_is_reported(self):
        entry = FoodSearchCache.objects.create(term='oats', payload=[])
        FoodSearchCache.objects.filter(pk=entry.pk).update(
            fetched_at=timezone.now() - FoodSearchCache.RETENTION - timedelta(days=1)
        )
        problems = scheduled_cleanup_is_running(None)
        self.assertEqual([problem.id for problem in problems], ['core.W007'])
        self.assertIn('food-search cache', problems[0].msg)

    def test_a_deletion_stuck_for_a_day_is_reported(self):
        job = PendingMediaDeletion.objects.create(name='post-photos/gone.jpg')
        PendingMediaDeletion.objects.filter(pk=job.pk).update(
            created_at=timezone.now() - timedelta(days=2)
        )
        problems = scheduled_cleanup_is_running(None)
        self.assertEqual([problem.id for problem in problems], ['core.W007'])
        self.assertIn('media deletions', problems[0].msg)

    def test_a_deletion_in_flight_is_not_reported(self):
        """These are retried after a storage failure; a few in flight is
        normal and warning about them would train people to ignore this."""
        PendingMediaDeletion.objects.create(name='post-photos/recent.jpg')
        self.assertEqual(scheduled_cleanup_is_running(None), [])

    def test_running_the_maintenance_work_clears_it(self):
        """The check has to be satisfiable by doing the thing it asks for."""
        entry = FoodSearchCache.objects.create(term='oats', payload=[])
        FoodSearchCache.objects.filter(pk=entry.pk).update(
            fetched_at=timezone.now() - FoodSearchCache.RETENTION - timedelta(days=1)
        )
        self.assertTrue(scheduled_cleanup_is_running(None))

        FoodSearchCache.prune()
        SaveReceipt.prune()
        self.assertEqual(scheduled_cleanup_is_running(None), [])
