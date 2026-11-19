"""The capability marker a client sends before replacing per-set targets.

Only version 1 exists. Anything else must refuse the change and leave the
plan exactly as it was, because a client that sends an unknown version is
one that has not understood what it is about to overwrite.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from .fixtures_for_tests import FAST_PASSWORDS, make_account
from .models import Exercise, WorkoutExercise, WorkoutTemplate
from .test_workout_planned_sets import targets


@FAST_PASSWORDS
class PlannedSetVersionTests(APITestCase):
    def setUp(self):
        _, self.owner, self.token = make_account("planned-version")
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")
        self.exercise = Exercise.objects.create(name="Bench")
        self.workout = WorkoutTemplate.objects.create(owner=self.owner, name="Push")
        self.rows = targets()
        self.line = WorkoutExercise.objects.create(workout=self.workout, exercise=self.exercise, order=1,
            target_sets=3, target_reps=8, target_weight_kg=Decimal("60.00"),
            set_types=[row["set_type"] for row in self.rows], planned_sets=self.rows)

    def remove_plan(self, version):
        body = {"exercise_plan": []}
        if version is not ...:
            body["planned_sets_version"] = version
        return self.client.patch(f"/api/v1/workouts/{self.workout.pk}/", body, format="json")

    def assert_plan_untouched(self):
        self.line.refresh_from_db()
        self.assertEqual(self.line.planned_sets, self.rows)

    def test_versions_other_than_one_are_refused_and_change_nothing(self):
        for version in (0, 2, -1, 10 ** 6, None, "two", "", 1.5, True):
            with self.subTest(version=version):
                response = self.remove_plan(version)
                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn("planned_sets_version", response.data)
                self.assert_plan_untouched()

    def test_omitting_the_version_refuses_removing_explicit_targets(self):
        response = self.remove_plan(...)
        self.assertEqual(response.status_code, 400)
        self.assert_plan_untouched()

    def test_version_one_as_a_number_or_numeric_text_is_accepted(self):
        # JSON clients send a number; DRF's integer field also reads "1".
        self.assertEqual(self.remove_plan("1").status_code, 200)
        self.assertFalse(WorkoutExercise.objects.filter(pk=self.line.pk).exists())

    def test_the_version_is_never_echoed_back_on_the_template(self):
        response = self.client.get(f"/api/v1/workouts/{self.workout.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("planned_sets_version", response.data)

    def test_the_capability_reports_the_one_supported_version(self):
        response = self.client.get("/api/v1/workouts/capabilities/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {"planned_sets_version": 1})
