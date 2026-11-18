"""Milestone 8: each part of the new Social is released by itself, to staff,
then an invited cohort, then everybody; widening pauses when the system is
already in trouble; and the plain newest-first page is there to fall back to.

The rest of the Milestone 8 evidence is the suites that already exist;
SOCIAL_RELEASE.md maps each row of the plan's table to them.
"""

import threading
import uuid
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, connections
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from . import rollout
from .analytics_models import ApiAlert, SocialDailyCounter, SocialInsightSnapshot
from .fixtures_for_tests import make_account, workout_post
from .models import (
    Block, Post, PostEngagementStats, PostFeedback, PostLike, PostTried, PostView,
    SocialCohortMember, SocialFlag, SocialRolloutEvent,
)
from .recommendations import FeedWeights, MEASURED_VARIANT, record_latency
from .social_insights import flush_counters
from .test_recommendations import FOR_YOU, ForYouTestCase
from .test_sounds_and_takes import SoundTestMixin
from .tests import RepbaseAPITestMixin

FEATURES = "/api/v1/social/features/"
IMPRESSIONS = "/api/v1/social/impressions/"


def stage(name, value):
    SocialFlag.objects.update_or_create(name=name, defaults={"stage": value})
    rollout.forget_stages()


def fresh_rollout(test):
    """Start from no rows, and leave the next test the same."""
    cache.clear()
    rollout.assume_no_rows()
    test.addCleanup(rollout.assume_no_rows)


class RolloutTestMixin(RepbaseAPITestMixin):
    def setUp(self):
        super().setUp()
        fresh_rollout(self)

    def features(self, token):
        self.authenticate(token)
        response = self.client.get(FEATURES)
        self.assertEqual(response.status_code, 200)
        return response.data


