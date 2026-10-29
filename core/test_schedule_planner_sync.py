"""Schedule changes remove only the unfinished planner tasks they generated."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone as datetime_timezone
from threading import Barrier
from unittest import mock, skipUnless

from django.db import close_old_connections, connection, transaction
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase, APITransactionTestCase

from .models import (
    PlannerEntry,
    WorkoutCycle,
    WorkoutCycleSlot,
    WorkoutRecurrence,
    WorkoutSchedule,
    WorkoutSession,
    WorkoutTemplate,
)
from .tests import RepbaseAPITestMixin


class SchedulePlannerSyncTests(RepbaseAPITestMixin, APITestCase):
    SYNC = "/api/v1/schedules/sync-planner/"
    CLEAR = "/api/v1/schedules/clear/"

    def setUp(self):
        self.now = datetime(2026, 10, 28, 12, tzinfo=datetime_timezone.utc)
        clock = mock.patch("django.utils.timezone.now", return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.today = self.now.date()
        self.user, self.profile, self.token = self.create_account("schedule-sync")
        self.authenticate(self.token)
        self.push = WorkoutTemplate.objects.create(
            owner=self.profile, name="Push", workout_type="lifting"
        )
        self.pull = WorkoutTemplate.objects.create(
            owner=self.profile, name="Pull", workout_type="lifting"
        )

    def schedule(self, offset=0, workout=None, **fields):
        return WorkoutSchedule.objects.create(
            owner=self.profile,
            workout=workout or self.push,
            scheduled_date=self.today + timedelta(days=offset),
            **fields,
        )

    def sync(self, start=None, end=None):
        response = self.client.post(
            self.SYNC,
            {"start": str(start or self.today), "end": str(end or start or self.today)},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def generated_task(self, schedule):
        created = self.sync(schedule.scheduled_date)
        self.assertEqual(len(created), 1, created)
        task = PlannerEntry.objects.get(pk=created[0]["id"])
        self.assertEqual(task.source_schedule_id, schedule.pk)
        return task

    def manual_task(self, day, workout=None, **fields):
        return PlannerEntry.objects.create(
            owner=self.profile,
            title="My own training reminder",
            category="workout",
            kind="task",
            scheduled_date=day,
            workout=workout or self.push,
            **fields,
        )

    def cycle(self, owner=None, workout=None):
        cycle = WorkoutCycle.objects.create(
            owner=owner or self.profile,
            name="Daily training",
            length=1,
            anchor_date=self.today,
            effective_from=self.today,
        )
        WorkoutCycleSlot.objects.create(
            cycle=cycle, position=1, workout=workout or self.push
        )
        return cycle

    def test_sync_records_provenance_without_changing_api_response(self):
        schedule = self.schedule()
        result = self.sync()
        self.assertEqual(len(result), 1)
        entry = PlannerEntry.objects.get(pk=result[0]["id"])
        self.assertEqual(entry.source_schedule_id, schedule.pk)
        self.assertNotIn("source_schedule", result[0])
        schedule.refresh_from_db()
        self.assertIsNotNone(schedule.planner_synced_at)

    def test_repeated_sync_and_deleted_task_do_not_recreate_a_task(self):
        schedule = self.schedule()
        entry = self.generated_task(schedule)
        self.assertEqual(self.sync(), [])
        response = self.client.delete(f"/api/v1/planner/{entry.pk}/")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.sync(), [])
        self.assertFalse(PlannerEntry.objects.filter(source_schedule=schedule).exists())

    def test_adopted_manual_task_is_never_claimed_or_deleted_with_schedule(self):
        schedule = self.schedule()
        manual = self.manual_task(self.today)
        self.assertEqual(self.sync(), [])
        manual.refresh_from_db()
        self.assertIsNone(manual.source_schedule_id)
        schedule.refresh_from_db()
        self.assertIsNotNone(schedule.planner_synced_at)
        response = self.client.delete(f"/api/v1/schedules/{schedule.pk}/")
        self.assertEqual(response.status_code, 204)
        self.assertTrue(PlannerEntry.objects.filter(pk=manual.pk).exists())

    def test_individual_unschedule_removes_only_its_generated_task(self):
        removed_schedule = self.schedule()
        kept_schedule = self.schedule(1)
        removed = self.generated_task(removed_schedule)
        kept = self.generated_task(kept_schedule)
        response = self.client.delete(f"/api/v1/schedules/{removed_schedule.pk}/")
        self.assertEqual(response.status_code, 204)
        self.assertFalse(PlannerEntry.objects.filter(pk=removed.pk).exists())
        self.assertTrue(PlannerEntry.objects.filter(pk=kept.pk).exists())

    def test_moving_schedule_updates_untouched_generated_task_in_place(self):
        schedule = self.schedule()
        task = self.generated_task(schedule)
        moved_to = self.today + timedelta(days=2)
        response = self.client.patch(
            f"/api/v1/schedules/{schedule.pk}/", {"scheduled_date": str(moved_to)}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        task.refresh_from_db()
        self.assertEqual(task.scheduled_date, moved_to)
        self.assertEqual(task.title, self.push.name)
        self.assertEqual(task.source_schedule_id, schedule.pk)
        self.assertEqual(self.sync(moved_to), [])
        self.assertEqual(PlannerEntry.objects.filter(source_schedule=schedule).count(), 1)

    def test_replacing_scheduled_workout_updates_untouched_task_in_place(self):
        schedule = self.schedule()
        task = self.generated_task(schedule)
        response = self.client.patch(
            f"/api/v1/schedules/{schedule.pk}/", {"workout": self.pull.pk}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        task.refresh_from_db()
        self.assertEqual(task.workout_id, self.pull.pk)
        self.assertEqual(task.title, self.pull.name)
        self.assertEqual(task.source_schedule_id, schedule.pk)
        self.assertEqual(self.sync(), [])
        self.assertEqual(PlannerEntry.objects.filter(source_schedule=schedule).count(), 1)

    def test_moving_schedule_does_not_rewrite_manual_completed_or_customized_tasks(self):
        for offset, kind in enumerate(("manual", "completed", "customized")):
            with self.subTest(kind=kind):
                schedule = self.schedule(offset)
                old_date = schedule.scheduled_date
                if kind == "manual":
                    task = self.manual_task(old_date)
                    self.assertEqual(self.sync(old_date), [])
                else:
                    task = self.generated_task(schedule)
                    if kind == "completed":
                        task.completed_at = self.now
                        task.save(update_fields=["completed_at"])
                    else:
                        task.notes = "Personal preparation notes"
                        task.save(update_fields=["notes"])
                old_title = task.title
                moved_to = old_date + timedelta(days=7)
                response = self.client.patch(
                    f"/api/v1/schedules/{schedule.pk}/", {"scheduled_date": str(moved_to)}, format="json"
                )
                self.assertEqual(response.status_code, 200, response.data)
                task.refresh_from_db()
                self.assertEqual(task.scheduled_date, old_date)
                self.assertEqual(task.title, old_title)
                self.assertEqual(task.workout_id, self.push.pk)
                self.assertIsNone(task.source_schedule_id)
                if kind == "completed":
                    self.assertEqual(task.completed_at, self.now)
                if kind == "customized":
                    self.assertEqual(task.notes, "Personal preparation notes")
                new_tasks = self.sync(moved_to)
                self.assertEqual(len(new_tasks), 1, new_tasks)
                self.assertNotEqual(new_tasks[0]["id"], task.pk)
                replacement = PlannerEntry.objects.get(pk=new_tasks[0]["id"])
                self.assertEqual(replacement.source_schedule_id, schedule.pk)

    def test_template_rename_updates_only_untouched_generated_task_titles(self):
        untouched = self.generated_task(self.schedule())
        customized = self.generated_task(self.schedule(1))
        customized.notes = "Keep these notes with the original plan"
        customized.save(update_fields=["notes"])
        completed = self.generated_task(self.schedule(2))
        completed.completed_at = self.now
        completed.save(update_fields=["completed_at"])
        manual_schedule = self.schedule(3)
        manual = self.manual_task(manual_schedule.scheduled_date)
        manual.title = self.push.name
        manual.save(update_fields=["title"])
        self.assertEqual(self.sync(manual_schedule.scheduled_date), [])
        old_name = self.push.name
        response = self.client.patch(
            f"/api/v1/workouts/{self.push.pk}/", {"name": "Upper body strength"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        untouched.refresh_from_db()
        self.assertEqual(untouched.title, "Upper body strength")
        for task in (customized, completed, manual):
            task.refresh_from_db()
            self.assertEqual(task.title, old_name)

    def test_clear_removes_generated_tasks_in_past_present_and_future(self):
        generated_ids = [
            self.generated_task(self.schedule(offset)).pk for offset in (-3, 0, 8)
        ]
        manual = self.manual_task(self.today + timedelta(days=4))
        response = self.client.post(self.CLEAR, {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["cleared"], 3)
        self.assertFalse(PlannerEntry.objects.filter(pk__in=generated_ids).exists())
        self.assertTrue(PlannerEntry.objects.filter(pk=manual.pk).exists())

    def test_clear_preserves_completed_task_and_training_history(self):
        schedule = self.schedule()
        entry = self.generated_task(schedule)
        entry.completed_at = self.now
        entry.save(update_fields=["completed_at"])
        session = WorkoutSession.objects.create(
            repbase_user=self.profile,
            workout=self.push,
            status="completed",
            started_at=self.now,
            ended_at=self.now + timedelta(hours=1),
        )
        response = self.client.post(self.CLEAR, {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        entry.refresh_from_db()
        self.assertEqual(entry.completed_at, self.now)
        self.assertIsNone(entry.source_schedule_id)
        session.refresh_from_db()
        self.assertEqual(session.status, "completed")
        self.assertEqual(session.workout_id, self.push.pk)

    def test_legacy_unlinked_task_is_not_guess_deleted(self):
        legacy = self.manual_task(self.today)
        schedule = self.schedule(planner_synced_at=self.now)
        response = self.client.post(self.CLEAR, {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(WorkoutSchedule.objects.filter(pk=schedule.pk).exists())
        self.assertTrue(PlannerEntry.objects.filter(pk=legacy.pk).exists())

    def test_clear_and_individual_delete_are_owner_scoped(self):
        _, other, _ = self.create_account("schedule-bystander")
        workout = WorkoutTemplate.objects.create(owner=other, name="Other", workout_type="lifting")
        theirs = WorkoutSchedule.objects.create(owner=other, workout=workout, scheduled_date=self.today)
        task = PlannerEntry.objects.create(
            owner=other, workout=workout, title="Other", kind="task", category="workout",
            scheduled_date=self.today, source_schedule=theirs,
        )
        ours = self.generated_task(self.schedule())
        self.assertEqual(self.client.delete(f"/api/v1/schedules/{theirs.pk}/").status_code, 404)
        self.assertEqual(self.client.post(self.CLEAR, {}, format="json").status_code, 200)
        self.assertFalse(PlannerEntry.objects.filter(pk=ours.pk).exists())
        self.assertTrue(WorkoutSchedule.objects.filter(pk=theirs.pk).exists())
        self.assertTrue(PlannerEntry.objects.filter(pk=task.pk).exists())

    def test_unauthenticated_request_cannot_delete_schedules_or_tasks(self):
        schedule = self.schedule()
        task = self.generated_task(schedule)
        self.client.credentials()
        self.assertIn(self.client.delete(f"/api/v1/schedules/{schedule.pk}/").status_code, (401, 403))
        self.assertIn(self.client.post(self.CLEAR, {}, format="json").status_code, (401, 403))
        self.assertTrue(WorkoutSchedule.objects.filter(pk=schedule.pk).exists())
        self.assertTrue(PlannerEntry.objects.filter(pk=task.pk).exists())

    def test_client_cannot_claim_a_manual_task_as_schedule_generated(self):
        schedule = self.schedule()
        response = self.client.post(
            "/api/v1/planner/",
            {
                "title": "Written by me", "kind": "task", "category": "workout",
                "scheduled_date": str(self.today), "workout": self.push.pk,
                "source_schedule": schedule.pk,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        task = PlannerEntry.objects.get(pk=response.data["id"])
        self.assertIsNone(task.source_schedule_id)
        self.assertNotIn("source_schedule", response.data)

    def test_schedule_delete_and_planner_cleanup_roll_back_together(self):
        schedule = self.schedule()
        schedule_id = schedule.pk
        task = self.generated_task(schedule)
        with self.assertRaisesRegex(RuntimeError, "cancel deletion"):
            with transaction.atomic():
                schedule.delete()
                self.assertFalse(PlannerEntry.objects.filter(pk=task.pk).exists())
                raise RuntimeError("cancel deletion")
        self.assertTrue(WorkoutSchedule.objects.filter(pk=schedule_id).exists())
        self.assertTrue(PlannerEntry.objects.filter(pk=task.pk, source_schedule_id=schedule_id).exists())

    def test_moved_generated_task_is_preserved_and_detached(self):
        schedule = self.schedule()
        task = self.generated_task(schedule)
        response = self.client.patch(
            f"/api/v1/planner/{task.pk}/",
            {"scheduled_date": str(self.today + timedelta(days=2))},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.client.delete(f"/api/v1/schedules/{schedule.pk}/").status_code, 204)
        task.refresh_from_db()
        self.assertEqual(task.scheduled_date, self.today + timedelta(days=2))
        self.assertIsNone(task.source_schedule_id)

    def test_generated_task_with_authored_steps_is_preserved(self):
        schedule = self.schedule()
        task = self.generated_task(schedule)
        completed_step = PlannerEntry.objects.create(
            owner=self.profile, parent=task, kind="task", category="other",
            title="Warm up", scheduled_date=self.today, completed_at=self.now,
        )
        unfinished_step = PlannerEntry.objects.create(
            owner=self.profile, parent=task, kind="task", category="other",
            title="Record technique notes", scheduled_date=self.today,
        )
        self.assertEqual(self.client.delete(f"/api/v1/schedules/{schedule.pk}/").status_code, 204)
        task.refresh_from_db()
        self.assertIsNone(task.source_schedule_id)
        completed_step.refresh_from_db()
        self.assertEqual(completed_step.completed_at, self.now)
        self.assertTrue(PlannerEntry.objects.filter(pk=unfinished_step.pk, parent=task).exists())

    def test_customized_generated_task_is_preserved(self):
        updates = (
            {"title": "Technique focus"},
            {"notes": "Go light on my shoulder"},
            {"scheduled_time": "17:30:00"},
        )
        for offset, update in enumerate(updates):
            with self.subTest(update=update):
                schedule = self.schedule(offset)
                task = self.generated_task(schedule)
                response = self.client.patch(f"/api/v1/planner/{task.pk}/", update, format="json")
                self.assertEqual(response.status_code, 200, response.data)
                self.assertEqual(self.client.delete(f"/api/v1/schedules/{schedule.pk}/").status_code, 204)
                task.refresh_from_db()
                self.assertIsNone(task.source_schedule_id)

    def test_deleting_template_cleans_generated_tasks_but_keeps_manual_and_history(self):
        pending = self.generated_task(self.schedule())
        completed = self.generated_task(self.schedule(1))
        completed.completed_at = self.now
        completed.save(update_fields=["completed_at"])
        manual = self.manual_task(self.today + timedelta(days=2))
        session = WorkoutSession.objects.create(repbase_user=self.profile, workout=self.push, status="completed")
        response = self.client.delete(f"/api/v1/workouts/{self.push.pk}/")
        self.assertEqual(response.status_code, 204)
        self.assertFalse(PlannerEntry.objects.filter(pk=pending.pk).exists())
        completed.refresh_from_db()
        self.assertIsNone(completed.source_schedule_id)
        self.assertIsNone(completed.workout_id)
        self.assertEqual(completed.completed_at, self.now)
        manual.refresh_from_db()
        self.assertIsNone(manual.workout_id)
        self.assertTrue(WorkoutSession.objects.filter(pk=session.pk).exists())

    def test_ending_weekly_repeat_cleans_only_its_future_generated_tasks(self):
        monday = self.today - timedelta(days=self.today.weekday())
        recurrence = WorkoutRecurrence.objects.create(
            owner=self.profile, workout=self.push, weekday=self.today.weekday(),
            effective_from=monday, materialized_through=monday + timedelta(days=14),
        )
        current = self.generated_task(self.schedule(source_recurrence=recurrence))
        future = self.generated_task(self.schedule(7, source_recurrence=recurrence))
        manual_schedule = self.schedule(14, source_recurrence=recurrence)
        manual = self.manual_task(manual_schedule.scheduled_date)
        self.assertEqual(self.sync(manual_schedule.scheduled_date), [])
        response = self.client.delete(f"/api/v1/recurrences/{recurrence.pk}/")
        self.assertEqual(response.status_code, 204)
        self.assertTrue(PlannerEntry.objects.filter(pk=current.pk).exists())
        self.assertFalse(PlannerEntry.objects.filter(pk=future.pk).exists())
        self.assertTrue(PlannerEntry.objects.filter(pk=manual.pk).exists())

    def test_ending_rotation_removes_own_future_untrained_rows_and_tasks(self):
        cycle = self.cycle()
        old_schedule = self.schedule(-1, source_cycle=cycle)
        old = self.generated_task(old_schedule)
        today = self.generated_task(self.schedule(source_cycle=cycle))
        future = self.generated_task(self.schedule(5, source_cycle=cycle))
        manual_schedule = self.schedule(2, workout=self.pull)
        manual = self.generated_task(manual_schedule)
        response = self.client.delete(f"/api/v1/cycles/{cycle.pk}/")
        self.assertEqual(response.status_code, 204)
        cycle.refresh_from_db()
        self.assertEqual(cycle.effective_until, self.today)
        self.assertFalse(PlannerEntry.objects.filter(pk__in=[today.pk, future.pk]).exists())
        self.assertTrue(WorkoutSchedule.objects.filter(pk=old_schedule.pk).exists())
        self.assertTrue(PlannerEntry.objects.filter(pk=old.pk).exists())
        self.assertTrue(WorkoutSchedule.objects.filter(pk=manual_schedule.pk).exists())
        self.assertTrue(PlannerEntry.objects.filter(pk=manual.pk).exists())

    def test_ending_rotation_preserves_an_active_session_and_completed_task(self):
        cycle = self.cycle()
        active_schedule = self.schedule(source_cycle=cycle)
        active_task = self.generated_task(active_schedule)
        session = WorkoutSession.objects.create(
            repbase_user=self.profile, workout=self.push, status="active", started_at=self.now,
        )
        done = self.generated_task(self.schedule(1, source_cycle=cycle))
        done.completed_at = self.now
        done.save(update_fields=["completed_at"])
        response = self.client.delete(f"/api/v1/cycles/{cycle.pk}/")
        self.assertEqual(response.status_code, 204)
        self.assertTrue(WorkoutSchedule.objects.filter(pk=active_schedule.pk).exists())
        self.assertTrue(PlannerEntry.objects.filter(pk=active_task.pk).exists())
        self.assertTrue(WorkoutSession.objects.filter(pk=session.pk).exists())
        done.refresh_from_db()
        self.assertEqual(done.completed_at, self.now)

    def test_ending_future_rotation_withdraws_its_dates_before_activation(self):
        cycle = self.cycle()
        starts_on = self.today + timedelta(days=7)
        cycle.anchor_date = starts_on
        cycle.effective_from = starts_on
        cycle.save(update_fields=["anchor_date", "effective_from"])
        schedule = self.schedule(7, source_cycle=cycle)
        task = self.generated_task(schedule)
        response = self.client.delete(f"/api/v1/cycles/{cycle.pk}/")
        self.assertEqual(response.status_code, 204)
        cycle.refresh_from_db()
        self.assertEqual(cycle.effective_until, starts_on)
        self.assertFalse(cycle.is_active)
        self.assertFalse(WorkoutSchedule.objects.filter(pk=schedule.pk).exists())
        self.assertFalse(PlannerEntry.objects.filter(pk=task.pk).exists())
        listed = self.client.get("/api/v1/cycles/")
        self.assertEqual(listed.status_code, 200, listed.data)
        self.assertEqual(listed.data["results"], [])

    def test_deleting_already_ended_rotation_does_not_extend_its_history(self):
        cycle = self.cycle()
        cycle.effective_from = self.today - timedelta(days=10)
        cycle.effective_until = self.today - timedelta(days=2)
        cycle.save(update_fields=["effective_from", "effective_until"])
        previous_end = cycle.effective_until
        response = self.client.delete(f"/api/v1/cycles/{cycle.pk}/?include_ended=true")
        self.assertEqual(response.status_code, 204)
        cycle.refresh_from_db()
        self.assertEqual(cycle.effective_until, previous_end)

    def test_ending_rotation_before_future_handover_stops_it_today(self):
        cycle = self.cycle()
        cycle.effective_until = self.today + timedelta(days=7)
        cycle.save(update_fields=["effective_until"])
        old_schedule = self.schedule(-1, source_cycle=cycle)
        old_task = self.generated_task(old_schedule)
        today_schedule = self.schedule(source_cycle=cycle)
        today_task = self.generated_task(today_schedule)
        future_schedule = self.schedule(3, source_cycle=cycle)
        future_task = self.generated_task(future_schedule)
        response = self.client.delete(f"/api/v1/cycles/{cycle.pk}/?include_ended=true")
        self.assertEqual(response.status_code, 204)
        cycle.refresh_from_db()
        self.assertEqual(cycle.effective_until, self.today)
        self.assertTrue(WorkoutSchedule.objects.filter(pk=old_schedule.pk).exists())
        self.assertTrue(PlannerEntry.objects.filter(pk=old_task.pk).exists())
        self.assertFalse(WorkoutSchedule.objects.filter(pk__in=[today_schedule.pk, future_schedule.pk]).exists())
        self.assertFalse(PlannerEntry.objects.filter(pk__in=[today_task.pk, future_task.pk]).exists())

    def test_ending_rotation_keeps_training_on_account_day_not_utc_day(self):
        self.profile.time_zone = "America/Los_Angeles"
        self.profile.save(update_fields=["time_zone"])
        cycle = self.cycle()
        schedule = self.schedule(source_cycle=cycle)
        task = self.generated_task(schedule)
        # This is 23:30 on Oct 28 in Los Angeles, but Oct 29 in UTC.
        trained_at = datetime(2026, 10, 29, 6, 30, tzinfo=datetime_timezone.utc)
        session = WorkoutSession.objects.create(
            repbase_user=self.profile, workout=self.push, status="completed",
            started_at=trained_at, ended_at=trained_at + timedelta(minutes=15),
        )
        with mock.patch("django.utils.timezone.now", return_value=trained_at + timedelta(minutes=30)):
            response = self.client.delete(f"/api/v1/cycles/{cycle.pk}/")
        self.assertEqual(response.status_code, 204)
        self.assertTrue(WorkoutSchedule.objects.filter(pk=schedule.pk).exists())
        self.assertTrue(PlannerEntry.objects.filter(pk=task.pk).exists())
        self.assertTrue(WorkoutSession.objects.filter(pk=session.pk).exists())

    def test_ending_rotation_does_not_touch_other_accounts_or_rotations(self):
        cycle = self.cycle()
        mine = self.generated_task(self.schedule(source_cycle=cycle))
        other_cycle = self.cycle(workout=self.pull)
        other_schedule = self.schedule(1, workout=self.pull, source_cycle=other_cycle)
        other_task = self.generated_task(other_schedule)
        _, other_owner, _ = self.create_account("rotation-bystander")
        other_workout = WorkoutTemplate.objects.create(owner=other_owner, name="Other workout", workout_type="lifting")
        theirs = self.cycle(owner=other_owner, workout=other_workout)
        self.assertEqual(self.client.delete(f"/api/v1/cycles/{theirs.pk}/").status_code, 404)
        self.assertEqual(self.client.delete(f"/api/v1/cycles/{cycle.pk}/").status_code, 204)
        self.assertFalse(PlannerEntry.objects.filter(pk=mine.pk).exists())
        self.assertTrue(PlannerEntry.objects.filter(pk=other_task.pk).exists())
        self.assertTrue(WorkoutSchedule.objects.filter(pk=other_schedule.pk).exists())
        theirs.refresh_from_db()
        self.assertIsNone(theirs.effective_until)

    def test_shifting_rotation_preserves_adopted_manual_task(self):
        cycle = self.cycle()
        schedule = self.schedule(source_cycle=cycle)
        manual = self.manual_task(schedule.scheduled_date)
        self.assertEqual(self.sync(), [])
        response = self.client.post(f"/api/v1/cycles/{cycle.pk}/shift/", {"days": 1}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(PlannerEntry.objects.filter(pk=manual.pk).exists())
        manual.refresh_from_db()
        self.assertIsNone(manual.source_schedule_id)


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locks")
class SchedulePlannerConcurrencyTests(RepbaseAPITestMixin, APITransactionTestCase):
    def test_parallel_sync_creates_one_provenance_linked_task(self):
        _, profile, token = self.create_account("parallel-schedule-sync")
        workout = WorkoutTemplate.objects.create(owner=profile, name="Push", workout_type="lifting")
        day = timezone.now().date()
        schedule = WorkoutSchedule.objects.create(owner=profile, workout=workout, scheduled_date=day)
        barrier = Barrier(2)

        def sync():
            close_old_connections()
            try:
                client = APIClient()
                client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
                barrier.wait(timeout=10)
                response = client.post(
                    "/api/v1/schedules/sync-planner/", {"start": str(day), "end": str(day)}, format="json"
                )
                return response.status_code, len(response.data)
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = [pool.submit(sync) for _ in range(2)]
            results = [result.result(timeout=20) for result in pending]
        self.assertEqual([status for status, _ in results], [200, 200])
        self.assertEqual(sum(count for _, count in results), 1)
        self.assertEqual(PlannerEntry.objects.filter(source_schedule=schedule).count(), 1)

    def assert_sync_and_removal_do_not_leave_orphans(self, *, clear):
        _, profile, token = self.create_account("parallel-schedule-removal")
        workout = WorkoutTemplate.objects.create(owner=profile, name="Push", workout_type="lifting")
        day = timezone.now().date()
        schedule = WorkoutSchedule.objects.create(owner=profile, workout=workout, scheduled_date=day)
        barrier = Barrier(2)

        def request(remove):
            close_old_connections()
            try:
                client = APIClient()
                client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
                barrier.wait(timeout=10)
                if remove:
                    if clear:
                        response = client.post("/api/v1/schedules/clear/", {}, format="json")
                    else:
                        response = client.delete(f"/api/v1/schedules/{schedule.pk}/")
                else:
                    response = client.post(
                        "/api/v1/schedules/sync-planner/",
                        {"start": str(day), "end": str(day)}, format="json",
                    )
                return response.status_code
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            synced = pool.submit(request, False)
            removed = pool.submit(request, True)
            self.assertEqual(synced.result(timeout=20), 200)
            self.assertEqual(removed.result(timeout=20), 200 if clear else 204)
        self.assertFalse(WorkoutSchedule.objects.filter(pk=schedule.pk).exists())
        self.assertFalse(
            PlannerEntry.objects.filter(owner=profile, workout=workout, scheduled_date=day).exists(),
            "Concurrent sync must not leave a generated task after its schedule was removed.",
        )

    def test_sync_and_clear_do_not_leave_a_generated_orphan(self):
        self.assert_sync_and_removal_do_not_leave_orphans(clear=True)

    def test_sync_and_individual_unschedule_do_not_leave_a_generated_orphan(self):
        self.assert_sync_and_removal_do_not_leave_orphans(clear=False)
