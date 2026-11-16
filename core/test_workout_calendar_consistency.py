"""Home, Training and the calendar read one plan.

Training and Home's workout card read `WorkoutSchedule`; the calendar and
Home's agenda read the `PlannerEntry` task each scheduled day is given. These
tests hold the two together from both ends: whatever is done to a workout on
one screen is what every other screen reads back, and reading again -- a
relaunch, a resync, a fresh week -- never brings back what was removed.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone as datetime_timezone
from importlib import import_module
from threading import Barrier
from unittest import mock, skipUnless

from django.apps import apps
from django.db import close_old_connections, connection
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase, APITransactionTestCase

from .models import (
    PlannerEntry,
    WorkoutRecurrence,
    WorkoutSchedule,
    WorkoutSession,
    WorkoutTemplate,
)
from .tests import RepbaseAPITestMixin


class WorkoutCalendarConsistencyTests(RepbaseAPITestMixin, APITestCase):
    SCHEDULES = "/api/v1/schedules/"
    PLANNER = "/api/v1/planner/"
    SYNC = "/api/v1/schedules/sync-planner/"

    def setUp(self):
        # A Wednesday, so a week has days on both sides of today.
        self.now = datetime(2026, 10, 28, 12, tzinfo=datetime_timezone.utc)
        clock = mock.patch("django.utils.timezone.now", return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.today = self.now.date()
        self.user, self.profile, self.token = self.create_account("calendar-sync")
        self.authenticate(self.token)
        self.push = WorkoutTemplate.objects.create(owner=self.profile, name="Push", workout_type="lifting")
        self.pull = WorkoutTemplate.objects.create(owner=self.profile, name="Pull", workout_type="lifting")

    # -- What the app does --------------------------------------------------

    def day(self, offset):
        return self.today + timedelta(days=offset)

    def plan(self, offset=0, workout=None):
        """Plans a workout from the Workout page."""
        response = self.client.post(
            self.SCHEDULES,
            {"workout": (workout or self.push).pk, "scheduled_date": str(self.day(offset))},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        return WorkoutSchedule.objects.get(pk=response.data["id"])

    def open_calendar(self, first=-7, last=14):
        """What the calendar does on every read: sync the range, then list it."""
        start, end = str(self.day(first)), str(self.day(last))
        synced = self.client.post(self.SYNC, {"start": start, "end": end}, format="json")
        self.assertEqual(synced.status_code, 200, synced.data)
        listed = self.client.get(self.PLANNER, {"start": start, "end": end})
        self.assertEqual(listed.status_code, 200, listed.data)
        return listed.data["results"]

    def calendar_workouts(self, **window):
        return sorted(
            (entry["scheduled_date"], entry["title"])
            for entry in self.open_calendar(**window)
            if entry["category"] == "workout"
        )

    def training_week(self, first=-7, last=14):
        """What Training and Home's workout card read."""
        response = self.client.get(
            self.SCHEDULES, {"start": str(self.day(first)), "end": str(self.day(last))}
        )
        self.assertEqual(response.status_code, 200, response.data)
        return sorted((row["scheduled_date"], row["workout_name"]) for row in response.data["results"])

    def task_for(self, schedule):
        self.open_calendar()
        return PlannerEntry.objects.get(source_schedule=schedule)

    def legacy_task(self, offset, workout=None, **fields):
        """A task written before tasks were linked to the day they stand for.

        The app used to post these itself, and the first sync endpoint adopted
        them without a link, so every account from before 1.1 has them.
        """
        workout = workout or self.push
        return PlannerEntry.objects.create(
            owner=self.profile, kind="task", category="workout", title=workout.name,
            scheduled_date=self.day(offset), workout=workout, **fields,
        )

    def assertConsistent(self, expected):
        """Training and the calendar agree, and keep agreeing after a reread."""
        self.assertEqual(self.training_week(), expected)
        self.assertEqual(self.calendar_workouts(), expected)
        # A relaunch is another sync over the same range.
        self.assertEqual(self.calendar_workouts(), expected)
        self.assertEqual(self.training_week(), expected)

    # -- Deleting from the Workout page -------------------------------------

    def test_removing_a_day_clears_its_calendar_task(self):
        removed = self.plan(1)
        self.plan(2)
        self.task_for(removed)
        self.assertEqual(self.client.delete(f"{self.SCHEDULES}{removed.pk}/").status_code, 204)
        self.assertConsistent([(str(self.day(2)), "Push")])

    def test_removing_a_day_clears_a_task_given_a_time_notes_or_a_new_title(self):
        changes = ({"scheduled_time": "17:30:00"}, {"notes": "Go light"}, {"title": "Chest day"})
        for offset, change in enumerate(changes, start=1):
            with self.subTest(change=change):
                schedule = self.plan(offset)
                task = self.task_for(schedule)
                self.assertEqual(self.client.patch(f"{self.PLANNER}{task.pk}/", change, format="json").status_code, 200)
                self.assertEqual(self.client.delete(f"{self.SCHEDULES}{schedule.pk}/").status_code, 204)
                self.assertFalse(PlannerEntry.objects.filter(pk=task.pk).exists())
        self.assertConsistent([])

    def test_removing_a_day_clears_a_task_with_steps(self):
        schedule = self.plan(1)
        task = self.task_for(schedule)
        step = self.client.post(
            self.PLANNER,
            {"title": "Warm up", "kind": "task", "category": "workout", "scheduled_date": str(self.day(1)), "parent": task.pk},
            format="json",
        )
        self.assertEqual(step.status_code, 201, step.data)
        self.assertEqual(self.client.delete(f"{self.SCHEDULES}{schedule.pk}/").status_code, 204)
        self.assertFalse(PlannerEntry.objects.filter(pk__in=[task.pk, step.data["id"]]).exists())
        self.assertConsistent([])

    def test_removing_a_day_clears_a_task_written_before_tasks_were_linked(self):
        legacy = self.legacy_task(1)
        schedule = WorkoutSchedule.objects.create(
            owner=self.profile, workout=self.push, scheduled_date=self.day(1), planner_synced_at=self.now
        )
        self.assertEqual(self.client.delete(f"{self.SCHEDULES}{schedule.pk}/").status_code, 204)
        self.assertFalse(PlannerEntry.objects.filter(pk=legacy.pk).exists())
        self.assertConsistent([])

    def test_a_trained_day_stays_on_the_calendar_as_a_record(self):
        schedule = self.plan(-1)
        task = self.task_for(schedule)
        task.completed_at = self.now
        task.save(update_fields=["completed_at"])
        self.assertEqual(self.client.delete(f"{self.SCHEDULES}{schedule.pk}/").status_code, 204)
        task.refresh_from_db()
        self.assertIsNone(task.source_schedule_id)
        self.assertEqual(task.completed_at, self.now)

    def test_deleting_a_saved_workout_clears_every_unfinished_task_for_it(self):
        linked = self.task_for(self.plan(1))
        customized = self.task_for(self.plan(2))
        self.client.patch(f"{self.PLANNER}{customized.pk}/", {"scheduled_time": "07:00:00"}, format="json")
        legacy = self.legacy_task(3)
        WorkoutSchedule.objects.create(owner=self.profile, workout=self.push, scheduled_date=self.day(3), planner_synced_at=self.now)
        trained = self.task_for(self.plan(-2))
        trained.completed_at = self.now
        trained.save(update_fields=["completed_at"])
        kept = self.plan(1, workout=self.pull)

        self.assertEqual(self.client.delete(f"/api/v1/workouts/{self.push.pk}/").status_code, 204)

        self.assertFalse(PlannerEntry.objects.filter(pk__in=[linked.pk, customized.pk, legacy.pk]).exists())
        trained.refresh_from_db()
        self.assertEqual(trained.completed_at, self.now)
        self.assertIsNone(trained.workout_id)
        self.assertEqual(self.training_week(), [(str(kept.scheduled_date), "Pull")])
        self.assertEqual(
            [(d, t) for d, t in self.calendar_workouts() if d >= str(self.today)],
            [(str(kept.scheduled_date), "Pull")],
        )

    def test_clearing_the_schedule_clears_tasks_written_before_tasks_were_linked(self):
        self.legacy_task(1)
        WorkoutSchedule.objects.create(owner=self.profile, workout=self.push, scheduled_date=self.day(1), planner_synced_at=self.now)
        self.task_for(self.plan(2))
        self.assertEqual(self.client.post(f"{self.SCHEDULES}clear/", {}, format="json").status_code, 200)
        self.assertConsistent([])

    def test_ending_a_weekly_repeat_clears_its_future_days_everywhere(self):
        rule = self.client.post("/api/v1/recurrences/", {"workout": self.push.pk, "weekday": self.day(1).weekday()}, format="json")
        self.assertEqual(rule.status_code, 201, rule.data)
        next_week = self.client.post(f"{self.SCHEDULES}plan-week/", {"start": str(self.day(7))}, format="json")
        self.assertEqual(next_week.status_code, 200, next_week.data)
        self.assertEqual(len(next_week.data), 1)
        planned = WorkoutSchedule.objects.get(pk=next_week.data[0]["id"])
        task = self.task_for(planned)
        task.scheduled_time = "06:30"
        task.save(update_fields=["scheduled_time"])
        self.assertEqual(self.client.delete(f"/api/v1/recurrences/{rule.data['id']}/").status_code, 204)
        self.assertFalse(PlannerEntry.objects.filter(pk=task.pk).exists())
        # This week's day stays, by design, on both: it is in front of the user.
        self.assertConsistent([(str(self.day(1)), "Push")])

    # -- Changing a workout from the calendar -------------------------------

    def test_deleting_a_workout_from_the_calendar_takes_it_off_training(self):
        schedule = self.plan(1)
        self.plan(2, workout=self.pull)
        task = self.task_for(schedule)
        self.assertEqual(self.client.delete(f"{self.PLANNER}{task.pk}/").status_code, 204)
        self.assertFalse(WorkoutSchedule.objects.filter(pk=schedule.pk).exists())
        self.assertConsistent([(str(self.day(2)), "Pull")])

    def test_a_repeating_day_deleted_from_the_calendar_does_not_come_back(self):
        rule = self.client.post("/api/v1/recurrences/", {"workout": self.push.pk, "weekday": self.day(1).weekday()}, format="json")
        self.assertEqual(rule.status_code, 201, rule.data)
        tomorrow = WorkoutSchedule.objects.get(owner=self.profile, scheduled_date=self.day(1))
        task = self.task_for(tomorrow)
        self.assertEqual(self.client.delete(f"{self.PLANNER}{task.pk}/").status_code, 204)
        for _ in range(2):
            response = self.client.post(f"{self.SCHEDULES}plan-week/", {"start": str(self.today)}, format="json")
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(response.data, [])
        self.assertNotIn(str(self.day(1)), [d for d, _ in self.calendar_workouts()])

    def test_moving_a_workout_on_the_calendar_moves_it_on_training(self):
        schedule = self.plan(1)
        task = self.task_for(schedule)
        response = self.client.patch(f"{self.PLANNER}{task.pk}/", {"scheduled_date": str(self.day(3))}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        schedule.refresh_from_db()
        self.assertEqual(schedule.scheduled_date, self.day(3))
        self.assertConsistent([(str(self.day(3)), "Push")])
        self.assertEqual(PlannerEntry.objects.get(pk=task.pk).source_schedule_id, schedule.pk)

    def test_moving_onto_a_day_the_workout_is_already_planned_merges_the_two(self):
        moved = self.plan(1)
        task = self.task_for(moved)
        # Planned for the later day too, but not read by a calendar yet.
        waiting = WorkoutSchedule.objects.create(owner=self.profile, workout=self.push, scheduled_date=self.day(20))
        response = self.client.patch(f"{self.PLANNER}{task.pk}/", {"scheduled_date": str(self.day(20))}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(WorkoutSchedule.objects.filter(pk=moved.pk).exists())
        self.assertEqual(PlannerEntry.objects.get(pk=task.pk).source_schedule_id, waiting.pk)
        self.assertEqual(self.training_week(last=30), [(str(self.day(20)), "Push")])
        self.assertEqual(self.calendar_workouts(last=30), [(str(self.day(20)), "Push")])

    def test_choosing_another_workout_on_the_calendar_changes_the_plan(self):
        schedule = self.plan(1)
        task = self.task_for(schedule)
        response = self.client.patch(f"{self.PLANNER}{task.pk}/", {"workout": self.pull.pk, "title": "Pull"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        schedule.refresh_from_db()
        self.assertEqual(schedule.workout_id, self.pull.pk)
        self.assertConsistent([(str(self.day(1)), "Pull")])

    def test_making_a_workout_task_an_ordinary_task_takes_it_off_training(self):
        schedule = self.plan(1)
        task = self.task_for(schedule)
        response = self.client.patch(f"{self.PLANNER}{task.pk}/", {"category": "other", "workout": None}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(WorkoutSchedule.objects.filter(pk=schedule.pk).exists())
        self.assertTrue(PlannerEntry.objects.filter(pk=task.pk, source_schedule__isnull=True).exists())
        self.assertConsistent([])

    def test_adding_a_workout_on_the_calendar_plans_it_on_training(self):
        response = self.client.post(
            self.PLANNER,
            {"title": "Push", "kind": "task", "category": "workout", "scheduled_date": str(self.day(2)), "workout": self.push.pk},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        schedule = WorkoutSchedule.objects.get(owner=self.profile, workout=self.push, scheduled_date=self.day(2))
        self.assertEqual(PlannerEntry.objects.get(pk=response.data["id"]).source_schedule_id, schedule.pk)
        self.assertConsistent([(str(self.day(2)), "Push")])

    def test_adding_a_workout_task_where_it_is_already_planned_joins_that_plan(self):
        schedule = WorkoutSchedule.objects.create(owner=self.profile, workout=self.push, scheduled_date=self.day(2))
        response = self.client.post(
            self.PLANNER,
            {"title": "Push", "kind": "task", "category": "workout", "scheduled_date": str(self.day(2)), "workout": self.push.pk},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(PlannerEntry.objects.get(pk=response.data["id"]).source_schedule_id, schedule.pk)
        self.assertConsistent([(str(self.day(2)), "Push")])

    def test_a_retried_calendar_save_plans_the_workout_once(self):
        body = {"title": "Push", "kind": "task", "category": "workout", "scheduled_date": str(self.day(2)), "workout": self.push.pk}
        key = "1b671a64-40d5-491e-99b0-da01ff1e3341"
        for _ in range(2):
            response = self.client.post(self.PLANNER, body, format="json", HTTP_IDEMPOTENCY_KEY=key)
            self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(WorkoutSchedule.objects.filter(owner=self.profile).count(), 1)
        self.assertConsistent([(str(self.day(2)), "Push")])

    def test_an_event_naming_a_workout_plans_nothing(self):
        response = self.client.post(
            self.PLANNER,
            {"title": "Push class", "kind": "event", "category": "social", "scheduled_date": str(self.day(2)),
             "scheduled_time": "18:00:00", "workout": self.push.pk},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertFalse(WorkoutSchedule.objects.filter(owner=self.profile).exists())

    # -- Editing a workout --------------------------------------------------

    def test_renaming_a_workout_retitles_its_calendar_tasks(self):
        timed = self.task_for(self.plan(1))
        self.client.patch(f"{self.PLANNER}{timed.pk}/", {"scheduled_time": "07:00:00"}, format="json")
        retitled = self.task_for(self.plan(2))
        self.client.patch(f"{self.PLANNER}{retitled.pk}/", {"title": "Chest day"}, format="json")
        response = self.client.patch(f"/api/v1/workouts/{self.push.pk}/", {"name": "Push A"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        timed.refresh_from_db()
        retitled.refresh_from_db()
        self.assertEqual(timed.title, "Push A")
        self.assertEqual(retitled.title, "Chest day")
        self.assertEqual(self.training_week(), [(str(self.day(1)), "Push A"), (str(self.day(2)), "Push A")])

    # -- Reading again ------------------------------------------------------

    def test_a_day_still_ahead_gets_back_a_task_it_lost(self):
        # Removed from the calendar before deleting a calendar task also
        # unplanned the workout: Training still has it, so the calendar must.
        WorkoutSchedule.objects.create(owner=self.profile, workout=self.push, scheduled_date=self.day(1), planner_synced_at=self.now)
        self.assertConsistent([(str(self.day(1)), "Push")])

    def test_a_past_day_whose_task_was_removed_is_left_alone(self):
        WorkoutSchedule.objects.create(owner=self.profile, workout=self.push, scheduled_date=self.day(-1), planner_synced_at=self.now)
        self.assertEqual(self.calendar_workouts(), [])

    def test_a_task_written_before_linking_is_adopted_rather_than_duplicated(self):
        legacy = self.legacy_task(1)
        schedule = WorkoutSchedule.objects.create(owner=self.profile, workout=self.push, scheduled_date=self.day(1), planner_synced_at=self.now)
        self.assertConsistent([(str(self.day(1)), "Push")])
        legacy.refresh_from_db()
        self.assertEqual(legacy.source_schedule_id, schedule.pk)

    def test_another_account_is_never_planned_or_unplanned(self):
        _, other, _ = self.create_account("calendar-bystander")
        theirs = WorkoutTemplate.objects.create(owner=other, name="Theirs", workout_type="lifting")
        their_day = WorkoutSchedule.objects.create(owner=other, workout=theirs, scheduled_date=self.day(1))
        refused = self.client.post(
            self.PLANNER,
            {"title": "Theirs", "kind": "task", "category": "workout", "scheduled_date": str(self.day(1)), "workout": theirs.pk},
            format="json",
        )
        self.assertEqual(refused.status_code, 400)
        mine = self.task_for(self.plan(1))
        self.assertEqual(self.client.delete(f"{self.PLANNER}{mine.pk}/").status_code, 204)
        self.assertTrue(WorkoutSchedule.objects.filter(pk=their_day.pk).exists())



@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locks")
class WorkoutCalendarConcurrencyTests(RepbaseAPITestMixin, APITransactionTestCase):
    """Calendar writes lock schedules before tasks, like every schedule write."""

    def race(self, token, first, second):
        barrier = Barrier(2)

        def run(send):
            close_old_connections()
            try:
                client = APIClient()
                client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
                barrier.wait(timeout=10)
                return send(client).status_code
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run, first), pool.submit(run, second)]
            return [future.result(timeout=20) for future in futures]

    def planned_day(self, name):
        _, profile, token = self.create_account(name)
        workout = WorkoutTemplate.objects.create(owner=profile, name="Push", workout_type="lifting")
        day = timezone.now().date() + timedelta(days=1)
        schedule = WorkoutSchedule.objects.create(owner=profile, workout=workout, scheduled_date=day)
        task = PlannerEntry.objects.create(
            owner=profile, kind="task", category="workout", title="Push",
            scheduled_date=day, workout=workout, source_schedule=schedule,
        )
        return profile, token, workout, schedule, task

    def test_a_calendar_delete_racing_a_sync_leaves_nothing_behind(self):
        profile, token, workout, schedule, task = self.planned_day("race-calendar-delete")
        day = str(schedule.scheduled_date)
        statuses = self.race(
            token,
            lambda client: client.delete(f"/api/v1/planner/{task.pk}/"),
            lambda client: client.post("/api/v1/schedules/sync-planner/", {"start": day, "end": day}, format="json"),
        )
        self.assertEqual(sorted(statuses), [200, 204])
        self.assertFalse(WorkoutSchedule.objects.filter(owner=profile).exists())
        self.assertFalse(PlannerEntry.objects.filter(owner=profile, workout=workout).exists())

    def test_a_calendar_move_racing_a_removed_day_leaves_no_orphan(self):
        profile, token, workout, schedule, task = self.planned_day("race-calendar-move")
        later = str(schedule.scheduled_date + timedelta(days=2))
        statuses = self.race(
            token,
            lambda client: client.patch(f"/api/v1/planner/{task.pk}/", {"scheduled_date": later}, format="json"),
            lambda client: client.delete(f"/api/v1/schedules/{schedule.pk}/"),
        )
        # Whichever lands second finds its row gone: a 404, never a 500.
        self.assertIn(204, statuses)
        self.assertTrue(set(statuses) <= {200, 204, 404}, statuses)
        days = set(WorkoutSchedule.objects.filter(owner=profile).values_list("scheduled_date", flat=True))
        tasks = set(PlannerEntry.objects.filter(owner=profile, workout=workout).values_list("scheduled_date", flat=True))
        self.assertEqual(tasks, days)


class LinkCalendarTasksMigrationTests(RepbaseAPITestMixin, APITestCase):
    """The one-off repair of tasks written before they were linked."""

    def setUp(self):
        self.today = timezone.now().date()
        _, self.profile, _ = self.create_account("calendar-repair")
        self.push = WorkoutTemplate.objects.create(owner=self.profile, name="Push", workout_type="lifting")
        self.pull = WorkoutTemplate.objects.create(owner=self.profile, name="Pull", workout_type="lifting")

    def repair(self):
        migration = import_module("core.migrations.0087_link_workout_calendar_tasks")
        migration.link_workout_calendar_tasks(apps, None)

    def task(self, offset, workout=None, **fields):
        workout = workout or self.push
        return PlannerEntry.objects.create(
            owner=self.profile, kind="task", category="workout", title=workout.name,
            scheduled_date=self.today + timedelta(days=offset), workout=workout, **fields,
        )

    def schedule(self, offset, workout=None, **fields):
        return WorkoutSchedule.objects.create(
            owner=self.profile, workout=workout or self.push,
            scheduled_date=self.today + timedelta(days=offset), **fields,
        )

    def test_links_a_task_to_the_day_it_stands_for(self):
        schedule = self.schedule(1, planner_synced_at=timezone.now())
        task = self.task(1, scheduled_time="07:00")
        trained = self.task(-1, completed_at=timezone.now())
        trained_day = self.schedule(-1)
        self.repair()
        task.refresh_from_db()
        trained.refresh_from_db()
        self.assertEqual(task.source_schedule_id, schedule.pk)
        self.assertEqual(trained.source_schedule_id, trained_day.pk)
        trained_day.refresh_from_db()
        self.assertIsNotNone(trained_day.planner_synced_at)

    def test_removes_unfinished_tasks_left_behind_by_deleted_days(self):
        stale = self.task(2, notes="Was planned once")
        step = PlannerEntry.objects.create(
            owner=self.profile, kind="task", category="workout", title="Warm up",
            scheduled_date=stale.scheduled_date, parent=stale,
        )
        missed = self.task(-3)
        record = self.task(-2, completed_at=timezone.now())
        ordinary = PlannerEntry.objects.create(
            owner=self.profile, kind="task", category="other", title="Call home", scheduled_date=self.today,
        )
        event = PlannerEntry.objects.create(
            owner=self.profile, kind="event", category="social", title="Push class",
            scheduled_date=self.today, scheduled_time="18:00", workout=self.push,
        )
        self.repair()
        self.assertFalse(PlannerEntry.objects.filter(pk__in=[stale.pk, step.pk, missed.pk]).exists())
        self.assertEqual(
            set(PlannerEntry.objects.values_list("pk", flat=True)),
            {record.pk, ordinary.pk, event.pk},
        )

    def test_returns_a_task_moved_off_its_day_to_that_day(self):
        schedule = self.schedule(1, planner_synced_at=timezone.now())
        moved = self.task(4, notes="Moved on the calendar")
        moved.source_schedule = schedule
        moved.save(update_fields=["source_schedule"])
        self.repair()
        moved.refresh_from_db()
        self.assertEqual(moved.scheduled_date, schedule.scheduled_date)
        self.assertEqual(moved.source_schedule_id, schedule.pk)
        self.assertEqual(moved.notes, "Moved on the calendar")

    def test_a_moved_task_whose_day_is_taken_gives_way(self):
        schedule = self.schedule(1, planner_synced_at=timezone.now())
        moved = self.task(4)
        moved.source_schedule = schedule
        moved.save(update_fields=["source_schedule"])
        occupant = self.task(1)
        self.repair()
        self.assertFalse(PlannerEntry.objects.filter(pk=moved.pk).exists())
        occupant.refresh_from_db()
        self.assertEqual(occupant.source_schedule_id, schedule.pk)

    def test_a_trained_task_moved_off_its_day_stays_where_it_was_ticked(self):
        schedule = self.schedule(-1, planner_synced_at=timezone.now())
        ticked = self.task(-2, completed_at=timezone.now())
        ticked.source_schedule = schedule
        ticked.save(update_fields=["source_schedule"])
        self.repair()
        ticked.refresh_from_db()
        self.assertEqual(ticked.scheduled_date, self.today - timedelta(days=2))
        self.assertIsNone(ticked.source_schedule_id)

    def test_leaves_other_accounts_and_sessions_alone(self):
        _, other, _ = self.create_account("calendar-repair-bystander")
        theirs = WorkoutTemplate.objects.create(owner=other, name="Theirs", workout_type="lifting")
        their_day = WorkoutSchedule.objects.create(owner=other, workout=theirs, scheduled_date=self.today)
        their_task = PlannerEntry.objects.create(
            owner=other, kind="task", category="workout", title="Theirs",
            scheduled_date=self.today, workout=theirs,
        )
        session = WorkoutSession.objects.create(repbase_user=self.profile, workout=self.push, status="completed")
        self.task(3)
        self.repair()
        their_task.refresh_from_db()
        self.assertEqual(their_task.source_schedule_id, their_day.pk)
        self.assertTrue(WorkoutSession.objects.filter(pk=session.pk).exists())

    def test_running_twice_changes_nothing_more(self):
        self.schedule(1)
        self.task(1)
        self.task(5)
        self.repair()
        before = list(PlannerEntry.objects.order_by("pk").values())
        self.repair()
        self.assertEqual(list(PlannerEntry.objects.order_by("pk").values()), before)
