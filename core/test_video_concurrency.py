"""Run on PostgreSQL: real row locks, not SQLite's different locking model."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless, mock
import tempfile
from django.contrib.auth import get_user_model
from django.db import connection, connections
from django.test import TransactionTestCase, override_settings
from .models import RepbaseUser
from .fixtures_for_tests import workout_post
from .video_pipeline import begin, claim
from .video_jobs_models import VideoJob, VideoDailyUpload


@skipUnless(connection.vendor == 'postgresql', 'Requires PostgreSQL row locks.')
@override_settings(VIDEO_STORAGE_BACKEND='local', VIDEO_DIRECT_MAX_BYTES=15 * 1024 * 1024)
class VideoConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.override = override_settings(VIDEO_LOCAL_ROOT=self.directory.name)
        self.override.enable()
        self.addCleanup(self.override.disable)
        user = get_user_model().objects.create_user(username='videorace')
        self.owner = RepbaseUser.objects.create(user=user, height_cm=180, weight_kg=80)

    def test_parallel_uploads_cannot_exceed_account_quota(self):
        posts = [workout_post(self.owner), workout_post(self.owner)]
        barrier = Barrier(2)
        def attempt(post):
            connections.close_all()
            try:
                barrier.wait(timeout=10)
                begin(post, 100, 'video/mp4')
                return 'accepted'
            except ValueError:
                return 'limited'
            finally:
                connections.close_all()
        with mock.patch('core.video_local_store.available', return_value=10 * 1024**3), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, posts))
        self.assertCountEqual(results, ['accepted', 'limited'])
        self.assertEqual(VideoJob.objects.count(), 1)
        self.assertEqual(VideoDailyUpload.objects.get(owner=self.owner).count, 1)

    def test_parallel_claims_never_claim_the_same_job(self):
        with mock.patch('core.video_local_store.available', return_value=10 * 1024**3):
            job, _ = begin(workout_post(self.owner), 100, 'video/mp4')
        VideoJob.objects.filter(pk=job.pk).update(state='queued')
        barrier = Barrier(2)
        def attempt(_):
            connections.close_all()
            try:
                barrier.wait(timeout=10)
                found = claim()
                return str(found.pk) if found else None
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))
        self.assertCountEqual(results, [str(job.pk), None])

    def test_global_pending_limit_is_atomic_across_accounts(self):
        posts = []
        for index in range(11):
            user = get_user_model().objects.create_user(username=f'videoquota{index}')
            owner = RepbaseUser.objects.create(user=user, height_cm=180, weight_kg=80)
            posts.append(workout_post(owner))
        with mock.patch('core.video_local_store.available', return_value=10 * 1024**3):
            for post in posts[:9]:
                begin(post, 100, 'video/mp4')
        barrier = Barrier(2)
        def attempt(post):
            connections.close_all()
            try:
                barrier.wait(timeout=10)
                begin(post, 100, 'video/mp4')
                return 'accepted'
            except ValueError:
                return 'limited'
            finally:
                connections.close_all()
        with mock.patch('core.video_local_store.available', return_value=10 * 1024**3), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, posts[9:]))
        self.assertCountEqual(results, ['accepted', 'limited'])
        self.assertEqual(VideoJob.objects.count(), 10)
