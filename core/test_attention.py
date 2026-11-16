"""Milestone 3, the server's half: attention summaries are bounded and safe to
send twice.

The exit criterion is that duplicate, reordered, interrupted and malicious
batches cannot inflate totals, poison other users, or exhaust memory. Each of
those words has a test class below.
"""

import json
import threading
import uuid
from datetime import timedelta

from django.core.management import call_command
from django.db import connection, connections
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from .attention import MAX_BODY_BYTES
from .fixtures_for_tests import make_account, meal_post, planner_post, workout_post
from .models import Block, ImpressionBatchReceipt, Post, PostVideo, PostView
from .tests import RepbaseAPITestMixin

IMPRESSIONS = "/api/v1/social/impressions/"
CLIP_MS = 10_000


def clip(post, duration_ms=CLIP_MS, status=PostVideo.Status.APPROVED):
    return PostVideo.objects.create(
        post=post, file=f"post-videos/{post.pk}.mp4", content_type="video/mp4", codec="avc1",
        duration_ms=duration_ms, width=64, height=48, size_bytes=10, status=status,
    )


class AttentionTestMixin(RepbaseAPITestMixin):
    def setUp(self):
        _, self.author, _ = self.create_account("author")
        _, self.reader, self.reader_token = self.create_account("reader")
        self.video_post = workout_post(self.author)
        clip(self.video_post)
        self.photo_post = meal_post(self.author)
        self.session = uuid.uuid4()
        self.sequence = 0
        self.authenticate(self.reader_token)

    def summary(self, post, **figures):
        """A truthful summary of watching a ten-second clip for four seconds,
        with anything said in `figures` said instead."""
        self.sequence += 1
        event = {
            "post": post.pk, "surface": "following", "sequence": self.sequence,
            "observed_at": timezone.now().isoformat(),
            "dwell_ms": 5000, "active_dwell_ms": 4500,
            "playback_ms": 4000, "coverage_ms": 4000, "rewatch_ms": 0,
        }
        event.update(figures)
        return event

    def send(self, events, batch_id=None, **body):
        return self.client.post(IMPRESSIONS, {
            "schema_version": 2, "batch_id": str(batch_id or uuid.uuid4()),
            "session_id": str(self.session), "events": events, **body,
        }, format="json")

    def sent(self, events, **body):
        response = self.send(events, **body)
        self.assertEqual(response.status_code, 202, response.data)
        return response.data

    def row(self, post=None, viewer=None):
        return PostView.objects.get(viewer=viewer or self.reader, post=post or self.video_post)

    def measured(self, post=None):
        row = self.row(post)
        return {
            name: getattr(row, name) for name in (
                "view_count", "qualified_views", "active_dwell_ms", "playback_ms", "coverage_ms",
                "rewatch_ms", "watched_through", "attachment_opens", "attachment_read_ms",
                "playback_failures", "skipped",
            )
        }