class StageTests(RolloutTestMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.staff_user, self.staff, self.staff_token = self.create_account("staff")
        self.staff_user.is_staff = True
        self.staff_user.save()
        _, self.invited, self.invited_token = self.create_account("invited")
        SocialCohortMember.objects.create(user=self.invited)
        _, self.member, self.member_token = self.create_account("member")

    def who_has(self, name):
        return [
            label for label, token in (
                ("staff", self.staff_token), ("invited", self.invited_token), ("member", self.member_token),
            ) if self.features(token)[name]
        ]

    def test_each_stage_reaches_who_it_says(self):
        for value, expected in (
            ("off", []), ("staff", ["staff"]), ("cohort", ["staff", "invited"]),
            ("everyone", ["staff", "invited", "member"]),
        ):
            stage("takes", value)
            self.assertEqual(self.who_has("takes"), expected, value)

    def test_the_parts_are_independent(self):
        for name in rollout.FLAGS:
            stage(name, "off")
        stage("sounds", "everyone")

        on = {name for name, value in self.features(self.member_token).items() if value}

        self.assertEqual(on, {"sounds"})

    @override_settings(SOCIAL_ROLLOUT_DEFAULT="off")
    def test_in_production_nothing_new_is_on_until_released(self):
        self.assertEqual(
            {name for name, value in self.features(self.member_token).items() if value},
            # The ranking people already have is not something new.
            {"ranked_for_you"},
        )

    def test_the_ranking_arm_is_off_until_released_even_in_development(self):
        self.assertFalse(self.features(self.member_token)["measured_ranking"])

    @override_settings(SOCIAL_ROLLOUT_DEFAULT="nonsense")
    def test_a_default_that_is_not_a_stage_means_off(self):
        self.assertFalse(self.features(self.member_token)["takes"])

    def test_a_stage_in_the_table_that_is_not_one_falls_back_to_the_default(self):
        SocialFlag.objects.create(name="takes", stage="soon")
        with override_settings(SOCIAL_ROLLOUT_DEFAULT="off"):
            self.assertFalse(self.features(self.member_token)["takes"])

    def test_it_needs_signing_in(self):
        self.assertEqual(APIClient().get(FEATURES).status_code, 401)

    def test_an_unknown_flag_is_an_error(self):
        with self.assertRaises(ValueError):
            rollout.enabled("everything", self.member)

    def test_reading_the_stages_costs_one_query_and_then_none(self):
        rollout.forget_stages()
        with self.assertNumQueries(1):
            rollout.stages()
        with self.assertNumQueries(0):
            rollout.stages()
            rollout.enabled_for_id("takes", self.member.pk)

    def test_a_change_made_by_another_worker_is_picked_up(self):
        self.assertTrue(rollout.enabled("takes", self.member))
        SocialFlag.objects.create(name="takes", stage="off")
        # Nothing told this process yet.
        self.assertTrue(rollout.enabled("takes", self.member))

        # What `set_stage` does in the worker that made the change.
        cache.set(rollout._VERSION_KEY, "another-worker", None)

        self.assertFalse(rollout.enabled("takes", self.member))

    def test_with_the_cache_away_a_worker_catches_up_within_ten_minutes(self):
        rollout.stages()
        SocialFlag.objects.create(name="takes", stage="off")
        with mock.patch.object(rollout.time, "monotonic", return_value=rollout.time.monotonic() + 601):
            self.assertFalse(rollout.enabled("takes", self.member))

    def test_leaving_the_cohort_takes_it_away(self):
        stage("takes", "cohort")
        self.assertTrue(self.features(self.invited_token)["takes"])

        call_command("social_rollout", "uninvite", "invited", stdout=StringIO())

        self.assertFalse(self.features(self.invited_token)["takes"])


class TakesAndSoundsSwitchTests(SoundTestMixin, APITestCase):
    def setUp(self):
        super().setUp()
        fresh_rollout(self)
        self.recipe_id = self.recipe(self.mia_token)["id"]

    def card(self, token, post_id):
        self.authenticate(token)
        return self.client.get(f"/api/v1/social/posts/{post_id}/").data

    def test_takes_off_refuses_a_new_take_and_keeps_the_ones_there_are(self):
        existing = self.recipe_take(self.jules_token, self.recipe_id)
        stage("takes", "off")

        refused = self.take_attempt(self.jules_token, self.recipe_id)
        card = self.card(self.reader_token, self.recipe_id)

        self.assertEqual(refused.status_code, 400)
        self.assertIn("source_post_id", refused.data)
        self.assertFalse(card["viewer_can_take"])
        # Nothing published is hidden by a switch.
        self.assertEqual(card["take_count"], 1)
        self.assertTrue(self.card(self.reader_token, existing["id"])["is_take"])

    def test_takes_off_does_not_stop_an_ordinary_post(self):
        stage("takes", "off")

        self.recipe(self.jules_token)

    def test_takes_for_the_cohort_only(self):
        stage("takes", "cohort")
        SocialCohortMember.objects.create(user=self.jules)

        self.assertEqual(self.take_attempt(self.jules_token, self.recipe_id).status_code, 201)
        self.assertEqual(self.take_attempt(self.reader_token, self.recipe_id).status_code, 400)

    def test_sounds_off_refuses_publishing_and_using_and_keeps_the_credit(self):
        sound = self.sound(title="Rack taken")
        used = self.use(self.jules_token, sound["id"])
        self.assertEqual(used.status_code, 201, used.data)
        stage("sounds", "off")

        self.assertEqual(self.make_sound().status_code, 403)
        self.assertEqual(self.use(self.jules_token, sound["id"]).status_code, 400)
        self.assertEqual(self.card(self.reader_token, used.data["id"])["sound"]["id"], sound["id"])

    def test_the_two_are_separate(self):
        stage("sounds", "off")

        self.assertEqual(self.take_attempt(self.jules_token, self.recipe_id).status_code, 201)


class MeasurementSwitchTests(RolloutTestMixin, APITestCase):
    def setUp(self):
        super().setUp()
        _, self.author, _ = self.create_account("author")
        _, self.reader, self.token = self.create_account("reader")
        self.post = workout_post(self.author)

    def send(self, version=2):
        self.authenticate(self.token)
        event = {"post": self.post.pk, "surface": "following", "dwell_ms": 5000}
        body = {"events": [event]}
        if version == 2:
            event.update(sequence=1, observed_at=timezone.now().isoformat(), active_dwell_ms=5000)
            body.update(schema_version=2, batch_id=str(uuid.uuid4()), session_id=str(uuid.uuid4()))
        return self.client.post(IMPRESSIONS, body, format="json")

    def test_off_sets_summaries_aside_without_an_error(self):
        stage("measurement_v2", "off")

        response = self.send()

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data, {"recorded": 0, "duplicate": False})
        self.assertFalse(PostView.objects.exists())
        flush_counters()
        self.assertEqual(
            SocialDailyCounter.objects.get(name="attention_batches_switched_off").value, 1
        )

    def test_off_leaves_the_old_events_working(self):
        """An app in the field that knows nothing of summaries is not
        switched off with them."""
        stage("measurement_v2", "off")

        response = self.send(version=1)

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["recorded"], 1)
        row = PostView.objects.get()
        self.assertEqual(row.qualified_views, 0)

    def test_on_records(self):
        stage("measurement_v2", "everyone")

        self.assertEqual(self.send().data["recorded"], 1)


