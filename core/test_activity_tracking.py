"""Activities, sessions logged after the fact, what counts as training, and
the shared exercise library.

Workouts gained an `activity` beside the legacy `workout_type`; sessions gained
what a person says about them (effort, calories, a typed distance, notes and
activity details) and a way to be logged without being recorded live; the
training count learned that a tennis match logs no sets; and the exercise
catalogue learned to hold a library. Every response field here was added to a
response the app already decodes, so the last class checks the contract keeps
each one optional.
"""

import importlib
import json
import tempfile
import uuid
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest import mock

from django.apps import apps as django_apps
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from . import activities
from .activities import (
    ACTIVITIES,
    LEGACY_DEFAULT_ACTIVITY,
    ActivityDetailsError,
    clean_activity_details,
)
from .exercise_library import ExerciseLibraryError, sync_exercise_library
from .fixtures_for_tests import FAST_PASSWORDS, make_account
from .models import (
    Exercise,
    Gear,
    PlannerEntry,
    PostWorkout,
    SessionExercise,
    SetEntry,
    WorkoutSchedule,
    WorkoutSession,
    WorkoutTemplate,
    training_day_of,
)

SESSIONS = "/api/v1/sessions/"
LOG = "/api/v1/sessions/log/"
WORKOUTS = "/api/v1/workouts/"
EXERCISES = "/api/v1/exercises/"
STATS = "/api/v1/sessions/training-stats/"

#: Every slug, in the registry's order: a wire format, so pinned here.
ALL_SLUGS = [
    "weightlifting", "calisthenics", "crossfit", "circuit_training",
    "functional_training", "hiit", "running", "jogging", "sprinting",
    "trail_running", "walking", "hiking", "cycling", "indoor_cycling",
    "mountain_biking", "swimming", "open_water_swimming", "rowing",
    "indoor_rowing", "paddling", "elliptical", "stair_climbing", "jump_rope",
    "tennis", "padel", "badminton", "table_tennis", "squash", "pickleball",
    "soccer", "american_football", "basketball", "volleyball", "rugby",
    "hockey", "baseball", "handball", "cricket", "boxing", "kickboxing",
    "martial_arts", "yoga", "pilates", "stretching", "dance", "climbing",
    "skiing", "snowboarding", "surfing", "golf", "other",
]


def migration(name):
    return importlib.import_module(f"core.migrations.{name}")


class Authenticated(APITestCase):
    def as_user(self, token):
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")


# ------------------------------------------------------------------ registry


class ActivityRegistryTests(SimpleTestCase):
    def test_the_slugs_are_exactly_these_in_this_order(self):
        self.assertEqual(list(ACTIVITIES), ALL_SLUGS)
        self.assertEqual(len(ACTIVITIES), 51)

    def test_families_are_only_the_four_legacy_types(self):
        self.assertEqual(
            {spec.family for spec in ACTIVITIES.values()},
            set(WorkoutTemplate.WorkoutType.values),
        )

    def test_families_and_profiles(self):
        expected = {
            "weightlifting": ("lifting", "strength"),
            "calisthenics": ("lifting", "strength"),
            "crossfit": ("lifting", "session"),
            "sprinting": ("running", "session"),
            "hiking": ("running", "distance"),
            "mountain_biking": ("biking", "distance"),
            "open_water_swimming": ("swimming", "distance"),
            "rowing": ("lifting", "distance"),
            "tennis": ("lifting", "session"),
            "cricket": ("lifting", "session"),
            "climbing": ("lifting", "session"),
            "other": ("lifting", "session"),
        }
        for slug, (family, profile) in expected.items():
            with self.subTest(slug=slug):
                self.assertEqual((ACTIVITIES[slug].family, ACTIVITIES[slug].profile), (family, profile))

    def test_detail_keys_per_activity(self):
        expected = {
            "crossfit": {"format", "rounds", "reps", "movements"},
            "martial_arts": {"style", "rounds"},
            "climbing": {"style", "routes"},
            "swimming": {"pool_length_m"},
            "padel": {"session_type"},
            "basketball": {"session_type"},
            "weightlifting": set(),
            "golf": set(),
        }
        for slug, keys in expected.items():
            with self.subTest(slug=slug):
                self.assertEqual(set(ACTIVITIES[slug].details), keys)

    def test_legacy_defaults(self):
        self.assertEqual(LEGACY_DEFAULT_ACTIVITY, {
            "lifting": "weightlifting", "running": "running",
            "biking": "cycling", "swimming": "swimming",
        })
        for family, slug in LEGACY_DEFAULT_ACTIVITY.items():
            self.assertEqual(ACTIVITIES[slug].family, family)

    def refused(self, activity, details):
        with self.assertRaises(ActivityDetailsError) as caught:
            clean_activity_details(activity, details)
        return caught.exception.errors

    def test_valid_details_are_kept(self):
        self.assertEqual(
            clean_activity_details("crossfit", {
                "format": "amrap", "rounds": 5, "reps": 120, "movements": "  burpees, pull-ups  ",
            }),
            {"format": "amrap", "rounds": 5, "reps": 120, "movements": "burpees, pull-ups"},
        )
        self.assertEqual(clean_activity_details("tennis", {"session_type": "match"}),
                         {"session_type": "match"})
        self.assertEqual(clean_activity_details("yoga", {}), {})

    def test_an_unknown_key_is_refused_by_name(self):
        self.assertIn("rounds", self.refused("tennis", {"rounds": 3}))
        self.assertIn("anything", self.refused("weightlifting", {"anything": 1}))
        # A session with no workout has no activity, so takes nothing.
        self.assertIn("rounds", self.refused(None, {"rounds": 3}))

    def test_wrong_types_are_refused_by_name(self):
        self.assertIn("rounds", self.refused("boxing", {"rounds": "3"}))
        self.assertIn("rounds", self.refused("boxing", {"rounds": 3.5}))
        self.assertIn("session_type", self.refused("tennis", {"session_type": 1}))
        self.assertIn("movements", self.refused("crossfit", {"movements": ["a"]}))

    def test_a_bool_is_not_a_whole_number(self):
        for key, activity in (("rounds", "boxing"), ("reps", "crossfit"),
                              ("intervals", "sprinting"), ("routes", "climbing"),
                              ("elevation_gain_m", "hiking"), ("pool_length_m", "swimming")):
            with self.subTest(key=key):
                self.assertIn(key, self.refused(activity, {key: True}))

    def test_whole_number_bounds(self):
        bounds = {
            ("boxing", "rounds"): (0, 999), ("crossfit", "reps"): (0, 9999),
            ("sprinting", "intervals"): (0, 999), ("climbing", "routes"): (0, 999),
            ("trail_running", "elevation_gain_m"): (0, 10000),
            ("swimming", "pool_length_m"): (10, 100),
        }
        for (activity, key), (low, high) in bounds.items():
            with self.subTest(key=key):
                self.assertEqual(clean_activity_details(activity, {key: low}), {key: low})
                self.assertEqual(clean_activity_details(activity, {key: high}), {key: high})
                self.assertIn(key, self.refused(activity, {key: low - 1}))
                self.assertIn(key, self.refused(activity, {key: high + 1}))

    def test_choices(self):
        for value in ("match", "training", "casual"):
            clean_activity_details("squash", {"session_type": value})
        self.assertIn("session_type", self.refused("squash", {"session_type": "league"}))
        for value in ("amrap", "emom", "for_time", "tabata", "rounds", "chipper", "other"):
            clean_activity_details("crossfit", {"format": value})
        self.assertIn("format", self.refused("crossfit", {"format": "AMRAP"}))

    def test_style_depends_on_the_activity(self):
        clean_activity_details("yoga", {"style": "vinyasa"})
        clean_activity_details("pilates", {"style": "reformer"})
        clean_activity_details("martial_arts", {"style": "jiu_jitsu"})
        clean_activity_details("climbing", {"style": "bouldering"})
        self.assertIn("style", self.refused("yoga", {"style": "reformer"}))
        self.assertIn("style", self.refused("pilates", {"style": "vinyasa"}))
        self.assertIn("style", self.refused("climbing", {"style": "judo"}))

    def test_movements_are_trimmed_and_bounded(self):
        self.assertEqual(clean_activity_details("crossfit", {"movements": " " + "x" * 500 + " "}),
                         {"movements": "x" * 500})
        self.assertIn("movements", self.refused("crossfit", {"movements": "x" * 501}))
        # Text that trims to nothing, like null, is no value at all.
        self.assertEqual(clean_activity_details("crossfit", {"movements": "   "}), {})

    def test_null_is_left_out(self):
        self.assertEqual(clean_activity_details("boxing", {"rounds": None}), {})

    def test_not_an_object(self):
        self.assertIn("activity_details", self.refused("boxing", [1, 2]))

    def test_how_sessions_are_counted(self):
        trained = activities.session_trained
        self.assertTrue(trained(activity="tennis", workout_type="lifting", logged_sets=0,
                                duration_seconds=3600, distances=()))
        self.assertFalse(trained(activity="tennis", workout_type="lifting", logged_sets=0,
                                 duration_seconds=0, distances=(None,)))
        self.assertTrue(trained(activity="rowing", workout_type="lifting", logged_sets=0,
                                duration_seconds=0, distances=(Decimal("2.000"),)))
        self.assertFalse(trained(activity="weightlifting", workout_type="lifting", logged_sets=0,
                                 duration_seconds=3600, distances=()))
        self.assertTrue(trained(activity="weightlifting", workout_type="lifting", logged_sets=1,
                                duration_seconds=0, distances=()))
        # A deleted workout: by sets, as it always was.
        self.assertFalse(trained(activity=None, workout_type=None, logged_sets=0,
                                 duration_seconds=3600, distances=()))
        # A blank activity falls back to its type's default.
        self.assertTrue(trained(activity="", workout_type="running", logged_sets=0,
                                duration_seconds=60, distances=()))