class SummaryTests(AttentionTestMixin, APITestCase):
    def test_a_summary_is_added_to_the_readers_row(self):
        self.assertEqual(self.sent([self.summary(self.video_post)]), {"recorded": 1, "duplicate": False})
        self.assertEqual(self.measured(), {
            "view_count": 1, "qualified_views": 1, "active_dwell_ms": 4500, "playback_ms": 4000,
            "coverage_ms": 4000, "rewatch_ms": 0, "watched_through": False,
            "attachment_opens": 0, "attachment_read_ms": 0, "playback_failures": 0,
            "skipped": False,
        })

    def test_totals_are_summed_and_coverage_is_the_most_in_one_exposure(self):
        self.sent([self.summary(self.video_post, coverage_ms=3000, rewatch_ms=500)])
        self.sent([self.summary(self.video_post, coverage_ms=6000, playback_ms=4500, rewatch_ms=200)])
        self.sent([self.summary(self.video_post, coverage_ms=2000)])
        found = self.measured()
        self.assertEqual(found["view_count"], 3)
        self.assertEqual(found["active_dwell_ms"], 13500)
        self.assertEqual(found["playback_ms"], 12500)
        self.assertEqual(found["coverage_ms"], 6000)
        self.assertEqual(found["rewatch_ms"], 700)

    def test_the_same_post_twice_in_a_batch_is_one_row_with_both_added(self):
        self.assertEqual(self.sent([
            self.summary(self.video_post), self.summary(self.video_post, coverage_ms=1000),
        ])["recorded"], 1)
        self.assertEqual(self.measured()["view_count"], 2)
        self.assertEqual(self.measured()["coverage_ms"], 4000)

    def test_a_photo_has_no_playback_rather_than_none_watched(self):
        self.sent([self.summary(self.photo_post, completed=True)])
        found = self.measured(self.photo_post)
        self.assertIsNone(found["playback_ms"])
        self.assertIsNone(found["coverage_ms"])
        self.assertIsNone(found["rewatch_ms"])
        self.assertFalse(found["watched_through"])
        self.assertEqual(found["active_dwell_ms"], 4500)
        row = self.row(self.photo_post)
        self.assertIsNone(row.max_watch_ms)
        self.assertFalse(row.completed)

    def test_a_clip_waiting_for_review_is_not_a_clip(self):
        pending = workout_post(self.author)
        clip(pending, status=PostVideo.Status.PENDING)
        self.sent([self.summary(pending)])
        self.assertIsNone(self.measured(pending)["playback_ms"])

    def test_watched_through_needs_a_natural_end_and_nine_tenths_covered(self):
        cases = (
            ("ended, all covered", dict(completed=True, coverage_ms=10000, playback_ms=10000, dwell_ms=11000, active_dwell_ms=11000), True),
            ("ended at exactly 90%", dict(completed=True, coverage_ms=9000, playback_ms=9000, dwell_ms=11000, active_dwell_ms=11000), True),
            ("sought to the end", dict(completed=True, coverage_ms=1500, playback_ms=1500), False),
            ("covered but never ended", dict(completed=False, coverage_ms=10000, playback_ms=10000, dwell_ms=11000, active_dwell_ms=11000), False),
        )
        for label, figures, expected in cases:
            with self.subTest(label):
                post = workout_post(self.author)
                clip(post)
                self.sent([self.summary(post, **figures)])
                self.assertEqual(self.measured(post)["watched_through"], expected)

    def test_opening_the_recipe_is_counted_and_is_not_a_skip(self):
        self.sent([self.summary(
            self.photo_post, dwell_ms=900, active_dwell_ms=600,
            attachment_opened=True, attachment_read_ms=20000,
        )])
        found = self.measured(self.photo_post)
        self.assertEqual((found["attachment_opens"], found["attachment_read_ms"]), (1, 20000))
        self.assertFalse(found["skipped"])
        self.assertEqual(found["qualified_views"], 0)

    def test_a_post_with_nothing_attached_has_nothing_to_open(self):
        plan = planner_post(self.author)
        self.sent([self.summary(plan, attachment_opened=True, attachment_read_ms=5000)])
        self.assertEqual(self.measured(plan)["attachment_opens"], 0)

    def test_a_quick_look_is_a_skip_and_a_broken_player_is_not(self):
        quick = dict(dwell_ms=700, active_dwell_ms=700, playback_ms=300, coverage_ms=300)
        self.sent([self.summary(self.video_post, **quick)])
        self.assertTrue(self.measured()["skipped"])
        other = workout_post(self.author)
        clip(other)
        self.sent([self.summary(other, **{**quick, "playback_ms": 0, "coverage_ms": 0}, playback_failed=True)])
        found = self.measured(other)
        self.assertFalse(found["skipped"])
        self.assertEqual(found["playback_failures"], 1)

    def test_a_flick_past_is_not_an_exposure_at_all(self):
        self.assertEqual(self.sent([self.summary(self.video_post, dwell_ms=249, active_dwell_ms=249)])["recorded"], 0)
        self.assertFalse(PostView.objects.exists())

    def test_the_ranking_in_use_today_still_hears_from_new_apps(self):
        """The legacy columns are fed their honest equivalents."""
        self.sent([self.summary(
            self.video_post, completed=True, coverage_ms=9500, playback_ms=9500,
            dwell_ms=12000, active_dwell_ms=11000, watch_ms=99999,
        )])
        row = self.row()
        self.assertEqual((row.view_count, row.total_dwell_ms), (1, 11000))
        # What was covered, not the position a seek reached or a client claimed.
        self.assertEqual(row.max_watch_ms, 9500)
        self.assertTrue(row.completed)
        self.assertEqual(row.surface, "following")


