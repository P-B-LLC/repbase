import base64
import io
from unittest.mock import patch

from django.core.files.storage import InMemoryStorage
from django.db import IntegrityError
from django.utils import timezone
from PIL import Image
from rest_framework.test import APITransactionTestCase

from .models import FoodMeal, FoodEntry, Gear, Post, PendingMediaDeletion
from .media_cleanup import delete_pending_media
from .tests import RepbaseAPITestMixin


class BetaPublicationTests(RepbaseAPITestMixin, APITransactionTestCase):
    def setUp(self):
        _, self.profile, token = self.create_account('beta-publication')
        self.authenticate(token)
        self.storage = InMemoryStorage()
        self.meal = FoodMeal.objects.create(owner=self.profile, date=timezone.localdate(), name='Meal', position=1)
        FoodEntry.objects.create(meal=self.meal, name='Rice', calories=100, position=1)
        image = io.BytesIO()
        Image.new('RGB', (100, 100)).save(image, format='JPEG')
        self.payload = dict(kind='meal', source_id=self.meal.pk, content_type='image/jpeg',
                            image_base64=base64.b64encode(image.getvalue()).decode())

    def publish(self):
        with patch('core.post_publication.default_storage', self.storage), \
             patch('core.media_cleanup.default_storage', self.storage):
            return self.client.post('/api/v1/social/posts/', self.payload, format='json')

    def test_switch_default_on_create_and_update(self):
        first = self.client.post('/api/v1/gear/', dict(kind='shoe', name='First', is_default=True), format='json')
        second = self.client.post('/api/v1/gear/', dict(kind='shoe', name='Second', is_default=True), format='json')
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(second.status_code, 201, second.data)
        self.assertFalse(Gear.objects.get(pk=first.data['id']).is_default)
        response = self.client.patch(f"/api/v1/gear/{first.data['id']}/", {'is_default': True}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(Gear.objects.get(pk=second.data['id']).is_default)
        self.assertEqual(Gear.objects.filter(owner=self.profile, is_default=True).count(), 1)

    def test_failed_default_save_restores_previous_default(self):
        original = Gear.objects.create(owner=self.profile, kind='shoe', name='First', is_default=True)
        with patch('core.serializers.GearSerializer.create', side_effect=IntegrityError('injected')):
            with self.assertRaises(IntegrityError):
                self.client.post('/api/v1/gear/', dict(kind='shoe', name='Second', is_default=True), format='json')
        original.refresh_from_db()
        self.assertTrue(original.is_default)

    def test_original_upload_failure_publishes_nothing(self):
        with patch.object(self.storage, 'save', side_effect=OSError('offline')):
            response = self.publish()
        self.assertEqual(response.status_code, 503)
        self.assertFalse(Post.objects.exists())
        self.assertFalse(PendingMediaDeletion.objects.exists())

    def test_thumbnail_failure_rolls_back_and_cleans_original(self):
        save = self.storage.save
        written = []
        def fail_thumbnail(name, content):
            if '/feed/' in name:
                raise OSError('thumbnail offline')
            result = save(name, content)
            written.append(result)
            return result
        with patch('core.post_publication.feed_variant', return_value=b'thumbnail'), \
             patch.object(self.storage, 'save', side_effect=fail_thumbnail):
            self.assertEqual(self.publish().status_code, 503)
        self.assertFalse(Post.objects.exists())
        self.assertTrue(written)
        self.assertTrue(all(not self.storage.exists(name) for name in written))

    def test_cleanup_failure_is_durable_and_retryable(self):
        with patch('core.post_publication.feed_variant', return_value=None), \
             patch('core.views.create_post_from_source', side_effect=RuntimeError('database failure')), \
             patch.object(self.storage, 'delete', side_effect=OSError('delete offline')):
            with self.assertRaises(RuntimeError):
                self.publish()
        self.assertFalse(Post.objects.exists())
        jobs = list(PendingMediaDeletion.objects.all())
        self.assertTrue(jobs)
        with patch('core.media_cleanup.default_storage', self.storage):
            for job in jobs:
                self.assertTrue(delete_pending_media(job.pk))
                self.assertFalse(self.storage.exists(job.name))
        self.assertFalse(PendingMediaDeletion.objects.exists())

    def test_success_publishes_photo_and_consumes_cleanup_jobs(self):
        with patch('core.post_publication.feed_variant', return_value=None):
            response = self.publish()
        self.assertEqual(response.status_code, 201, response.data)
        post = Post.objects.get()
        self.assertTrue(self.storage.exists(post.image.name))
        self.assertFalse(PendingMediaDeletion.objects.exists())

    def test_failure_after_snapshot_creation_rolls_back_publication(self):
        with patch('core.post_publication.feed_variant', return_value=None), \
             patch('core.serializers.PostSerializer.to_representation', side_effect=RuntimeError('response failed')):
            with self.assertRaises(RuntimeError):
                self.publish()
        self.assertFalse(Post.objects.exists())
        self.assertFalse(PendingMediaDeletion.objects.exists())

    def test_interrupted_request_leaves_cleanup_record_before_file_write(self):
        class InterruptedPublication(BaseException):
            pass
        save = self.storage.save
        def interrupted(name, content):
            save(name, content)
            # Bypass ordinary exception cleanup, like a terminated worker.
            raise InterruptedPublication()
        with patch.object(self.storage, 'save', side_effect=interrupted):
            with self.assertRaises(InterruptedPublication):
                self.publish()
        self.assertFalse(Post.objects.exists())
        jobs = list(PendingMediaDeletion.objects.all())
        self.assertTrue(jobs)
        self.assertTrue(any(self.storage.exists(job.name) for job in jobs))
        with patch('core.media_cleanup.default_storage', self.storage):
            for job in jobs:
                self.assertTrue(delete_pending_media(job.pk))
                self.assertFalse(self.storage.exists(job.name))
