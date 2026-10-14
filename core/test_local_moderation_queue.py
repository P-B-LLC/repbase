from datetime import timedelta
from unittest.mock import patch

from django.db import connection, close_old_connections
from django.test import TransactionTestCase, override_settings, skipUnlessDBFeature
from django.utils import timezone
from django.core.management import call_command
from django.core.management.base import CommandError

from .fixtures_for_tests import workout_post
from .tests import RepbaseAPITestMixin
from .local_moderation_models import LocalModerationJob, LocalModerationCase
from .local_moderation_policy import Category, Finding, Signal, evaluate
from .local_moderation_queue import enqueue_shadow, claim, finish, fail, process_one


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class LocalModerationQueueTests(RepbaseAPITestMixin, TransactionTestCase):
    def setUp(self):
        _, self.owner, _ = self.create_account('queue_author')
        self.post = workout_post(self.owner)

    def decision(self, category=Category.NUDITY, finding=Finding.SUSPECTED):
        return evaluate([Signal(category, finding, 'fixture-v1', 'fixture-rule')], screening_complete=True)

    def test_repeat_enqueue_is_one_job_and_one_inference_claim(self):
        first = enqueue_shadow(self.post.pk)
        second = enqueue_shadow(self.post.pk)
        self.assertEqual(first.generation, second.generation)
        self.assertEqual(LocalModerationJob.objects.count(), 1)
        self.assertIsNotNone(claim())
        self.assertIsNone(claim())

    @skipUnlessDBFeature('has_select_for_update_skip_locked')
    def test_postgres_two_workers_cannot_claim_the_same_job(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        enqueue_shadow(self.post.pk)
        start = Barrier(2)
        def worker():
            close_old_connections()
            try:
                start.wait(timeout=10)
                job = claim()
                return job.pk if job else None
            finally:
                connection.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker) for _ in range(2)]
            results = [future.result(timeout=15) for future in futures]
        self.assertEqual(sum(value is not None for value in results), 1)
        self.assertEqual(LocalModerationJob.objects.get().attempts, 1)

    def test_inference_does_not_hold_transaction_or_change_live_visibility(self):
        enqueue_shadow(self.post.pk)
        def detector(post_id):
            self.assertFalse(connection.in_atomic_block)
            return self.decision(Category.CHILD_SEXUAL_EXPLOITATION)
        self.assertTrue(process_one(detector))
        case = LocalModerationCase.objects.get()
        self.assertEqual(case.outcome, 'hold')
        self.assertTrue(case.shadow)
        self.post.refresh_from_db()
        self.assertFalse(self.post.is_hidden)  # Explicitly shadow, NOT activated protection.

    def test_allow_creates_no_review_case(self):
        enqueue_shadow(self.post.pk)
        self.assertTrue(finish(claim(), evaluate([], screening_complete=True)))
        self.assertFalse(LocalModerationCase.objects.exists())

    def test_result_retries_cannot_duplicate_cases(self):
        enqueue_shadow(self.post.pk)
        job = claim()
        self.assertTrue(finish(job, self.decision()))
        self.assertFalse(finish(job, self.decision()))
        self.assertEqual(LocalModerationCase.objects.count(), 1)

    def test_edit_invalidates_old_lease_and_old_result(self):
        enqueue_shadow(self.post.pk)
        old = claim()
        self.post.caption = 'Updated caption'
        self.post.save()
        new = enqueue_shadow(self.post.pk)
        self.assertNotEqual(old.generation, new.generation)
        self.assertFalse(finish(old, self.decision()))
        self.assertEqual(fail(old), 0)
        self.assertFalse(LocalModerationCase.objects.exists())

    def test_unenqueued_edit_also_rejects_stale_result(self):
        enqueue_shadow(self.post.pk)
        job = claim()
        self.post.caption = 'Edited during analysis'
        self.post.save()
        self.assertFalse(finish(job, self.decision()))
        self.assertEqual(LocalModerationJob.objects.get().state, 'stale')

    def test_manual_takedown_is_never_undone(self):
        enqueue_shadow(self.post.pk)
        job = claim()
        self.post.is_hidden = True
        self.post.save()
        self.assertFalse(finish(job, evaluate([], screening_complete=True)))
        self.post.refresh_from_db()
        self.assertTrue(self.post.is_hidden)

    def test_expired_lease_is_reclaimed_but_old_worker_cannot_commit(self):
        enqueue_shadow(self.post.pk)
        old = claim()
        LocalModerationJob.objects.update(lease_until=timezone.now()-timedelta(seconds=1))
        new = claim()
        self.assertNotEqual(new.lease, old.lease)
        self.assertFalse(finish(old, self.decision()))
        self.assertTrue(finish(new, self.decision()))

    def test_retries_back_off_and_stop_without_raw_error_storage(self):
        enqueue_shadow(self.post.pk)
        for attempt in range(3):
            LocalModerationJob.objects.update(available_at=timezone.now())
            with patch('core.local_moderation_queue.finish') as result:
                def broken(post_id):
                    raise RuntimeError('private content must not be stored')
                process_one(broken)
                result.assert_not_called()
            job = LocalModerationJob.objects.get()
            self.assertEqual(job.attempts, attempt+1)
            self.assertEqual(job.error_code, 'screening_unavailable')
        self.assertEqual(job.state, 'failed')
        self.assertIsNone(claim())

    def test_deleted_post_result_is_discarded(self):
        enqueue_shadow(self.post.pk)
        job = claim()
        self.post.delete()
        self.assertFalse(finish(job, self.decision()))

    @override_settings(LOCAL_MODERATION_SHADOW_ENABLED=False)
    def test_worker_disabled_by_default_cannot_enqueue(self):
        with self.assertRaises(CommandError):
            call_command('process_local_moderation', post=[self.post.pk])
        self.assertFalse(LocalModerationJob.objects.exists())

    @override_settings(LOCAL_MODERATION_SHADOW_ENABLED=True, LOCAL_MODERATION_DETECTOR_FACTORY='')
    def test_missing_detector_cannot_be_mistaken_for_success(self):
        with self.assertRaises(CommandError):
            call_command('process_local_moderation', post=[self.post.pk])
        self.assertFalse(LocalModerationJob.objects.exists())
