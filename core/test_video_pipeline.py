"""Durable local queue, real file storage and access-revocation regressions."""
import io
import tempfile
import shutil
from datetime import timedelta
from pathlib import Path
from unittest import mock
from django.test import RequestFactory, override_settings, SimpleTestCase
from django.conf import settings
from django.utils import timezone
from rest_framework.test import APITestCase
from .fixtures_for_tests import workout_post, video_fixture
from .tests import RepbaseAPITestMixin
from .models import PostVideo, Block
from .video_jobs_models import VideoJob, VideoStoredObject, VideoDailyUpload
from .video_pipeline import queue_local, claim, process, cleanup
from .video_pipeline_views import serve_asset, asset_url
from .videos import ReceivedVideo
from . import video_local_store as store
from .video_transcode import InvalidVideo


@override_settings(VIDEO_PIPELINE_ENABLED=True, VIDEO_STORAGE_BACKEND='local',
    VIDEO_MODERATION_BYPASS=True, VIDEO_LOCAL_ACCEL_PREFIX='',
    VIDEO_DIRECT_MAX_BYTES=15 * 1024 * 1024)
class LocalVideoTests(RepbaseAPITestMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings_override = override_settings(VIDEO_LOCAL_ROOT=self.directory.name)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.user, self.owner, self.token = self.create_account('videobeta')
        self.other_user, self.other, self.other_token = self.create_account('videoviewer')
        self.post = workout_post(self.owner)
        self.authenticate(self.token)

    def queued(self, post=None):
        data = video_fixture('h264_faststart.mp4')
        received = ReceivedVideo(io.BytesIO(data), len(data), 'video/mp4')
        job = queue_local(post or self.post, received)
        self.assertTrue(received.file.closed)
        return job

    def fake_transcode(self, source, directory):
        path = Path(directory) / '854.mp4'
        path.write_bytes(video_fixture('h264_faststart.mp4'))
        thumb = Path(directory) / 'thumbnail.jpg'
        thumb.write_bytes(b'jpeg')
        return [{'path': path, 'width': 64, 'height': 48, 'size_bytes': path.stat().st_size,
            'duration_ms': 1000, 'has_audio': True}], thumb

    def ready(self):
        self.queued()
        job = claim()
        with mock.patch('core.video_pipeline.transcode', side_effect=self.fake_transcode):
            self.assertTrue(process(job))
        return PostVideo.objects.get(post=self.post)

    def test_upload_does_not_encode_or_publish_and_complete_is_idempotent(self):
        from .video_pipeline import complete
        with mock.patch('core.video_pipeline.transcode') as encoder:
            job = self.queued()
            encoder.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.state, 'queued')
        self.assertEqual(job.video.processing_state, 'processing')
        self.assertEqual(job.video.status, 'pending')
        self.assertEqual(complete(job.pk, self.user.pk).state, 'queued')
        self.assertEqual(VideoJob.objects.count(), 1)

    @override_settings(MEDIA_SCREENING_ENABLED=True)
    def test_screening_waits_for_transcode_and_overrides_beta_bypass(self):
        from .models import MediaScreeningJob
        data = video_fixture('h264_faststart.mp4')
        queue_local(self.post, ReceivedVideo(io.BytesIO(data), len(data), 'video/mp4'),
                    consent_version=settings.MODERATION_CONSENT_VERSION)
        self.post.refresh_from_db()
        self.assertIsNone(self.post.published_at)
        self.assertEqual(MediaScreeningJob.objects.get(post=self.post).state, 'waiting')
        with mock.patch('core.video_pipeline.transcode', side_effect=self.fake_transcode):
            self.assertTrue(process(claim()))
        self.assertEqual(PostVideo.objects.get(post=self.post).status, 'pending')
        self.assertEqual(MediaScreeningJob.objects.get(post=self.post).state, 'queued')

    def test_raw_endpoint_is_backwards_compatible_and_enforces_owner(self):
        data = video_fixture('h264_faststart.mp4')
        with mock.patch('core.video_pipeline.transcode') as encoder:
            response = self.client.post(f'/api/v1/social/posts/{self.post.pk}/video/', data, content_type='video/mp4')
        self.assertEqual(response.status_code, 200, response.data)
        encoder.assert_not_called()
        self.assertEqual(response.data['video']['processing_state'], 'processing')
        self.assertIsNone(response.data['video']['url'])
        self.authenticate(self.other_token)
        response = self.client.post(f'/api/v1/social/posts/{self.post.pk}/video/', data, content_type='video/mp4')
        self.assertIn(response.status_code, [403, 404])

    def test_local_does_not_issue_direct_upload_policies(self):
        response = self.client.post(f'/api/v1/social/posts/{self.post.pk}/video-upload/', {'size_bytes': 100, 'content_type': 'video/mp4'})
        self.assertEqual(response.status_code, 503)

    def test_one_pending_job_per_account(self):
        self.queued()
        from rest_framework.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            self.queued(workout_post(self.owner))
        self.assertEqual(VideoJob.objects.count(), 1)

    def test_daily_budget_survives_deleting_posts(self):
        for _ in range(5):
            post = workout_post(self.owner)
            self.queued(post)
            post.delete()
        from rest_framework.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            self.queued()
        self.assertEqual(VideoDailyUpload.objects.get(owner=self.owner).count, 5)

    def test_disk_pressure_refuses_before_accepting_job(self):
        from rest_framework.exceptions import ValidationError
        with mock.patch('core.video_local_store.available', return_value=1):
            with self.assertRaises(ValidationError):
                self.queued()
        self.assertFalse(VideoJob.objects.exists())

    def test_failed_disk_write_leaves_a_recoverable_failed_job(self):
        from .post_publication import PublicationMediaError
        with mock.patch('core.video_local_store.write', side_effect=OSError('full')):
            with self.assertRaises(PublicationMediaError):
                self.queued()
        self.assertEqual(VideoJob.objects.get().state, 'failed')
        self.assertEqual(PostVideo.objects.get(post=self.post).processing_state, 'failed')

    def test_changed_source_is_never_published(self):
        job = self.queued()
        store.path(job.source_key).write_bytes(b'tampered')
        with mock.patch('core.video_pipeline.transcode') as encoder:
            self.assertFalse(process(claim()))
            encoder.assert_not_called()
        self.assertEqual(PostVideo.objects.get(post=self.post).processing_state, 'failed')

    def test_approval_cannot_serve_unprocessed_assets(self):
        self.queued()
        video = PostVideo.objects.get(post=self.post)
        video.status = 'approved'
        video.variants = [{'key': video.job.source_key, 'width': 1, 'height': 1, 'size_bytes': 1}]
        video.save()
        self.assertEqual(serve_asset(self.asset_request(video)).status_code, 403)

    def test_expired_upload_and_expired_playback_token(self):
        job = self.queued()
        VideoJob.objects.filter(pk=job.pk).update(state='uploading', created_at=timezone.now() - timedelta(days=2))
        cleanup()
        job.refresh_from_db()
        self.assertEqual(job.state, 'failed')
        self.post.delete()
        self.post = workout_post(self.owner)
        video = self.ready()
        request = self.asset_request(video)
        with mock.patch('django.core.signing.time.time', return_value=timezone.now().timestamp() + 3601):
            self.assertEqual(serve_asset(request).status_code, 403)

    def test_ready_assets_are_retained_and_source_is_cleaned(self):
        video = self.ready()
        self.assertEqual(video.status, 'approved')
        self.assertEqual(video.processing_state, 'ready')
        self.assertEqual(VideoStoredObject.objects.filter(retained=True).count(), 2)
        VideoStoredObject.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(cleanup(), 1)
        self.assertTrue(store.path(video.variants[0]['key']).exists())
        self.post.delete()
        self.assertEqual(cleanup(), 2)

    @override_settings(VIDEO_MODERATION_BYPASS=False)
    def test_without_bypass_ready_video_still_waits_for_approval(self):
        self.assertEqual(self.ready().status, 'pending')

    def test_invalid_clip_is_terminal_and_not_published(self):
        self.queued()
        job = claim()
        with mock.patch('core.video_pipeline.transcode', side_effect=InvalidVideo('duration_limit')):
            self.assertFalse(process(job))
        job.refresh_from_db()
        self.assertEqual(job.state, 'failed')
        self.assertEqual(job.video.processing_state, 'failed')
        self.assertEqual(job.video.status, 'pending')

    def test_temporary_failure_retries_but_never_forever(self):
        self.queued()
        for attempt in range(3):
            VideoJob.objects.update(next_attempt_at=None)
            job = claim()
            with mock.patch('core.video_pipeline.transcode', side_effect=OSError('transient')):
                self.assertFalse(process(job))
            job.refresh_from_db()
            self.assertEqual(job.state, 'failed' if attempt == 2 else 'queued')
        self.assertIsNone(claim())

    def test_reclaimed_worker_cannot_publish_stale_outputs(self):
        self.queued()
        stale = claim()
        VideoJob.objects.filter(pk=stale.pk).update(lease_until=timezone.now() - timedelta(seconds=1))
        newer = claim()
        self.assertNotEqual(stale.lease, newer.lease)
        with mock.patch('core.video_pipeline.transcode', side_effect=self.fake_transcode):
            self.assertFalse(process(stale))
            self.assertTrue(process(newer))
        video = PostVideo.objects.get(post=self.post)
        self.assertIn(str(newer.lease), video.variants[0]['key'])

    def test_cleanup_failure_preserves_retry_record(self):
        self.queued()
        VideoStoredObject.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        with mock.patch('core.video_local_store.delete', side_effect=OSError('disk')):
            self.assertEqual(cleanup(), 0)
        self.assertEqual(VideoStoredObject.objects.count(), 1)

    def asset_request(self, video, user=None, **headers):
        request = RequestFactory().get('/')
        request.user = user or self.other_user
        url = asset_url(video, video.variants[0]['key'], request)
        return RequestFactory().get(url, **headers)

    def test_range_playback_and_revocation(self):
        video = self.ready()
        request = self.asset_request(video, HTTP_RANGE='bytes=0-1')
        response = serve_asset(request)
        self.assertEqual(response.status_code, 206)
        self.assertEqual(b''.join(response.streaming_content), video_fixture('h264_faststart.mp4')[:2])
        Block.objects.create(blocker=self.owner, blocked=self.other)
        self.assertEqual(serve_asset(request).status_code, 403)

    def test_private_takedown_pending_and_bad_signatures_are_denied(self):
        video = self.ready()
        request = self.asset_request(video)
        self.post.visibility = 'private'
        self.post.save()
        self.assertEqual(serve_asset(request).status_code, 403)
        owner_request = self.asset_request(video, self.user)
        self.post.is_hidden = True
        self.post.save()
        self.assertEqual(serve_asset(owner_request).status_code, 403)
        self.assertEqual(serve_asset(RequestFactory().get('/?token=bad')).status_code, 403)

    def test_internal_offload_and_safe_methods(self):
        video = self.ready()
        request = self.asset_request(video)
        with override_settings(VIDEO_LOCAL_ACCEL_PREFIX='/_private_video/'):
            response = serve_asset(request)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response['X-Accel-Redirect'].startswith('/_private_video/video-ready/'))
        self.assertEqual(response.content, b'')
        self.assertEqual(serve_asset(RequestFactory().post(request.get_full_path())).status_code, 405)

    def test_paths_are_confined_and_keys_are_immutable(self):
        with self.assertRaises(ValueError):
            store.path('../outside')
        store.write('safe/file', io.BytesIO(b'a'))
        with self.assertRaises(FileExistsError):
            store.write('safe/file', io.BytesIO(b'b'))
        self.assertEqual(store.path('safe/file').read_bytes(), b'a')

    def test_worker_command_serializes_manual_invocations(self):
        from .management.commands.process_videos import worker_lock
        with worker_lock() as first:
            with worker_lock() as second:
                self.assertTrue(first)
                self.assertFalse(second)


