from datetime import timedelta
import uuid
from unittest.mock import patch

from django.utils import timezone
from rest_framework.test import APITestCase

from .models import (WorkoutSession, WorkoutTemplate, PlannerEntry, SaveReceipt,
                     FoodMeal, FoodEntry, SavedFoodMeal, Exercise, WorkoutExercise, WorkoutSchedule)
from .tests import RepbaseAPITestMixin


class WorkoutSaveRecoveryTests(RepbaseAPITestMixin, APITestCase):
    def setUp(self):
        _, profile, token = self.create_account('recovery')
        self.authenticate(token)
        workout = WorkoutTemplate.objects.create(owner=profile, name='Run', workout_type='running')
        self.session = WorkoutSession.objects.create(
            repbase_user=profile, workout=workout, status='active', started_at=timezone.now()
        )
        self.base = f'/api/v1/sessions/{self.session.pk}'

    def point(self, index):
        return dict(latitude='41.000000', longitude=f'-87.00{index:04d}',
                    recorded_at=(self.session.started_at + timedelta(seconds=index)).isoformat())

    def upload(self, points):
        response = self.client.post(self.base + '/route/', {'points': points}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        return response

    def test_retry_does_not_duplicate_route_or_completion(self):
        points = [self.point(0), self.point(1)]
        self.upload(points)
        first = self.client.post(self.base + '/end/')
        self.assertEqual(first.status_code, 200)
        self.upload(points)
        second = self.client.post(self.base + '/end/')
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.data['ended_at'], second.data['ended_at'])
        self.assertEqual(self.session.route_points.count(), 2)
        self.assertEqual(WorkoutSession.objects.count(), 1)

    def test_overlapping_and_repeated_samples_are_safe(self):
        self.upload([self.point(0), self.point(1)])
        self.upload([self.point(1), self.point(2), self.point(2)])
        self.assertEqual(self.session.route_points.count(), 3)

    def test_another_account_cannot_retry_this_session(self):
        _, _, token = self.create_account('other')
        self.authenticate(token)
        self.assertEqual(self.client.post(self.base + '/end/').status_code, 404)
        self.assertEqual(self.client.post(self.base + '/route/', {'points': [self.point(0)]}, format='json').status_code, 404)

    def test_offline_finish_uses_original_time_and_replay_preserves_it(self):
        self.session.started_at = timezone.now() - timedelta(hours=3)
        self.session.save()
        ended_at = self.session.started_at + timedelta(minutes=30)
        response = self.client.post(self.base + '/end/', {'ended_at': ended_at.isoformat()}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.session.refresh_from_db()
        self.assertEqual(self.session.ended_at, ended_at)
        replay = self.client.post(self.base + '/end/', {'ended_at': timezone.now().isoformat()}, format='json')
        self.assertEqual(replay.data['ended_at'], response.data['ended_at'])

    def test_invalid_finish_does_not_complete_session(self):
        for value in [self.session.started_at - timedelta(seconds=1), timezone.now() + timedelta(days=1)]:
            response = self.client.post(self.base + '/end/', {'ended_at': value.isoformat()}, format='json')
            self.assertEqual(response.status_code, 400, response.data)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, 'active')