class RankingSwitchTests(ForYouTestCase):
    def setUp(self):
        super().setUp()
        fresh_rollout(self)

    def test_the_measured_arm_goes_only_to_whom_it_was_released(self):
        invited, _ = self.reader("invited")
        other, _ = self.reader("other")
        SocialCohortMember.objects.create(user=invited)
        stage("measured_ranking", "cohort")

        weights, variant = FeedWeights.for_viewer(invited.pk)

        self.assertEqual(variant, "measured")
        self.assertEqual(weights, FeedWeights.from_settings(MEASURED_VARIANT))
        # Everybody else keeps the ranker they had: the control.
        self.assertEqual(FeedWeights.for_viewer(other.pk), (FeedWeights.from_settings(), None))

    @override_settings(FOR_YOU_EXPERIMENT={"name": "x", "variants": {"control": {}, "fresher": {"recency": 1.6}}})
    def test_a_running_experiment_is_not_overridden(self):
        reader, _ = self.reader("reader")
        stage("measured_ranking", "everyone")

        self.assertIn(FeedWeights.for_viewer(reader.pk)[1], ("control", "fresher"))

    def test_ranking_off_serves_the_newest_public_posts_in_order(self):
        reader, token = self.reader("reader")
        stage("ranked_for_you", "off")

        first = self.feed(token, page_size=10)
        second = self.follow_next(first)
        third = self.follow_next(second)

        ids = self.ids(first) + self.ids(second) + self.ids(third)
        expected = list(
            Post.objects.filter(visibility="public").order_by("-published_at", "-pk")
            .values_list("pk", flat=True)
        )
        self.assertEqual(ids, expected)
        self.assertEqual(len(ids), 24)
        self.assertIsNone(third.data["next"])
        self.assertNotIn("Server-Timing", first)

    def test_the_plain_page_needs_no_cache_statistics_or_ranking(self):
        _, token = self.reader("reader")
        stage("ranked_for_you", "off")
        PostEngagementStats.objects.all().delete()

        with mock.patch("core.for_you.serve_page", side_effect=AssertionError("ranked")), \
                mock.patch("core.for_you.cache_is_available", return_value=False):
            response = self.feed(token)

        self.assertEqual(len(response.data["results"]), 20)

    def test_the_plain_page_keeps_every_visibility_rule(self):
        reader, token = self.reader("reader")
        blocked, private, hidden, removed, turned_down, mine = self.running[:6]
        Block.objects.create(blocker=reader, blocked=blocked.author)
        Post.objects.filter(pk=private.pk).update(visibility="private")
        Post.objects.filter(pk=hidden.pk).update(is_hidden=True)
        Post.objects.filter(pk=removed.pk).update(moderation_status="rejected")
        PostFeedback.objects.create(viewer=reader, post=turned_down)
        own = workout_post(reader)
        stage("ranked_for_you", "off")

        ids = set(self.ids(self.feed(token, page_size=50)))

        blocked_posts = set(Post.objects.filter(author=blocked.author).values_list("pk", flat=True))
        self.assertFalse(ids & (blocked_posts | {private.pk, hidden.pk, removed.pk, turned_down.pk, own.pk}))
        self.assertIn(mine.pk, ids)

    def test_a_ranking_cursor_after_the_switch_starts_the_plain_list(self):
        _, token = self.reader("reader")
        first = self.feed(token, page_size=5)
        stage("ranked_for_you", "off")

        page = self.follow_next(first)

        self.assertEqual(page.status_code, 200)
        self.assertEqual(len(page.data["results"]), 5)

    def test_a_plain_cursor_still_works_once_ranking_is_back(self):
        _, token = self.reader("reader")
        stage("ranked_for_you", "off")
        first = self.feed(token, page_size=5)
        stage("ranked_for_you", "everyone")

        page = self.follow_next(first)

        self.assertEqual(page.status_code, 200)
        self.assertFalse(set(self.ids(first)) & set(self.ids(page)))

    def test_a_forged_plain_cursor_is_refused(self):
        _, token = self.reader("reader")
        self.authenticate(token)

        self.assertEqual(self.client.get(FOR_YOU, {"cursor": "recent.forged"}).status_code, 400)

    def test_the_plain_page_is_a_bounded_number_of_queries(self):
        _, token = self.reader("reader")
        stage("ranked_for_you", "off")
        self.feed(token, page_size=5)
        with self.assertNumQueries(self.queries_for(token, 5)):
            self.feed(token, page_size=20)

    def queries_for(self, token, size):
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as captured:
            self.feed(token, page_size=size)
        return len(captured)