# ----------------------------------------------------------- template activity


@FAST_PASSWORDS
class TemplateActivityTests(Authenticated):
    @classmethod
    def setUpTestData(cls):
        _, cls.profile, cls.token = make_account("activity-owner")

    def setUp(self):
        self.as_user(self.token)

    def create(self, **body):
        response = self.client.post(WORKOUTS, {"name": "Workout", **body}, format="json")
        return response

    def patch(self, workout, **body):
        return self.client.patch(f"{WORKOUTS}{workout.pk}/", body, format="json")

    def test_an_activity_sets_its_family(self):
        response = self.create(activity="tennis")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual((response.data["activity"], response.data["workout_type"]), ("tennis", "lifting"))
        workout = WorkoutTemplate.objects.get(pk=response.data["id"])
        self.assertEqual((workout.activity, workout.workout_type), ("tennis", "lifting"))

        response = self.create(name="Hike", activity="hiking")
        self.assertEqual(response.data["workout_type"], "running")

    def test_the_activity_wins_over_a_conflicting_type(self):
        response = self.create(activity="cycling", workout_type="swimming")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["workout_type"], "biking")

    def test_neither_is_weightlifting(self):
        response = self.create()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual((response.data["activity"], response.data["workout_type"]),
                         ("weightlifting", "lifting"))

    def test_an_old_app_sending_only_the_type_gets_its_default(self):
        for family, slug in LEGACY_DEFAULT_ACTIVITY.items():
            with self.subTest(family=family):
                response = self.create(name=f"Old {family}", workout_type=family)
                self.assertEqual(response.status_code, 201, response.data)
                self.assertEqual(response.data["activity"], slug)

    def test_an_unknown_activity_is_refused(self):
        for value in ("quidditch", "", "Tennis", "x" * 41):
            with self.subTest(value=value):
                response = self.create(name=f"W {value[:5]}", activity=value)
                self.assertEqual(response.status_code, 400)
                self.assertIn("activity", response.data)

    def test_changing_the_activity_changes_the_family(self):
        workout = WorkoutTemplate.objects.create(owner=self.profile, name="Cardio")
        response = self.patch(workout, activity="indoor_rowing")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["workout_type"], "lifting")
        response = self.patch(workout, activity="open_water_swimming")
        self.assertEqual(response.data["workout_type"], "swimming")
        workout.refresh_from_db()
        self.assertEqual((workout.activity, workout.workout_type), ("open_water_swimming", "swimming"))

    def test_an_old_app_editing_keeps_an_activity_of_the_same_family(self):
        workout = WorkoutTemplate.objects.create(
            owner=self.profile, name="Sunday Tennis", activity="tennis", workout_type="lifting"
        )
        # What an old build sends on rename: the type it shows, and no activity.
        response = self.patch(workout, name="Tennis", workout_type="lifting")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["activity"], "tennis")
        # A PATCH that says nothing about either leaves both alone.
        response = self.patch(workout, description="Club courts")
        self.assertEqual(response.data["activity"], "tennis")
        # A different family is a real change, and the default comes with it.
        response = self.patch(workout, workout_type="running")
        self.assertEqual((response.data["activity"], response.data["workout_type"]), ("running", "running"))

    def test_a_full_update_from_an_old_app(self):
        workout = WorkoutTemplate.objects.create(
            owner=self.profile, name="Spin", activity="indoor_cycling", workout_type="biking"
        )
        response = self.client.put(
            f"{WORKOUTS}{workout.pk}/", {"name": "Spin class", "workout_type": "biking"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["activity"], "indoor_cycling")

    def test_every_path_that_makes_a_workout_gives_it_an_activity(self):
        # Made in code, as a Health import or saving a post does.
        made = WorkoutTemplate.objects.create(owner=self.profile, name="Swim", workout_type="swimming")
        self.assertEqual(made.activity, "swimming")
        # And a row written around save() still answers with one.
        WorkoutTemplate.objects.filter(pk=made.pk).update(activity="")
        response = self.client.get(f"{WORKOUTS}{made.pk}/")
        self.assertEqual(response.data["activity"], "swimming")
        listed = self.client.get(WORKOUTS)
        self.assertTrue(all(row["activity"] for row in listed.data["results"]))

    def test_health_imports_are_their_familys_default(self):
        started = timezone.now() - timedelta(hours=2)
        response = self.client.post("/api/v1/sessions/import-health/", {"workouts": [{
            "external_id": "abc", "activity": "biking",
            "started_at": started.isoformat(), "ended_at": (started + timedelta(hours=1)).isoformat(),
        }]}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        session = WorkoutSession.objects.get(health_external_id="abc")
        self.assertEqual(session.workout.activity, "cycling")

    def test_the_backfill_gives_every_old_row_its_familys_default(self):
        made = {
            family: WorkoutTemplate.objects.create(owner=self.profile, name=f"B {family}", workout_type=family)
            for family in LEGACY_DEFAULT_ACTIVITY
        }
        kept = WorkoutTemplate.objects.create(owner=self.profile, name="Kept", activity="yoga")
        WorkoutTemplate.objects.filter(pk__in=[w.pk for w in made.values()]).update(activity="")
        migration("0084_backfill_activity_and_search_text").backfill(django_apps, None)
        for family, workout in made.items():
            workout.refresh_from_db()
            self.assertEqual(workout.activity, LEGACY_DEFAULT_ACTIVITY[family])
        kept.refresh_from_db()
        self.assertEqual(kept.activity, "yoga")

    def test_the_backfill_fills_snapshots_and_search_text(self):
        from .fixtures_for_tests import workout_post

        post = workout_post(self.profile, workout_type="biking")
        ad_hoc = workout_post(self.profile, workout_type="lifting")
        PostWorkout.objects.filter(post=ad_hoc).update(workout_type=None)
        exercise = Exercise.objects.create(name="Front Squat", created_by=self.profile)
        Exercise.objects.filter(pk=exercise.pk).update(search_text="")
        migration("0084_backfill_activity_and_search_text").backfill(django_apps, None)
        self.assertEqual(PostWorkout.objects.get(post=post).activity, "cycling")
        self.assertIsNone(PostWorkout.objects.get(post=ad_hoc).activity)
        exercise.refresh_from_db()
        self.assertEqual(exercise.search_text, "front squat")


# ------------------------------------------------------ session PATCH fields


@FAST_PASSWORDS
class SessionEntryFieldTests(Authenticated):
    @classmethod
    def setUpTestData(cls):
        _, cls.profile, cls.token = make_account("entry-owner")
        _, cls.other, cls.other_token = make_account("entry-other")
        cls.boxing = WorkoutTemplate.objects.create(owner=cls.profile, name="Boxing", activity="boxing")
        cls.running = WorkoutTemplate.objects.create(owner=cls.profile, name="Run", activity="running",
                                                 workout_type="running")

    def setUp(self):
        self.as_user(self.token)
        ended = timezone.now() - timedelta(minutes=5)
        self.session = WorkoutSession.objects.create(
            repbase_user=self.profile, workout=self.boxing,
            status=WorkoutSession.Status.COMPLETED,
            started_at=ended - timedelta(hours=1), ended_at=ended,
        )

    def patch(self, session=None, **body):
        session = session or self.session
        return self.client.patch(f"{SESSIONS}{session.pk}/", body, format="json")

    def test_a_completed_session_takes_what_the_person_says(self):
        response = self.patch(
            perceived_effort=80, manual_calories_kcal=450, manual_distance_km="3.250",
            notes="  Sparring, felt sharp  ", activity_details={"rounds": 6},
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["perceived_effort"], 80)
        self.assertEqual(response.data["manual_calories_kcal"], 450)
        self.assertEqual(response.data["manual_distance_km"], "3.250")
        self.assertEqual(response.data["notes"], "Sparring, felt sharp")
        self.assertEqual(response.data["activity_details"], {"rounds": 6})
        self.assertEqual(response.data["activity"], "boxing")
        self.assertIs(response.data["is_manual_entry"], False)
        self.assertEqual(response.data["status"], "completed")
        self.session.refresh_from_db()
        self.assertEqual(self.session.perceived_effort, 80)
        self.assertEqual(self.session.activity_details, {"rounds": 6})

    def test_a_session_that_was_never_rated_reads_as_before(self):
        response = self.client.get(f"{SESSIONS}{self.session.pk}/")
        self.assertIsNone(response.data["perceived_effort"])
        self.assertIsNone(response.data["manual_calories_kcal"])
        self.assertIsNone(response.data["manual_distance_km"])
        self.assertEqual(response.data["notes"], "")
        self.assertEqual(response.data["activity_details"], {})

    def test_out_of_range_values_are_refused(self):
        for body, field in (
            ({"perceived_effort": 101}, "perceived_effort"),
            ({"perceived_effort": -1}, "perceived_effort"),
            ({"manual_calories_kcal": 0}, "manual_calories_kcal"),
            ({"manual_calories_kcal": 20001}, "manual_calories_kcal"),
            ({"manual_distance_km": "0.001"}, "manual_distance_km"),
            ({"manual_distance_km": "0.000"}, "manual_distance_km"),
            ({"manual_distance_km": "10000"}, "manual_distance_km"),
            ({"notes": "x" * 2001}, "notes"),
            ({"activity_details": {"rounds": 1000}}, "activity_details"),
            ({"activity_details": {"rounds": True}}, "activity_details"),
            ({"activity_details": {"session_type": "match"}}, "activity_details"),
            ({"activity_details": [1]}, "activity_details"),
        ):
            with self.subTest(body=body):
                response = self.patch(**body)
                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn(field, response.data)
        # The refusal names the detail key, not just the field.
        response = self.patch(activity_details={"session_type": "match", "rounds": "x"})
        self.assertEqual(set(response.data["activity_details"]), {"session_type", "rounds"})
        self.session.refresh_from_db()
        self.assertIsNone(self.session.perceived_effort)
        self.assertEqual(self.session.activity_details, {})

    def test_the_bounds_themselves_are_accepted(self):
        for body in ({"perceived_effort": 0}, {"perceived_effort": 100},
                     {"manual_calories_kcal": 1}, {"manual_calories_kcal": 20000},
                     {"manual_distance_km": "0.01"}, {"manual_distance_km": "9999.999"},
                     {"notes": "x" * 2000}):
            with self.subTest(body=body):
                self.assertEqual(self.patch(**body).status_code, 200)

    def test_fields_can_be_cleared(self):
        self.patch(perceived_effort=50, manual_calories_kcal=300, notes="x",
                   activity_details={"rounds": 3})
        response = self.patch(perceived_effort=None, manual_calories_kcal=None, notes="",
                              activity_details={})
        self.assertEqual(response.status_code, 200, response.data)
        self.session.refresh_from_db()
        self.assertEqual((self.session.perceived_effort, self.session.manual_calories_kcal,
                          self.session.notes, self.session.activity_details), (None, None, "", {}))

    def test_details_follow_the_activity_of_the_workout(self):
        tennis = WorkoutTemplate.objects.create(owner=self.profile, name="Tennis", activity="tennis")
        response = self.patch(workout=tennis.pk, activity_details={"session_type": "casual"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["activity"], "tennis")

    def test_a_distance_with_nothing_measured_becomes_the_recorded_one(self):
        response = self.patch(manual_distance_km="5.000")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["recorded_distance_km"], "5.000")
        self.session.refresh_from_db()
        self.assertEqual(self.session.recorded_distance_km, Decimal("5.000"))
        # Gear mileage is summed from that column, so it counts.
        shoe = Gear.objects.create(owner=self.profile, kind="shoe", name="Daily")
        WorkoutSession.objects.filter(pk=self.session.pk).update(gear=shoe)
        gear = self.client.get(f"/api/v1/gear/{shoe.pk}/")
        self.assertEqual(gear.data["total_distance_km"], 5.0)

        response = self.patch(manual_distance_km=None)
        self.assertIsNone(response.data["manual_distance_km"])
        self.assertIsNone(response.data["recorded_distance_km"])

    def test_a_measured_distance_is_never_replaced(self):
        WorkoutSession.objects.filter(pk=self.session.pk).update(
            health_distance_km=Decimal("10.000"), recorded_distance_km=Decimal("10.000")
        )
        response = self.patch(manual_distance_km="9.500")
        self.assertEqual(response.data["manual_distance_km"], "9.500")
        self.assertEqual(response.data["recorded_distance_km"], "10.000")
        response = self.patch(manual_distance_km=None)
        self.assertEqual(response.data["recorded_distance_km"], "10.000")

    def test_a_gps_distance_is_never_replaced(self):
        summary = {field: None for field in WorkoutSession.ROUTE_SUMMARY_FIELDS}
        summary.update(route_distance_km=4.2, splits=[])
        WorkoutSession.objects.filter(pk=self.session.pk).update(
            route_summary=summary, recorded_distance_km=Decimal("4.200")
        )
        response = self.patch(manual_distance_km="4.000")
        self.assertEqual(response.data["recorded_distance_km"], "4.200")
        self.session.refresh_from_db()
        self.assertEqual(self.session.route_summary["route_distance_km"], 4.2)

    def test_a_patch_writes_only_what_it_names(self):
        from rest_framework.test import APIRequestFactory

        from .serializers import WorkoutSessionSerializer

        # The serializer holds the row as it was read; a route upload then
        # commits underneath it, as it would mid-request.
        stale = WorkoutSession.objects.get(pk=self.session.pk)
        summary = {field: None for field in WorkoutSession.ROUTE_SUMMARY_FIELDS}
        summary.update(route_distance_km=2.0, splits=[])
        WorkoutSession.objects.filter(pk=self.session.pk).update(
            route_summary=summary, recorded_distance_km=Decimal("2.000")
        )
        request = APIRequestFactory().patch("/")
        request.user = self.profile.user
        serializer = WorkoutSessionSerializer(
            stale, data={"perceived_effort": 40}, partial=True, context={"request": request}
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()
        self.session.refresh_from_db()
        self.assertEqual(self.session.perceived_effort, 40)
        self.assertEqual(self.session.recorded_distance_km, Decimal("2.000"))
        self.assertEqual(self.session.route_summary["route_distance_km"], 2.0)

    def test_what_was_recorded_stays_read_only(self):
        before = (self.session.status, self.session.started_at, self.session.ended_at)
        response = self.patch(
            status="active", started_at=timezone.now().isoformat(), is_manual_entry=True,
            recorded_distance_km="99.000", activity="tennis", perceived_effort=10,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.session.refresh_from_db()
        self.assertEqual((self.session.status, self.session.started_at, self.session.ended_at), before)
        self.assertFalse(self.session.is_manual_entry)
        self.assertIsNone(self.session.recorded_distance_km)
        self.assertEqual(self.session.workout.activity, "boxing")

    def test_a_live_session_takes_them_too(self):
        live = WorkoutSession.objects.create(
            repbase_user=self.profile, workout=self.running,
            status=WorkoutSession.Status.ACTIVE, started_at=timezone.now(),
        )
        response = self.patch(live, perceived_effort=30, manual_distance_km="1.500")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["recorded_distance_km"], "1.500")

    def test_a_new_session_may_carry_them(self):
        response = self.client.post(SESSIONS, {
            "workout": self.running.pk, "manual_distance_km": "2.000", "notes": "Treadmill",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["recorded_distance_km"], "2.000")

    def test_a_session_whose_workout_was_deleted_has_no_activity(self):
        orphan = WorkoutSession.objects.create(
            repbase_user=self.profile, status=WorkoutSession.Status.COMPLETED,
            started_at=timezone.now() - timedelta(hours=1), ended_at=timezone.now(),
        )
        response = self.client.get(f"{SESSIONS}{orphan.pk}/")
        self.assertIsNone(response.data["activity"])
        self.assertIsNone(response.data["workout_type"])
        self.assertEqual(self.patch(orphan, perceived_effort=20).status_code, 200)
        response = self.patch(orphan, activity_details={"rounds": 2})
        self.assertEqual(response.status_code, 400)
        self.assertIn("rounds", response.data["activity_details"])

    def test_someone_elses_session_is_not_found(self):
        self.as_user(self.other_token)
        self.assertEqual(self.patch(perceived_effort=10).status_code, 404)

    def test_the_session_list_filters_by_activity(self):
        WorkoutSession.objects.create(repbase_user=self.profile, workout=self.running,
                                      status=WorkoutSession.Status.COMPLETED)
        listed = self.client.get(f"{SESSIONS}?activity=boxing")
        self.assertEqual([row["id"] for row in listed.data["results"]], [self.session.pk])
        self.assertEqual(self.client.get(f"{SESSIONS}?activity=running").data["count"], 1)
        self.assertEqual(self.client.get(f"{SESSIONS}?activity=quidditch").data["count"], 0)
        self.assertEqual(self.client.get(f"{SESSIONS}?activity=").data["count"], 2)


# -------------------------------------------------------------- the log


@FAST_PASSWORDS
class ActivityLogTests(Authenticated):
    @classmethod
    def setUpTestData(cls):
        _, cls.profile, cls.token = make_account("logger")
        _, cls.other, cls.other_token = make_account("log-other")

    def setUp(self):
        self.as_user(self.token)
        self.started = (timezone.now() - timedelta(hours=3)).replace(microsecond=0)

    def body(self, **overrides):
        body = {
            "workout_name": "Sunday Tennis",
            "activity": "tennis",
            "started_at": self.started.isoformat(),
            "duration_seconds": 5400,
        }
        body.update(overrides)
        return {key: value for key, value in body.items() if value is not ...}

    def log(self, key=None, **overrides):
        extra = {"HTTP_IDEMPOTENCY_KEY": key} if key else {}
        return self.client.post(LOG, self.body(**overrides), format="json", **extra)

    def test_logging_by_name_makes_the_workout_and_a_finished_session(self):
        response = self.log(
            perceived_effort=70, manual_calories_kcal=600, notes=" Doubles ",
            activity_details={"session_type": "match"},
        )
        self.assertEqual(response.status_code, 201, response.data)
        workout = WorkoutTemplate.objects.get(owner=self.profile, name="Sunday Tennis")
        self.assertEqual((workout.activity, workout.workout_type), ("tennis", "lifting"))
        session = WorkoutSession.objects.get(pk=response.data["id"])
        self.assertEqual(session.workout, workout)
        self.assertEqual(session.status, WorkoutSession.Status.COMPLETED)
        self.assertTrue(session.is_manual_entry)
        self.assertEqual(session.started_at, self.started)
        self.assertEqual(session.ended_at, self.started + timedelta(seconds=5400))
        self.assertEqual(response.data["activity"], "tennis")
        self.assertEqual(response.data["workout_type"], "lifting")
        self.assertIs(response.data["is_manual_entry"], True)
        self.assertEqual(response.data["duration_seconds"], 5400.0)
        self.assertEqual(response.data["perceived_effort"], 70)
        self.assertEqual(response.data["notes"], "Doubles")
        self.assertEqual(response.data["activity_details"], {"session_type": "match"})
        # Nothing was planned, so nothing was copied in.
        self.assertFalse(SessionExercise.objects.filter(session=session).exists())

    def test_the_name_finds_an_existing_workout_ignoring_case(self):
        existing = WorkoutTemplate.objects.create(owner=self.profile, name="Sunday Tennis",
                                                  activity="tennis")
        response = self.log(workout_name="  sunday TENNIS ")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["workout"], existing.pk)
        self.assertEqual(WorkoutTemplate.objects.filter(owner=self.profile).count(), 1)

    def test_a_named_workout_of_another_activity_is_refused(self):
        WorkoutTemplate.objects.create(owner=self.profile, name="Sunday Tennis", activity="padel")
        response = self.log()
        self.assertEqual(response.status_code, 400)
        self.assertIn("padel", str(response.data["workout_name"]))
        self.assertFalse(WorkoutSession.objects.exists())

    def test_another_persons_workout_name_is_theirs(self):
        WorkoutTemplate.objects.create(owner=self.other, name="Sunday Tennis", activity="yoga")
        response = self.log()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(WorkoutSession.objects.get().workout.owner, self.profile)

    def test_logging_by_id(self):
        workout = WorkoutTemplate.objects.create(owner=self.profile, name="Lane swim",
                                                 activity="swimming", workout_type="swimming")
        response = self.log(workout=workout.pk, workout_name=..., activity="swimming",
                            manual_distance_km="1.500", activity_details={"pool_length_m": 25})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["workout"], workout.pk)
        self.assertEqual(response.data["manual_distance_km"], "1.500")
        self.assertEqual(response.data["recorded_distance_km"], "1.500")
        self.assertEqual(response.data["activity_details"], {"pool_length_m": 25})

    def test_an_id_of_another_activity_is_refused(self):
        workout = WorkoutTemplate.objects.create(owner=self.profile, name="Lane swim",
                                                 activity="swimming", workout_type="swimming")
        response = self.log(workout=workout.pk, activity="running")
        self.assertEqual(response.status_code, 400)
        self.assertIn("activity", response.data)

    def test_someone_elses_workout_reads_like_a_missing_one(self):
        theirs = WorkoutTemplate.objects.create(owner=self.other, name="Theirs", activity="tennis")
        borrowed = self.log(workout=theirs.pk)
        missing = self.log(workout=987654)
        self.assertEqual(borrowed.status_code, 400)
        self.assertEqual(borrowed.data, missing.data)
        self.assertIn("workout", borrowed.data)
        self.assertFalse(WorkoutSession.objects.exists())

    def test_a_workout_or_a_name_is_required(self):
        for overrides in ({"workout_name": ...}, {"workout_name": ""}, {"workout_name": None},
                          {"workout_name": "   "}):
            with self.subTest(overrides=overrides):
                response = self.log(**overrides)
                self.assertEqual(response.status_code, 400)
                self.assertIn("workout_name", response.data)

    def test_required_and_invalid_fields(self):
        for overrides, field in (
            ({"activity": ...}, "activity"),
            ({"activity": "quidditch"}, "activity"),
            ({"started_at": ...}, "started_at"),
            ({"duration_seconds": ...}, "duration_seconds"),
            ({"workout_name": "x" * 151}, "workout_name"),
            ({"perceived_effort": 101}, "perceived_effort"),
            ({"manual_calories_kcal": 0}, "manual_calories_kcal"),
            ({"manual_distance_km": "0"}, "manual_distance_km"),
            ({"notes": "x" * 2001}, "notes"),
            ({"activity_details": {"rounds": 3}}, "activity_details"),
            ({"activity_details": {"session_type": "league"}}, "activity_details"),
        ):
            with self.subTest(field=field, overrides=str(overrides)[:40]):
                response = self.log(**overrides)
                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn(field, response.data)
        self.assertFalse(WorkoutSession.objects.exists())
        self.assertFalse(WorkoutTemplate.objects.exists())

    def test_duration_bounds(self):
        for seconds in (0, 59, 86401):
            with self.subTest(seconds=seconds):
                response = self.log(duration_seconds=seconds,
                                    started_at=(timezone.now() - timedelta(days=2)).isoformat())
                self.assertEqual(response.status_code, 400)
                self.assertIn("duration_seconds", response.data)
        for seconds in (60, 86400):
            with self.subTest(seconds=seconds):
                response = self.log(duration_seconds=seconds,
                                    started_at=(timezone.now() - timedelta(days=2)).isoformat())
                self.assertEqual(response.status_code, 201, response.data)

    def test_a_session_ending_in_the_future_is_refused(self):
        response = self.log(started_at=(timezone.now() - timedelta(minutes=30)).isoformat(),
                            duration_seconds=3600)
        self.assertEqual(response.status_code, 400)
        self.assertIn("started_at", response.data)
        # A clock a few minutes fast is not the future.
        response = self.log(started_at=(timezone.now() - timedelta(minutes=56)).isoformat(),
                            duration_seconds=3600)
        self.assertEqual(response.status_code, 201, response.data)

    def test_a_scheduled_date_schedules_it_once(self):
        day = timezone.localdate() - timedelta(days=1)
        first = self.log(scheduled_date=day.isoformat())
        self.assertEqual(first.status_code, 201, first.data)
        second = self.log(scheduled_date=day.isoformat(),
                          started_at=(self.started - timedelta(hours=4)).isoformat())
        self.assertEqual(second.status_code, 201, second.data)
        schedules = WorkoutSchedule.objects.filter(owner=self.profile)
        self.assertEqual(schedules.count(), 1)
        self.assertEqual(schedules.get().scheduled_date, day)
        self.assertEqual(WorkoutSession.objects.count(), 2)

    def test_an_already_scheduled_day_is_reused(self):
        workout = WorkoutTemplate.objects.create(owner=self.profile, name="Sunday Tennis",
                                                 activity="tennis")
        day = timezone.localdate()
        planned = WorkoutSchedule.objects.create(owner=self.profile, workout=workout, scheduled_date=day)
        response = self.log(scheduled_date=day.isoformat())
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(list(WorkoutSchedule.objects.values_list("pk", flat=True)), [planned.pk])

    def test_the_days_planner_task_is_ticked(self):
        workout = WorkoutTemplate.objects.create(owner=self.profile, name="Sunday Tennis",
                                                 activity="tennis")
        probe = WorkoutSession(repbase_user=self.profile, started_at=self.started)
        task = PlannerEntry.objects.create(
            owner=self.profile, kind=PlannerEntry.Kind.TASK, title="Tennis",
            category="workout", scheduled_date=training_day_of(probe), workout=workout,
        )
        other_day = PlannerEntry.objects.create(
            owner=self.profile, kind=PlannerEntry.Kind.TASK, title="Tennis",
            category="workout", scheduled_date=training_day_of(probe) + timedelta(days=1),
            workout=workout,
        )
        response = self.log()
        self.assertEqual(response.status_code, 201, response.data)
        task.refresh_from_db()
        other_day.refresh_from_db()
        self.assertEqual(task.completed_at, self.started + timedelta(seconds=5400))
        self.assertIsNone(other_day.completed_at)

    def test_the_same_key_replays_the_same_session(self):
        key = str(uuid.uuid4())
        first = self.log(key=key)
        self.assertEqual(first.status_code, 201, first.data)
        again = self.log(key=key)
        self.assertEqual(again.status_code, 201)
        self.assertEqual(again.data["id"], first.data["id"])
        self.assertEqual(WorkoutSession.objects.count(), 1)
        # The same key with something else in it is a different save.
        changed = self.log(key=key, duration_seconds=600)
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(WorkoutSession.objects.count(), 1)
        # A new key is a new session, and so is no key at all.
        self.assertEqual(self.log(key=str(uuid.uuid4())).status_code, 201)
        self.assertEqual(self.log().status_code, 201)
        self.assertEqual(WorkoutSession.objects.count(), 3)

    def test_a_key_that_is_not_a_uuid_is_refused(self):
        response = self.log(key="not-a-uuid")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Idempotency-Key", response.data)

    def test_a_refused_log_leaves_its_key_unused(self):
        key = str(uuid.uuid4())
        self.assertEqual(self.log(key=key, activity="quidditch").status_code, 400)
        self.assertEqual(self.log(key=key).status_code, 201)

    def test_signed_out_is_refused(self):
        self.client.credentials()
        self.assertEqual(self.log().status_code, 401)

    def test_a_log_can_be_posted_and_the_post_says_what_it_was(self):
        session_id = self.log().data["id"]
        response = self.client.post("/api/v1/social/posts/",
                                    {"kind": "workout", "source_id": session_id}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["workout"]["activity"], "tennis")
        self.assertEqual(response.data["workout"]["workout_type"], "lifting")
        self.assertEqual(PostWorkout.objects.get().activity, "tennis")


# ------------------------------------------------------- counting training


@FAST_PASSWORDS
class TrainedSessionCountingTests(Authenticated):
    @classmethod
    def setUpTestData(cls):
        _, cls.profile, cls.token = make_account("counter")
        cls.squat = Exercise.objects.create(name="Squat")

    def setUp(self):
        self.as_user(self.token)

    def stats(self):
        response = self.client.get(STATS)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def last_performed(self, workout):
        return self.client.get(f"{WORKOUTS}{workout.pk}/").data["last_performed_at"]

    def finished(self, workout, *, minutes=60, ago=timedelta(hours=1), **fields):
        ended = timezone.now() - ago
        return WorkoutSession.objects.create(
            repbase_user=self.profile, workout=workout,
            status=WorkoutSession.Status.COMPLETED,
            started_at=ended - timedelta(minutes=minutes), ended_at=ended, **fields,
        )

    def test_a_logged_tennis_session_counts(self):
        response = self.client.post(LOG, {
            "workout_name": "Tennis", "activity": "tennis", "duration_seconds": 3600,
            "started_at": (timezone.now() - timedelta(hours=2)).isoformat(),
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.stats()["total_workouts"], 1)
        self.assertEqual(self.stats()["completed_this_week"], 1)
        workout = WorkoutTemplate.objects.get(name="Tennis")
        self.assertEqual(self.last_performed(workout), response.data["ended_at"])

    def test_a_strength_session_without_sets_still_does_not_count(self):
        lifting = WorkoutTemplate.objects.create(owner=self.profile, name="Legs")
        session = self.finished(lifting)
        self.assertEqual(self.stats()["total_workouts"], 0)
        self.assertIsNone(self.last_performed(lifting))
        # An empty set row is not a set either.
        line = SessionExercise.objects.create(session=session, exercise=self.squat, order=1)
        SetEntry.objects.create(session_exercise=line, set_number=1)
        self.assertEqual(self.stats()["total_workouts"], 0)
        self.assertIsNone(self.last_performed(lifting))
        SetEntry.objects.create(session_exercise=line, set_number=2, reps=5)
        self.assertEqual(self.stats()["total_workouts"], 1)
        self.assertIsNotNone(self.last_performed(lifting))

    def test_a_timed_activity_needs_time_or_distance(self):
        yoga = WorkoutTemplate.objects.create(owner=self.profile, name="Yoga", activity="yoga")
        self.finished(yoga, minutes=0)
        self.assertEqual(self.stats()["total_workouts"], 0)
        self.assertIsNone(self.last_performed(yoga))
        rowing = WorkoutTemplate.objects.create(owner=self.profile, name="Row", activity="rowing")
        self.finished(rowing, minutes=0, manual_distance_km=Decimal("2.000"))
        self.assertEqual(self.stats()["total_workouts"], 1)
        self.assertIsNotNone(self.last_performed(rowing))
        self.finished(yoga, minutes=45, ago=timedelta(hours=3))
        self.assertEqual(self.stats()["total_workouts"], 2)
        self.assertIsNotNone(self.last_performed(yoga))

    def test_runs_count_as_they_always_did(self):
        run = WorkoutTemplate.objects.create(owner=self.profile, name="Run", workout_type="running")
        self.finished(run, minutes=0, recorded_distance_km=Decimal("5.000"))
        self.assertEqual(self.stats()["total_workouts"], 1)
        self.assertIsNotNone(self.last_performed(run))

    def test_a_blank_activity_is_counted_by_its_type(self):
        run = WorkoutTemplate.objects.create(owner=self.profile, name="Run", workout_type="running")
        WorkoutTemplate.objects.filter(pk=run.pk).update(activity="")
        self.finished(run)
        self.assertEqual(self.stats()["total_workouts"], 1)
        self.assertIsNotNone(self.last_performed(run))

    def test_a_session_whose_workout_was_deleted_counts_by_its_sets(self):
        timed = self.finished(None)
        self.assertEqual(self.stats()["total_workouts"], 0)
        line = SessionExercise.objects.create(session=timed, exercise=self.squat, order=1)
        SetEntry.objects.create(session_exercise=line, set_number=1, weight_kg=Decimal("100"), reps=5)
        self.assertEqual(self.stats()["total_workouts"], 1)

    def test_last_performed_is_the_newest_that_counts(self):
        tennis = WorkoutTemplate.objects.create(owner=self.profile, name="Tennis", activity="tennis")
        counted = self.finished(tennis, ago=timedelta(days=2))
        self.finished(tennis, minutes=0, ago=timedelta(hours=1))
        self.assertEqual(self.last_performed(tennis), counted.ended_at.isoformat().replace("+00:00", "Z"))
        listed = self.client.get(WORKOUTS).data["results"]
        self.assertEqual(listed[0]["last_performed_at"], self.last_performed(tennis))


# --------------------------------------------------------- exercise library


def library(*exercises, version=1):
    return {
        "schema_version": version,
        "library_version": "test",
        "sources": [],
        "exercises": list(exercises),
    }


def entry(slug, name, **extra):
    values = {
        "slug": slug, "name": name, "aliases": [], "category": "strength",
        "equipment": "barbell", "primary_muscles": ["chest"], "secondary_muscles": [],
        "movement_pattern": "horizontal_push", "mechanic": "compound", "force": "push",
        "level": "beginner", "disciplines": ["powerlifting"], "family": slug,
        "instructions": ["Lie down.", "Press."],
    }
    values.update(extra)
    return values


@FAST_PASSWORDS
class ExerciseLibrarySyncTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        _, cls.profile, _ = make_account("library-reader")

    def test_it_inserts_shared_rows_with_everything_the_library_says(self):
        counts = sync_exercise_library(Exercise, library(
            entry("barbell_bench_press", "Barbell Bench Press", aliases=["Bench", "Flat Bench"],
                  primary_muscles=["chest", "triceps"], mechanic=None, force=None),
            entry("pull_up", "Pull-Up", primary_muscles=["lats"], equipment="bodyweight"),
        ))
        self.assertEqual(counts, {"created": 2, "adopted": 0, "updated": 0, "unchanged": 0})
        bench = Exercise.objects.get(slug="barbell_bench_press")
        self.assertIsNone(bench.created_by)
        self.assertEqual(bench.name, "Barbell Bench Press")
        self.assertEqual(bench.aliases, ["Bench", "Flat Bench"])
        self.assertEqual(bench.muscle_group, "Chest")
        self.assertEqual((bench.mechanic, bench.force), ("", ""))
        self.assertEqual(bench.instructions, ["Lie down.", "Press."])
        self.assertEqual(bench.search_text, "barbell bench press\nbench\nflat bench")
        self.assertEqual(bench.primary_muscle_index, "|chest|triceps|")
        self.assertEqual(Exercise.objects.get(slug="pull_up").muscle_group, "Back")

    def test_every_region(self):
        regions = {
            "chest": "Chest", "lats": "Back", "traps": "Back", "upper_back": "Back",
            "lower_back": "Back", "shoulders": "Shoulders", "biceps": "Arms",
            "triceps": "Arms", "forearms": "Arms", "abdominals": "Core", "obliques": "Core",
            "hip_flexors": "Core", "quadriceps": "Legs", "hamstrings": "Legs",
            "adductors": "Legs", "abductors": "Legs", "calves": "Legs", "tibialis": "Legs",
            "glutes": "Glutes", "neck": "Neck", "full_body": "Full Body",
        }
        sync_exercise_library(Exercise, library(*(
            entry(f"ex_{muscle}", f"Exercise {muscle}", primary_muscles=[muscle, "chest"])
            for muscle in regions
        )))
        for muscle, region in regions.items():
            with self.subTest(muscle=muscle):
                self.assertEqual(Exercise.objects.get(slug=f"ex_{muscle}").muscle_group, region)

    def test_a_second_run_changes_nothing(self):
        data = library(entry("squat", "Back Squat", primary_muscles=["quadriceps"]))
        sync_exercise_library(Exercise, data)
        before = Exercise.objects.values().get(slug="squat")
        counts = sync_exercise_library(Exercise, data)
        self.assertEqual(counts, {"created": 0, "adopted": 0, "updated": 0, "unchanged": 1})
        self.assertEqual(Exercise.objects.values().get(slug="squat"), before)
        self.assertEqual(Exercise.objects.count(), 1)

    def test_a_changed_entry_updates_its_row(self):
        sync_exercise_library(Exercise, library(entry("squat", "Back Squat")))
        row = Exercise.objects.get(slug="squat")
        counts = sync_exercise_library(Exercise, library(
            entry("squat", "Barbell Back Squat", aliases=["Squat"], primary_muscles=["glutes"])
        ))
        self.assertEqual(counts["updated"], 1)
        row.refresh_from_db()
        self.assertEqual((row.name, row.muscle_group), ("Barbell Back Squat", "Glutes"))
        self.assertEqual(row.search_text, "barbell back squat\nsquat")

    def test_an_unslugged_shared_row_of_the_same_name_is_adopted(self):
        old = Exercise.objects.create(name="bench press", muscle_group="Pecs")
        workout = WorkoutTemplate.objects.create(owner=self.profile, name="Push")
        session = WorkoutSession.objects.create(repbase_user=self.profile, workout=workout)
        SessionExercise.objects.create(session=session, exercise=old, order=1)
        counts = sync_exercise_library(Exercise, library(entry("bench_press", "Bench Press")))
        self.assertEqual(counts["adopted"], 1)
        self.assertEqual(Exercise.objects.count(), 1)
        old.refresh_from_db()
        self.assertEqual((old.slug, old.name, old.muscle_group), ("bench_press", "Bench Press", "Chest"))
        self.assertEqual(SessionExercise.objects.get().exercise_id, old.pk)
        # And from then on it is found by its slug, not adopted again.
        self.assertEqual(sync_exercise_library(Exercise, library(entry("bench_press", "Bench Press")))["unchanged"], 1)

    def test_an_unslugged_shared_row_named_by_an_alias_is_adopted(self):
        old = Exercise.objects.create(name="Lateral Raise", muscle_group="Shoulders")
        workout = WorkoutTemplate.objects.create(owner=self.profile, name="Push")
        session = WorkoutSession.objects.create(repbase_user=self.profile, workout=workout)
        SessionExercise.objects.create(session=session, exercise=old, order=1)
        counts = sync_exercise_library(Exercise, library(
            entry("lateral_raise_dumbbell", "Lateral Raise (Dumbbell)", aliases=["DB Lateral Raise", "lateral raise "],
                  primary_muscles=["shoulders"]),
        ))
        self.assertEqual(counts, {"created": 0, "adopted": 1, "updated": 0, "unchanged": 0})
        old.refresh_from_db()
        self.assertEqual((old.slug, old.name), ("lateral_raise_dumbbell", "Lateral Raise (Dumbbell)"))
        self.assertEqual(SessionExercise.objects.get().exercise_id, old.pk)
        self.assertEqual(Exercise.objects.count(), 1)

    def test_a_name_wins_over_an_alias(self):
        # "Lateral Raise" is one entry's name and, wrongly, another's alias:
        # the entry it names gets the row, whatever order they come in.
        old = Exercise.objects.create(name="Lateral Raise")
        sync_exercise_library(Exercise, library(
            entry("side_raise", "Side Raise", aliases=["Lateral Raise"]),
            entry("lateral_raise", "Lateral Raise"),
        ))
        old.refresh_from_db()
        self.assertEqual(old.slug, "lateral_raise")
        self.assertEqual(Exercise.objects.get(slug="side_raise").name, "Side Raise")

    def test_a_muscle_with_no_region_keeps_the_heading(self):
        Exercise.objects.create(name="Neck Thing", muscle_group="Neck stuff")
        sync_exercise_library(Exercise, library(entry("neck_thing", "Neck Thing", primary_muscles=["jaw"])))
        self.assertEqual(Exercise.objects.get(slug="neck_thing").muscle_group, "Neck stuff")
        sync_exercise_library(Exercise, library(entry("new_thing", "New Thing", primary_muscles=[])))
        self.assertEqual(Exercise.objects.get(slug="new_thing").muscle_group, "")

    def test_custom_exercises_are_never_touched(self):
        mine = Exercise.objects.create(name="Bench Press", muscle_group="Mine", created_by=self.profile)
        before = Exercise.objects.values().get(pk=mine.pk)
        sync_exercise_library(Exercise, library(entry("bench_press", "Bench Press")))
        self.assertEqual(Exercise.objects.values().get(pk=mine.pk), before)
        self.assertEqual(Exercise.objects.filter(created_by__isnull=True).count(), 1)

    def test_nothing_is_ever_deleted(self):
        sync_exercise_library(Exercise, library(entry("a", "A"), entry("b", "B")))
        Exercise.objects.create(name="Old shared")
        sync_exercise_library(Exercise, library(entry("a", "A")))
        self.assertEqual(Exercise.objects.count(), 3)

    def test_an_unknown_schema_version_is_refused(self):
        for version in (2, None, "1"):
            with self.subTest(version=version):
                with self.assertRaisesRegex(ExerciseLibraryError, "schema_version"):
                    sync_exercise_library(Exercise, library(entry("a", "A"), version=version))
        self.assertFalse(Exercise.objects.exists())

    def test_a_bad_entry_writes_nothing(self):
        for bad in (
            entry("b", "x" * 151),
            entry("b", ""),
            entry("", "B"),
            entry("b", "B", aliases="Bench"),
            entry("b", "B", equipment="x" * 51),
            entry("a", "A again"),
        ):
            with self.subTest(bad=str(bad)[:50]):
                with self.assertRaises(ExerciseLibraryError):
                    sync_exercise_library(Exercise, library(entry("a", "A"), bad))
                self.assertFalse(Exercise.objects.exists())

    def test_a_custom_rows_slug_is_a_clash_not_a_crash(self):
        Exercise.objects.create(name="Mine", slug="a", created_by=self.profile)
        with self.assertRaisesRegex(ExerciseLibraryError, "slug"):
            sync_exercise_library(Exercise, library(entry("a", "A")))

    def write(self, data):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        with handle:
            json.dump(data, handle)
        self.addCleanup(Path(handle.name).unlink, missing_ok=True)
        return Path(handle.name)

    def test_the_command(self):
        path = self.write(library(entry("deadlift", "Deadlift", primary_muscles=["hamstrings"])))
        out = StringIO()
        call_command("sync_exercise_library", "--file", str(path), stdout=out)
        self.assertEqual(json.loads(out.getvalue())["created"], 1)
        call_command("sync_exercise_library", "--file", str(path), stdout=out)
        self.assertEqual(Exercise.objects.get(slug="deadlift").muscle_group, "Legs")
        with self.assertRaisesRegex(CommandError, "schema_version"):
            call_command("sync_exercise_library", "--file", str(self.write(library(version=9))))
        with self.assertRaisesRegex(CommandError, "No exercise library"):
            call_command("sync_exercise_library", "--file", str(path.with_name("missing.json")))

    def test_the_migration_is_a_no_op_without_the_file(self):
        module = migration("0085_sync_exercise_library")
        with mock.patch.object(module, "DEFAULT_LIBRARY_PATH", Path("/nonexistent/library.json")):
            module.sync(django_apps, None)
        self.assertFalse(Exercise.objects.exists())

    def test_the_migration_applies_the_file_unless_switched_off(self):
        module = migration("0085_sync_exercise_library")
        path = self.write(library(entry("plank", "Plank", primary_muscles=["abdominals"])))
        with mock.patch.object(module, "DEFAULT_LIBRARY_PATH", path):
            with override_settings(EXERCISE_LIBRARY_SYNC_ON_MIGRATE=False):
                module.sync(django_apps, None)
            self.assertFalse(Exercise.objects.exists())
            with override_settings(EXERCISE_LIBRARY_SYNC_ON_MIGRATE=True):
                module.sync(django_apps, None)
        self.assertEqual(Exercise.objects.get(slug="plank").muscle_group, "Core")


@FAST_PASSWORDS
class ExerciseLibraryReadTests(Authenticated):
    @classmethod
    def setUpTestData(cls):
        _, cls.profile, cls.token = make_account("library-user")
        _, cls.other, _ = make_account("library-other")
        sync_exercise_library(Exercise, library(
            entry("barbell_bench_press", "Barbell Bench Press", aliases=["Flat Bench"],
                  primary_muscles=["chest", "triceps"]),
            entry("lat_pulldown", "Lat Pulldown", equipment="cable", category="strength",
                  movement_pattern="vertical_pull", primary_muscles=["lats"],
                  secondary_muscles=["biceps"]),
            entry("good_morning", "Good Morning", primary_muscles=["lower_back"],
                  movement_pattern="hinge", category="mobility"),
        ))
        cls.mine = Exercise.objects.create(name="Lat Pulldown", created_by=cls.profile)
        Exercise.objects.create(name="Flat Bench Hold", created_by=cls.other)

    def setUp(self):
        self.as_user(self.token)

    def names(self, query=""):
        response = self.client.get(f"{EXERCISES}{query}")
        self.assertEqual(response.status_code, 200, response.data)
        return sorted(row["name"] for row in response.data["results"])

    def test_search_matches_an_alias(self):
        self.assertEqual(self.names("?search=flat%20BENCH"), ["Barbell Bench Press"])
        self.assertEqual(self.names("?search=bench"), ["Barbell Bench Press"])
        self.assertEqual(self.names("?search=pulldown"), ["Lat Pulldown", "Lat Pulldown"])

    def test_each_filter(self):
        self.assertEqual(self.names("?equipment=cable"), ["Lat Pulldown"])
        self.assertEqual(self.names("?equipment=CABLE"), ["Lat Pulldown"])
        self.assertEqual(self.names("?category=mobility"), ["Good Morning"])
        self.assertEqual(self.names("?pattern=vertical_pull"), ["Lat Pulldown"])
        self.assertEqual(self.names("?pattern=vertical"), [])
        self.assertEqual(self.names("?muscle=chest"), ["Barbell Bench Press"])
        self.assertEqual(self.names("?muscle=Triceps"), ["Barbell Bench Press"])
        # Exactly one muscle of the list: not a substring of another, and
        # never a secondary muscle.
        self.assertEqual(self.names("?muscle=back"), [])
        self.assertEqual(self.names("?muscle=lower_back"), ["Good Morning"])
        self.assertEqual(self.names("?muscle=biceps"), [])
        self.assertEqual(self.names("?muscle=chest%7Ctriceps"), [])
        self.assertEqual(self.names("?equipment=barbell&muscle=chest&search=press"),
                         ["Barbell Bench Press"])
        self.assertEqual(self.names("?equipment="), self.names())

    def test_name_puts_the_readers_own_first(self):
        response = self.client.get(f"{EXERCISES}?name=lat%20pulldown")
        rows = response.data["results"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["id"], self.mine.pk)
        self.assertIsNone(rows[1]["created_by"])
        # Without one of their own, the shared row is the answer.
        self.assertEqual(
            [row["slug"] for row in self.client.get(f"{EXERCISES}?name=good%20morning").data["results"]],
            ["good_morning"],
        )

    def test_library_fields_are_in_the_response(self):
        row = self.client.get(f"{EXERCISES}?name=barbell%20bench%20press").data["results"][0]
        self.assertEqual(row["slug"], "barbell_bench_press")
        self.assertEqual(row["aliases"], ["Flat Bench"])
        self.assertEqual(row["primary_muscles"], ["chest", "triceps"])
        self.assertEqual(row["muscle_group"], "Chest")
        self.assertEqual(row["instructions"], ["Lie down.", "Press."])
        self.assertNotIn("search_text", row)
        self.assertNotIn("primary_muscle_index", row)

    def test_custom_exercises_carry_none_of_it_and_cannot_set_it(self):
        response = self.client.post(EXERCISES, {
            "name": "My Curl", "muscle_group": "Arms", "slug": "my_curl",
            "aliases": ["Curl"], "equipment": "dumbbell", "primary_muscles": ["biceps"],
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNone(response.data["slug"])
        self.assertEqual((response.data["aliases"], response.data["equipment"],
                          response.data["primary_muscles"]), ([], "", []))
        made = Exercise.objects.get(pk=response.data["id"])
        self.assertEqual(made.search_text, "my curl")
        # Still found by its own name, including after a rename.
        self.assertEqual(self.names("?search=my%20cu"), ["My Curl"])
        self.client.patch(f"{EXERCISES}{made.pk}/", {"name": "Hammer Curl"}, format="json")
        self.assertEqual(self.names("?search=hammer"), ["Hammer Curl"])


# -------------------------------------------------------------- the contract


class NewResponseFieldsAreOptionalTests(SimpleTestCase):
    """Each field added here must be absent from its component's `required`.

    The generated Swift client makes a required property non-optional and
    refuses a response without it, so documenting one of these as required
    would break the newer app against a server deployed before it. And none
    of the activity fields may be an enum, which the Swift client decodes
    strictly: the list of activities must be able to grow.
    """

    ADDED = {
        "WorkoutTemplate": ["activity"],
        "WorkoutSession": [
            "activity", "perceived_effort", "manual_calories_kcal", "manual_distance_km",
            "notes", "activity_details", "is_manual_entry",
        ],
        "PostWorkout": ["activity"],
        "Exercise": [
            "slug", "aliases", "category", "equipment", "movement_pattern", "level",
            "mechanic", "force", "family", "primary_muscles", "secondary_muscles",
            "disciplines", "instructions",
        ],
        "WorkoutTemplateRequest": ["activity"],
        "PatchedWorkoutTemplateRequest": ["activity"],
        "WorkoutSessionRequest": [
            "perceived_effort", "manual_calories_kcal", "manual_distance_km", "notes",
            "activity_details",
        ],
        "PatchedWorkoutSessionRequest": [
            "perceived_effort", "manual_calories_kcal", "manual_distance_km", "notes",
            "activity_details",
        ],
    }

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from drf_spectacular.generators import SchemaGenerator

        cls.schema = SchemaGenerator().get_schema(request=None, public=True)
        cls.components = cls.schema["components"]["schemas"]

    def test_added_fields_are_documented_and_optional(self):
        for component, fields in self.ADDED.items():
            for field in fields:
                with self.subTest(component=component, field=field):
                    self.assertIn(field, self.components[component]["properties"])
                    self.assertNotIn(field, self.components[component].get("required", []))
        # Still required where they always were.
        self.assertIn("workout_type", self.components["WorkoutSession"]["required"])
        self.assertIn("name", self.components["Exercise"]["required"])

    def test_activities_are_plain_strings(self):
        for component in ("WorkoutTemplate", "WorkoutSession", "PostWorkout", "ActivityLogRequest"):
            with self.subTest(component=component):
                schema = self.components[component]["properties"]["activity"]
                self.assertEqual(schema.get("type"), "string")
                self.assertNotIn("enum", schema)
                self.assertNotIn("$ref", schema)
        self.assertEqual(
            self.components["WorkoutTypeEnum"]["enum"], ["lifting", "running", "biking", "swimming"]
        )

    def test_the_log_endpoint(self):
        operation = self.schema["paths"]["/api/v1/sessions/log/"]["post"]
        self.assertEqual(operation["operationId"], "sessions_log_create")
        self.assertEqual(
            operation["requestBody"]["content"]["application/json"]["schema"],
            {"$ref": "#/components/schemas/ActivityLogRequest"},
        )
        self.assertEqual(
            operation["responses"]["201"]["content"]["application/json"]["schema"],
            {"$ref": "#/components/schemas/WorkoutSession"},
        )
        self.assertIn("Idempotency-Key", [p["name"] for p in operation["parameters"]])
        self.assertEqual(
            sorted(self.components["ActivityLogRequest"]["required"]),
            ["activity", "duration_seconds", "started_at"],
        )
