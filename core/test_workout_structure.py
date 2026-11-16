"""Set types, planned set structure, session reordering, and the small reads
added beside them: "follows you", a workout's last performed date and
exercise discovery.

Every field here was added to a response the app already decodes, so the last
class checks the contract keeps each one optional: a newer app must still be
able to read an older server, which does not send them.
"""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import AnonymousUser
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIRequestFactory, APITestCase

from .fixtures_for_tests import FAST_PASSWORDS, make_account
from .models import (
    Exercise,
    Follow,
    ProfileHighlight,
    SessionExercise,
    SetEntry,
    SetType,
    WorkoutExercise,
    WorkoutSession,
    WorkoutTemplate,
    best_set_for,
)
from .serializers import PublicRepbaseUserSerializer


def session_with_sets(owner, exercise, sets, *, ended_ago=timedelta(0), workout=None,
                      status=WorkoutSession.Status.COMPLETED, order=1):
    """A session of one exercise, its sets given as (weight, reps, set_type).

    Completion times sit just before the session's end, so a later session's
    record check sees them as history.
    """
    ended = timezone.now() - ended_ago
    session = WorkoutSession.objects.create(
        repbase_user=owner,
        workout=workout,
        status=status,
        started_at=ended - timedelta(hours=1),
        ended_at=ended if status == WorkoutSession.Status.COMPLETED else None,
    )
    line = SessionExercise.objects.create(session=session, exercise=exercise, order=order)
    for number, (weight, reps, set_type) in enumerate(sets, start=1):
        SetEntry.objects.create(
            session_exercise=line,
            set_number=number,
            weight_kg=None if weight is None else Decimal(weight),
            reps=reps,
            set_type=set_type,
            completed_at=ended - timedelta(minutes=10) + timedelta(seconds=number),
        )
    return session


class Authenticated(APITestCase):
    def as_user(self, token):
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")


# --------------------------------------------------------------- set types


