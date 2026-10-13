from datetime import timedelta
from django.test import override_settings
from rest_framework.test import APITestCase
from .tests import RepbaseAPITestMixin
from .models import PlannerEntry, PlannerRecurrence, materialize_planner_repeats, today_for


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class PlannerWeekdayTests(RepbaseAPITestMixin, APITestCase):
    def setUp(self):
        self.user, self.profile, self.token = self.create_account('weekday-member')
        self.authenticate(self.token)
        self.start = today_for(self.profile)

    def create(self, **extra):
        body = dict(kind='task', title='Practice', category='habit', scheduled_date=str(self.start))
        body.update(extra)
        return self.client.post('/api/v1/planner/', body, format='json')

    def test_requested_weekdays_only_including_week_boundary(self):
        for selected in ([0, 4], [6, 0, 2], [2], list(range(7))):
            with self.subTest(selected=selected):
                result = self.create(repeat_on_weekdays=selected)
                self.assertEqual(result.status_code, 201, result.data)
                entry = PlannerEntry.objects.get(pk=result.data['id'])
                self.assertEqual(result.data['repeat_weekdays'], sorted(selected))
                self.assertIsNone(result.data['repeat_interval_days'])
                end = self.start + timedelta(days=20)
                materialize_planner_repeats(self.profile, end)
                expected = [self.start + timedelta(days=n) for n in range(21)
                            if (self.start + timedelta(days=n)).weekday() in selected]
                actual = list(PlannerEntry.objects.filter(recurrence=entry.recurrence,
                              scheduled_date__lte=end).order_by('scheduled_date')
                              .values_list('scheduled_date', flat=True))
                self.assertEqual(actual, expected)

    def test_deduplicates_and_never_creates_unselected_start_day(self):
        selected = (self.start.weekday() + 2) % 7
        result = self.create(repeat_on_weekdays=[selected, selected])
        self.assertEqual(result.status_code, 201, result.data)
        self.assertEqual(result.data['scheduled_date'], str(self.start + timedelta(days=2)))
        self.assertEqual(result.data['repeat_weekdays'], [selected])

    def test_rejects_invalid_or_conflicting_configuration(self):
        for extra in [dict(repeat_on_weekdays=[]), dict(repeat_on_weekdays=[-1]),
                      dict(repeat_on_weekdays=[7]), dict(repeat_on_weekdays=[1.5]),
                      dict(repeat_on_weekdays=[0], scheduled_date='9999-12-31'),
                      dict(repeat_on_weekdays=[0], repeat_every_days=2),
                      dict(repeat_on_weekdays=[0], kind='event', category='appointment')]:
            with self.subTest(extra=extra):
                self.assertEqual(self.create(**extra).status_code, 400)
        self.assertEqual(PlannerRecurrence.objects.count(), 0)

    def test_end_is_exclusive_and_must_follow_first_occurrence(self):
        selected = (self.start.weekday() + 2) % 7
        self.assertEqual(self.create(repeat_on_weekdays=[selected],
                         repeat_ends_on=str(self.start + timedelta(days=1))).status_code, 400)
        result = self.create(repeat_on_weekdays=[selected],
                             repeat_ends_on=str(self.start + timedelta(days=9)))
        self.assertEqual(result.status_code, 201, result.data)
        self.assertEqual(PlannerEntry.objects.filter(recurrence__isnull=False).count(), 1)

    def test_materializing_twice_does_not_duplicate_or_restore_deleted_day(self):
        result = self.create(repeat_on_weekdays=[0, 4])
        entry = PlannerEntry.objects.get(pk=result.data['id'])
        first_id = entry.id
        entry.delete()
        before = PlannerEntry.objects.count()
        for _ in range(2):
            materialize_planner_repeats(self.profile, self.start + timedelta(days=15))
        self.assertEqual(PlannerEntry.objects.count(), before)
        self.assertFalse(PlannerEntry.objects.filter(pk=first_id).exists())

    def test_existing_interval_repeats_keep_their_semantics(self):
        result = self.create(repeat_every_days=2)
        self.assertEqual(result.status_code, 201, result.data)
        self.assertEqual(result.data['repeat_weekdays'], [])
        self.assertEqual(result.data['repeat_interval_days'], 2)

    def test_cannot_silently_change_repeat_using_patch(self):
        result = self.create(repeat_on_weekdays=[0, 4])
        response = self.client.patch(f"/api/v1/planner/{result.data['id']}/",
                                     {'repeat_on_weekdays': [2]}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_weekday_steps_follow_shifted_first_date_and_do_not_share_completion(self):
        result = self.create(repeat_on_weekdays=[0, 4])
        first = PlannerEntry.objects.get(pk=result.data['id'])
        response = self.client.post('/api/v1/planner/', dict(kind='task', title='Warm up',
                                    category='habit', scheduled_date=str(self.start), parent=first.pk), format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['scheduled_date'], str(first.scheduled_date))
        self.client.patch(f"/api/v1/planner/{response.data['id']}/", {'is_complete': True}, format='json')
        later = PlannerEntry.objects.filter(recurrence=first.recurrence,
                    scheduled_date__gt=first.scheduled_date).order_by('scheduled_date').first()
        self.assertIsNotNone(later)
        self.assertEqual(later.subtasks.count(), 1)
        self.assertIsNone(later.subtasks.get().completed_at)
