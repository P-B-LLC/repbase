"""Real PostgreSQL locking checks, isolated from provider calls."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection, connections
from django.test import TransactionTestCase
from .models import RepbaseUser, Post
from .fixtures_for_tests import workout_post
from .media_screening_jobs import enqueue, claim
from .media_publication import decide
from .access import AccessConflict
from .access_models import AccessAudit


@skipUnless(connection.vendor == 'postgresql', 'Requires PostgreSQL row locks')
class MediaScreeningConcurrencyTests(TransactionTestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(username='screeningrace')
        owner = RepbaseUser.objects.create(user=user, height_cm=180, weight_kg=80)
        self.post = workout_post(owner)
        self.job = enqueue(self.post.pk, consent_version=settings.MODERATION_CONSENT_VERSION,
                           threshold=0.5)

    def race(self, operation):
        barrier = Barrier(2)
        def attempt(index):
            connections.close_all()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                barrier.wait(timeout=10)
                return operation(index)
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(attempt, range(2)))

    def test_parallel_claims_take_job_once(self):
        def operation(_):
            job = claim()
            return job.pk if job else None
        self.assertCountEqual(self.race(operation), [self.job.pk, None])

    def test_duplicate_approval_has_one_timestamp_and_audit(self):
        results = self.race(lambda _: decide(self.post.pk, self.job.generation, 'approved').published_at)
        self.assertEqual(results[0], results[1])
        self.assertEqual(AccessAudit.objects.filter(action='media_approved').count(), 1)

    def test_conflicting_automatic_results_have_one_winner(self):
        def operation(index):
            try:
                return decide(self.post.pk, self.job.generation,
                              ['approved', 'flagged'][index]).moderation_status
            except AccessConflict:
                return 'conflict'
        results = self.race(operation)
        self.assertEqual(results.count('conflict'), 1)
        post = Post.objects.get(pk=self.post.pk)
        self.assertEqual(post.published_at is not None, post.moderation_status == 'approved')