class LegacyTests(AttentionTestMixin, APITestCase):
    """A version 1 app is recorded as it always was, and nowhere else."""

    def legacy(self, **event):
        return self.client.post(IMPRESSIONS, {"events": [
            {"post": self.video_post.pk, "dwell_ms": 4000, **event}
        ]}, format="json")

    def test_a_version_1_body_writes_only_the_legacy_columns(self):
        response = self.legacy(watch_ms=8000, completed=True)
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(response.data["recorded"], 1)
        row = self.row()
        self.assertEqual((row.view_count, row.total_dwell_ms, row.max_watch_ms, row.completed),
                         (1, 4000, 8000, True))
        # Furthest position is not evidence of watching, and is not filed as it.
        self.assertEqual(self.measured() | {"view_count": 0, "skipped": False}, {
            "view_count": 0, "qualified_views": 0, "active_dwell_ms": 0, "playback_ms": None,
            "coverage_ms": None, "rewatch_ms": None, "watched_through": False,
            "attachment_opens": 0, "attachment_read_ms": 0, "playback_failures": 0,
            "skipped": False,
        })
        self.assertFalse(ImpressionBatchReceipt.objects.exists())

    def test_version_1_fields_sent_with_new_ones_are_still_version_1(self):
        self.legacy(watch_ms=8000, coverage_ms=9000, playback_ms=9000, active_dwell_ms=4000)
        self.assertIsNone(self.measured()["coverage_ms"])

    def test_old_and_new_apps_on_one_account_do_not_mix_their_evidence(self):
        self.legacy(watch_ms=9000)
        self.sent([self.summary(self.video_post, coverage_ms=2000, playback_ms=2000)])
        row = self.row()
        self.assertEqual(row.max_watch_ms, 9000)
        self.assertEqual(row.coverage_ms, 2000)
        self.assertEqual(row.view_count, 2)

    def test_version_2_says_what_it_needs(self):
        event = self.summary(self.video_post)
        for missing in ("batch_id", "session_id"):
            body = {"schema_version": 2, "batch_id": str(uuid.uuid4()),
                    "session_id": str(uuid.uuid4()), "events": [event]}
            del body[missing]
            response = self.client.post(IMPRESSIONS, body, format="json")
            self.assertEqual(response.status_code, 400)
            self.assertIn(missing, response.data)
        for missing in ("sequence", "observed_at"):
            partial = {k: v for k, v in event.items() if k != missing}
            self.assertEqual(self.send([partial]).status_code, 400)
        self.assertEqual(self.send([event], schema_version=3).status_code, 400)
        self.assertFalse(PostView.objects.exists())


