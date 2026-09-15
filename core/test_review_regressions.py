import tempfile
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.core.files.storage import InMemoryStorage
from django.utils import timezone
from rest_framework.test import APITestCase

from .media_storage import ReservedNameFileSystemStorage
from .models import Gear, PlannerEntry, SaveReceipt, WorkoutSession
from .tests import RepbaseAPITestMixin


class ReceiptAndGearReviewTests(RepbaseAPITestMixin, APITestCase):
    def setUp(self):
        _, self.owner, token = self.create_account('review')
        self.authenticate(token)

    def test_expired_retry_cannot_duplicate_a_saved_task(self):
        key = str(uuid.uuid4())
        body = dict(title='Walk', scheduled_date='2026-09-13', kind='task', category='other')
        def send(payload):
            return self.client.post('/api/v1/planner/', payload, format='json', HTTP_IDEMPOTENCY_KEY=key)
        first = send(body)
        self.assertEqual(first.status_code, 201, first.data)
        SaveReceipt.objects.update(created_at=timezone.now() - timedelta(days=31))
        self.assertEqual(SaveReceipt.prune(), 1)
        for _ in range(2):
            self.assertEqual(send(body).status_code, 409)
            self.assertEqual(PlannerEntry.objects.count(), 1)
        self.assertEqual(send({**body, 'title': 'Changed'}).status_code, 409)
        self.assertEqual(SaveReceipt.prune(), 0)
        self.assertEqual(SaveReceipt.objects.count(), 1)
        self.owner.delete()
        self.assertFalse(SaveReceipt.objects.exists())

    def test_gear_update_and_retirement_keep_history_totals(self):
        gear = Gear.objects.create(owner=self.owner, kind='shoe', name='Shoe', initial_distance_km=2)
        started = timezone.now() - timedelta(hours=1)
        WorkoutSession.objects.create(repbase_user=self.owner, gear=gear, status='completed',
                                      started_at=started, recorded_distance_km=5)
        path = f'/api/v1/gear/{gear.pk}/'
        before = self.client.get(path).data
        for change in [{'is_default': True}, {'name': 'Renamed'}, {'retired_at': timezone.now().isoformat()}]:
            result = self.client.patch(path, change, format='json')
            self.assertEqual(result.status_code, 200, result.data)
            for field in ['total_distance_km', 'session_count', 'last_used_at']:
                self.assertEqual(result.data[field], before[field])
        self.assertEqual(before['total_distance_km'], 7)
        self.assertEqual(before['session_count'], 1)


class ReservedStorageReviewTests(APITestCase):
    def test_deployment_check_requires_exact_name_storage(self):
        from .checks import publication_storage_preserves_names
        with patch('django.core.files.storage.default_storage', InMemoryStorage()):
            self.assertEqual([error.id for error in publication_storage_preserves_names(None)], ['core.E005'])
        with tempfile.TemporaryDirectory() as location:
            with patch('django.core.files.storage.default_storage', ReservedNameFileSystemStorage(location=location)):
                self.assertEqual(publication_storage_preserves_names(None), [])

    def test_filesystem_collision_does_not_rename_or_overwrite(self):
        with tempfile.TemporaryDirectory() as location:
            storage = ReservedNameFileSystemStorage(location=location)
            self.assertEqual(storage.save_reserved('photo.jpg', ContentFile(b'first')), 'photo.jpg')
            with self.assertRaises(FileExistsError):
                storage.save_reserved('photo.jpg', ContentFile(b'second'))
            with storage.open('photo.jpg') as file:
                self.assertEqual(file.read(), b'first')
            self.assertEqual(storage.listdir('')[1], ['photo.jpg'])

    def test_unsupported_renaming_storage_fails_before_upload(self):
        from .post_publication import publication_media, PublicationMediaError
        from .models import PendingMediaDeletion
        storage = InMemoryStorage()
        with patch('core.post_publication.default_storage', storage), patch.object(storage, 'save') as save:
            with self.assertRaises(PublicationMediaError):
                with publication_media(b'photo', '.jpg'):
                    self.fail('Must not publish')
            save.assert_not_called()
        self.assertFalse(PendingMediaDeletion.objects.exists())