class CreateSaveRecoveryTests(RepbaseAPITestMixin, APITestCase):
    def setUp(self):
        _, self.profile, token = self.create_account('save-retry')
        self.authenticate(token)
        self.key = str(uuid.uuid4())
        self.path = '/api/v1/planner/'
        self.payload = {'title': 'Walk', 'scheduled_date': '2026-09-12', 'kind': 'task', 'category': 'other'}

    def save(self, payload=None, key=None):
        return self.client.post(self.path, self.payload if payload is None else payload,
                                format='json', HTTP_IDEMPOTENCY_KEY=key or self.key)

    def test_lost_response_replay_creates_only_once(self):
        first = self.save()
        self.assertEqual(first.status_code, 201, first.data)
        second = self.save(dict(reversed(list(self.payload.items()))))
        self.assertEqual(second.status_code, 201, second.data)
        self.assertEqual(first.data, second.data)
        self.assertEqual(PlannerEntry.objects.count(), 1)
        self.assertEqual(SaveReceipt.objects.count(), 1)

    def test_changed_payload_is_rejected(self):
        self.assertEqual(self.save().status_code, 201)
        self.assertEqual(self.save({**self.payload, 'title': 'Changed'}).status_code, 409)
        self.assertEqual(PlannerEntry.objects.count(), 1)

    def test_failed_validation_does_not_consume_key(self):
        self.assertEqual(self.save({**self.payload, 'title': ''}).status_code, 400)
        self.assertEqual(SaveReceipt.objects.count(), 0)
        self.assertEqual(self.save().status_code, 201)

    def test_receipt_write_failure_rolls_back_created_resource(self):
        with patch('core.save_recovery.SaveReceipt.objects.create', side_effect=RuntimeError('disk failure')):
            with self.assertRaises(RuntimeError):
                self.save()
        self.assertEqual(PlannerEntry.objects.count(), 0)
        self.assertEqual(SaveReceipt.objects.count(), 0)

    def test_same_key_is_isolated_by_account(self):
        first = self.save()
        _, _, token = self.create_account('another-save')
        self.authenticate(token)
        second = self.save()
        self.assertEqual(second.status_code, 201, second.data)
        self.assertNotEqual(first.data['id'], second.data['id'])

    def test_account_deletion_removes_receipts(self):
        self.assertEqual(self.save().status_code, 201)
        self.profile.delete()
        self.assertFalse(SaveReceipt.objects.exists())

    def test_food_and_recipe_replays(self):
        meal = FoodMeal.objects.create(owner=self.profile, date='2026-09-12', name='Meal 1')
        nutrition = {'name': 'Rice', 'servings': '1', 'calories': '100',
                     'protein_grams': '2', 'carbohydrate_grams': '20', 'fat_grams': '1'}
        for path, payload, model in [
            ('/api/v1/food/entries/', {**nutrition, 'meal': meal.pk}, FoodEntry),
            ('/api/v1/food/saved-meals/', {'name': 'Lunch', 'ingredients': [nutrition]}, SavedFoodMeal),
        ]:
            self.path = path
            first = self.save(payload)
            self.assertEqual(first.status_code, 201, first.data)
            second = self.save(payload)
            self.assertEqual(second.status_code, 201, second.data)
            self.assertEqual(first.data, second.data)
            self.assertEqual(model.objects.count(), 1)

    def workout_payload(self):
        exercise = Exercise.objects.create(name='Squat')
        return {'name': 'Strength', 'workout_type': 'lifting', 'initial_date': '2026-09-12',
                'initial_exercises': [{'exercise': exercise.pk, 'order': 1, 'target_sets': 3}]}

    def test_atomic_workout_and_schedule_replay(self):
        self.path = '/api/v1/workouts/'
        payload = self.workout_payload()
        first = self.save(payload)
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(self.save(payload).data, first.data)
        self.assertEqual(WorkoutTemplate.objects.count(), 1)
        self.assertEqual(WorkoutExercise.objects.count(), 1)
        self.assertEqual(WorkoutSchedule.objects.count(), 1)

    def test_schedule_failure_rolls_back_entire_workout(self):
        self.path = '/api/v1/workouts/'
        payload = self.workout_payload()
        with patch('core.serializers.WorkoutSchedule.objects.create', side_effect=RuntimeError('write failed')):
            with self.assertRaises(RuntimeError):
                self.save(payload)
        self.assertFalse(WorkoutTemplate.objects.exists())
        self.assertFalse(WorkoutExercise.objects.exists())
        self.assertFalse(SaveReceipt.objects.exists())

    def test_private_exercise_rejected_before_template_creation(self):
        self.path = '/api/v1/workouts/'
        payload = self.workout_payload()
        _, other, _ = self.create_account('private-catalog')
        Exercise.objects.filter(pk=payload['initial_exercises'][0]['exercise']).update(created_by=other)
        self.assertEqual(self.save(payload).status_code, 400)
        self.assertFalse(WorkoutTemplate.objects.exists())

    def test_atomic_edit_reorders_and_replays_without_duplicate_relations(self):
        self.path = '/api/v1/workouts/'
        payload = self.workout_payload()
        workout_id = self.save(payload).data['id']
        first = WorkoutExercise.objects.get(workout_id=workout_id)
        second = WorkoutExercise.objects.create(workout_id=workout_id, exercise=first.exercise, order=2)
        plan = {'name': 'Updated', 'exercise_plan': [
            {'relation_id': first.pk, 'exercise': first.exercise_id, 'order': 2, 'target_sets': 4},
            {'relation_id': second.pk, 'exercise': first.exercise_id, 'order': 1, 'target_sets': 2},
        ]}
        for _ in range(2):
            result = self.client.patch(f'{self.path}{workout_id}/', plan, format='json')
            self.assertEqual(result.status_code, 200, result.data)
        first.refresh_from_db()
        self.assertEqual(first.order, 2)
        self.assertEqual(first.target_sets, 4)
        self.assertEqual(WorkoutExercise.objects.count(), 2)

    def test_atomic_edit_failure_preserves_existing_plan(self):
        self.path = '/api/v1/workouts/'
        workout_id = self.save(self.workout_payload()).data['id']
        first = WorkoutExercise.objects.get(workout_id=workout_id)
        plan = {'name': 'Should not save', 'exercise_plan': [
            {'exercise': first.exercise_id, 'order': 2, 'target_sets': 4},
        ]}
        with patch('core.serializers.WorkoutExercise.objects.create', side_effect=RuntimeError('write failed')):
            with self.assertRaises(RuntimeError):
                self.client.patch(f'{self.path}{workout_id}/', plan, format='json')
        first.refresh_from_db()
        self.assertEqual(first.order, 1)
        self.assertEqual(WorkoutTemplate.objects.get(pk=workout_id).name, 'Strength')