class DuplicateTests(AttentionTestMixin, APITestCase):
    def test_a_batch_sent_again_adds_nothing(self):
        batch_id = uuid.uuid4()
        events = [self.summary(self.video_post), self.summary(self.photo_post)]
        first = self.send(events, batch_id=batch_id).data
        before = (self.measured(), self.measured(self.photo_post))
        for _ in range(3):
            again = self.send(events, batch_id=batch_id)
            self.assertEqual(again.status_code, 202)
            self.assertEqual(again.data, {"recorded": first["recorded"], "duplicate": True})
        self.assertEqual((self.measured(), self.measured(self.photo_post)), before)
        self.assertEqual(ImpressionBatchReceipt.objects.count(), 1)

    def test_a_retry_adds_nothing_even_if_its_contents_were_changed(self):
        batch_id = uuid.uuid4()
        self.send([self.summary(self.video_post)], batch_id=batch_id)
        self.send([self.summary(self.video_post, active_dwell_ms=5000, dwell_ms=600000)], batch_id=batch_id)
        self.assertEqual(self.measured()["active_dwell_ms"], 4500)

    def test_a_receipt_is_one_accounts_and_cannot_be_spent_by_another(self):
        batch_id = uuid.uuid4()
        self.send([self.summary(self.video_post)], batch_id=batch_id)
        _, other, other_token = self.create_account("other")
        self.authenticate(other_token)
        response = self.send([self.summary(self.video_post)], batch_id=batch_id)
        self.assertEqual(response.data, {"recorded": 1, "duplicate": False})
        self.assertEqual(self.row(viewer=other).view_count, 1)

    def test_a_batch_that_recorded_nothing_is_still_remembered(self):
        batch_id = uuid.uuid4()
        gone = self.summary(self.video_post)
        gone["post"] = 999_999
        self.assertEqual(self.send([gone], batch_id=batch_id).data["recorded"], 0)
        self.assertTrue(self.send([gone], batch_id=batch_id).data["duplicate"])

    def test_receipts_outlive_the_retry_window_and_are_then_pruned(self):
        self.send([self.summary(self.video_post)])
        self.send([self.summary(self.video_post)])
        old = ImpressionBatchReceipt.objects.first()
        ImpressionBatchReceipt.objects.filter(pk=old.pk).update(
            received_at=timezone.now() - timedelta(days=8)
        )
        recent = ImpressionBatchReceipt.objects.exclude(pk=old.pk).get()
        ImpressionBatchReceipt.objects.filter(pk=recent.pk).update(
            received_at=timezone.now() - timedelta(hours=49)
        )
        call_command("prune_expired_rows", verbosity=0)
        self.assertEqual(list(ImpressionBatchReceipt.objects.values_list("pk", flat=True)), [recent.pk])


class ReorderedTests(AttentionTestMixin, APITestCase):
    def at(self, minutes_ago, **figures):
        return self.summary(
            self.video_post,
            observed_at=(timezone.now() - timedelta(minutes=minutes_ago)).isoformat(), **figures,
        )

    SKIP = dict(dwell_ms=600, active_dwell_ms=600, playback_ms=200, coverage_ms=200)

    def test_an_older_exposure_arriving_late_does_not_undo_the_latest_state(self):
        stayed_earlier = self.at(30)
        skipped_later = self.at(5, **self.SKIP)
        self.sent([skipped_later])
        self.assertTrue(self.measured()["skipped"])
        self.sent([stayed_earlier])
        found = self.measured()
        self.assertTrue(found["skipped"], "the skip happened after the stay")
        # But what the earlier exposure measured still counts.
        self.assertEqual(found["view_count"], 2)
        self.assertEqual(found["coverage_ms"], 4000)

    def test_arrival_order_does_not_change_the_result(self):
        events = [self.at(40), self.at(20, **self.SKIP), self.at(10, coverage_ms=7000, playback_ms=4500)]
        results = []
        for order in ([0, 1, 2], [2, 1, 0], [1, 2, 0], [2, 0, 1]):
            PostView.objects.all().delete()
            for index in order:
                self.sent([events[index]])
            results.append(self.measured())
        self.assertTrue(all(result == results[0] for result in results), results)
        self.assertFalse(results[0]["skipped"])

    def test_within_one_batch_the_latest_observed_decides_whatever_its_position(self):
        self.sent([self.at(5, **self.SKIP), self.at(30)])
        self.assertTrue(self.measured()["skipped"])

    def test_the_same_instant_is_settled_by_sequence_every_time(self):
        moment = (timezone.now() - timedelta(minutes=3)).isoformat()
        stay = self.summary(self.video_post, observed_at=moment)
        skip = self.summary(self.video_post, observed_at=moment, **self.SKIP)
        for first, second in ((stay, skip), (skip, stay)):
            PostView.objects.all().delete()
            self.sent([first])
            self.sent([second])
            self.assertTrue(self.measured()["skipped"], "the higher sequence is the later one")


