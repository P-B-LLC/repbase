"""Milestone 7: private analytics that are aggregate, bounded and withheld when
small; retention that is what the policy says; and a recommendation profile
that never reads anything private.
"""

import inspect
import uuid
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from . import ranking_model, recommendations, social_insights
from .analytics_models import (
    AnalyticsAccess, DailyActiveAccount, SocialDailyCounter, SocialInsightSnapshot,
)
from .fixtures_for_tests import finished_session, meal_post, workout_post
from .management.commands import prune_expired_rows
from .models import (
    ImpressionBatchReceipt, PostFeedback, PostTried, PostVideo, PostView, RepbaseUser,
    WorkoutTemplate,
)
from .recommendations import FeedWeights, build_profile
from .tests import RepbaseAPITestMixin

IMPRESSIONS = "/api/v1/social/impressions/"


def seen(viewer, post, **figures):
    """A measured, qualified exposure of `post` to `viewer`."""
    now = timezone.now()
    return PostView.objects.create(
        viewer=viewer, post=post, view_count=1, qualified_views=1, active_dwell_ms=4000,
        last_observed_at=now, last_seen_at=now, **figures,
    )


class InsightsTestMixin(RepbaseAPITestMixin):
    def setUp(self):
        cache.clear()
        _, self.author, self.author_token = self.create_account("author")
        self.post = workout_post(self.author, caption="SECRET-CAPTION leg day")
        self.today = timezone.now().date()

    def readers(self, count, prefix="reader"):
        return [self.create_account(f"{prefix}{index}") for index in range(count)]

    def figures(self, days=7):
        social_insights.roll_up(day=self.today)
        return SocialInsightSnapshot.objects.get(day=self.today, window_days=days).figures

    def save_post(self, token, post=None):
        self.authenticate(token)
        response = self.client.post(f"/api/v1/social/posts/{(post or self.post).pk}/save-workout/")
        self.assertIn(response.status_code, (200, 201), response.data)

    def say_tried(self, token, post=None, method="post"):
        self.authenticate(token)
        return getattr(self.client, method)(f"/api/v1/social/posts/{(post or self.post).pk}/tried/")


