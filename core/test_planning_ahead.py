"""Planning up to a week ahead, and saving every activity.

The app plans as far as seven days from today: the rest of this week, then
next week's days from the plan's week switch. Every activity is planned through
the same two requests -- a new workout created with its first date, or a saved
one put on a date -- and logged after the fact through a third. These pin that
each of the 51 activities saves all three ways, that every day of the coming
week is accepted in the zones furthest either side of UTC, and that a date
comes back exactly as it was sent.
"""

from datetime import timedelta

from django.utils import timezone
from rest_framework.test import APITestCase

from .activities import ACTIVITIES
from .fixtures_for_tests import make_account
from .models import WorkoutSchedule, WorkoutSession, WorkoutTemplate, today_for, week_start_for

WORKOUTS = "/api/v1/workouts/"
SCHEDULES = "/api/v1/schedules/"
PLAN_WEEK = "/api/v1/schedules/plan-week/"
RECURRENCES = "/api/v1/recurrences/"
LOG = "/api/v1/sessions/log/"
SESSIONS = "/api/v1/sessions/"


class PlannedAhead(APITestCase):
    zone = "UTC"

    @classmethod
    def setUpTestData(cls):
        _, cls.profile, cls.token = make_account(f"ahead-{cls.zone.replace('/', '-').lower()}")
        cls.profile.time_zone = cls.zone
        cls.profile.save(update_fields=["time_zone"])

    def setUp(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")
        self.today = today_for(self.profile)

    def day(self, offset):
        return (self.today + timedelta(days=offset)).isoformat()

    def create(self, name, activity="weightlifting", initial_date=None, key=None):
        body = {"name": name, "workout_type": "lifting", "activity": activity, "initial_exercises": []}
        if initial_date is not None:
            body["initial_date"] = initial_date
        extra = {"HTTP_IDEMPOTENCY_KEY": key} if key else {}
        return self.client.post(WORKOUTS, body, format="json", **extra)


class EveryActivitySavesTests(PlannedAhead):
    def test_every_activity_can_be_planned_on_each_day_of_the_coming_week(self):
        self.assertEqual(len(ACTIVITIES), 51)
        for index, slug in enumerate(ACTIVITIES):
            offset = index % 8
            with self.subTest(activity=slug, offset=offset):
                response = self.create(f"{slug} plan", activity=slug, initial_date=self.day(offset))
                self.assertEqual(response.status_code, 201, response.data)
                self.assertEqual(response.data["activity"], slug)
                self.assertEqual(response.data["workout_type"], ACTIVITIES[slug].family)
                schedule = WorkoutSchedule.objects.get(workout_id=response.data["id"])
                self.assertEqual(schedule.scheduled_date.isoformat(), self.day(offset))
        listed = self.client.get(SCHEDULES, {"page_size": 100})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.data["count"], 51)

    def test_every_activity_can_be_logged_after_the_fact(self):
        started = (timezone.now() - timedelta(hours=3)).replace(microsecond=0)
        for slug in ACTIVITIES:
            with self.subTest(activity=slug):
                response = self.client.post(LOG, {
                    "workout_name": f"{slug} log",
                    "activity": slug,
                    "scheduled_date": self.day(0),
                    "started_at": started.isoformat(),
                    "duration_seconds": 2700,
                    "perceived_effort": 41,
                    "manual_calories_kcal": 5,
                    "notes": "Fine",
                }, format="json")
                self.assertEqual(response.status_code, 201, response.data)
                # Read back as a separate request: what was saved, not what
                # the create happened to answer.
                saved = self.client.get(f"{SESSIONS}{response.data['id']}/")
                self.assertEqual(saved.status_code, 200)
                self.assertEqual(saved.data["activity"], slug)
                self.assertEqual(saved.data["status"], WorkoutSession.Status.COMPLETED)
                self.assertEqual(saved.data["duration_seconds"], 2700.0)
                self.assertEqual(saved.data["perceived_effort"], 41)
                self.assertEqual(saved.data["manual_calories_kcal"], 5)
                self.assertEqual(saved.data["notes"], "Fine")
        self.assertEqual(WorkoutSession.objects.filter(repbase_user=self.profile).count(), 51)

    def test_a_saved_workout_of_every_activity_can_be_put_on_a_day(self):
        for index, slug in enumerate(ACTIVITIES):
            workout = WorkoutTemplate.objects.create(owner=self.profile, name=f"{slug} saved", activity=slug)
            with self.subTest(activity=slug):
                response = self.client.post(
                    SCHEDULES, {"workout": workout.pk, "scheduled_date": self.day(index % 8)}, format="json"
                )
                self.assertEqual(response.status_code, 201, response.data)
                self.assertEqual(response.data["scheduled_date"], self.day(index % 8))


