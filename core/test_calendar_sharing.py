"""Read-only calendar permissions must never broaden private planner CRUD."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event, local
from unittest import skipUnless
from unittest.mock import patch

from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APITestCase
from rest_framework.throttling import SimpleRateThrottle

from .calendar_sharing import create_invitations
from .models import (Block, CalendarShare, CalendarShareExclusion, Follow, PlannerEntry,
                     PlannerRecurrence, RepbaseUser, materialize_planner_repeats, today_for)
from .tests import RepbaseAPITestMixin


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class CalendarSharingTests(RepbaseAPITestMixin, APITestCase):
    def setUp(self):
        self.owner_user, self.owner, self.owner_token = self.create_account("calendar-owner")
        self.reader_user, self.reader, self.reader_token = self.create_account("calendar-reader")
        self.other_user, self.other, self.other_token = self.create_account("calendar-other")
        self.day = today_for(self.owner)
        Follow.objects.create(follower=self.reader, following=self.owner)
        self.authenticate(self.owner_token)

    def entry(self, **extra):
        attrs = dict(owner=self.owner, title="Read together", category="study", scheduled_date=self.day,
                     scheduled_time="10:00", duration_minutes=30, color="teal", scheduling_timezone="America/Chicago",
                     notes="DO NOT SHARE THESE NOTES")
        attrs.update(extra)
        return PlannerEntry.objects.create(**attrs)

    def series(self, *, starts_on=None, materialize=5, **extra):
        attrs = dict(owner=self.owner, title="Daily practice", category="study", starts_on=starts_on or self.day,
                     scheduled_time="12:00", duration_minutes=45, color="sage", scheduling_timezone="America/Chicago")
        attrs.update(extra)
        rule = PlannerRecurrence.objects.create(**attrs)
        materialize_planner_repeats(self.owner, rule.starts_on + timedelta(days=materialize), recurrence_ids=[rule.pk])
        return rule, PlannerEntry.objects.filter(recurrence=rule, parent__isnull=True).order_by("scheduled_date").first()

    def invite(self, entry, *, recipients=None, scope="occurrence", reinvite=False, code=201):
        self.authenticate(self.owner_token)
        result = self.client.post("/api/v1/calendar/shares/", dict(source_entry_id=entry.pk,
            recipient_ids=recipients or [self.reader.pk], scope=scope, reinvite=reinvite), format="json")
        self.assertEqual(result.status_code, code, result.data)
        return result

    def accept(self, grant, *, token=None, code=200):
        identifier = grant["id"] if isinstance(grant, dict) else grant.pk
        self.authenticate(token or self.reader_token)
        response = self.client.post(f"/api/v1/calendar/invitations/{identifier}/accept/", format="json")
        self.assertEqual(response.status_code, code, response.data)
        return response

    def day_items(self, day=None, **params):
        self.authenticate(self.reader_token)
        return self.client.get("/api/v1/calendar/shared/day/", {"date": str(day or self.day), **params})

    def month(self, start=None, end=None, **params):
        self.authenticate(self.reader_token)
        return self.client.get("/api/v1/calendar/shared/month/", dict(start=str(start or self.day), end=str(end or self.day), **params))

    def active(self, entry, **kwargs):
        row = self.invite(entry, **kwargs).data["shares"][0]
        self.accept(row)
        return CalendarShare.objects.get(pk=row["id"])

    def test_pending_then_accept_is_read_only_and_limited(self):
        entry = self.entry()
        PlannerEntry.objects.create(owner=self.owner, title="PRIVATE STEP", notes="PRIVATE STEP NOTES", parent=entry,
                                    scheduled_date=self.day, category="study")
        invite = self.invite(entry).data["shares"][0]
        self.assertEqual(self.day_items().data["results"], [])
        self.authenticate(self.reader_token)
        inbox = self.client.get("/api/v1/calendar/invitations/")
        self.assertEqual(inbox.data["count"], 1)
        self.assertEqual(inbox.data["results"][0]["item"]["title"], entry.title)
        self.accept(invite)
        item = self.day_items().data["results"][0]
        self.assertEqual(set(item), {"id", "share_id", "owner", "kind", "title", "category", "color", "scheduling_timezone",
                                      "scheduled_date", "scheduled_time", "duration_minutes", "is_recurring", "is_complete"})
        self.assertEqual(item["scheduling_timezone"], "America/Chicago")
        self.assertNotIn("DO NOT SHARE", str(item))
        self.assertNotIn("PRIVATE STEP", str(item))
        self.assertEqual(self.client.get(f"/api/v1/planner/{entry.pk}/").status_code, 404)
        self.assertEqual(self.client.patch(f"/api/v1/planner/{entry.pk}/", {"is_complete": True}, format="json").status_code, 404)
        self.assertEqual(self.client.delete(f"/api/v1/planner/{entry.pk}/").status_code, 404)
        entry.refresh_from_db()
        self.assertIsNone(entry.completed_at)
        own_list = self.client.get("/api/v1/planner/")
        self.assertEqual(own_list.data["count"], 0)
        self.assertIn("no-store", inbox["Cache-Control"])

    def test_followers_only_not_reverse_follows_or_pending_requests(self):
        Follow.objects.create(follower=self.owner, following=self.other)
        response = self.invite(self.entry(), recipients=[self.other.pk], code=400)
        self.assertIn("recipient_ids", response.data)
        self.assertFalse(CalendarShare.objects.exists())
        self.authenticate(self.owner_token)
        people = self.client.get("/api/v1/calendar/share-recipients/").data["results"]
        self.assertEqual([row["id"] for row in people], [self.reader.pk])
        self.assertEqual(self.client.get("/api/v1/calendar/share-recipients/", {"search": "@calendar-read"}).data["count"], 1)
        self.assertEqual(self.client.get("/api/v1/calendar/share-recipients/", {"search": "calendar-other"}).data["count"], 0)

    def test_batch_is_atomic_and_validated(self):
        entry = self.entry()
        for payload in (
            {"recipient_ids": [self.reader.pk, self.other.pk]}, {"recipient_ids": [self.owner.pk]},
            {"recipient_ids": [self.reader.pk, self.reader.pk]}, {"recipient_ids": []},
            {"recipient_ids": list(range(1, 22))}, {"scope": "everyone"}, {"recipient_ids": [False]},
        ):
            with self.subTest(payload=payload):
                self.authenticate(self.owner_token)
                body = {"source_entry_id": entry.pk, "recipient_ids": [self.reader.pk], **payload}
                self.assertEqual(self.client.post("/api/v1/calendar/shares/", body, format="json").status_code, 400)
                self.assertFalse(CalendarShare.objects.exists())

    def test_cannot_share_someone_elses_item_or_subtask_or_series_of_once(self):
        entry = self.entry(owner=self.other)
        self.invite(entry, code=404)
        parent = self.entry()
        step = self.entry(parent=parent)
        self.invite(step, code=404)
        self.invite(parent, scope="series", code=400)
        self.assertFalse(CalendarShare.objects.exists())

    def test_duplicate_pending_and_accepted_are_idempotent(self):
        entry = self.entry()
        first = self.invite(entry).data["shares"][0]
        duplicate = self.invite(entry, code=200).data["shares"][0]
        self.assertEqual(first["id"], duplicate["id"])
        self.accept(first)
        again = self.invite(entry, code=200).data["shares"][0]
        self.assertEqual(again["status"], "accepted")
        self.assertEqual(CalendarShare.objects.count(), 1)

    def test_decline_requires_explicit_reinvite(self):
        entry = self.entry()
        share = self.invite(entry).data["shares"][0]
        self.authenticate(self.reader_token)
        response = self.client.post(f"/api/v1/calendar/invitations/{share['id']}/decline/")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data["item"])
        self.assertEqual(self.invite(entry, code=409).data["code"], "reinvite_required")
        renewed = self.invite(entry, reinvite=True).data["shares"][0]
        self.assertEqual(renewed["id"], share["id"])
        self.assertEqual(renewed["status"], "pending")
        self.assertIsNone(renewed["responded_at"])

    def test_revoke_is_owner_only_and_cannot_silently_reactivate(self):
        entry = self.entry()
        share = self.active(entry)
        self.authenticate(self.other_token)
        self.assertEqual(self.client.post(f"/api/v1/calendar/shares/{share.pk}/revoke/").status_code, 404)
        self.authenticate(self.owner_token)
        response = self.client.post(f"/api/v1/calendar/shares/{share.pk}/revoke/")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data["item"])
        self.assertEqual(self.day_items().data["count"], 0)
        self.accept(share, code=409)
        self.invite(entry, code=409)
        self.invite(entry, reinvite=True)

    def test_other_recipient_and_owner_cannot_answer_invitation(self):
        share = self.invite(self.entry()).data["shares"][0]
        self.accept(share, token=self.other_token, code=404)
        self.accept(share, token=self.owner_token, code=404)
        self.authenticate(self.other_token)
        self.assertEqual(self.client.get("/api/v1/calendar/invitations/").data["count"], 0)

    def test_anonymous_is_refused_every_read_and_write(self):
        entry = self.entry()
        share = self.invite(entry).data["shares"][0]
        self.client.credentials()
        for path in ("calendar/shares/", "calendar/invitations/", "calendar/share-recipients/", "calendar/shared/people/",
                     f"calendar/shared/day/?date={self.day}", f"calendar/shared/month/?start={self.day}&end={self.day}"):
            self.assertEqual(self.client.get("/api/v1/" + path).status_code, 401)
        self.assertEqual(self.client.post(f"/api/v1/calendar/invitations/{share['id']}/accept/").status_code, 401)

    def test_unfollow_revokes_durably_including_refollow(self):
        share = self.active(self.entry())
        self.authenticate(self.reader_token)
        self.assertEqual(self.client.delete(f"/api/v1/users/{self.owner.pk}/follow/").status_code, 204)
        share.refresh_from_db()
        self.assertEqual(share.status, "revoked")
        Follow.objects.create(follower=self.reader, following=self.owner)
        self.assertEqual(self.day_items().data["count"], 0)
        self.assertEqual(self.client.get("/api/v1/calendar/shared/people/").data["count"], 0)

    def test_raw_blocks_both_directions_revoke_and_unblocking_does_not_restore(self):
        for blocker, blocked in ((self.owner, self.reader), (self.reader, self.owner)):
            with self.subTest(blocker=blocker.pk):
                entry = self.entry()
                share = self.active(entry)
                block = Block.objects.create(blocker=blocker, blocked=blocked)
                share.refresh_from_db()
                self.assertEqual(share.status, "revoked")
                self.assertEqual(self.day_items().data["count"], 0)
                block.delete()
                self.assertEqual(self.day_items().data["count"], 0)

    def test_blocked_pending_and_suspended_accounts_never_appear(self):
        entry = self.entry()
        share = self.invite(entry).data["shares"][0]
        self.owner_user.is_active = False
        self.owner_user.save(update_fields=["is_active"])
        self.authenticate(self.reader_token)
        self.assertEqual(self.client.get("/api/v1/calendar/invitations/").data["count"], 0)
        self.accept(share, code=404)
        self.owner_user.is_active = True
        self.owner_user.save(update_fields=["is_active"])
        Block.objects.create(blocker=self.owner, blocked=self.reader)
        self.authenticate(self.owner_token)
        self.assertEqual(self.client.get("/api/v1/calendar/share-recipients/").data["count"], 0)
        self.invite(entry, code=400)

    def test_block_committing_during_projection_is_rechecked_before_response(self):
        from . import calendar_sharing
        self.active(self.entry())
        original = calendar_sharing.people_payload

        def block_while_projecting(*args, **kwargs):
            people = original(*args, **kwargs)
            Block.objects.create(blocker=self.owner, blocked=self.reader)
            return people

        with patch("core.calendar_sharing.people_payload", side_effect=block_while_projecting):
            self.assertEqual(self.day_items().data["count"], 0)

    def test_private_recipient_identity_does_not_leak_name_or_photo(self):
        self.reader.is_profile_public = False
        self.reader.save(update_fields=["is_profile_public"])
        self.reader_user.first_name = "PRIVATE NAME"
        self.reader_user.save(update_fields=["first_name"])
        self.authenticate(self.owner_token)
        item = self.client.get("/api/v1/calendar/share-recipients/").data["results"][0]
        self.assertEqual(item["username"], self.reader_user.username)
        self.assertEqual(item["display_name"], "")
        self.assertIsNone(item["profile_photo_url"])
        self.assertEqual(self.client.get("/api/v1/calendar/share-recipients/", {"search": "PRIVATE NAME"}).data["count"], 0)
        Follow.objects.create(follower=self.owner, following=self.reader)
        self.assertEqual(self.client.get("/api/v1/calendar/share-recipients/").data["results"][0]["display_name"], self.reader_user.get_full_name())

    def test_series_starts_at_selected_identity_not_prior_history(self):
        rule, first = self.series(starts_on=self.day - timedelta(days=3), materialize=6)
        current = PlannerEntry.objects.get(recurrence=rule, scheduled_date=self.day)
        share = self.active(current, scope="series")
        self.assertEqual(share.starts_on, self.day)
        self.assertEqual(self.day_items(self.day - timedelta(days=1)).data["count"], 0)
        self.assertEqual(self.day_items().data["count"], 1)
        self.assertEqual(self.day_items(self.day + timedelta(days=1)).data["count"], 1)
        self.assertEqual(self.invite(first, scope="series", code=409).data["code"], "scope_conflict")

    def test_future_projection_does_not_write_or_recreate_deleted_days(self):
        rule, first = self.series(materialize=2)
        self.active(first, scope="series")
        PlannerEntry.objects.get(recurrence=rule, scheduled_date=self.day + timedelta(days=1)).delete()
        before = PlannerEntry.objects.count()
        self.assertEqual(self.day_items(self.day + timedelta(days=1)).data["count"], 0)
        projected = self.day_items(self.day + timedelta(days=20))
        self.assertEqual(projected.data["count"], 1)
        self.assertEqual(PlannerEntry.objects.count(), before)
        self.assertFalse(projected.data["results"][0]["is_complete"])
        stable = projected.data["results"][0]["id"]
        materialize_planner_repeats(self.owner, self.day + timedelta(days=20), recurrence_ids=[rule.pk])
        self.assertEqual(self.day_items(self.day + timedelta(days=20)).data["results"][0]["id"], stable)

    def test_series_grant_survives_first_occurrence_deletion(self):
        rule, first = self.series()
        share = self.active(first, scope="series")
        first.delete()
        share.refresh_from_db()
        self.assertIsNone(share.invited_entry_id)
        self.assertEqual(self.day_items(self.day + timedelta(days=1)).data["count"], 1)

    def test_occurrence_and_series_grants_coalesce(self):
        rule, first = self.series()
        self.active(first, scope="series")
        direct = self.active(first)
        items = self.day_items()
        self.assertEqual(items.data["count"], 1)
        self.assertEqual(items.data["results"][0]["share_id"], direct.pk)
        self.assertEqual(self.month().data["days"][0]["total"], 1)

    def test_make_occurrence_private_survives_date_move(self):
        rule, first = self.series()
        self.active(first, scope="series")
        direct = self.active(first)
        self.authenticate(self.owner_token)
        result = self.client.post("/api/v1/calendar/shares/privacy/", {"source_entry_id": first.pk}, format="json")
        self.assertEqual(result.status_code, 204)
        direct.refresh_from_db()
        self.assertEqual(direct.status, "revoked")
        first.scheduled_date = self.day + timedelta(days=10)
        first.save(update_fields=["scheduled_date"])
        self.assertEqual(self.day_items(first.scheduled_date).data["count"], 1)  # virtual day10 only, not moved private day0
        self.assertEqual(self.day_items().data["count"], 0)
        self.assertEqual(self.day_items(self.day + timedelta(days=1)).data["count"], 1)
        self.assertEqual(CalendarShareExclusion.objects.get().occurrence_date, self.day)

    def test_make_series_private_revokes_all_lineage_and_direct_grants(self):
        rule, first = self.series()
        self.active(first, scope="series")
        later = PlannerEntry.objects.get(recurrence=rule, scheduled_date=self.day + timedelta(days=1))
        self.active(later)
        self.authenticate(self.owner_token)
        response = self.client.post("/api/v1/calendar/shares/privacy/", {"source_entry_id": first.pk, "scope": "series"}, format="json")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(set(CalendarShare.objects.values_list("status", flat=True)), {"revoked"})
        self.assertEqual(self.day_items(later.scheduled_date).data["count"], 0)

    def test_moved_occurrence_is_not_rebuilt_at_original_position(self):
        rule, first = self.series(materialize=1)
        self.active(first, scope="series")
        first.scheduled_date = self.day + timedelta(days=10)
        first.save(update_fields=["scheduled_date"])
        self.assertEqual(self.day_items().data["count"], 0)
        self.assertEqual(self.day_items(first.scheduled_date).data["count"], 2)

    def test_moving_old_history_after_grant_start_cannot_publish_it(self):
        rule, first = self.series(starts_on=self.day - timedelta(days=3), materialize=6)
        current = PlannerEntry.objects.get(recurrence=rule, scheduled_date=self.day)
        self.active(current, scope="series")
        first.scheduled_date = self.day + timedelta(days=10)
        first.save(update_fields=["scheduled_date"])
        rows = self.day_items(first.scheduled_date).data["results"]
        self.assertEqual(len(rows), 1)
        self.assertNotEqual(rows[0]["id"].split(":")[-1], str(first.recurrence_date))

    def test_following_edit_keeps_grant_and_private_identity_after_moving_earlier(self):
        rule, first = self.series(materialize=5)
        selected = PlannerEntry.objects.get(recurrence=rule, scheduled_date=self.day + timedelta(days=2))
        share = self.active(selected, scope="series")
        hidden = PlannerEntry.objects.get(recurrence=rule, scheduled_date=self.day + timedelta(days=3))
        self.authenticate(self.owner_token)
        self.client.post("/api/v1/calendar/shares/privacy/", {"source_entry_id": hidden.pk}, format="json")
        response = self.client.patch(f"/api/v1/planner/{selected.pk}/?scope=following", {
            "scheduled_date": str(self.day), "color": "rose", "title": "Rescheduled practice",
        }, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        selected.refresh_from_db()
        hidden.refresh_from_db()
        self.assertEqual(selected.recurrence.series_id, share.series_id)
        self.assertEqual(selected.recurrence_date, share.starts_on)
        visible = self.day_items()
        self.assertEqual(visible.data["count"], 1)
        self.assertEqual(visible.data["results"][0]["title"], "Rescheduled practice")
        self.assertEqual(visible.data["results"][0]["color"], "rose")
        self.assertEqual(self.day_items(hidden.scheduled_date).data["count"], 0)  # old pre-grant row and moved private occurrence both stay hidden
        hidden_id = f"{self.owner.pk}:series:{share.series_id}:{hidden.recurrence_date}"
        self.assertNotIn(hidden_id, {row["id"] for row in self.day_items(hidden.scheduled_date).data["results"]})

    def test_live_completion_is_visible_but_no_completed_timestamp(self):
        from django.utils import timezone
        entry = self.entry(completed_at=timezone.now())
        self.active(entry)
        self.assertTrue(self.day_items().data["results"][0]["is_complete"])
        entry.completed_at = None
        entry.save(update_fields=["completed_at"])
        self.assertFalse(self.day_items().data["results"][0]["is_complete"])
        event = self.entry(kind="event", category="meeting", completed_at=timezone.now())
        self.active(event)
        rows = self.day_items().data["results"]
        self.assertFalse(next(row for row in rows if row["kind"] == "event")["is_complete"])

    def test_month_preview_two_with_full_count_and_selected_day_all(self):
        for number in range(4):
            self.active(self.entry(title=f"Item{number}"))
        day = self.month().data["days"][0]
        self.assertEqual(day["total"], 4)
        self.assertEqual(len(day["items"]), 2)
        self.assertEqual(self.day_items().data["count"], 4)

    def test_owner_filter_precedes_counts_samples_and_people_are_distinct(self):
        for number in range(3):
            self.active(self.entry(title=f"Owner item{number}"))
        Follow.objects.create(follower=self.reader, following=self.other)
        second = self.entry(owner=self.other, title="Other owner's item")
        extra = CalendarShare.objects.create(owner=self.other, recipient=self.reader, scope="occurrence", source_entry=second,
                                            invited_entry=second, status="accepted")
        summary = self.month(owner_ids=str(self.other.pk)).data["days"][0]
        self.assertEqual(summary["total"], 1)
        self.assertEqual(summary["items"][0]["owner"]["id"], self.other.pk)
        self.assertEqual(self.day_items(owner_ids=str(self.other.pk)).data["count"], 1)
        self.assertEqual(self.month(owner_ids="").data["days"], [])
        self.assertEqual(self.day_items(owner_ids="99999999").data["count"], 0)
        self.authenticate(self.reader_token)
        people = self.client.get("/api/v1/calendar/shared/people/").data
        self.assertEqual(people["count"], 2)
        self.assertEqual({row["id"] for row in people["results"]}, {self.owner.pk, self.other.pk})
        extra.status = "revoked"
        extra.save(update_fields=["status"])
        self.assertEqual(self.client.get("/api/v1/calendar/shared/people/").data["count"], 1)

    def test_bad_windows_and_owner_filters_are_refused(self):
        self.assertEqual(self.month(end=self.day + timedelta(days=41)).status_code, 200)
        self.assertEqual(self.month(end=self.day + timedelta(days=42)).status_code, 400)
        self.assertEqual(self.month(end=self.day - timedelta(days=1)).status_code, 400)
        self.authenticate(self.reader_token)
        self.assertEqual(self.client.get("/api/v1/calendar/shared/month/").status_code, 400)
        self.assertEqual(self.client.get("/api/v1/calendar/shared/day/", {"date": "garbage"}).status_code, 400)
        for value in ("0", "-1", "1,", "one", "1, 2", ",".join(str(i + 1) for i in range(251))):
            self.assertEqual(self.day_items(owner_ids=value).status_code, 400)

    def test_owner_management_source_filter_and_no_staff_bypass(self):
        first, second = self.entry(), self.entry(title="Other occurrence")
        selected = self.active(first)
        self.active(second)
        self.authenticate(self.owner_token)
        result = self.client.get("/api/v1/calendar/shares/", {"source_entry_id": first.pk})
        self.assertEqual([row["id"] for row in result.data["results"]], [selected.pk])
        self.other_user.is_superuser = True
        self.other_user.save(update_fields=["is_superuser"])
        self.authenticate(self.other_token)
        self.assertEqual(self.client.get("/api/v1/calendar/shares/").data["count"], 0)
        self.assertEqual(self.client.get("/api/v1/calendar/shares/", {"source_entry_id": first.pk}).status_code, 404)
        self.assertEqual(self.client.post("/api/v1/calendar/shares/privacy/", {"source_entry_id": first.pk}, format="json").status_code, 404)

    def test_private_occurrence_manages_series_without_claiming_current_visibility(self):
        rule, first = self.series()
        self.active(first, scope="series")
        self.authenticate(self.owner_token)
        self.client.post("/api/v1/calendar/shares/privacy/", {"source_entry_id": first.pk}, format="json")
        result = self.client.get("/api/v1/calendar/shares/", {"source_entry_id": first.pk})
        self.assertEqual(result.data["count"], 1)
        self.assertFalse(result.data["results"][0]["applies_to_selected_occurrence"])
        self.assertIsNone(result.data["results"][0]["item"])
        later = PlannerEntry.objects.get(recurrence=rule, scheduled_date=self.day + timedelta(days=1))
        response = self.client.get("/api/v1/calendar/shares/", {"source_entry_id": later.pk})
        self.assertEqual(response.data["count"], 1)
        self.assertTrue(response.data["results"][0]["applies_to_selected_occurrence"])
        self.assertEqual(response.data["results"][0]["item"]["scheduled_date"], str(later.scheduled_date))

    def test_earlier_occurrence_can_manage_later_series_boundary(self):
        rule, first = self.series()
        later = PlannerEntry.objects.get(recurrence=rule, scheduled_date=self.day + timedelta(days=2))
        share = self.active(later, scope="series")
        self.authenticate(self.owner_token)
        response = self.client.get("/api/v1/calendar/shares/", {"source_entry_id": first.pk})
        self.assertEqual(response.data["count"], 1)
        result = response.data["results"][0]
        self.assertEqual(result["id"], share.pk)
        self.assertEqual(result["starts_on"], str(later.recurrence_date))
        self.assertFalse(result["applies_to_selected_occurrence"])
        self.assertIsNone(result["item"])
        self.assertEqual(self.client.post(f"/api/v1/calendar/shares/{share.pk}/revoke/").status_code, 200)

    def test_named_invite_throttle_and_no_partial_write_when_limited(self):
        with patch.dict(SimpleRateThrottle.THROTTLE_RATES, {"calendar_invite": "1/day"}):
            self.invite(self.entry())
            self.invite(self.entry(), code=429)
        self.assertEqual(CalendarShare.objects.count(), 1)

    def test_quotas_and_projection_ceiling_are_explicit(self):
        self.active(self.entry())
        with patch("core.calendar_sharing.MAX_OWNER_ACTIVE_SHARES", 1):
            self.invite(self.entry(), code=400)
        with patch("core.calendar_sharing.MAX_RECIPIENT_ACTIVE_SHARES", 1):
            self.invite(self.entry(), code=400)
        self.active(self.entry())
        with patch("core.calendar_sharing.MAX_RECIPIENT_ACTIVE_SHARES", 1):
            self.assertEqual(self.day_items().status_code, 503)

    def test_many_one_off_items_have_constant_query_count_and_paginate(self):
        self.active(self.entry())
        with CaptureQueriesContext(connection) as small:
            self.assertEqual(self.month().status_code, 200)
        entries = [self.entry(title=f"Planned{index}") for index in range(50)]
        CalendarShare.objects.bulk_create([CalendarShare(owner=self.owner, recipient=self.reader, scope="occurrence", source_entry=entry,
                                                       invited_entry=entry, status="accepted") for entry in entries])
        with CaptureQueriesContext(connection) as large:
            self.assertEqual(self.month().data["days"][0]["total"], 51)
        self.assertLessEqual(len(large), len(small) + 1)
        page = self.day_items()
        self.assertEqual(page.data["count"], 51)
        self.assertEqual(len(page.data["results"]), 50)
        self.assertTrue(page.data["next"])
        second = self.client.get(page.data["next"])
        self.assertEqual(len(second.data["results"]), 1)

    def test_series_query_count_is_bounded_and_reads_do_not_write(self):
        _, first = self.series()
        self.active(first, scope="series")
        with CaptureQueriesContext(connection) as small:
            self.assertEqual(self.month(end=self.day + timedelta(days=20)).status_code, 200)
        for number in range(8):
            _, first = self.series(title=f"Practice{number}")
            self.active(first, scope="series")
        before = PlannerEntry.objects.count()
        with CaptureQueriesContext(connection) as large:
            result = self.month(end=self.day + timedelta(days=20))
            self.assertEqual(result.status_code, 200, result.data)
        self.assertLessEqual(len(large), len(small) + 1)
        self.assertEqual(PlannerEntry.objects.count(), before)


@skipUnless(connection.vendor == "postgresql", "PostgreSQL row locks are required")
@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class CalendarSharingConcurrencyTests(RepbaseAPITestMixin, TransactionTestCase):
    def setUp(self):
        _, self.owner, _ = self.create_account("concurrent-calendar-owner")
        _, self.reader, _ = self.create_account("concurrent-calendar-reader")
        Follow.objects.create(follower=self.reader, following=self.owner)
        self.entry = PlannerEntry.objects.create(owner=self.owner, title="Concurrent", scheduled_date=today_for(self.owner))

    def test_duplicate_concurrent_invitations_have_one_pending_row(self):
        barrier = Barrier(2)

        def create():
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                rows, changed = create_invitations(self.owner, {"source_entry_id": self.entry.pk, "recipient_ids": [self.reader.pk],
                                                              "scope": "occurrence", "reinvite": False})
                return rows[0].pk, changed
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: create(), range(2)))
        self.assertEqual(len({row[0] for row in results}), 1)
        self.assertEqual(sorted(row[1] for row in results), [False, True])
        self.assertEqual(CalendarShare.objects.count(), 1)

    def _create_racing_relationship_change(self, change):
        reached_create, allow_create, changing = Event(), Event(), Event()
        original = CalendarShare.objects.create

        def delayed_create(**kwargs):
            reached_create.set()
            if not allow_create.wait(timeout=5):
                raise AssertionError("Relationship race did not release invitation creation")
            return original(**kwargs)

        def create():
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                return create_invitations(self.owner, {"source_entry_id": self.entry.pk, "recipient_ids": [self.reader.pk],
                                                       "scope": "occurrence", "reinvite": False})[0][0].pk
            finally:
                close_old_connections()

        def mutate():
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                changing.set()
                change()
            finally:
                close_old_connections()

        with patch.object(CalendarShare.objects, "create", side_effect=delayed_create):
            with ThreadPoolExecutor(max_workers=2) as pool:
                invitation = pool.submit(create)
                self.assertTrue(reached_create.wait(timeout=5))
                mutation = pool.submit(mutate)
                self.assertTrue(changing.wait(timeout=5))
                allow_create.set()
                identifier = invitation.result(timeout=10)
                mutation.result(timeout=10)
        self.assertEqual(CalendarShare.objects.get(pk=identifier).status, "revoked")

    def test_concurrent_unfollow_cannot_miss_a_new_uncommitted_invitation(self):
        self._create_racing_relationship_change(lambda: Follow.objects.filter(follower=self.reader, following=self.owner).delete())

    def test_concurrent_raw_block_cannot_miss_a_new_uncommitted_invitation(self):
        self._create_racing_relationship_change(lambda: Block.objects.create(blocker=self.reader, blocked=self.owner))

    def test_inverse_raw_block_fk_keyshare_does_not_deadlock_invitation_mutex(self):
        # Deliberately force immediate FK checks: normal Django PostgreSQL FKs
        # are deferred. This stress interleaving verifies the profile mutex is
        # NO KEY UPDATE, which can coexist with unrelated FK KEY SHARE locks.
        before_mutex, release_block = Event(), Event()
        worker = local()
        original = RepbaseUser.objects.select_for_update

        def before_profile_lock(*args, **kwargs):
            if getattr(worker, "is_block", False):
                before_mutex.set()
                if not release_block.wait(timeout=10):
                    raise AssertionError("Invitation did not release block signal")
            return original(*args, **kwargs)

        def block():
            close_old_connections()
            worker.is_block = True
            try:
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute("SET LOCAL lock_timeout = '5s'")
                        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
                    Block.objects.create(blocker=self.reader, blocked=self.owner)
            finally:
                close_old_connections()

        def invite():
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                return create_invitations(self.owner, {"source_entry_id": self.entry.pk, "recipient_ids": [self.reader.pk],
                                                       "scope": "occurrence", "reinvite": False})[0][0].pk
            finally:
                close_old_connections()

        with patch.object(RepbaseUser.objects, "select_for_update", side_effect=before_profile_lock):
            with ThreadPoolExecutor(max_workers=2) as pool:
                blocking = pool.submit(block)
                self.assertTrue(before_mutex.wait(timeout=5))
                invitation = pool.submit(invite)
                try:
                    identifier = invitation.result(timeout=7)
                finally:
                    release_block.set()
                blocking.result(timeout=7)
        self.assertEqual(CalendarShare.objects.get(pk=identifier).status, "revoked")

    def test_mutual_invitations_use_one_profile_lock_order(self):
        Follow.objects.create(follower=self.owner, following=self.reader)
        reverse = PlannerEntry.objects.create(owner=self.reader, title="Reverse", scheduled_date=today_for(self.reader))
        barrier = Barrier(2)

        def create(args):
            owner, recipient, entry = args
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                barrier.wait(timeout=5)
                return create_invitations(owner, {"source_entry_id": entry.pk, "recipient_ids": [recipient.pk],
                                                  "scope": "occurrence", "reinvite": False})[0][0].pk
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(create, [(self.owner, self.reader, self.entry), (self.reader, self.owner, reverse)]))
        self.assertEqual(len(set(results)), 2)