class ProductFigureTests(InsightsTestMixin, APITestCase):
    def test_exposure_to_save_and_save_to_tried(self):
        readers = self.readers(6)
        for _, profile, _ in readers:
            seen(profile, self.post)
        for _, _, token in readers[:3]:
            self.save_post(token)
        self.assertEqual(self.say_tried(readers[0][2]).status_code, 200)

        product = self.figures()["product"]

        self.assertEqual(product["exposure_to_save"]["value"], 3)
        self.assertEqual(product["exposure_to_save"]["of"], 6)
        self.assertEqual(product["exposure_to_save"]["rate"], 0.5)
        # Three savers is too few to publish what they went on to do.
        self.assertTrue(product["save_to_tried"]["withheld"])
        self.assertIsNone(product["save_to_tried"]["value"])

    def test_a_post_with_nothing_to_save_is_not_a_failed_save(self):
        from .fixtures_for_tests import planner_post

        plain = planner_post(self.author)
        for _, profile, _ in self.readers(6):
            seen(profile, plain)

        product = self.figures()["product"]

        self.assertEqual(product["qualified_exposures"]["value"], 6)
        self.assertEqual(product["exposure_to_save"]["of"], 0)
        self.assertIsNone(product["exposure_to_save"]["rate"])

    def test_an_exposure_under_a_second_is_not_an_exposure(self):
        for _, profile, _ in self.readers(6):
            PostView.objects.create(viewer=profile, post=self.post, view_count=1, qualified_views=0)

        self.assertEqual(self.figures()["product"]["qualified_exposures"]["value"], 0)

    def test_small_groups_are_never_stored(self):
        readers = self.readers(4)
        for _, profile, _ in readers:
            seen(profile, self.post)
        self.save_post(readers[0][2])

        product = self.figures()["product"]

        for name in ("qualified_exposures", "exposure_to_save"):
            self.assertEqual(
                product[name], {"value": None, "of": None, "rate": None, "people": None, "withheld": True}
            )

    def test_staff_are_left_out(self):
        for user, profile, _ in self.readers(6):
            user.is_staff = True
            user.save()
            seen(profile, self.post)

        self.assertEqual(self.figures()["product"]["qualified_exposures"]["value"], 0)

    def test_broken_playback_is_counted_apart_from_watching(self):
        PostVideo.objects.create(
            post=self.post, file="post-videos/x.mp4", content_type="video/mp4", codec="avc1",
            duration_ms=10_000, width=64, height=48, size_bytes=10, status=PostVideo.Status.APPROVED,
        )
        readers = self.readers(6)
        for _, profile, _ in readers[:3]:
            seen(profile, self.post, playback_ms=9500, coverage_ms=9500, watched_through=True)
        for _, profile, _ in readers[3:]:
            seen(profile, self.post, playback_ms=0, coverage_ms=0, playback_failures=1)

        product = self.figures()["product"]

        self.assertEqual(product["playback_failure"]["rate"], 0.5)
        self.assertEqual(product["watched_through"]["rate"], 0.5)
        # Every clip that played was watched: the other half never started.
        self.assertEqual(product["watched_through_when_played"]["rate"], 1.0)

    def test_satisfaction_and_creator_coverage(self):
        quiet = self.create_account("quiet")[1]
        workout_post(quiet)
        more = {0, 1, 2, 3}
        for index, (_, profile, _) in enumerate(self.readers(6)):
            seen(profile, self.post)
            PostFeedback.objects.create(
                viewer=profile, post=self.post,
                kind=PostFeedback.Kind.MORE if index in more else PostFeedback.Kind.FEWER,
            )
        for index in range(4):
            workout_post(self.create_account(f"unseen{index}")[1])

        product = self.figures()["product"]

        self.assertEqual(product["satisfaction"]["rate"], round(4 / 6, 4))
        # Six people published; one of them was properly seen.
        self.assertEqual((product["creator_coverage"]["value"], product["creator_coverage"]["of"]), (1, 6))

    @override_settings(ANALYTICS_ENABLED=True)
    def test_next_day_return(self):
        joined = timezone.now() - timedelta(days=3)
        for index, (user, profile, _) in enumerate(self.readers(6)):
            RepbaseUser.objects.filter(pk=profile.pk).update(created_at=joined)
            if index < 3:
                DailyActiveAccount.objects.create(day=(joined + timedelta(days=1)).date(), user=user)
        RepbaseUser.objects.filter(pk=self.author.pk).update(created_at=joined - timedelta(days=60))

        product = self.figures()["product"]

        self.assertEqual((product["d1_return"]["value"], product["d1_return"]["of"]), (3, 6))
        # Nobody who joined three days ago has had a seventh day yet.
        self.assertEqual(product["d7_return"]["of"], 0)

    def test_rolling_up_twice_replaces(self):
        for _, profile, _ in self.readers(6):
            seen(profile, self.post)
        self.figures()
        self.figures()

        self.assertEqual(SocialInsightSnapshot.objects.filter(day=self.today).count(), 2)

    def test_the_command_prints_counts_and_what_was_withheld(self):
        for _, profile, _ in self.readers(2):
            seen(profile, self.post)
        out = StringIO()

        call_command("roll_up_social_insights", day=self.today.isoformat(), stdout=out)

        self.assertIn('"qualified_exposures"', out.getvalue())
        self.assertNotIn("reader0", out.getvalue())


@override_settings(DEBUG=True)
class InsightsPageTests(InsightsTestMixin, APITestCase):
    def sign_in(self):
        admin, _, _ = self.create_account("insights-owner")
        admin.email, admin.is_staff = "admin@rytivo.app", True
        admin.save()
        AnalyticsAccess.objects.create(
            user=admin, verified_email="admin@rytivo.app", verified_at=timezone.now(), enabled=True
        )
        self.client.force_login(admin)

    def test_it_is_for_the_analytics_role_only(self):
        anonymous = self.client.get("/insights/social/")
        self.assertEqual(anonymous.status_code, 302)
        self.assertIn("/insights/login/", anonymous.url)

        member, _, _ = self.create_account("member")
        self.client.force_login(member)
        self.assertEqual(self.client.get("/insights/social/").status_code, 403)

        # Staff alone is not the role either.
        member.is_staff = True
        member.save()
        self.assertEqual(self.client.get("/insights/social/").status_code, 403)

    def test_it_reads_the_snapshot_and_names_nobody(self):
        readers = self.readers(6)
        for _, profile, _ in readers:
            seen(profile, self.post)
        for _, _, token in readers[:3]:
            self.save_post(token)
        social_insights.roll_up(day=self.today)
        self.client.credentials()
        self.sign_in()

        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as queries:
            response = self.client.get("/insights/social/")

        # Who is asking, then the snapshot and the one before it. Nothing
        # about what any account did is read to draw the page.
        tables = {query["sql"].split(' FROM "')[1].split('"')[0] for query in queries}
        self.assertEqual(tables, {
            "django_session", "auth_user", "core_accountaccess", "core_analyticsaccess",
            "core_socialinsightsnapshot",
        })

        self.assertEqual(response.status_code, 200)
        page = response.content.decode()
        self.assertIn("50.0%", page)
        self.assertIn("no-store", response["Cache-Control"])
        for private in ("SECRET-CAPTION", "reader0", "Reader0", "author@example.com", "post-videos/"):
            self.assertNotIn(private, page)

    def test_a_withheld_figure_says_so(self):
        for _, profile, _ in self.readers(2):
            seen(profile, self.post)
        social_insights.roll_up(day=self.today)
        self.sign_in()

        page = self.client.get("/insights/social/").content.decode()

        self.assertIn("fewer than 5", page)

    def test_before_the_first_roll_up_it_says_there_is_nothing_yet(self):
        self.sign_in()

        response = self.client.get("/insights/social/", {"days": 30})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No figures yet")
        self.assertEqual(self.client.get("/insights/social/", {"days": 365}).status_code, 400)