@override_settings(VIDEO_STORAGE_BACKEND='local', VIDEO_MAX_SECONDS=30,
    VIDEO_MAX_DIMENSION=1920, VIDEO_MAX_PIXELS=1920 * 1080)
class RealEncoderTests(SimpleTestCase):
    def setUp(self):
        if not shutil.which(settings.VIDEO_FFMPEG) or not shutil.which(settings.VIDEO_FFPROBE):
            self.skipTest('Install ffmpeg and ffprobe for real encoder verification.')

    def test_real_phone_clips_are_encoded_and_metadata_removed(self):
        from .video_transcode import transcode, probe
        for name in ('h264_faststart.mp4', 'moov_at_end.mp4', 'hevc.mov', 'with_location.mov'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / 'source.mp4'
                source.write_bytes(video_fixture(name))
                outputs, thumb = transcode(source, directory)
                self.assertEqual(len(outputs), 1)
                self.assertEqual(probe(outputs[0]['path'])['width'], 64)
                self.assertGreater(thumb.stat().st_size, 0)
                data = outputs[0]['path'].read_bytes()
                self.assertLess(data.index(b'moov'), data.index(b'mdat'))
                self.assertNotIn(b'40.4406', data)
                self.assertNotIn(b'My house', data)

    def test_corrupt_audio_only_and_unsupported_codecs_fail_permanently(self):
        from .video_transcode import probe
        for data in (b'not a video', video_fixture('audio_only.m4a'), video_fixture('mpeg4part2.mp4')):
            with tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / 'source.mp4'
                source.write_bytes(data)
                with self.assertRaises(InvalidVideo):
                    probe(source)

    def test_portrait_and_landscape_fit_480_class_without_upscaling(self):
        from .video_transcode import transcode, run
        for geometry in ('1920x1080', '1080x1920'):
            with self.subTest(geometry=geometry), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / 'source.mp4'
                run([settings.VIDEO_FFMPEG, '-v', 'error', '-y', '-f', 'lavfi', '-i',
                    f'color=c=black:s={geometry}:r=30', '-t', '0.2', '-c:v', 'libx264', '-threads', '1', str(source)])
                outputs, _ = transcode(source, directory)
                self.assertLessEqual(max(outputs[0]['width'], outputs[0]['height']), 854)
                self.assertEqual(min(outputs[0]['width'], outputs[0]['height']), 480)
