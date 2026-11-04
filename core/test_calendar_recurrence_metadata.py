from datetime import date, datetime, timedelta
from importlib import import_module
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from django.apps import apps
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from .models import (
    PlannerEntry, PlannerRecurrence, WorkoutTemplate, materialize_planner_repeats, today_for,
)
from .serializers import PlannerEntrySerializer
from .tests import RepbaseAPITestMixin


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class CalendarRecurrenceMetadataTests(RepbaseAPITestMixin, APITestCase):
    def setUp(self):
        self.user, self.profile, self.token = self.create_account("calendar-owner")
        self.authenticate(self.token)
        self.start = today_for(self.profile) + timedelta(days=10)

    def create(self, **changes):
        body = {
            "kind": "event", "title": "Evening class", "category": "appointment",
            "scheduled_date": str(self.start), "color": "teal",
        }
        body.update(changes)
        response = self.client.post("/api/v1/planner/", body, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return PlannerEntry.objects.get(pk=response.data["id"])

    def patch(self, entry, changes, scope=None):
        suffix = f"?scope={scope}" if scope else ""
        return self.client.patch(f"/api/v1/planner/{entry.pk}/{suffix}", changes, format="json")

    def rows(self, entry):
        return list(PlannerEntry.objects.filter(recurrence=entry.recurrence).order_by("recurrence_date", "id"))

    def test_recurring_events_retain_kind_color_timezone_and_no_checklist(self):
        entry = self.create(
            repeat_every_days=3, scheduled_time="18:30", duration_minutes=45,
            scheduling_timezone="America/Chicago",
        )
        for row in self.rows(entry):
            self.assertEqual((row.kind, row.color, row.scheduling_timezone), ("event", "teal", "America/Chicago"))
            self.assertEqual(row.scheduled_time.isoformat(), "18:30:00")
            self.assertEqual(row.duration_minutes, 45)
            self.assertEqual(row.recurrence_date, row.scheduled_date)
            self.assertIsNone(row.completed_at)
            self.assertFalse(row.subtasks.exists())
        result = self.patch(entry, {"is_complete": True})
        self.assertEqual(result.status_code, 400, result.data)
        step = self.client.post("/api/v1/planner/", {
            "kind": "task", "title": "Cannot attach", "category": "other",
            "scheduled_date": str(self.start), "parent": entry.pk,
        }, format="json")
        self.assertEqual(step.status_code, 400, step.data)

    def test_events_repeat_only_on_selected_weekdays(self):
        entry = self.create(repeat_on_weekdays=[0, 4])
        rows = self.rows(entry)
        self.assertGreater(len(rows), 5)
        self.assertTrue(all(row.kind == "event" and row.scheduled_date.weekday() in (0, 4) for row in rows))

    def test_legacy_timed_client_snapshots_profile_zone_and_survives_profile_move(self):
        self.profile.time_zone = "America/New_York"
        self.profile.save(update_fields=["time_zone"])
        entry = self.create(repeat_every_days=7, scheduled_time="09:00")
        self.assertEqual(entry.scheduling_timezone, "America/New_York")
        self.profile.time_zone = "Asia/Tokyo"
        self.profile.save(update_fields=["time_zone"])
        materialize_planner_repeats(self.profile, self.start + timedelta(days=100))
        self.assertEqual({row.scheduling_timezone for row in self.rows(entry)}, {"America/New_York"})

    def test_wall_clock_recurrence_does_not_become_a_fixed_utc_interval_across_dst(self):
        # Pick a future US spring transition without coupling the test to the
        # wall clock running the suite. The second Sunday in March changes DST.
        year = self.start.year
        while True:
            march_first = date(year, 3, 1)
            transition = march_first + timedelta(days=(6 - march_first.weekday()) % 7 + 7)
            before = transition - timedelta(days=7)
            if before >= self.start:
                break
            year += 1
        entry = self.create(
            scheduled_date=str(before), repeat_every_days=7,
            scheduled_time="09:00", scheduling_timezone="America/New_York",
        )
        rows = PlannerEntry.objects.filter(recurrence=entry.recurrence).order_by("scheduled_date")[:2]
        instants = [datetime.combine(row.scheduled_date, row.scheduled_time, ZoneInfo(row.scheduling_timezone)) for row in rows]
        self.assertEqual([instant.hour for instant in instants], [9, 9])
        self.assertEqual([instant.utcoffset() for instant in instants], [timedelta(hours=-5), timedelta(hours=-4)])

    def test_invalid_color_or_timezone_is_refused_and_blank_is_compatible(self):
        entry = self.create(color="", scheduling_timezone="")
        self.assertEqual(entry.color, "")
        self.assertEqual(entry.scheduling_timezone, "")
        for invalid in ({"color": "neon"}, {"color": None}, {"scheduling_timezone": "UTC+03"}):
            with self.subTest(invalid=invalid):
                result = self.patch(entry, invalid)
                self.assertEqual(result.status_code, 400, result.data)

    def test_new_repeat_lookback_is_30_owner_local_days_but_one_off_history_is_allowed(self):
        today = today_for(self.profile)
        recent = self.create(scheduled_date=str(today - timedelta(days=30)), repeat_every_days=7)
        self.assertEqual(recent.scheduled_date, today - timedelta(days=30))
        response = self.client.post("/api/v1/planner/", {
            "kind": "event", "title": "Too old", "category": "appointment",
            "scheduled_date": str(today - timedelta(days=31)), "repeat_every_days": 7,
        }, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        historical = self.create(scheduled_date="2000-01-01")
        self.assertEqual(historical.scheduled_date, date(2000, 1, 1))
        self.assertIsNone(historical.recurrence_id)

    def test_occurrence_edit_never_rewrites_the_rule_or_immutable_identity(self):
        entry = self.create(repeat_every_days=2)
        original_rule_id = entry.recurrence_id
        original_identity = entry.recurrence_date
        result = self.patch(entry, {
            "title": "Private exception", "color": "rose", "scheduled_date": str(self.start - timedelta(days=3)),
        })
        self.assertEqual(result.status_code, 200, result.data)
        entry.refresh_from_db()
        self.assertEqual(entry.recurrence_id, original_rule_id)
        self.assertEqual(entry.recurrence_date, original_identity)
        self.assertEqual(entry.recurrence.color, "teal")
        later = self.rows(entry)[1]
        self.assertEqual((later.title, later.color), ("Evening class", "teal"))

    def test_following_metadata_split_preserves_ids_history_and_other_exceptions(self):
        first = self.create(kind="task", category="habit", repeat_every_days=1)
        rows = self.rows(first)
        earlier, selected, custom, completed = rows[0], rows[2], rows[3], rows[4]
        custom.notes, custom.color = "Keep this note", "lavender"
        custom.save()
        completed.completed_at = timezone.now()
        completed.save()
        original_rule, series = first.recurrence, first.recurrence.series_id
        result = self.patch(selected, {"title": "Updated", "is_complete": False}, "following")
        self.assertEqual(result.status_code, 200, result.data)
        for row in (earlier, selected, custom, completed):
            row.refresh_from_db()
        self.assertEqual((earlier.title, earlier.recurrence_id), ("Evening class", original_rule.pk))
        self.assertEqual((completed.title, completed.recurrence_id), ("Evening class", original_rule.pk))
        self.assertIsNotNone(completed.completed_at)
        self.assertEqual(selected.pk, result.data["id"])
        self.assertNotEqual(selected.recurrence_id, original_rule.pk)
        self.assertEqual(selected.recurrence.series_id, series)
        self.assertEqual(custom.title, "Updated")
        self.assertEqual((custom.notes, custom.color), ("Keep this note", "lavender"))
        original_rule.refresh_from_db()
        self.assertEqual(original_rule.ends_on, selected.recurrence_date)

    def test_moving_a_series_earlier_keeps_original_sharing_boundary(self):
        first = self.create(repeat_every_days=2)
        earlier, selected = self.rows(first)[0], self.rows(first)[2]
        identity = selected.recurrence_date
        original_series = selected.recurrence.series_id
        new_day = self.start - timedelta(days=3)
        result = self.patch(selected, {"scheduled_date": str(new_day), "color": "sky"}, "following")
        self.assertEqual(result.status_code, 200, result.data)
        selected.refresh_from_db()
        earlier.refresh_from_db()
        self.assertEqual(selected.scheduled_date, new_day)
        self.assertEqual(selected.recurrence_date, identity)
        self.assertEqual(selected.recurrence.series_id, original_series)
        self.assertEqual(earlier.scheduled_date, self.start)
        for row in self.rows(selected):
            self.assertGreaterEqual(row.recurrence_date, identity)
            self.assertEqual(row.recurrence_date, row.scheduled_date + timedelta(days=selected.recurrence.identity_offset_days))
        materialize_planner_repeats(self.profile, new_day + timedelta(days=110))
        self.assertTrue(all(row.recurrence_date >= identity for row in self.rows(selected)))

    def test_following_split_of_a_moved_occurrence_does_not_shift_the_whole_series(self):
        first = self.create(repeat_every_days=2)
        selected = self.rows(first)[2]
        original_identity = selected.recurrence_date
        moved_day = selected.scheduled_date + timedelta(days=1)
        self.assertEqual(self.patch(selected, {"scheduled_date": str(moved_day)}).status_code, 200)
        selected.refresh_from_db()
        response = self.patch(selected, {"title": "New title"}, "following")
        self.assertEqual(response.status_code, 200, response.data)
        selected.refresh_from_db()
        self.assertEqual(selected.scheduled_date, moved_day)
        self.assertEqual(selected.recurrence_date, original_identity)
        later = self.rows(selected)[1]
        self.assertEqual(later.scheduled_date, original_identity + timedelta(days=2))

    def test_moved_occurrence_cannot_replace_a_different_later_occurrence(self):
        first = self.create(repeat_every_days=1)
        later_day = self.start + timedelta(days=80)
        response = self.patch(first, {"scheduled_date": str(later_day)})
        self.assertEqual(response.status_code, 200, response.data)
        materialize_planner_repeats(self.profile, later_day)
        on_day = PlannerEntry.objects.filter(recurrence=first.recurrence, scheduled_date=later_day)
        self.assertEqual(set(on_day.values_list("recurrence_date", flat=True)), {self.start, later_day})

    def test_recoloring_series_does_not_restore_a_deleted_occurrence(self):
        first = self.create(repeat_every_days=1)
        deleted = self.rows(first)[4]
        identity = deleted.recurrence_date
        deleted.delete()
        response = self.patch(first, {"color": "amber"}, "following")
        self.assertEqual(response.status_code, 200, response.data)
        first.refresh_from_db()
        materialize_planner_repeats(self.profile, self.start + timedelta(days=100))
        self.assertFalse(PlannerEntry.objects.filter(recurrence__series_id=first.recurrence.series_id, recurrence_date=identity).exists())

    def test_following_can_change_interval_to_weekdays_without_touching_earlier_days(self):
        first = self.create(repeat_every_days=1)
        selected = self.rows(first)[2]
        earlier_id = first.pk
        weekday = (selected.scheduled_date.weekday() + 1) % 7
        response = self.patch(selected, {"repeat_on_weekdays": [weekday]}, "following")
        self.assertEqual(response.status_code, 200, response.data)
        selected.refresh_from_db()
        self.assertEqual(selected.scheduled_date.weekday(), weekday)
        self.assertTrue(PlannerEntry.objects.filter(pk=earlier_id).exists())
        self.assertTrue(all(row.scheduled_date.weekday() == weekday for row in self.rows(selected)))

    def test_unknown_scope_nonrepeat_following_and_unsafely_changing_repeat_are_refused(self):
        ordinary = self.create()
        self.assertEqual(self.patch(ordinary, {"color": "rose"}, "following").status_code, 400)
        repeated = self.create(repeat_every_days=1)
        self.assertEqual(self.patch(repeated, {"color": "rose"}, "all").status_code, 400)
        self.assertEqual(self.patch(repeated, {"repeat_every_days": 2}).status_code, 400)
        repeated.refresh_from_db()
        self.assertEqual(repeated.color, "teal")

    def test_following_rejects_invalid_pair_from_an_occurrence_override_atomically(self):
        first = self.create(repeat_every_days=1, scheduled_time="09:00", duration_minutes=30)
        selected = self.rows(first)[1]
        self.assertEqual(self.patch(selected, {"scheduled_time": None, "duration_minutes": None}).status_code, 200)
        selected.refresh_from_db()
        count = PlannerRecurrence.objects.count()
        response = self.patch(selected, {"scheduled_time": None}, "following")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(PlannerRecurrence.objects.count(), count)
        first.refresh_from_db()
        self.assertEqual(first.duration_minutes, 30)

    def test_other_account_even_staff_cannot_read_edit_delete_or_assign_a_series(self):
        entry = self.create(repeat_every_days=1)
        user, _, token = self.create_account("calendar-stranger")
        user.is_staff, user.is_superuser = True, True
        user.save(update_fields=["is_staff", "is_superuser"])
        self.authenticate(token)
        url = f"/api/v1/planner/{entry.pk}/?scope=following"
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.patch(url, {"color": "rose"}, format="json").status_code, 404)
        self.assertEqual(self.client.delete(url).status_code, 404)
        other = self.create(recurrence_id=entry.recurrence_id)
        self.assertIsNone(other.recurrence_id)
        self.assertTrue(PlannerEntry.objects.filter(pk=entry.pk).exists())

    def test_completed_task_cannot_turn_into_a_completed_event(self):
        entry = self.create(kind="task", category="habit", is_complete=True)
        response = self.patch(entry, {"kind": "event", "category": "appointment"})
        self.assertEqual(response.status_code, 400, response.data)
        entry.refresh_from_db()
        self.assertEqual(entry.kind, "task")
        self.assertIsNotNone(entry.completed_at)

    def test_workout_repeat_retains_association_and_does_not_adopt_an_independent_task(self):
        workout = WorkoutTemplate.objects.create(owner=self.profile, name="Training")
        independent = PlannerEntry.objects.create(
            owner=self.profile, kind="task", category="workout", title="My own title",
            scheduled_date=self.start + timedelta(days=2), workout=workout,
        )
        first = self.create(kind="task", category="workout", repeat_every_days=1, workout=workout.pk)
        independent.refresh_from_db()
        self.assertIsNone(independent.recurrence_id)
        self.assertEqual(independent.title, "My own title")
        self.assertTrue(all(row.workout_id == workout.pk for row in self.rows(first)))
        self.assertEqual(PlannerEntry.objects.filter(workout=workout, scheduled_date=independent.scheduled_date).count(), 1)

    def test_workout_collision_on_series_move_rolls_back_everything(self):
        workout = WorkoutTemplate.objects.create(owner=self.profile, name="Training")
        first = self.create(kind="task", category="workout", repeat_every_days=2, workout=workout.pk)
        selected = self.rows(first)[2]
        target = selected.scheduled_date + timedelta(days=1)
        PlannerEntry.objects.create(owner=self.profile, kind="task", category="workout", title="Independent", scheduled_date=target, workout=workout)
        count, original_rule = PlannerRecurrence.objects.count(), first.recurrence_id
        response = self.patch(selected, {"scheduled_date": str(target), "color": "rose"}, "following")
        self.assertEqual(response.status_code, 400, response.data)
        selected.refresh_from_db()
        self.assertEqual(PlannerRecurrence.objects.count(), count)
        self.assertEqual(selected.recurrence_id, original_rule)
        self.assertEqual(selected.color, "teal")

    def test_materialization_can_be_bounded_to_named_owned_rules(self):
        first = self.create(repeat_every_days=1)
        second = self.create(repeat_every_days=1)
        for entry in (first, second):
            entry.recurrence.entries.exclude(pk=entry.pk).delete()
            entry.recurrence.materialized_through = self.start
            entry.recurrence.save()
        materialize_planner_repeats(self.profile, self.start + timedelta(days=3), recurrence_ids=[first.recurrence_id])
        self.assertEqual(first.recurrence.entries.count(), 4)
        self.assertEqual(second.recurrence.entries.count(), 1)

    def test_new_response_fields_are_backward_compatible_not_required_additions(self):
        self.assertEqual(PlannerEntrySerializer.optional_response_fields, {"color", "scheduling_timezone", "recurrence_id"})
        entry = self.create()
        data = PlannerEntrySerializer(entry).data
        self.assertIsNone(data["recurrence_id"])
        self.assertEqual(data["color"], "teal")

    def test_legacy_backfill_preserves_rows_and_assigns_independent_series_ids(self):
        first = self.create(repeat_every_days=7, scheduled_time="08:00")
        second = self.create(repeat_every_days=7)
        before = PlannerEntry.objects.count()
        PlannerEntry.objects.filter(recurrence__isnull=False).update(recurrence_date=None, scheduling_timezone="")
        migration = import_module("core.migrations.0091_calendar_recurrence_metadata")
        migration.retain_existing_occurrence_identity(apps, SimpleNamespace(connection=SimpleNamespace(alias="default")))
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(PlannerEntry.objects.count(), before)
        self.assertEqual(first.recurrence_date, first.scheduled_date)
        self.assertEqual(first.scheduling_timezone, "UTC")
        self.assertNotEqual(first.recurrence.series_id, second.recurrence.series_id)