class TelemetryCounterTests(InsightsTestMixin, APITestCase):
    def batch(self, token, batch_id, post=None, dwell=5000):
        self.authenticate(token)
        return self.client.post(IMPRESSIONS, {
            "schema_version": 2, "batch_id": str(batch_id), "session_id": str(uuid.uuid4()),
            "events": [{
                "post": (post or self.post).pk, "surface": "following", "sequence": 1,
                "observed_at": timezone.now().isoformat(), "dwell_ms": dwell, "active_dwell_ms": dwell,
            }],
        }, format="json")

    def counters(self):
        social_insights.flush_counters()
        return dict(SocialDailyCounter.objects.values_list("name", "value"))

    def test_batches_duplicates_and_ignored_events_are_counted(self):
        _, _, token = self.create_account("reader")
        first = uuid.uuid4()
        self.assertEqual(self.batch(token, first).status_code, 202)
        self.assertTrue(self.batch(token, first).data["duplicate"])
        # Too short to be an exposure: received, and set aside.
        self.assertEqual(self.batch(token, uuid.uuid4(), dwell=100).status_code, 202)

        self.assertEqual(self.counters(), {
            "attention_batches": 3, "attention_duplicate_batches": 1,
            "attention_events": 2, "attention_events_ignored": 1,
        })

    def test_a_flush_cannot_add_the_same_counts_twice(self):
        social_insights.count("attention_batches", 4)

        self.assertEqual(self.counters()["attention_batches"], 4)
        self.assertEqual(self.counters()["attention_batches"], 4)
        social_insights.count("attention_batches")
        self.assertEqual(self.counters()["attention_batches"], 5)

    def test_an_oversize_body_is_counted(self):
        _, _, token = self.create_account("reader")
        self.authenticate(token)
        self.client.post(IMPRESSIONS, data=b"x" * 70_000, content_type="application/json")

        self.assertEqual(self.counters()["attention_oversize"], 1)

    def test_an_unknown_counter_is_an_error_and_a_broken_cache_is_not(self):
        with self.assertRaises(ValueError):
            social_insights.count("reader-42-opened-a-post")
        with mock.patch.object(social_insights.cache, "add", side_effect=ConnectionError):
            social_insights.count("attention_batches")

    def test_the_snapshot_carries_them(self):
        social_insights.count("attention_batches", 10)
        social_insights.count("attention_duplicate_batches", 2)

        telemetry = self.figures()["reliability"]["telemetry"]

        self.assertEqual((telemetry["attention_batches"], telemetry["attention_duplicate_batches"]), (10, 2))

    def test_old_counters_are_pruned_and_snapshots_kept(self):
        SocialDailyCounter.objects.create(day=self.today - timedelta(days=200), name="attention_batches", value=1)
        SocialInsightSnapshot.objects.create(day=self.today - timedelta(days=200), window_days=7)

        social_insights.prune()

        self.assertFalse(SocialDailyCounter.objects.exists())
        self.assertTrue(SocialInsightSnapshot.objects.exists())