@override_settings(ANALYTICS_ENABLED=True)
class GateTests(RolloutTestMixin, APITestCase):
    def healthy(self):
        """Everything a gate reads, as a system in good order records it."""
        _, author, _ = self.create_account("gate-author")
        post = workout_post(author)
        PostEngagementStats.objects.create(post=post, computed_at=timezone.now(), version=2)
        for _ in range(20):
            record_latency(40, kind="first")
            record_latency(30, kind="next")
        SocialInsightSnapshot.objects.create(day=timezone.now().date(), window_days=7)

    def failing(self):
        return [name for name, passed, _ in rollout.gates() if not passed]

    def test_a_healthy_system_passes_every_gate(self):
        self.healthy()

        self.assertEqual(self.failing(), [])

    def test_nothing_recorded_does_not_pass(self):
        """An empty deployment has proved nothing."""
        self.assertEqual(set(self.failing()), {
            "first_page_latency", "feed_statistics_fresh", "daily_maintenance_running",
        })

    @override_settings(ANALYTICS_ENABLED=False)
    def test_no_alerts_means_nothing_when_nothing_is_watching(self):
        self.healthy()

        self.assertEqual(self.failing(), ["api_health_is_recorded"])

    def test_each_pause_condition_fails_its_gate(self):
        self.healthy()
        now = timezone.now()
        ApiAlert.objects.create(
            endpoint="planner-list", method="GET", kind="failing", opened_at=now, last_seen_at=now
        )
        self.assertEqual(self.failing(), ["no_open_api_alerts"])
        ApiAlert.objects.all().delete()

        for _ in range(20):
            record_latency(2400, kind="first")
        self.assertEqual(self.failing(), ["first_page_latency"])
        cache.clear()
        for _ in range(20):
            record_latency(40, kind="first")

        PostEngagementStats.objects.update(computed_at=now - timedelta(hours=12))
        self.assertEqual(self.failing(), ["feed_statistics_fresh"])
        PostEngagementStats.objects.update(computed_at=now)

        from .models import PendingMediaDeletion

        waiting = PendingMediaDeletion.objects.create(name="posts/x.jpg")
        PendingMediaDeletion.objects.filter(pk=waiting.pk).update(created_at=now - timedelta(hours=2))
        self.assertEqual(self.failing(), ["queues_moving"])
        waiting.delete()

        _, reader, _ = self.create_account("gate-reader")
        row = PostView.objects.create(viewer=reader, post=Post.objects.first())
        PostView.objects.filter(pk=row.pk).update(last_seen_at=now - timedelta(days=120))
        self.assertEqual(self.failing(), ["retention_running"])
        row.delete()

        SocialInsightSnapshot.objects.update(day=now.date() - timedelta(days=5))
        self.assertEqual(self.failing(), ["daily_maintenance_running"])

    def set(self, *arguments, **options):
        out = StringIO()
        call_command("social_rollout", "set", *arguments, stdout=out, stderr=StringIO(), **options)
        return out.getvalue()

    def test_widening_is_refused_while_a_gate_fails_and_nothing_changes(self):
        stage("takes", "staff")

        with self.assertRaises(CommandError):
            self.set("takes", "cohort", backend_revision="a71fa89", client_build="1.2 (580)")

        self.assertEqual(rollout.stages()["takes"], "staff")
        self.assertFalse(SocialRolloutEvent.objects.exists())

    def test_widening_records_the_revisions_and_the_gates(self):
        self.healthy()
        stage("takes", "staff")

        self.set("takes", "cohort", backend_revision="a71fa89", client_build="1.2 (580)", by="mateo")

        event = SocialRolloutEvent.objects.get()
        self.assertEqual(
            (event.flag, event.from_stage, event.to_stage, event.backend_revision, event.client_build),
            ("takes", "staff", "cohort", "a71fa89", "1.2 (580)"),
        )
        self.assertFalse(event.forced)
        self.assertTrue(all(gate["passed"] for gate in event.gates))
        self.assertEqual(rollout.stages()["takes"], "cohort")

    def test_widening_needs_the_revisions_it_was_tested_at(self):
        self.healthy()
        stage("takes", "staff")

        with self.assertRaises(CommandError):
            self.set("takes", "cohort")

    def test_pulling_back_always_works(self):
        stage("takes", "everyone")

        self.set("takes", "off")

        self.assertEqual(rollout.stages()["takes"], "off")
        self.assertFalse(SocialRolloutEvent.objects.get().forced)

    def test_forcing_past_a_failing_gate_is_recorded_as_forced(self):
        stage("takes", "staff")

        self.set("takes", "cohort", backend_revision="a71fa89", client_build="1.2 (580)", force=True)

        event = SocialRolloutEvent.objects.get()
        self.assertTrue(event.forced)
        self.assertTrue(any(not gate["passed"] for gate in event.gates))

    def test_the_gates_command_fails_loudly(self):
        with self.assertRaises(CommandError):
            call_command("social_rollout", "gates", stdout=StringIO())
        self.healthy()
        out = StringIO()
        call_command("social_rollout", "gates", stdout=out)
        self.assertNotIn("FAIL", out.getvalue())

    def test_show_names_no_account(self):
        _, invited, _ = self.create_account("invited-person")
        SocialCohortMember.objects.create(user=invited)
        out = StringIO()

        call_command("social_rollout", "show", stdout=out)

        self.assertIn('"cohort_members": 1', out.getvalue())
        self.assertNotIn("invited-person", out.getvalue())