@FAST_PASSWORDS
class SetTypeTests(Authenticated):
    @classmethod
    def setUpTestData(cls):
        _, cls.profile, cls.token = make_account("set-type-owner")
        cls.exercise = Exercise.objects.create(name="Bench Press")
        cls.session = WorkoutSession.objects.create(
            repbase_user=cls.profile,
            status=WorkoutSession.Status.ACTIVE,
            started_at=timezone.now(),
        )
        cls.line = SessionExercise.objects.create(
            session=cls.session, exercise=cls.exercise, order=1
        )

    def setUp(self):
        self.as_user(self.token)

    def log(self, **extra):
        body = {"session_exercise": self.line.pk, "set_number": 1, "weight_kg": "60.00", "reps": 8}
        body.update(extra)
        return self.client.post("/api/v1/set-entries/", body, format="json")

    def test_a_set_that_does_not_say_is_normal(self):
        # What every client before this field sends.
        response = self.log()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["set_type"], "normal")
        self.assertEqual(SetEntry.objects.get().set_type, SetType.NORMAL)

    def test_the_type_is_written_on_create_and_changed_by_patch(self):
        response = self.log(set_type="warmup")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["set_type"], "warmup")
        url = f"/api/v1/set-entries/{response.data['id']}/"

        changed = self.client.patch(url, {"set_type": "drop"}, format="json")
        self.assertEqual(changed.status_code, 200, changed.data)
        self.assertEqual(changed.data["set_type"], "drop")

        # A PATCH that does not mention it leaves it alone.
        other = self.client.patch(url, {"reps": 6}, format="json")
        self.assertEqual(other.status_code, 200, other.data)
        self.assertEqual(other.data["set_type"], "drop")
        self.assertEqual(SetEntry.objects.get().set_type, SetType.DROP)

        listed = self.client.get(f"/api/v1/set-entries/?session={self.session.pk}")
        self.assertEqual(listed.data["results"][0]["set_type"], "drop")

    def test_every_type_is_accepted(self):
        for number, value in enumerate(("normal", "warmup", "drop", "failure"), start=1):
            with self.subTest(value=value):
                response = self.log(set_number=number, set_type=value)
                self.assertEqual(response.status_code, 201, response.data)
                self.assertEqual(response.data["set_type"], value)

    def test_an_unknown_type_is_refused(self):
        for value in ("superset", "WARMUP", "", None):
            with self.subTest(value=value):
                response = self.log(set_type=value)
                self.assertEqual(response.status_code, 400)
                self.assertIn("set_type", response.data)
        self.assertFalse(SetEntry.objects.exists())

        saved = self.log().data
        response = self.client.patch(
            f"/api/v1/set-entries/{saved['id']}/", {"set_type": "rest"}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(SetEntry.objects.get().set_type, SetType.NORMAL)


# ----------------------------------------------- warm-ups are not performance


@FAST_PASSWORDS
class WarmupsAreNotPerformanceTests(Authenticated):
    @classmethod
    def setUpTestData(cls):
        _, cls.profile, cls.token = make_account("warmup-lifter")
        cls.bench = Exercise.objects.create(name="Bench Press")
        # Before: a working 100 x 5, and a heavy single labelled a warm-up.
        cls.before = session_with_sets(
            cls.profile,
            cls.bench,
            [("100.00", 5, SetType.NORMAL), ("140.00", 1, SetType.WARMUP)],
            ended_ago=timedelta(days=2),
        )
        # Now: a heavier warm-up still, then a working 110 x 5.
        cls.now = session_with_sets(
            cls.profile,
            cls.bench,
            [("150.00", 1, SetType.WARMUP), ("110.00", 5, SetType.NORMAL)],
        )

    def setUp(self):
        self.as_user(self.token)

    def records(self, session):
        response = self.client.get(f"/api/v1/sessions/{session.pk}/records/")
        self.assertEqual(response.status_code, 200, response.data)
        return {row["kind"]: row for row in response.data}

    def test_records_ignore_warmups_on_both_sides(self):
        records = self.records(self.now)
        # Not 150 (this session's warm-up) and not measured against 140 (the
        # old warm-up): 110 beats the 100 that was the real best.
        self.assertEqual(records["heaviest_weight"]["value"], 110.0)
        self.assertEqual(records["heaviest_weight"]["previous_value"], 100.0)
        # 110 x 5 estimates 128.3, 100 x 5 estimated 116.7. Counting the old
        # warm-up single (140 x 1 -> 144.7) would have hidden this record.
        self.assertEqual(records["best_estimated_1rm"]["value"], 128.3)
        self.assertEqual(records["best_estimated_1rm"]["previous_value"], 116.7)

    def test_a_session_of_only_warmups_sets_no_record(self):
        light = session_with_sets(
            self.profile, self.bench, [("200.00", 1, SetType.WARMUP)],
            ended_ago=timedelta(hours=-1),
        )
        self.assertEqual(light.personal_records(), [])

    def test_drop_and_failure_sets_count(self):
        for set_type, weight in ((SetType.FAILURE, "120.00"), (SetType.DROP, "125.00")):
            with self.subTest(set_type=set_type):
                session = session_with_sets(
                    self.profile, self.bench, [(weight, 3, set_type)],
                    ended_ago=timedelta(hours=-1),
                )
                records = self.records(session)
                self.assertEqual(records["heaviest_weight"]["value"], float(weight))
                session.delete()

    def test_progress_leaves_warmups_off_the_chart(self):
        session_with_sets(
            self.profile, self.bench,
            [("90.00", 4, SetType.DROP), ("105.00", 2, SetType.FAILURE)],
            ended_ago=timedelta(days=1),
        )
        response = self.client.get(f"/api/v1/progress/exercises/{self.bench.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        points = sorted((Decimal(p["weight_kg"]), p["reps"]) for p in response.data)
        self.assertEqual(
            points,
            [
                (Decimal("90.00"), 4),
                (Decimal("100.00"), 5),
                (Decimal("105.00"), 2),
                (Decimal("110.00"), 5),
            ],
        )

    def test_the_profile_highlight_is_never_a_warmup(self):
        best = best_set_for(self.profile, "bench")
        self.assertEqual(best.weight_kg, Decimal("110.00"))

        ProfileHighlight.objects.create(owner=self.profile, lift="bench")
        response = self.client.get(f"/api/v1/users/{self.profile.pk}/highlights/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data[0]["source"], "logged")
        self.assertEqual(Decimal(response.data[0]["weight_kg"]), Decimal("110.00"))

    def test_a_warmup_still_counts_as_a_logged_set(self):
        response = self.client.get(f"/api/v1/sessions/{self.now.pk}/")
        self.assertEqual(response.data["logged_set_count"], 2)

    def test_previous_sets_say_what_kind_of_set_each_was(self):
        response = self.client.get("/api/v1/sessions/previous-sets/")
        self.assertEqual(response.status_code, 200, response.data)
        rows = {(row["exercise"], row["set_number"]): row for row in response.data}
        # The most recent session supplies both set numbers, warm-up included:
        # this read shows what was done last time, not what counted.
        self.assertEqual(rows[(self.bench.pk, 1)]["set_type"], "warmup")
        self.assertEqual(Decimal(rows[(self.bench.pk, 1)]["weight_kg"]), Decimal("150.00"))
        self.assertEqual(rows[(self.bench.pk, 2)]["set_type"], "normal")
        for row in response.data:
            self.assertIn(row["set_type"], {"normal", "warmup", "drop", "failure"})


# ------------------------------------------------------ planned set structure


@FAST_PASSWORDS
class PlannedSetStructureTests(Authenticated):
    @classmethod
    def setUpTestData(cls):
        _, cls.profile, cls.token = make_account("plan-owner")
        cls.squat = Exercise.objects.create(name="Squat")
        cls.row = Exercise.objects.create(name="Row")

    def setUp(self):
        self.as_user(self.token)

    def create(self, exercises, name="Strength"):
        return self.client.post(
            "/api/v1/workouts/",
            {"name": name, "workout_type": "lifting", "initial_exercises": exercises},
            format="json",
        )

    def plan(self, workout_id, items):
        return self.client.patch(
            f"/api/v1/workouts/{workout_id}/", {"exercise_plan": items}, format="json"
        )

    def test_initial_exercises_carry_rest_supersets_and_set_types(self):
        response = self.create([
            {"exercise": self.squat.pk, "order": 1, "target_sets": 4, "target_reps": 5,
             "rest_seconds": 180, "superset_group": None, "set_types": ["warmup", "normal", "normal", "failure"]},
            {"exercise": self.row.pk, "order": 2, "target_sets": 3,
             "rest_seconds": 0, "superset_group": 1, "set_types": ["drop"]},
        ])
        self.assertEqual(response.status_code, 201, response.data)
        first, second = response.data["exercises"]
        self.assertEqual(first["rest_seconds"], 180)
        self.assertIsNone(first["superset_group"])
        self.assertEqual(first["set_types"], ["warmup", "normal", "normal", "failure"])
        self.assertEqual(second["rest_seconds"], 0)
        self.assertEqual(second["superset_group"], 1)
        self.assertEqual(second["set_types"], ["drop"])

        stored = WorkoutExercise.objects.get(exercise=self.squat)
        self.assertEqual(stored.rest_seconds, 180)
        self.assertEqual(stored.set_types, ["warmup", "normal", "normal", "failure"])

    def test_a_plan_that_says_nothing_new_reads_as_before(self):
        # An older app's create.
        response = self.create([{"exercise": self.squat.pk, "order": 1, "target_sets": 3}])
        self.assertEqual(response.status_code, 201, response.data)
        exercise = response.data["exercises"][0]
        self.assertIsNone(exercise["rest_seconds"])
        self.assertIsNone(exercise["superset_group"])
        self.assertEqual(exercise["set_types"], [])

    def test_exercise_plan_updates_the_new_fields(self):
        workout = self.create([{"exercise": self.squat.pk, "order": 1, "target_sets": 3}]).data
        relation = workout["exercises"][0]["id"]
        response = self.plan(workout["id"], [
            {"relation_id": relation, "exercise": self.squat.pk, "order": 1, "target_sets": 3,
             "rest_seconds": 90, "superset_group": 2, "set_types": ["warmup", "normal"]},
            {"exercise": self.row.pk, "order": 2, "target_sets": 2,
             "rest_seconds": 60, "superset_group": 2, "set_types": ["normal", "drop"]},
        ])
        self.assertEqual(response.status_code, 200, response.data)
        squat, row = response.data["exercises"]
        self.assertEqual(squat["id"], relation)
        self.assertEqual((squat["rest_seconds"], squat["superset_group"], squat["set_types"]),
                         (90, 2, ["warmup", "normal"]))
        self.assertEqual((row["rest_seconds"], row["superset_group"], row["set_types"]),
                         (60, 2, ["normal", "drop"]))

        # Cleared explicitly: null and an empty list are values, not omissions.
        response = self.plan(workout["id"], [
            {"relation_id": relation, "exercise": self.squat.pk, "order": 1,
             "rest_seconds": None, "superset_group": None, "set_types": []},
        ])
        self.assertEqual(response.status_code, 200, response.data)
        squat = WorkoutExercise.objects.get(pk=relation)
        self.assertEqual((squat.rest_seconds, squat.superset_group, squat.set_types), (None, None, []))
        self.assertEqual(WorkoutExercise.objects.filter(workout_id=workout["id"]).count(), 1)

    def test_a_plan_item_that_leaves_them_out_keeps_them(self):
        # The same as target_reps and notes: an older app editing a plan does
        # not wipe what a newer one set.
        workout = self.create([
            {"exercise": self.squat.pk, "order": 1, "target_sets": 3, "target_reps": 5,
             "notes": "Belt", "rest_seconds": 120, "superset_group": 4, "set_types": ["warmup"]},
        ]).data
        relation = workout["exercises"][0]["id"]
        response = self.plan(workout["id"], [
            {"relation_id": relation, "exercise": self.squat.pk, "order": 2, "target_sets": 3},
        ])
        self.assertEqual(response.status_code, 200, response.data)
        stored = WorkoutExercise.objects.get(pk=relation)
        self.assertEqual(stored.order, 2)
        self.assertEqual((stored.target_reps, stored.notes), (5, "Belt"))
        self.assertEqual((stored.rest_seconds, stored.superset_group, stored.set_types),
                         (120, 4, ["warmup"]))

    def test_lowering_the_set_count_trims_types_it_was_not_sent(self):
        workout = self.create([
            {"exercise": self.squat.pk, "order": 1, "target_sets": 4,
             "set_types": ["warmup", "normal", "drop", "failure"]},
        ]).data
        relation = workout["exercises"][0]["id"]
        response = self.plan(workout["id"], [
            {"relation_id": relation, "exercise": self.squat.pk, "order": 1, "target_sets": 2},
        ])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(WorkoutExercise.objects.get(pk=relation).set_types, ["warmup", "normal"])

        # And through the relation's own endpoint.
        patched = self.client.patch(
            f"/api/v1/workout-exercises/{relation}/", {"target_sets": 1}, format="json"
        )
        self.assertEqual(patched.status_code, 200, patched.data)
        self.assertEqual(patched.data["set_types"], ["warmup"])

    def test_more_set_types_than_sets_is_refused(self):
        too_many = {"exercise": self.squat.pk, "order": 1, "target_sets": 2,
                    "set_types": ["warmup", "normal", "normal"]}
        response = self.create([too_many])
        self.assertEqual(response.status_code, 400)
        self.assertIn("set_types", response.data["initial_exercises"][0])
        self.assertFalse(WorkoutTemplate.objects.exists())

        # Omitted, target_sets is the default of one.
        response = self.create([{"exercise": self.squat.pk, "order": 1, "set_types": ["warmup", "normal"]}])
        self.assertEqual(response.status_code, 400)

        workout = self.create([{"exercise": self.squat.pk, "order": 1, "target_sets": 2}]).data
        relation = workout["exercises"][0]["id"]

        # Both in the item.
        response = self.plan(workout["id"], [{"relation_id": relation, **too_many}])
        self.assertEqual(response.status_code, 400)
        self.assertIn("set_types", response.data["exercise_plan"][0])

        # Only the types in the item: checked against the relation's own count.
        response = self.plan(workout["id"], [
            {"relation_id": relation, "exercise": self.squat.pk, "order": 2,
             "set_types": ["warmup", "normal", "drop"]},
        ])
        self.assertEqual(response.status_code, 400)
        self.assertIn("exercise_plan", response.data)
        stored = WorkoutExercise.objects.get(pk=relation)
        self.assertEqual((stored.order, stored.set_types), (1, []))

        # The relation endpoint, on create and on PATCH.
        response = self.client.post("/api/v1/workout-exercises/", {
            "workout": workout["id"], **too_many, "order": 5,
        }, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("set_types", response.data)
        response = self.client.patch(
            f"/api/v1/workout-exercises/{relation}/",
            {"set_types": ["normal", "normal", "normal"]}, format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_out_of_range_values_are_refused(self):
        base = {"exercise": self.squat.pk, "order": 1, "target_sets": 3}
        for bad in (
            {"rest_seconds": 3601},
            {"rest_seconds": -1},
            {"superset_group": 0},
            {"superset_group": 101},
            {"set_types": ["warmup", "jumbo"]},
            {"set_types": "warmup"},
            {"target_sets": 60, "set_types": ["normal"] * 51},
        ):
            with self.subTest(bad=bad):
                response = self.create([{**base, **bad}], name=f"Bad {bad}")
                self.assertEqual(response.status_code, 400, response.data)
        for edge in ({"rest_seconds": 0}, {"rest_seconds": 3600}, {"superset_group": 100}):
            with self.subTest(edge=edge):
                response = self.create([{**base, **edge}], name=f"Edge {edge}")
                self.assertEqual(response.status_code, 201, response.data)


# -------------------------------------------------------- reordering a session


@FAST_PASSWORDS
class SessionReorderTests(Authenticated):
    @classmethod
    def setUpTestData(cls):
        _, cls.profile, cls.token = make_account("reorder-owner")
        _, cls.other, cls.other_token = make_account("reorder-other")
        cls.exercises = [Exercise.objects.create(name=name) for name in ("Squat", "Bench", "Row")]

    def setUp(self):
        self.as_user(self.token)
        self.session = WorkoutSession.objects.create(
            repbase_user=self.profile, status=WorkoutSession.Status.ACTIVE,
            started_at=timezone.now(),
        )
        self.a, self.b, self.c = [
            SessionExercise.objects.create(session=self.session, exercise=exercise, order=order)
            for order, exercise in enumerate(self.exercises, start=1)
        ]

    def reorder(self, ids, session=None):
        session = session or self.session
        return self.client.post(
            f"/api/v1/sessions/{session.pk}/reorder/", {"session_exercises": ids}, format="json"
        )

    def stored(self):
        return list(
            SessionExercise.objects.filter(session=self.session)
            .order_by("order").values_list("pk", "order")
        )

    def test_the_new_order_is_saved_and_returned(self):
        response = self.reorder([self.c.pk, self.a.pk, self.b.pk])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([row["id"] for row in response.data], [self.c.pk, self.a.pk, self.b.pk])
        self.assertEqual([row["order"] for row in response.data], [1, 2, 3])
        self.assertEqual(response.data[0]["exercise_name"], "Row")
        self.assertEqual(self.stored(), [(self.c.pk, 1), (self.a.pk, 2), (self.b.pk, 3)])

    def test_swapping_and_reversing_never_trip_the_unique_order(self):
        # Every one of these needs a number another row holds at the start.
        for ids in (
            [self.b.pk, self.a.pk, self.c.pk],
            [self.c.pk, self.b.pk, self.a.pk],
            [self.a.pk, self.b.pk, self.c.pk],
        ):
            with self.subTest(ids=ids):
                response = self.reorder(ids)
                self.assertEqual(response.status_code, 200, response.data)
                self.assertEqual(self.stored(), [(pk, n) for n, pk in enumerate(ids, start=1)])

    def test_gaps_and_huge_numbers_are_compacted(self):
        # A deleted exercise leaves a gap; a client may have used any number.
        SessionExercise.objects.filter(pk=self.a.pk).update(order=2_000_000_000)
        SessionExercise.objects.filter(pk=self.b.pk).update(order=4)
        SessionExercise.objects.filter(pk=self.c.pk).update(order=5)
        response = self.reorder([self.a.pk, self.c.pk, self.b.pk])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.stored(), [(self.a.pk, 1), (self.c.pk, 2), (self.b.pk, 3)])

    def test_the_same_list_twice_changes_nothing(self):
        ids = [self.b.pk, self.c.pk, self.a.pk]
        first = self.reorder(ids)
        second = self.reorder(ids)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.data, second.data)

    def test_sets_stay_with_their_exercise(self):
        SetEntry.objects.create(session_exercise=self.a, set_number=1, weight_kg=Decimal("50"), reps=5)
        self.reorder([self.b.pk, self.c.pk, self.a.pk])
        self.assertEqual(SetEntry.objects.get().session_exercise_id, self.a.pk)
        self.assertEqual(SessionExercise.objects.get(pk=self.a.pk).order, 3)

    def test_anything_but_exactly_this_sessions_exercises_is_refused(self):
        elsewhere = WorkoutSession.objects.create(
            repbase_user=self.profile, status=WorkoutSession.Status.ACTIVE
        )
        foreign = SessionExercise.objects.create(session=elsewhere, exercise=self.exercises[0], order=1)
        before = self.stored()
        for ids in (
            [self.a.pk, self.b.pk],                              # one missing
            [self.a.pk, self.b.pk, self.b.pk],                   # duplicate in place of one
            [self.a.pk, self.b.pk, self.c.pk, self.c.pk],        # duplicate on top
            [self.a.pk, self.b.pk, self.c.pk, foreign.pk],       # another session's
            [self.a.pk, self.b.pk, foreign.pk],                  # swapped for another's
            [self.a.pk, self.b.pk, 999_999_999],                 # no such row
            [],
        ):
            with self.subTest(ids=ids):
                response = self.reorder(ids)
                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn("session_exercises", response.data)
                self.assertEqual(self.stored(), before)
        for body in ({}, {"session_exercises": "1,2,3"}, {"session_exercises": [0]},
                     {"session_exercises": [self.a.pk, "x"]}):
            with self.subTest(body=body):
                response = self.client.post(
                    f"/api/v1/sessions/{self.session.pk}/reorder/", body, format="json"
                )
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.stored(), before)
        self.assertEqual(SessionExercise.objects.get(pk=foreign.pk).order, 1)

    def test_someone_elses_session_is_not_found(self):
        self.as_user(self.other_token)
        before = self.stored()
        response = self.reorder([self.c.pk, self.b.pk, self.a.pk])
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.stored(), before)

    def test_signed_out_is_refused(self):
        self.client.credentials()
        self.assertEqual(self.reorder([self.a.pk, self.b.pk, self.c.pk]).status_code, 401)

    def test_only_an_active_session_can_be_reordered(self):
        before = self.stored()
        for status in (WorkoutSession.Status.COMPLETED, WorkoutSession.Status.PLANNED):
            with self.subTest(status=status):
                WorkoutSession.objects.filter(pk=self.session.pk).update(status=status)
                response = self.reorder([self.c.pk, self.b.pk, self.a.pk])
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.stored(), before)

    def test_an_empty_session_reorders_to_an_empty_list(self):
        empty = WorkoutSession.objects.create(
            repbase_user=self.profile, status=WorkoutSession.Status.ACTIVE
        )
        response = self.reorder([], session=empty)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data, [])