class InterruptedTests(AttentionTestMixin, TransactionTestCase):
    """The receipt and the rows change together or not at all."""

    client_class = APIClient

    def test_a_batch_that_fails_part_way_leaves_no_receipt_and_can_be_sent_again(self):
        from unittest import mock

        batch_id = uuid.uuid4()
        events = [self.summary(self.video_post)]
        self.client.raise_request_exception = False
        with mock.patch("core.attention._upsert", side_effect=RuntimeError("connection lost")):
            self.assertEqual(self.send(events, batch_id=batch_id).status_code, 500)
        self.assertFalse(ImpressionBatchReceipt.objects.exists())
        self.assertFalse(PostView.objects.exists())
        self.assertEqual(self.send(events, batch_id=batch_id).data, {"recorded": 1, "duplicate": False})
        self.assertEqual(self.measured()["view_count"], 1)

    def test_the_same_batch_delivered_twice_at_once_is_recorded_once(self):
        if connection.vendor == "sqlite":
            self.skipTest("SQLite serialises writers; the race needs PostgreSQL")
        batch_id, events = str(uuid.uuid4()), [self.summary(self.video_post)]
        barrier, statuses = threading.Barrier(4), []

        def deliver():
            client = APIClient()
            client.credentials(HTTP_AUTHORIZATION=f"Token {self.reader_token.key}")
            barrier.wait()
            try:
                statuses.append(client.post(IMPRESSIONS, {
                    "schema_version": 2, "batch_id": batch_id,
                    "session_id": str(self.session), "events": events,
                }, format="json").data)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=deliver) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(statuses), 4)
        self.assertEqual(sum(1 for data in statuses if not data["duplicate"]), 1)
        self.assertEqual(self.measured()["view_count"], 1)
        self.assertEqual(self.measured()["active_dwell_ms"], 4500)


