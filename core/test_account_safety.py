from unittest import mock

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.db import transaction
from django.test import override_settings
from rest_framework.test import APITestCase

from .models import PendingMediaDeletion, Post
from .tests import RepbaseAPITestMixin


@override_settings(STORAGES={'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'}})
class AccountMediaCleanupTests(RepbaseAPITestMixin, APITestCase):
    def setUp(self):
        self.user, self.profile, self.token = self.create_account('cleanup')
        self.authenticate(self.token)
        self.profile.profile_photo.save('avatar.jpg', ContentFile(b'avatar'))
        self.post = Post.objects.create(author=self.profile, kind=Post.Kind.MEAL)
        self.post.image.save('photo.jpg', ContentFile(b'original'))
        self.post.feed_image.save('feed.jpg', ContentFile(b'variant'))
        self.names = [self.profile.profile_photo.name, self.post.image.name, self.post.feed_image.name]

    def test_account_deletion_removes_all_media_after_commit(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.delete('/api/v1/me/')
            self.assertEqual(response.status_code, 204)
            self.assertTrue(all(default_storage.exists(name) for name in self.names))
        self.assertFalse(any(default_storage.exists(name) for name in self.names))
        self.assertFalse(PendingMediaDeletion.objects.exists())
        self.assertEqual(self.client.get('/api/v1/me/').status_code, 401)

    def test_post_deletion_keeps_avatar_and_removes_both_sizes(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.post.delete()
        self.assertTrue(default_storage.exists(self.names[0]))
        self.assertFalse(any(default_storage.exists(name) for name in self.names[1:]))

    def test_rollback_keeps_files_and_discards_cleanup_jobs(self):
        with self.captureOnCommitCallbacks(execute=True):
            with self.assertRaises(RuntimeError):
                with transaction.atomic():
                    self.user.delete()
                    raise RuntimeError('rollback')
        self.assertTrue(all(default_storage.exists(name) for name in self.names))
        self.assertFalse(PendingMediaDeletion.objects.exists())

    def test_storage_failure_is_durable_and_retryable(self):
        with mock.patch.object(default_storage, 'delete', side_effect=OSError('storage offline')):
            with self.assertLogs('core.media_cleanup', level='ERROR'):
                with self.captureOnCommitCallbacks(execute=True):
                    self.user.delete()
        self.assertEqual(PendingMediaDeletion.objects.count(), 3)
        self.assertTrue(all(default_storage.exists(name) for name in self.names))
        call_command('retry_media_deletions', verbosity=0)
        self.assertFalse(PendingMediaDeletion.objects.exists())
        self.assertFalse(any(default_storage.exists(name) for name in self.names))

    def test_surviving_reference_is_not_deleted(self):
        other = Post.objects.create(author=self.profile, kind=Post.Kind.MEAL, image=self.names[1])
        with self.captureOnCommitCallbacks(execute=True):
            self.post.delete()
        self.assertTrue(default_storage.exists(self.names[1]))
        with self.captureOnCommitCallbacks(execute=True):
            other.delete()
        self.assertFalse(default_storage.exists(self.names[1]))