@FAST_PASSWORDS
class ChangingAnActiveSessionTests(Authenticated):
    """Adding and removing exercises mid-session, through the existing routes."""

    @classmethod
    def setUpTestData(cls):
        _, cls.profile, cls.token = make_account("active-owner")
        _, cls.other, cls.other_token = make_account("active-other")
        cls.squat = Exercise.objects.create(name="Squat")
        cls.curl = Exercise.objects.create(name="Curl")

    def setUp(self):
        self.as_user(self.token)
        self.session = WorkoutSession.objects.create(
            repbase_user=self.profile, status=WorkoutSession.Status.ACTIVE,
            started_at=timezone.now(),
        )
        self.first = SessionExercise.objects.create(session=self.session, exercise=self.squat, order=1)

    def add(self, **extra):
        body = {"session": self.session.pk, "exercise": self.curl.pk, "order": 2}
        body.update(extra)
        return self.client.post("/api/v1/session-exercises/", body, format="json")

    def test_the_owner_adds_an_exercise_at_the_next_free_order(self):
        response = self.add()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["order"], 2)
        self.assertEqual(response.data["exercise_name"], "Curl")
        listed = self.client.get(f"/api/v1/session-exercises/?session={self.session.pk}")
        self.assertEqual([row["exercise_name"] for row in listed.data["results"]], ["Squat", "Curl"])

    def test_a_taken_order_is_a_400_not_a_crash(self):
        response = self.add(order=1)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(SessionExercise.objects.filter(session=self.session).count(), 1)

    def test_the_owner_removes_an_exercise_and_its_sets_go_with_it(self):
        added = self.add().data
        line = SessionExercise.objects.get(pk=added["id"])
        SetEntry.objects.create(session_exercise=line, set_number=1, weight_kg=Decimal("12.5"), reps=10)
        SetEntry.objects.create(session_exercise=self.first, set_number=1, weight_kg=Decimal("100"), reps=5)

        response = self.client.delete(f"/api/v1/session-exercises/{line.pk}/")
        self.assertEqual(response.status_code, 204)
        self.assertFalse(SessionExercise.objects.filter(pk=line.pk).exists())
        self.assertFalse(SetEntry.objects.filter(session_exercise_id=line.pk).exists())
        # Only that exercise's sets.
        self.assertTrue(SetEntry.objects.filter(session_exercise=self.first).exists())

        # The gap it leaves is closed by a reorder.
        response = self.client.post(
            f"/api/v1/sessions/{self.session.pk}/reorder/",
            {"session_exercises": [self.first.pk]}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)

    def test_someone_elses_session_is_refused(self):
        self.as_user(self.other_token)
        response = self.add()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(SessionExercise.objects.filter(session=self.session).count(), 1)

        response = self.client.delete(f"/api/v1/session-exercises/{self.first.pk}/")
        self.assertEqual(response.status_code, 404)
        self.assertTrue(SessionExercise.objects.filter(pk=self.first.pk).exists())