class ConcurrentActionTests(TransactionTestCase):
    """Many taps at once on the same post: the count is the people."""

    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("Needs row-level concurrency; runs in the PostgreSQL job.")
        fresh_rollout(self)
        _, self.author, _ = make_account("author")
        self.post = workout_post(self.author)
        self.people = [make_account(f"person{index}") for index in range(8)]

    def hammer(self, path):
        errors, barrier = [], threading.Barrier(len(self.people) * 2)

        def tap(token):
            try:
                client = APIClient()
                client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
                barrier.wait(timeout=10)
                response = client.post(path)
                if response.status_code != 200:
                    errors.append(response.status_code)
            except Exception as error:  # noqa: BLE001
                errors.append(repr(error))
            finally:
                connections.close_all()

        # Everybody twice, at the same moment: a double tap from each.
        threads = [threading.Thread(target=tap, args=(token,)) for _, _, token in self.people * 2]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return errors

    def test_simultaneous_likes_count_each_person_once(self):
        errors = self.hammer(f"/api/v1/social/posts/{self.post.pk}/like/")

        self.assertEqual(errors, [])
        self.assertEqual(PostLike.objects.filter(post=self.post).count(), len(self.people))

    def test_simultaneous_tried_count_each_person_once(self):
        errors = self.hammer(f"/api/v1/social/posts/{self.post.pk}/tried/")

        self.assertEqual(errors, [])
        self.assertEqual(PostTried.objects.filter(post=self.post).count(), len(self.people))
