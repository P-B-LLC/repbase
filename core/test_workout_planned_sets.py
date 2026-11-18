"""Durable targets are plans, never logs; legacy writes cannot erase them."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
from threading import Barrier, Event, local
from unittest import skipUnless
from unittest.mock import patch
import uuid

from django.db import close_old_connections, connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase, APITransactionTestCase

from .fixtures_for_tests import FAST_PASSWORDS, make_account
from .models import Exercise, SessionExercise, SetEntry, WorkoutExercise, WorkoutSchedule, WorkoutSession, WorkoutTemplate


def targets():
    return [
        {"id": str(uuid.uuid4()), "set_type": "warmup", "target_reps": 10, "target_weight_kg": "20.00"},
        {"id": str(uuid.uuid4()), "set_type": "normal", "target_reps": 8, "target_weight_kg": "60.00"},
        {"id": str(uuid.uuid4()), "set_type": "drop", "target_reps": 12, "target_weight_kg": "45.00"},
    ]


@FAST_PASSWORDS
class PlannedSetTests(APITestCase):
    def setUp(self):
        _, self.owner, self.token = make_account("planned-owner")
        _, self.other, self.other_token = make_account("planned-other")
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")
        self.exercise = Exercise.objects.create(name="Squat")
        self.workout = WorkoutTemplate.objects.create(owner=self.owner, name="Legs")
        self.rows = targets()
        self.line = WorkoutExercise.objects.create(workout=self.workout, exercise=self.exercise, order=1,
            target_sets=3, target_reps=7, target_weight_kg=Decimal("50.00"),
            set_types=[row["set_type"] for row in self.rows], planned_sets=self.rows)

    def patch(self, data):
        return self.client.patch(f"/api/v1/workout-exercises/{self.line.pk}/", data, format="json")

    def plan(self, items, **extra):
        return self.client.patch(f"/api/v1/workouts/{self.workout.pk}/",
            {"exercise_plan": items, **extra}, format="json")

    def item(self, **extra):
        return {"relation_id": self.line.pk, "exercise": self.exercise.pk, "order": 1, **extra}

    def create_session(self, **headers):
        return self.client.post("/api/v1/sessions/", {"workout": self.workout.pk}, format="json", **headers)

    def test_capability_authenticated_and_present_even_without_templates(self):
        self.client.credentials()
        self.assertEqual(self.client.get("/api/v1/workouts/capabilities/").status_code, 401)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.other_token.key}")
        response = self.client.get("/api/v1/workouts/capabilities/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {"planned_sets_version": 1})
        self.assertFalse(WorkoutTemplate.objects.filter(owner=self.other).exists())

    def test_capability_does_not_scan_workouts(self):
        with CaptureQueriesContext(connection) as small:
            self.client.get("/api/v1/workouts/capabilities/")
        WorkoutTemplate.objects.bulk_create([WorkoutTemplate(owner=self.owner, name=f"Other {i}") for i in range(25)])
        with CaptureQueriesContext(connection) as large:
            self.client.get("/api/v1/workouts/capabilities/")
        self.assertEqual(len(small), len(large))
        self.assertFalse(any('core_workouttemplate' in q['sql'].lower() for q in large))

    def test_template_writes_lock_schedules_before_snapshot_parent(self):
        # Preserve the schedule/planner lock order used by schedule editing.
        # This also runs on SQLite, where SELECT FOR UPDATE itself is a no-op;
        # PostgreSQL concurrency coverage below tests the actual row locks.
        from . import views
        WorkoutSchedule.objects.create(owner=self.owner, workout=self.workout,
                                       scheduled_date=date(2030, 1, 1))
        real_schedules = views.lock_schedule_rows
        real_get = views.get_object_or_404
        for method in ("patch", "delete"):
            calls = []
            def schedules(rows):
                calls.append("schedules")
                return real_schedules(rows)
            def parent(queryset, *args, **kwargs):
                if getattr(queryset, "model", None) is WorkoutTemplate:
                    calls.append("template")
                return real_get(queryset, *args, **kwargs)
            with patch.object(views, "lock_schedule_rows", side_effect=schedules), \
                    patch.object(views, "get_object_or_404", side_effect=parent):
                if method == "patch":
                    response = self.client.patch(f"/api/v1/workouts/{self.workout.pk}/",
                                                 {"name": "Updated plan"}, format="json")
                    self.assertEqual(response.status_code, 200, response.data)
                else:
                    response = self.client.delete(f"/api/v1/workouts/{self.workout.pk}/")
                    self.assertEqual(response.status_code, 204, response.data)
            self.assertLess(calls.index("schedules"), calls.index("template"))

    def test_initial_create_and_read_roundtrip_authoritative_rows(self):
        response = self.client.post("/api/v1/workouts/", {
            "name": "New", "planned_sets_version": 1,
            "initial_exercises": [{"exercise": self.exercise.pk, "order": 1,
                "planned_sets": self.rows, "target_sets": 1, "set_types": []}],
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(response.data["supports_planned_sets"])
        line = response.data["exercises"][0]
        self.assertEqual(line["planned_sets"], self.rows)
        self.assertEqual(line["target_sets"], 3)
        self.assertEqual(line["set_types"], [r["set_type"] for r in self.rows])
        self.assertIsNone(line["target_reps"])
        self.assertIsNone(line["target_weight_kg"])
        fetched = self.client.get(f"/api/v1/workouts/{response.data['id']}/")
        self.assertEqual(fetched.data["exercises"][0]["planned_sets"], self.rows)

    def test_relation_create_targets_and_decimal_canonicalization(self):
        rows = [{"id": str(uuid.uuid4()), "set_type": "failure", "target_weight_kg": "1.5"}]
        response = self.client.post("/api/v1/workout-exercises/", {
            "workout": self.workout.pk, "exercise": self.exercise.pk, "order": 2, "planned_sets": rows,
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["planned_sets"][0]["target_weight_kg"], "1.50")
        self.assertIsNone(response.data["planned_sets"][0]["target_reps"])

    def test_bounded_shape_values_and_unique_ids(self):
        invalid = [None, {}, "x", [1], self.rows * 17, [self.rows[0], self.rows[0]]]
        for field, values in {
            "id": [None, "bad", 10, True],
            "set_type": [None, "bad", True],
            "target_reps": [0, -1, 1001, 2.5, True, "8"],
            "target_weight_kg": ["NaN", "Infinity", "-1", "100000", "1.234", 15, True, "12junk"],
        }.items():
            invalid.extend([{**self.rows[0], field: value}] for value in values)
        invalid += [[{**self.rows[0], "actual_reps": 4}], [{"set_type": "normal"}], [{"id": str(uuid.uuid4())}]]
        for rows in invalid:
            with self.subTest(rows=rows):
                response = self.patch({"planned_sets": rows})
                self.assertEqual(response.status_code, 400, response.data)
        self.line.refresh_from_db()
        self.assertEqual(self.line.planned_sets, self.rows)

    def test_fifty_rows_zero_weight_and_null_targets_are_valid(self):
        rows = [{"id": str(uuid.uuid4()), "set_type": "normal", "target_reps": None,
                 "target_weight_kg": "0.00"} for _ in range(50)]
        response = self.patch({"planned_sets": rows})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["target_sets"], 50)

    def test_row_edit_reorder_and_removal_persist_stable_ids(self):
        changed = [deepcopy(self.rows[2]), deepcopy(self.rows[0])]
        changed[1]["target_reps"] = 5
        response = self.patch({"planned_sets": changed})
        self.assertEqual(response.status_code, 200, response.data)
        self.line.refresh_from_db()
        self.assertEqual(self.line.planned_sets, changed)
        self.assertEqual(self.line.target_sets, 2)
        self.assertEqual(self.line.set_types, ["drop", "warmup"])

    def test_legacy_neutral_write_keeps_targets(self):
        response = self.patch({"notes": "Keep", "target_sets": 3, "target_reps": 7,
                               "target_weight_kg": "50.00", "set_types": self.line.set_types})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["planned_sets"], self.rows)

    def test_legacy_conflicting_writes_refused_direct_and_atomic(self):
        for change in ({"target_sets": 2}, {"set_types": []}, {"target_reps": 6}, {"target_weight_kg": "70.00"}):
            with self.subTest(change=change):
                self.assertEqual(self.patch(change).status_code, 400)
                self.assertEqual(self.plan([self.item(**change)]).status_code, 400)
        self.line.refresh_from_db()
        self.assertEqual(self.line.planned_sets, self.rows)

    def test_legacy_aggregate_rows_keep_previous_compatibility(self):
        self.line.planned_sets = []
        self.line.save(update_fields=["planned_sets"])
        response = self.patch({"target_sets": 2})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["planned_sets"], [])
        self.assertEqual(response.data["set_types"], ["warmup", "normal"])
        self.assertEqual(response.data["target_reps"], 7)

    def test_explicit_empty_reverts_to_legacy_without_erasing_aggregate_targets(self):
        response = self.patch({"planned_sets": []})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["target_sets"], 3)
        self.assertEqual(response.data["target_reps"], 7)
        self.assertEqual(response.data["target_weight_kg"], "50.00")

    def test_atomic_plan_reorder_preserves_duplicate_catalog_relation_identity(self):
        other_rows = targets()
        second = WorkoutExercise.objects.create(workout=self.workout, exercise=self.exercise, order=2,
            planned_sets=other_rows, target_sets=3)
        response = self.plan([
            {"relation_id": second.pk, "exercise": self.exercise.pk, "order": 1}, self.item(order=2),
        ], planned_sets_version=1)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([r["id"] for r in response.data["exercises"]], [second.pk, self.line.pk])
        self.assertEqual([r["planned_sets"] for r in response.data["exercises"]], [other_rows, self.rows])
        # Replaying the same complete plan must not duplicate relations or sets.
        again = self.plan([
            {"relation_id": second.pk, "exercise": self.exercise.pk, "order": 1}, self.item(order=2),
        ], planned_sets_version=1)
        self.assertEqual(again.status_code, 200, again.data)
        self.assertEqual(self.workout.workout_exercises.count(), 2)

    def test_removing_explicit_plan_requires_capability_marker(self):
        response = self.plan([])
        self.assertEqual(response.status_code, 400, response.data)
        self.assertTrue(WorkoutExercise.objects.filter(pk=self.line.pk).exists())
        self.assertEqual(self.plan([], planned_sets_version=1).status_code, 200)
        self.assertFalse(self.workout.workout_exercises.exists())

    def test_sparse_maximum_integer_order_replacement_uses_safe_spare_positions(self):
        self.line.order = 2147483647; self.line.save(update_fields=["order"])
        response = self.plan([self.item(order=1)], planned_sets_version=1)
        self.assertEqual(response.status_code, 200, response.data)
        self.line.refresh_from_db()
        self.assertEqual(self.line.order, 1)
        self.assertEqual(self.line.planned_sets, self.rows)

    def test_failure_after_prior_plan_changes_rolls_back_everything(self):
        second = WorkoutExercise.objects.create(workout=self.workout, exercise=self.exercise, order=2,
            planned_sets=targets(), target_sets=3)
        changed = deepcopy(self.rows)
        changed[0]["target_reps"] = 25
        response = self.plan([self.item(order=2, planned_sets=changed)])  # would delete second without marker
        self.assertEqual(response.status_code, 400, response.data)
        self.line.refresh_from_db(); second.refresh_from_db()
        self.assertEqual(self.line.planned_sets, self.rows)
        self.assertEqual([self.line.order, second.order], [1, 2])

    def test_foreign_relation_or_exercise_refused_without_partial_writes(self):
        foreign_workout = WorkoutTemplate.objects.create(owner=self.other, name="Private")
        foreign = WorkoutExercise.objects.create(workout=foreign_workout, exercise=self.exercise)
        secret = Exercise.objects.create(name="Private movement", created_by=self.other)
        for payload in ([self.item(relation_id=foreign.pk)], [self.item(exercise=secret.pk)]):
            self.assertEqual(self.plan(payload, planned_sets_version=1).status_code, 400)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.other_token.key}")
        self.assertEqual(self.patch({"planned_sets": []}).status_code, 404)
        self.assertEqual(self.create_session().status_code, 400)
        self.line.refresh_from_db()
        self.assertEqual(self.line.planned_sets, self.rows)

    def test_snapshot_count_kinds_targets_source_and_rest_without_logs(self):
        self.line.rest_seconds = 90; self.line.superset_group = 2
        self.line.save()
        response = self.create_session()
        self.assertEqual(response.status_code, 201, response.data)
        snap = SessionExercise.objects.get(session_id=response.data["id"])
        self.assertEqual(snap.planned_sets, self.rows)
        self.assertEqual(snap.workout_exercise_id, self.line.pk)
        self.assertEqual((snap.rest_seconds, snap.superset_group), (90, 2))
        self.assertFalse(SetEntry.objects.exists())
        read = self.client.get(f"/api/v1/session-exercises/?session={response.data['id']}")
        self.assertEqual(read.data["results"][0]["planned_sets"], self.rows)

    def test_snapshot_is_immutable_after_template_edit_delete_and_start_retry(self):
        created = self.create_session()
        self.assertEqual(created.status_code, 201, created.data)
        session_id = created.data["id"]
        snap = SessionExercise.objects.get(session_id=session_id)
        source_id = self.line.pk
        self.patch({"planned_sets": list(reversed(self.rows))})
        for _ in range(2):
            started = self.client.post(f"/api/v1/sessions/{session_id}/start/", {}, format="json")
            self.assertEqual(started.status_code, 200, started.data)
        self.assertEqual(self.client.delete(f"/api/v1/workouts/{self.workout.pk}/").status_code, 204)
        snap.refresh_from_db()
        self.assertEqual(snap.planned_sets, self.rows)
        self.assertEqual(snap.workout_exercise_id, source_id)
        self.assertFalse(SetEntry.objects.exists())

    def test_legacy_snapshot_freezes_aggregate_targets_except_warmups(self):
        self.line.planned_sets = []
        self.line.save()
        response = self.create_session()
        self.assertEqual(response.status_code, 201, response.data)
        rows = SessionExercise.objects.get(session_id=response.data["id"]).planned_sets
        self.assertEqual(len(rows), 3)
        self.assertIsNone(rows[0]["target_reps"])
        self.assertIsNone(rows[0]["target_weight_kg"])
        self.assertEqual(rows[1]["target_reps"], 7)
        self.assertEqual(rows[1]["target_weight_kg"], "50.00")
        self.assertEqual(len({row["id"] for row in rows}), 3)

    def test_legacy_zero_reps_and_zero_sets_snapshot_without_invented_targets(self):
        self.line.planned_sets = []; self.line.target_reps = 0
        self.line.save()
        response = self.create_session()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(all(row["target_reps"] is None for row in SessionExercise.objects.get(session_id=response.data["id"]).planned_sets))
        self.line.target_sets = 0; self.line.set_types = []; self.line.save()
        second = self.create_session()
        self.assertEqual(SessionExercise.objects.get(session_id=second.data["id"]).planned_sets, [])

    def test_historical_snapshot_is_null_and_manual_added_is_empty(self):
        session = WorkoutSession.objects.create(repbase_user=self.owner)
        old = SessionExercise.objects.create(session=session, exercise=self.exercise)
        read = self.client.get(f"/api/v1/session-exercises/{old.pk}/")
        self.assertIsNone(read.data["planned_sets"])
        self.assertIsNone(read.data["workout_exercise_id"])
        added = self.client.post("/api/v1/session-exercises/", {
            "session": session.pk, "exercise": self.exercise.pk, "order": 2,
            "planned_sets": self.rows, "workout_exercise_id": self.line.pk,
        }, format="json")
        self.assertEqual(added.status_code, 201, added.data)
        self.assertEqual(added.data["planned_sets"], [])
        self.assertIsNone(added.data["workout_exercise_id"])

    def test_snapshot_fields_are_read_only_and_reparenting_refused(self):
        created = self.create_session()
        snap = SessionExercise.objects.get(session_id=created.data["id"])
        response = self.client.patch(f"/api/v1/session-exercises/{snap.pk}/", {
            "planned_sets": [], "workout_exercise_id": 999, "rest_seconds": 500,
        }, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["planned_sets"], self.rows)
        other = Exercise.objects.create(name="Press")
        self.assertEqual(self.client.patch(f"/api/v1/session-exercises/{snap.pk}/", {"exercise": other.pk}, format="json").status_code, 400)

    def test_idempotent_template_and_session_create_do_not_duplicate_snapshots(self):
        key = str(uuid.uuid4())
        first = self.create_session(HTTP_IDEMPOTENCY_KEY=key)
        second = self.create_session(HTTP_IDEMPOTENCY_KEY=key)
        self.assertEqual((first.status_code, second.status_code), (201, 201))
        self.assertEqual(first.data["id"], second.data["id"])
        self.assertEqual(SessionExercise.objects.filter(session_id=first.data["id"]).count(), 1)
        self.assertEqual(SessionExercise.objects.get(session_id=first.data["id"]).planned_sets, self.rows)
        key = str(uuid.uuid4())
        payload = {"name": "Retry plan", "initial_exercises": [{"exercise": self.exercise.pk, "order": 1, "planned_sets": self.rows}]}
        responses = [self.client.post("/api/v1/workouts/", payload, format="json", HTTP_IDEMPOTENCY_KEY=key) for _ in range(2)]
        self.assertEqual([r.status_code for r in responses], [201, 201])
        self.assertEqual(responses[0].data["id"], responses[1].data["id"])

    def test_session_snapshot_duplicate_movements_keep_distinct_relation_ids(self):
        other_rows = targets()
        second = WorkoutExercise.objects.create(workout=self.workout, exercise=self.exercise, order=2,
            target_sets=3, planned_sets=other_rows)
        response = self.create_session()
        snaps = list(SessionExercise.objects.filter(session_id=response.data["id"]))
        self.assertEqual([s.workout_exercise_id for s in snaps], [self.line.pk, second.pk])
        self.assertEqual([s.planned_sets for s in snaps], [self.rows, other_rows])

    def test_another_user_cannot_read_snapshot_or_change_plan_source(self):
        created = self.create_session()
        snap = SessionExercise.objects.get(session_id=created.data["id"])
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.other_token.key}")
        self.assertEqual(self.client.get(f"/api/v1/session-exercises/{snap.pk}/").status_code, 404)
        self.assertEqual(self.client.patch(f"/api/v1/session-exercises/{snap.pk}/", {"planned_sets": []}, format="json").status_code, 404)

    def test_excessive_legacy_set_count_refuses_start_without_partial_session(self):
        self.line.planned_sets = []; self.line.target_sets = 100000; self.line.save()
        response = self.create_session()
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(WorkoutSession.objects.exists())

    def test_schema_keeps_new_responses_optional_and_snapshots_read_only(self):
        from drf_spectacular.generators import SchemaGenerator
        schema = SchemaGenerator().get_schema(request=None, public=True)
        schemas = schema["components"]["schemas"]
        for component, fields in {
            "WorkoutTemplate": ["supports_planned_sets"],
            "WorkoutExercise": ["planned_sets"],
            "SessionExercise": ["planned_sets", "workout_exercise_id", "rest_seconds", "superset_group"],
        }.items():
            for field in fields:
                self.assertIn(field, schemas[component]["properties"])
                self.assertNotIn(field, schemas[component].get("required", []))
        self.assertTrue(schemas["SessionExercise"]["properties"]["planned_sets"]["nullable"])
        self.assertNotIn("planned_sets", schemas["SessionExerciseRequest"]["properties"])
        self.assertEqual(schemas["WorkoutPlannedSetRequest"]["required"], ["id", "set_type"])
        self.assertEqual(schema["paths"]["/api/v1/workouts/capabilities/"]["get"]["operationId"], "workouts_capabilities_retrieve")


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locks")
@FAST_PASSWORDS
class PlannedSetConcurrencyTests(APITransactionTestCase):
    def setUp(self):
        _, self.owner, self.token = make_account("planned-concurrent")
        self.workout = WorkoutTemplate.objects.create(owner=self.owner, name="Plan")
        self.exercise = Exercise.objects.create(name="Squat")
        self.old = targets(); self.new = targets()
        self.line = WorkoutExercise.objects.create(workout=self.workout, exercise=self.exercise,
            planned_sets=self.old, target_sets=3)

    def race(self, actions):
        barrier = Barrier(len(actions))
        def run(action):
            close_old_connections()
            try:
                # A lock regression must fail CI, not leave its worker hung.
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                    cursor.execute("SET statement_timeout = '10s'")
                client = APIClient()
                client.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")
                barrier.wait(timeout=10)
                return action(client)
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=len(actions)) as pool:
            futures = [pool.submit(run, action) for action in actions]
            return [future.result(timeout=20) for future in futures]

    def test_direct_edit_and_session_snapshot_are_atomic(self):
        results = self.race([
            lambda c: c.patch(f"/api/v1/workout-exercises/{self.line.pk}/", {"planned_sets": self.new}, format="json"),
            lambda c: c.post("/api/v1/sessions/", {"workout": self.workout.pk}, format="json"),
        ])
        self.assertEqual([r.status_code for r in results], [200, 201])
        snap = SessionExercise.objects.get(session_id=results[1].data["id"])
        self.assertIn(snap.planned_sets, [self.old, self.new])

    def test_competing_atomic_plan_updates_do_not_interleave(self):
        def edit(rows):
            return lambda c: c.patch(f"/api/v1/workouts/{self.workout.pk}/", {
                "planned_sets_version": 1, "exercise_plan": [{"relation_id": self.line.pk,
                "exercise": self.exercise.pk, "order": 1, "planned_sets": rows}],
            }, format="json")
        responses = self.race([edit(self.old), edit(self.new)])
        self.assertEqual([r.status_code for r in responses], [200, 200])
        self.line.refresh_from_db()
        self.assertIn(self.line.planned_sets, [self.old, self.new])
        self.assertEqual(self.line.target_sets, 3)

    def test_template_edit_waits_for_schedule_without_holding_parent(self):
        from . import views
        schedule = WorkoutSchedule.objects.create(owner=self.owner, workout=self.workout,
            scheduled_date=timezone.localdate())
        schedule_locked = Event()
        template_waiting = Event()
        role = local()
        real_lock = views.lock_schedule_rows

        def coordinated_lock(rows):
            if role.name == "schedule":
                ids = real_lock(rows)
                schedule_locked.set()
                self.assertTrue(template_waiting.wait(timeout=10))
                # A schedule owns the earlier locks in the shared order. A
                # concurrent template writer must not already own the parent
                # while waiting for this schedule. NOWAIT makes inversion a
                # deterministic failure rather than relying on race timing.
                WorkoutTemplate.objects.select_for_update(nowait=True).get(pk=self.workout.pk)
                return ids
            template_waiting.set()
            return real_lock(rows)

        def schedule_edit(client):
            role.name = "schedule"
            return client.patch(f"/api/v1/schedules/{schedule.pk}/", {
                "scheduled_date": (timezone.localdate() + timedelta(days=1)).isoformat(),
            }, format="json")

        def template_edit(client):
            role.name = "template"
            self.assertTrue(schedule_locked.wait(timeout=10))
            return client.patch(f"/api/v1/workouts/{self.workout.pk}/",
                                {"name": "Schedule-safe plan"}, format="json")

        with patch.object(views, "lock_schedule_rows", side_effect=coordinated_lock):
            responses = self.race([schedule_edit, template_edit])
        self.assertEqual([response.status_code for response in responses], [200, 200])
        self.line.refresh_from_db()
        schedule.refresh_from_db()
        self.assertEqual(self.line.planned_sets, self.old)
        self.assertEqual(schedule.scheduled_date, timezone.localdate() + timedelta(days=1))