class MaliciousTests(AttentionTestMixin, APITestCase):
    def test_every_claim_is_brought_within_what_could_have_happened(self):
        self.sent([self.summary(
            self.video_post, dwell_ms=5000, active_dwell_ms=600000,
            playback_ms=600000, coverage_ms=3_600_000, rewatch_ms=600000, completed=True,
        )])
        found = self.measured()
        self.assertEqual(found["active_dwell_ms"], 5000, "no more active than on the card")
        self.assertEqual(found["playback_ms"], 5000, "no more playing than active")
        self.assertEqual(found["coverage_ms"], CLIP_MS, "no more covered than there is clip")
        self.assertEqual(found["rewatch_ms"], 5000)

    def test_coverage_cannot_outrun_the_player(self):
        self.sent([self.summary(self.video_post, playback_ms=1000, coverage_ms=10000, completed=True)])
        found = self.measured()
        self.assertEqual(found["coverage_ms"], 2250)
        self.assertFalse(found["watched_through"])
        other = workout_post(self.author)
        clip(other)
        self.sent([self.summary(other, playback_ms=0, coverage_ms=10000, completed=True)])
        self.assertEqual(self.measured(other)["coverage_ms"], 0)

    def test_a_loop_left_running_stops_counting(self):
        for _ in range(8):
            self.sent([self.summary(
                self.video_post, dwell_ms=600000, active_dwell_ms=600000,
                playback_ms=600000, coverage_ms=10000, rewatch_ms=590000,
            )])
        self.assertEqual(self.measured()["rewatch_ms"], 3 * CLIP_MS)

    def test_values_outside_the_contract_refuse_the_batch_and_record_nothing(self):
        for bad in (dict(dwell_ms=-1), dict(active_dwell_ms=10 * 60 * 1000 + 1),
                    dict(sequence=-1), dict(observed_at="yesterday"), dict(coverage_ms="lots"),
                    dict(surface="everywhere")):
            with self.subTest(bad=bad):
                self.assertEqual(self.send([self.summary(self.video_post, **bad)]).status_code, 400)
        self.assertEqual(self.send([self.summary(self.video_post) for _ in range(51)]).status_code, 400)
        self.assertEqual(self.send([]).status_code, 400)
        self.assertFalse(PostView.objects.exists())
        self.assertFalse(ImpressionBatchReceipt.objects.exists())

    def test_a_body_too_large_is_refused_before_it_is_read(self):
        body = json.dumps({
            "schema_version": 2, "batch_id": str(uuid.uuid4()), "session_id": str(self.session),
            "events": [self.summary(self.video_post)], "padding": "x" * (MAX_BODY_BYTES + 1),
        })
        response = self.client.post(IMPRESSIONS, body, content_type="application/json")
        self.assertEqual(response.status_code, 413)
        self.assertFalse(PostView.objects.exists())
        # Fifty full summaries fit with room to spare.
        full = json.dumps({
            "schema_version": 2, "batch_id": str(uuid.uuid4()), "session_id": str(self.session),
            "events": [self.summary(self.video_post, attachment_opened=True, attachment_read_ms=1,
                                    playback_failed=False, completed=False) for _ in range(50)],
        })
        self.assertLess(len(full), MAX_BODY_BYTES / 2)
        self.assertEqual(self.client.post(IMPRESSIONS, full, content_type="application/json").status_code, 202)

    def test_stale_and_future_summaries_are_dropped_and_the_rest_kept(self):
        now = timezone.now()
        self.assertEqual(self.sent([
            self.summary(self.video_post, observed_at=(now - timedelta(hours=49)).isoformat()),
            self.summary(self.video_post, observed_at=(now + timedelta(minutes=6)).isoformat()),
            self.summary(self.photo_post, observed_at=(now - timedelta(hours=47)).isoformat()),
        ])["recorded"], 1)
        self.assertFalse(PostView.objects.filter(post=self.video_post).exists())

    def test_nobody_can_report_for_anybody_else(self):
        """Identity is the token. There is no field to say otherwise, and one
        invented is ignored."""
        event = self.summary(self.video_post)
        event["viewer"] = self.author.pk
        self.sent([event], viewer=self.author.pk, viewer_id=self.author.pk)
        self.assertEqual(list(PostView.objects.values_list("viewer_id", flat=True)), [self.reader.pk])

    def test_posts_the_reader_cannot_see_and_their_own_are_ignored(self):
        private = workout_post(self.author, visibility="private")
        hidden = workout_post(self.author)
        Post.objects.filter(pk=hidden.pk).update(is_hidden=True)
        mine = workout_post(self.reader)
        _, blocker, _ = self.create_account("blocker")
        blocked = workout_post(blocker)
        Block.objects.create(blocker=blocker, blocked=self.reader)
        events = [self.summary(post) for post in (private, hidden, mine, blocked, self.video_post)]
        self.assertEqual(self.sent(events)["recorded"], 1)
        self.assertEqual(list(PostView.objects.values_list("post_id", flat=True)), [self.video_post.pk])

    def test_a_post_removed_after_it_was_seen_is_ignored_on_retry_too(self):
        event = self.summary(self.video_post)
        Post.objects.filter(pk=self.video_post.pk).update(is_hidden=True)
        self.assertEqual(self.sent([event])["recorded"], 0)

    def test_one_reader_cannot_write_to_another_readers_row(self):
        _, other, other_token = make_account("bystander")
        self.sent([self.summary(self.video_post)])
        self.authenticate(other_token)
        for _ in range(5):
            self.sent([self.summary(self.video_post, completed=True, coverage_ms=10000,
                                    playback_ms=10000, dwell_ms=11000, active_dwell_ms=11000)])
        self.assertEqual(self.measured()["view_count"], 1)
        self.assertFalse(self.measured()["watched_through"])

    def test_a_batch_is_one_write_however_many_posts_it_names(self):
        from django.test.utils import CaptureQueriesContext

        posts = [workout_post(self.author) for _ in range(40)]
        for post in posts[:20]:
            clip(post)
        self.sent([self.summary(posts[0])])
        with CaptureQueriesContext(connection) as small:
            self.sent([self.summary(posts[1])])
        with CaptureQueriesContext(connection) as large:
            self.sent([self.summary(post) for post in posts[2:]])
        self.assertEqual(len(large), len(small))
