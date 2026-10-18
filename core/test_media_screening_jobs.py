from contextlib import contextmanager
import uuid
from unittest.mock import patch
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import TestCase, override_settings

from .fixtures_for_tests import TemporaryMediaMixin, make_account, workout_post
from .models import Post, MediaScreeningJob
from .media_screening_jobs import enqueue, claim, process
from .openai_media_moderation import MediaVerdict, MediaScreeningUnavailable


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class ScreeningJobTests(TemporaryMediaMixin, TestCase):
    def setUp(self):
        from django.conf import settings
        _, self.author, _ = make_account('screening_author')
        self.post = workout_post(self.author)
        self.post.image = default_storage.save(f'post-photos/{uuid.uuid4().hex}.jpg', ContentFile(b'test-image'))
        self.post.save()
        self.job = enqueue(self.post.pk, consent_version=settings.MODERATION_CONSENT_VERSION, threshold=0.5)
        self.post.refresh_from_db()

    def test_clear_image_publishes(self):
        with patch('core.media_screening_jobs.screen_image', return_value=MediaVerdict(False, 0.1, False, 'test')):
            self.assertTrue(process(claim()))
        self.post.refresh_from_db()
        self.assertEqual(self.post.moderation_status, 'approved')
        self.assertIsNotNone(self.post.published_at)

    def test_flagged_image_stays_private(self):
        with patch('core.media_screening_jobs.screen_image', return_value=MediaVerdict(True, 0.9, True, 'test')):
            self.assertTrue(process(claim()))
        self.post.refresh_from_db()
        self.assertEqual(self.post.moderation_status, 'flagged')
        self.assertIsNone(self.post.published_at)

    def test_timeout_is_durable_retry_not_approval(self):
        with patch('core.media_screening_jobs.screen_image', side_effect=MediaScreeningUnavailable()):
            self.assertFalse(process(claim()))
        self.job.refresh_from_db()
        self.post.refresh_from_db()
        self.assertEqual(self.job.state, 'queued')
        self.assertIsNone(self.post.published_at)
        self.assertIsNone(claim())

    def test_replaced_generation_cannot_publish(self):
        from django.conf import settings
        job = claim()
        enqueue(self.post.pk, consent_version=settings.MODERATION_CONSENT_VERSION, threshold=0.5)
        with patch('core.media_screening_jobs.screen_image') as provider:
            self.assertFalse(process(job))
        provider.assert_not_called()

    def test_safety_hold_never_calls_provider(self):
        Post.objects.filter(pk=self.post.pk).update(moderation_child_hold=True)
        with patch('core.media_screening_jobs.screen_image') as provider:
            self.assertFalse(process(claim()))
        provider.assert_not_called()
        self.job.refresh_from_db()
        self.assertEqual(self.job.error_code, 'safety_hold')

    def test_expired_lease_cannot_publish(self):
        from datetime import timedelta
        from django.utils import timezone
        job = claim()
        MediaScreeningJob.objects.filter(pk=job.pk).update(lease_until=timezone.now() - timedelta(seconds=1))
        with patch('core.media_screening_jobs.screen_image') as provider:
            self.assertFalse(process(job))
        provider.assert_not_called()

    def check_video(self, flagged):
        from pathlib import Path
        import tempfile
        from .models import PostVideo
        self.post.image = ''
        self.post.save()
        name = default_storage.save(f'post-videos/{uuid.uuid4().hex}.mp4', ContentFile(b'test-video'))
        PostVideo.objects.create(post=self.post, file=name, content_type='video/mp4',
            codec='avc1', duration_ms=1000, width=64, height=48, size_bytes=10)
        @contextmanager
        def frames(source):
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'frame.jpg'
                path.write_bytes(b'test-frame')
                yield [path]
        with patch('core.moderation_frames.sampled_frames', side_effect=frames), patch(
                'core.media_screening_jobs.screen_image',
                return_value=MediaVerdict(flagged, 0.9 if flagged else 0.1, flagged, 'test')):
            self.assertTrue(process(claim()))
        self.post.refresh_from_db()
        self.assertEqual(self.post.moderation_status, 'flagged' if flagged else 'approved')

    def test_clear_video_publishes(self):
        self.check_video(False)

    def test_flagged_video_stays_private(self):
        self.check_video(True)

    @override_settings(MEDIA_SCREENING_ENABLED=True)
    def test_removing_video_revokes_old_review_and_does_not_approve(self):
        from .videos import remove_video
        self.check_video(True)
        generation = self.post.moderation_generation
        remove_video(self.post)
        self.post.refresh_from_db()
        self.assertNotEqual(self.post.moderation_generation, generation)
        self.assertIsNone(self.post.published_at)
        self.assertEqual(MediaScreeningJob.objects.get(post=self.post).state, 'cancelled')
