from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
import uuid

from django.db import close_old_connections, connection
from django.utils import timezone
from rest_framework.test import APIClient, APITransactionTestCase

from .models import Gear, PlannerEntry, WorkoutSession, WorkoutTemplate
from .tests import RepbaseAPITestMixin


@skipUnless(connection.vendor == 'postgresql', 'Requires PostgreSQL row locks')
class SaveConcurrencyTests(RepbaseAPITestMixin, APITransactionTestCase):
    def setUp(self):
        _, self.profile, self.token = self.create_account('concurrent-save')

    def concurrently_post(self, path, payload, **headers):
        barrier = Barrier(2)

        def send():
            close_old_connections()
            try:
                client = APIClient()
                client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
                barrier.wait(timeout=10)
                return client.post(path, payload, format='json', **headers).status_code
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(send) for _ in range(2)]
            return [result.result(timeout=20) for result in results]

    def test_concurrent_create_replay(self):
        statuses = self.concurrently_post('/api/v1/planner/', {
            'title': 'Walk', 'scheduled_date': '2026-09-12', 'kind': 'task', 'category': 'other',
        }, HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))
        self.assertEqual(statuses, [201, 201])
        self.assertEqual(PlannerEntry.objects.count(), 1)

    def test_concurrent_default_switches_leave_one_default(self):
        first = Gear.objects.create(owner=self.profile, kind='shoe', name='First', is_default=True)
        second = Gear.objects.create(owner=self.profile, kind='shoe', name='Second')
        third = Gear.objects.create(owner=self.profile, kind='shoe', name='Third')
        barrier = Barrier(2)
        def switch(pk):
            close_old_connections()
            try:
                client = APIClient()
                client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
                barrier.wait(timeout=10)
                return client.patch(f'/api/v1/gear/{pk}/', {'is_default': True}, format='json').status_code
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = [pool.submit(switch, gear.pk) for gear in (second, third)]
            self.assertEqual([result.result(timeout=20) for result in pending], [200, 200])
        self.assertEqual(Gear.objects.filter(owner=self.profile, is_default=True).count(), 1)
        first.refresh_from_db()
        self.assertFalse(first.is_default)

    def test_concurrent_route_and_end_replay(self):
        workout = WorkoutTemplate.objects.create(owner=self.profile, name='Run', workout_type='running')
        session = WorkoutSession.objects.create(repbase_user=self.profile, workout=workout,
                                                status='active', started_at=timezone.now())
        base = f'/api/v1/sessions/{session.pk}'
        point = {'latitude': '41.0', 'longitude': '-87.0', 'recorded_at': session.started_at.isoformat()}
        self.assertEqual(self.concurrently_post(base + '/route/', {'points': [point]}), [200, 200])
        self.assertEqual(session.route_points.count(), 1)
        self.assertEqual(self.concurrently_post(base + '/end/', {}), [200, 200])