class RetentionTests(InsightsTestMixin, APITestCase):
    def view(self, viewer, post, days_ago, features=None):
        row = PostView.objects.create(
            viewer=viewer, post=post, view_count=1, surface=PostView.Surface.FOR_YOU, features=features,
        )
        when = timezone.now() - timedelta(days=days_ago)
        PostView.objects.filter(pk=row.pk).update(first_seen_at=when, last_seen_at=when)
        return row

    def test_training_samples_go_after_fourteen_days_and_the_history_stays(self):
        _, reader, _ = self.create_account("reader")
        old = self.view(reader, self.post, 15, features=[0.1, 0.2])
        recent = self.view(reader, meal_post(self.author), 13, features=[0.3])

        call_command("prune_expired_rows", stdout=StringIO())

        old.refresh_from_db()
        recent.refresh_from_db()
        self.assertIsNone(old.features)
        self.assertEqual(old.view_count, 1)
        self.assertEqual(recent.features, [0.3])

    def test_a_dry_run_reports_and_changes_nothing(self):
        _, reader, _ = self.create_account("reader")
        old = self.view(reader, self.post, 15, features=[0.1])
        out = StringIO()

        call_command("prune_expired_rows", dry_run=True, stdout=out)

        self.assertIn("would clear 1 ranking training samples", out.getvalue())
        old.refresh_from_db()
        self.assertEqual(old.features, [0.1])

    def test_the_sweep_works_in_batches_and_finishes(self):
        _, reader, _ = self.create_account("reader")
        for index in range(5):
            self.view(reader, workout_post(self.author), 100 + index, features=[0.1])
            ImpressionBatchReceipt.objects.create(viewer=reader, batch_id=uuid.uuid4())
        ImpressionBatchReceipt.objects.update(received_at=timezone.now() - timedelta(days=30))
        kept = self.view(reader, self.post, 1)

        with mock.patch.object(prune_expired_rows, "BATCH", 2):
            statements = []
            original = prune_expired_rows.in_batches

            def counting(rows, change, batch=2):
                done = original(rows, change, batch=2)
                statements.append(done)
                return done

            with mock.patch.object(prune_expired_rows, "in_batches", counting):
                call_command("prune_expired_rows", stdout=StringIO())

        self.assertEqual(list(PostView.objects.values_list("pk", flat=True)), [kept.pk])
        self.assertFalse(ImpressionBatchReceipt.objects.exists())
        self.assertIn(5, statements)

    @override_settings(RECOMMENDATION_HISTORY_DAYS=30)
    def test_a_shorter_policy_is_honoured(self):
        _, reader, _ = self.create_account("reader")
        self.view(reader, self.post, 45)

        call_command("prune_expired_rows", stdout=StringIO())

        self.assertFalse(PostView.objects.exists())

    @override_settings(RECOMMENDATION_HISTORY_DAYS=400)
    def test_a_longer_policy_is_not(self):
        from .models import recommendation_history_days

        self.assertEqual(recommendation_history_days(), 90)

    def test_sampling_is_bounded_and_the_same_every_time(self):
        row = [0.5]
        with override_settings(FOR_YOU_TRAINING_SAMPLE_RATE=0):
            self.assertIsNone(ranking_model.sampled(1, 2, row))
        with override_settings(FOR_YOU_TRAINING_SAMPLE_RATE=1):
            self.assertEqual(ranking_model.sampled(1, 2, row), row)
        with override_settings(FOR_YOU_TRAINING_SAMPLE_RATE=0.25):
            kept = [ranking_model.sampled(viewer, 7, row) is not None for viewer in range(2000)]
            again = [ranking_model.sampled(viewer, 7, row) is not None for viewer in range(2000)]
        self.assertEqual(kept, again)
        self.assertTrue(0.2 < sum(kept) / len(kept) < 0.3)

    def test_training_reads_no_further_back_than_a_sample_is_kept(self):
        self.assertEqual(ranking_model.TRAIN_DAYS, 14)