class ComingWeekTests(PlannedAhead):
    """Today, tomorrow -- under, at and over 24 hours away -- several days,
    and a week. A schedule is a calendar date, so none of those is a boundary;
    only the server's own horizon, a year, is."""

    def test_each_day_from_today_to_a_week_ahead(self):
        workout = WorkoutTemplate.objects.create(owner=self.profile, name="Every day", activity="running")
        for offset in range(8):
            with self.subTest(offset=offset):
                response = self.client.post(
                    SCHEDULES, {"workout": workout.pk, "scheduled_date": self.day(offset)}, format="json"
                )
                self.assertEqual(response.status_code, 201, response.data)
                self.assertEqual(response.data["scheduled_date"], self.day(offset))
        dates = sorted(
            row["scheduled_date"] for row in self.client.get(SCHEDULES, {"page_size": 100}).data["results"]
        )
        self.assertEqual(dates, [self.day(offset) for offset in range(8)])

    def test_new_workouts_planned_ahead_keep_their_dates(self):
        for offset in (0, 1, 2, 5, 7):
            with self.subTest(offset=offset):
                response = self.create(f"Ahead {offset}", activity="tennis", initial_date=self.day(offset))
                self.assertEqual(response.status_code, 201, response.data)
                rows = self.client.get(SCHEDULES, {"page_size": 100}).data["results"]
                mine = [row for row in rows if row["workout"] == response.data["id"]]
                self.assertEqual([row["scheduled_date"] for row in mine], [self.day(offset)])

    def test_next_week_fills_in_from_weekly_repeats(self):
        workout = WorkoutTemplate.objects.create(owner=self.profile, name="Monday legs", activity="weightlifting")
        repeat = self.client.post(RECURRENCES, {"workout": workout.pk, "weekday": 0}, format="json")
        self.assertEqual(repeat.status_code, 201, repeat.data)
        next_monday = week_start_for(self.today) + timedelta(days=7)
        planned = self.client.post(PLAN_WEEK, {"start": next_monday.isoformat()}, format="json")
        self.assertEqual(planned.status_code, 200, planned.data)
        self.assertIn(next_monday.isoformat(), [row["scheduled_date"] for row in planned.data])
        # Asking again adds nothing: the app asks on every load.
        again = self.client.post(PLAN_WEEK, {"start": next_monday.isoformat()}, format="json")
        self.assertEqual(len(again.data), len(planned.data))
        self.assertEqual(
            WorkoutSchedule.objects.filter(workout=workout, scheduled_date=next_monday).count(), 1
        )

    def test_one_save_key_saves_one_workout(self):
        """Why the app gives every new workout a fresh draft: the draft's id is
        the key, and a key that saved one workout refuses a different one."""
        key = "6f1c1a37-7a1e-4d0f-9b77-2b8d1b0d6a11"
        first = self.create("Twice A", activity="tennis", initial_date=self.day(1), key=key)
        self.assertEqual(first.status_code, 201, first.data)
        replay = self.create("Twice A", activity="tennis", initial_date=self.day(1), key=key)
        self.assertEqual((replay.status_code, replay.data["id"]), (201, first.data["id"]))
        reused = self.create("Twice B", activity="tennis", initial_date=self.day(2), key=key)
        self.assertEqual(reused.status_code, 409)
        fresh = self.create("Twice B", activity="tennis", initial_date=self.day(2),
                            key="0a6a3a52-3f0d-4c1e-8f3c-55a1c1d2e3f4")
        self.assertEqual(fresh.status_code, 201, fresh.data)
        self.assertEqual(WorkoutTemplate.objects.filter(owner=self.profile).count(), 2)


class ComingWeekFarEastTests(ComingWeekTests):
    """UTC+14, where today began while it was still yesterday in London."""

    zone = "Pacific/Kiritimati"


class ComingWeekFarWestTests(ComingWeekTests):
    """UTC-11, the last place it is still today."""

    zone = "Pacific/Pago_Pago"


class ComingWeekAcrossTheClockChangeTests(ComingWeekTests):
    """New York, whose clocks went back on 1 November 2026."""

    zone = "America/New_York"