# ------------------------------------------------------------- follows you


@FAST_PASSWORDS
class FollowsViewerTests(Authenticated):
    @classmethod
    def setUpTestData(cls):
        cls.reader, cls.reader_profile, cls.reader_token = make_account("fv-reader")
        _, cls.other, cls.other_token = make_account("fv-other")

    def setUp(self):
        self.as_user(self.reader_token)

    def read(self, profile):
        response = self.client.get(f"/api/v1/users/{profile.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def test_nobody_follows_anybody(self):
        data = self.read(self.other)
        self.assertIs(data["follows_viewer"], False)
        self.assertIs(data["viewer_follows"], False)

    def test_they_follow_you(self):
        Follow.objects.create(follower=self.other, following=self.reader_profile)
        data = self.read(self.other)
        self.assertIs(data["follows_viewer"], True)
        self.assertIs(data["viewer_follows"], False)

    def test_you_follow_them(self):
        Follow.objects.create(follower=self.reader_profile, following=self.other)
        data = self.read(self.other)
        self.assertIs(data["follows_viewer"], False)
        self.assertIs(data["viewer_follows"], True)

    def test_mutual(self):
        Follow.objects.create(follower=self.other, following=self.reader_profile)
        Follow.objects.create(follower=self.reader_profile, following=self.other)
        data = self.read(self.other)
        self.assertIs(data["follows_viewer"], True)
        self.assertIs(data["viewer_follows"], True)
        # And from the other side.
        self.as_user(self.other_token)
        data = self.read(self.reader_profile)
        self.assertIs(data["follows_viewer"], True)

    def test_your_own_profile_does_not_follow_you(self):
        Follow.objects.create(follower=self.other, following=self.reader_profile)
        self.assertIs(self.read(self.reader_profile)["follows_viewer"], False)

    def test_a_closed_profile_still_says_it_follows_you(self):
        self.other.is_profile_public = False
        self.other.bio = "Private things"
        self.other.save(update_fields=["is_profile_public", "bio"])
        Follow.objects.create(follower=self.other, following=self.reader_profile)
        data = self.read(self.other)
        self.assertIs(data["is_readable"], False)
        self.assertEqual(data["bio"], "")
        self.assertIs(data["follows_viewer"], True)

        # Closed and not following: still false, not emptied into something else.
        Follow.objects.all().delete()
        self.assertIs(self.read(self.other)["follows_viewer"], False)

    def test_an_anonymous_reader_is_followed_by_nobody_and_asks_nothing(self):
        Follow.objects.create(follower=self.other, following=self.reader_profile)
        request = APIRequestFactory().get("/")
        request.user = AnonymousUser()
        serializer = PublicRepbaseUserSerializer(self.other, context={"request": request})
        with CaptureQueriesContext(connection) as queries:
            self.assertIs(serializer.data["follows_viewer"], False)
        self.assertFalse(
            [q["sql"] for q in queries.captured_queries if "core_follow" in q["sql"]]
        )

    def test_one_follow_lookup_each_way_whatever_the_page_size(self):
        def follow_queries():
            with CaptureQueriesContext(connection) as queries:
                response = self.client.get("/api/v1/users/")
            self.assertEqual(response.status_code, 200)
            return response, [q for q in queries.captured_queries if '"core_follow"' in q["sql"]]

        Follow.objects.create(follower=self.other, following=self.reader_profile)
        _, few = follow_queries()
        for index in range(4):
            _, extra, _ = make_account(f"fv-extra-{index}")
            Follow.objects.create(follower=extra, following=self.reader_profile)
        response, many = follow_queries()
        self.assertEqual(len(few), len(many))
        self.assertLessEqual(len(many), 2)
        flags = {row["username"]: row["follows_viewer"] for row in response.data["results"]}
        self.assertEqual(len(flags), 5)
        self.assertTrue(all(flags.values()), flags)


# ------------------------------------------------- when a workout was last done


@FAST_PASSWORDS
class LastPerformedTests(Authenticated):
    @classmethod
    def setUpTestData(cls):
        _, cls.profile, cls.token = make_account("lp-owner")
        cls.squat = Exercise.objects.create(name="Squat")

    def setUp(self):
        self.as_user(self.token)
        self.workout = WorkoutTemplate.objects.create(owner=self.profile, name="Legs")

    def last_performed(self, workout=None):
        workout = workout or self.workout
        response = self.client.get(f"/api/v1/workouts/{workout.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["last_performed_at"]

    def as_wire(self, moment):
        from rest_framework import serializers

        return serializers.DateTimeField().to_representation(moment)

    def test_never_trained_is_null(self):
        self.assertIsNone(self.last_performed())

    def test_the_latest_finished_session_that_recorded_something(self):
        older = session_with_sets(self.profile, self.squat, [("100", 5, SetType.NORMAL)],
                                  ended_ago=timedelta(days=3), workout=self.workout)
        newer = session_with_sets(self.profile, self.squat, [("100", 5, SetType.NORMAL)],
                                  ended_ago=timedelta(days=1), workout=self.workout)
        self.assertEqual(self.last_performed(), self.as_wire(newer.ended_at))
        newer.delete()
        self.assertEqual(self.last_performed(), self.as_wire(older.ended_at))

    def test_sessions_that_recorded_nothing_do_not_count(self):
        # Finished with only empty set rows, finished with no sets at all,
        # and an active one that did log something.
        session_with_sets(self.profile, self.squat, [(None, None, SetType.NORMAL)],
                          ended_ago=timedelta(hours=2), workout=self.workout)
        WorkoutSession.objects.create(
            repbase_user=self.profile, workout=self.workout,
            status=WorkoutSession.Status.COMPLETED,
            started_at=timezone.now() - timedelta(hours=1), ended_at=timezone.now(),
        )
        session_with_sets(self.profile, self.squat, [("100", 5, SetType.NORMAL)],
                          workout=self.workout, status=WorkoutSession.Status.ACTIVE)
        self.assertIsNone(self.last_performed())

        # Reps alone are something; so is a warm-up, which is still a logged set.
        done = session_with_sets(self.profile, self.squat, [(None, 12, SetType.WARMUP)],
                                 ended_ago=timedelta(days=5), workout=self.workout)
        self.assertEqual(self.last_performed(), self.as_wire(done.ended_at))

    def test_a_session_without_an_end_time_uses_its_start(self):
        session = session_with_sets(self.profile, self.squat, [("80", 8, SetType.NORMAL)],
                                    ended_ago=timedelta(days=2), workout=self.workout)
        WorkoutSession.objects.filter(pk=session.pk).update(ended_at=None)
        self.assertEqual(self.last_performed(), self.as_wire(session.started_at))

    def test_create_and_update_responses_carry_it_too(self):
        created = self.client.post(
            "/api/v1/workouts/", {"name": "Push", "workout_type": "lifting"}, format="json"
        )
        self.assertEqual(created.status_code, 201, created.data)
        self.assertIn("last_performed_at", created.data)
        self.assertIsNone(created.data["last_performed_at"])

        done = session_with_sets(self.profile, self.squat, [("100", 5, SetType.NORMAL)],
                                 ended_ago=timedelta(days=1), workout=self.workout)
        updated = self.client.patch(
            f"/api/v1/workouts/{self.workout.pk}/", {"description": "Heavy"}, format="json"
        )
        self.assertEqual(updated.status_code, 200, updated.data)
        self.assertEqual(updated.data["last_performed_at"], self.as_wire(done.ended_at))

    def test_the_list_costs_the_same_however_many_workouts(self):
        def list_queries():
            with CaptureQueriesContext(connection) as queries:
                response = self.client.get("/api/v1/workouts/")
            self.assertEqual(response.status_code, 200)
            return response, len(queries.captured_queries)

        # Every workout has an exercise, so the exercise prefetch runs in both
        # reads and only the number of workouts differs between them.
        WorkoutExercise.objects.create(workout=self.workout, exercise=self.squat, order=1)
        session_with_sets(self.profile, self.squat, [("100", 5, SetType.NORMAL)],
                          ended_ago=timedelta(days=1), workout=self.workout)
        _, few = list_queries()
        for index in range(4):
            workout = WorkoutTemplate.objects.create(owner=self.profile, name=f"Day {index}")
            WorkoutExercise.objects.create(workout=workout, exercise=self.squat, order=1)
            session_with_sets(self.profile, self.squat, [("100", 5, SetType.NORMAL)],
                              ended_ago=timedelta(days=index + 2), workout=workout)
        response, many = list_queries()
        self.assertEqual(few, many)
        dates = [row["last_performed_at"] for row in response.data["results"]]
        self.assertEqual(len(dates), 5)
        self.assertTrue(all(dates), dates)


# -------------------------------------------------------- exercise discovery


@FAST_PASSWORDS
class ExerciseDiscoveryTests(Authenticated):
    @classmethod
    def setUpTestData(cls):
        _, cls.profile, cls.token = make_account("ex-reader")
        _, cls.other, _ = make_account("ex-other")
        for name in ("Bench Press", "Incline Bench Press", "Back Squat"):
            Exercise.objects.create(name=name)
        cls.mine = Exercise.objects.create(name="My Bench Variant", created_by=cls.profile)
        Exercise.objects.create(name="Their Bench Variant", created_by=cls.other)
        cls.long = Exercise.objects.create(name="X" * 100 + " Long")

    def setUp(self):
        self.as_user(self.token)

    def names(self, query=""):
        response = self.client.get(f"/api/v1/exercises/{query}")
        self.assertEqual(response.status_code, 200, response.data)
        return sorted(row["name"] for row in response.data["results"])

    def test_search_matches_part_of_a_name_ignoring_case_and_space(self):
        expected = ["Bench Press", "Incline Bench Press", "My Bench Variant"]
        self.assertEqual(self.names("?search=bench"), expected)
        self.assertEqual(self.names("?search=%20%20BENCH%20"), expected)
        self.assertEqual(self.names("?search=squat"), ["Back Squat"])
        self.assertEqual(self.names("?search=deadlift"), [])

    def test_a_blank_search_is_no_search(self):
        everything = self.names()
        self.assertEqual(len(everything), 5)
        self.assertEqual(self.names("?search="), everything)
        self.assertEqual(self.names("?search=%20%20%20"), everything)

    def test_only_the_first_hundred_characters_are_searched(self):
        # Past the cut the text would match nothing; within it, it matches.
        query = "x" * 100 + "zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"
        self.assertEqual(self.names(f"?search={query}"), [self.long.name])

    def test_mine_is_only_what_the_reader_made(self):
        self.assertEqual(self.names("?mine=true"), ["My Bench Variant"])
        self.assertEqual(self.names("?mine=true&search=squat"), [])
        self.assertEqual(self.names("?mine=false"), self.names())
        response = self.client.get("/api/v1/exercises/?mine=yes")
        self.assertEqual(response.status_code, 400)
        self.assertIn("mine", response.data)

    def test_search_never_reaches_somebody_elses_exercises(self):
        self.assertNotIn("Their Bench Variant", self.names("?search=their"))

    def test_the_exact_name_filter_still_works(self):
        self.assertEqual(self.names("?name=bench%20press"), ["Bench Press"])
        self.assertEqual(self.names("?name=bench%20press&search=incline"), [])

    def test_discovery_parameters_do_not_hide_a_detail_read(self):
        response = self.client.get(f"/api/v1/exercises/{self.mine.pk}/?search=zzz&mine=nonsense")
        self.assertEqual(response.status_code, 200, response.data)


# -------------------------------------------------------------- the contract


class NewResponseFieldsAreOptionalTests(APITestCase):
    """Each field added here must be absent from its component's `required`.

    The generated Swift client makes a required property non-optional and
    refuses a response without it, so documenting one of these as required
    would break the newer app against a server deployed before it.
    """

    ADDED = {
        "SetEntry": ["set_type"],
        "WorkoutExercise": ["rest_seconds", "superset_group", "set_types"],
        "InitialWorkoutExercise": ["rest_seconds", "superset_group", "set_types"],
        "WorkoutPlanExercise": ["rest_seconds", "superset_group", "set_types"],
        "PreviousSet": ["set_type"],
        "PublicRepbaseUser": ["follows_viewer"],
        "WorkoutTemplate": ["last_performed_at"],
        "SetEntryRequest": ["set_type"],
        "PatchedSetEntryRequest": ["set_type"],
        "InitialWorkoutExerciseRequest": ["rest_seconds", "superset_group", "set_types"],
        "WorkoutPlanExerciseRequest": ["rest_seconds", "superset_group", "set_types"],
    }

    def test_added_fields_are_documented_and_optional(self):
        from drf_spectacular.generators import SchemaGenerator

        schemas = SchemaGenerator().get_schema(request=None, public=True)["components"]["schemas"]
        for component, fields in self.ADDED.items():
            for field in fields:
                with self.subTest(component=component, field=field):
                    self.assertIn(field, schemas[component]["properties"])
                    self.assertNotIn(field, schemas[component].get("required", []))

        # Still required where they always were.
        self.assertIn("viewer_follows", schemas["PublicRepbaseUser"]["required"])
        self.assertIn("exercises", schemas["WorkoutTemplate"]["required"])
        self.assertIn("weight_kg", schemas["PreviousSet"]["required"])

        self.assertEqual(schemas["SetTypeEnum"]["enum"], ["normal", "warmup", "drop", "failure"])
        self.assertEqual(
            schemas["WorkoutExercise"]["properties"]["set_types"]["items"],
            {"$ref": "#/components/schemas/SetTypeEnum"},
        )
        self.assertEqual(
            schemas["SessionReorderRequest"]["required"], ["session_exercises"]
        )