class TriedTests(InsightsTestMixin, APITestCase):
    def test_it_is_said_once_and_can_be_taken_back(self):
        _, reader, token = self.create_account("reader")

        first = self.say_tried(token)
        self.say_tried(token)

        self.assertTrue(first.data["viewer_has_tried"])
        self.assertEqual(first.data["tried_count"], 1)
        self.assertEqual(PostTried.objects.filter(user=reader, post=self.post).count(), 1)

        after = self.say_tried(token, method="delete")
        self.assertEqual(after.status_code, 200)
        self.assertFalse(after.data["viewer_has_tried"])
        self.assertEqual(after.data["tried_count"], 0)

    def test_not_about_your_own_post(self):
        self.assertEqual(self.say_tried(self.author_token).status_code, 400)

    def test_not_about_a_post_you_cannot_see(self):
        _, _, token = self.create_account("reader")
        private = workout_post(self.author, visibility="private")

        self.assertEqual(self.say_tried(token, post=private).status_code, 404)

    def test_the_feed_carries_it(self):
        _, _, token = self.create_account("reader")
        self.say_tried(token)

        card = self.client.get(f"/api/v1/social/posts/{self.post.pk}/").data

        self.assertTrue(card["viewer_has_tried"])
        self.assertEqual(card["tried_count"], 1)

    def test_nothing_tracked_ever_says_it_for_you(self):
        """Saving the workout and then doing it is private training. It must
        not become a public statement, or a ranking signal, by itself."""
        _, reader, token = self.create_account("reader")
        self.save_post(token)
        template = WorkoutTemplate.objects.get(owner=reader, source_post=self.post)
        session = finished_session(reader)
        session.template = template
        session.save()

        self.assertFalse(PostTried.objects.exists())

    def test_it_counts_as_the_readers_own_word_in_the_ranking(self):
        _, reader, token = self.create_account("reader")
        before = build_profile(reader, FeedWeights.from_settings())
        self.say_tried(token)
        after = build_profile(reader, FeedWeights.from_settings())

        self.assertNotIn(self.post.pk, before.engaged_posts)
        self.assertIn(self.post.pk, after.engaged_posts)


class PrivateDataBoundaryTests(InsightsTestMixin, APITestCase):
    #: Records that are nobody's business but their owner's.
    PRIVATE_MODELS = (
        "WorkoutSession", "SessionExercise", "SetEntry", "SessionRoutePoint", "BodyWeightEntry",
        "DailyStepCount", "FoodMeal", "FoodEntry", "NutritionGoal", "PlannerEntry", "Gear",
    )

    def test_nothing_that_ranks_or_measures_reads_private_records(self):
        from . import attention, suggestions

        for module in (recommendations, ranking_model, attention, suggestions):
            source = inspect.getsource(module)
            for name in self.PRIVATE_MODELS:
                self.assertNotRegex(
                    source, rf"\b{name}\b",
                    f"{module.__name__} mentions {name}: private records stay out of recommendations.",
                )

    def test_insights_read_no_private_record_either(self):
        source = inspect.getsource(social_insights)
        for name in self.PRIVATE_MODELS:
            self.assertNotRegex(source, rf"\b{name}\b")

    def test_private_training_does_not_move_the_profile(self):
        _, reader, _ = self.create_account("reader")
        before = build_profile(reader, FeedWeights.from_settings())
        for _ in range(3):
            finished_session(reader)
        after = build_profile(reader, FeedWeights.from_settings())

        self.assertEqual(before.topics, after.topics)
        self.assertEqual(before.signal_count, after.signal_count)

    def test_the_feed_and_its_telemetry_log_no_content_or_credentials(self):
        _, _, token = self.create_account("reader")
        self.authenticate(token)
        with self.assertLogs("core", level="DEBUG") as logs:
            self.client.get("/api/v1/social/for-you/")
            self.client.post(IMPRESSIONS, {
                "schema_version": 2, "batch_id": str(uuid.uuid4()), "session_id": str(uuid.uuid4()),
                "events": [{
                    "post": self.post.pk, "surface": "for_you", "sequence": 1,
                    "observed_at": timezone.now().isoformat(), "dwell_ms": 5000, "active_dwell_ms": 5000,
                }],
            }, format="json")
            social_insights.roll_up(day=self.today)
            call_command("prune_expired_rows", stdout=StringIO())
        written = "\n".join(logs.output)

        self.assertIn("for_you first", written)
        for private in ("SECRET-CAPTION", token.key, "example.com", "/media/", "Signature="):
            self.assertNotIn(private, written)

    def test_deleting_the_account_deletes_what_was_kept_about_it(self):
        user, reader, token = self.create_account("reader")
        seen(reader, self.post)
        self.say_tried(token)
        PostFeedback.objects.create(viewer=reader, post=self.post)
        ImpressionBatchReceipt.objects.create(viewer=reader, batch_id=uuid.uuid4())
        DailyActiveAccount.objects.create(day=self.today, user=user)

        user.delete()

        for model in (PostView, PostTried, PostFeedback, ImpressionBatchReceipt, DailyActiveAccount):
            self.assertFalse(model.objects.exists(), model.__name__)

    def test_starting_fresh_deletes_the_history_and_its_samples(self):
        _, reader, token = self.create_account("reader")
        seen(reader, self.post, features=[0.1])
        self.authenticate(token)

        self.assertEqual(self.client.delete("/api/v1/social/for-you/history/").status_code, 200)

        self.assertFalse(PostView.objects.filter(viewer=reader).exists())
