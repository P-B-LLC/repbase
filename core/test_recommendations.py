"""The For You page: personal, responsive, safe, varied, and bounded.

Grouped by the promise each test keeps:

- different people get different pages, from what they said and did;
- the page changes when they act on it;
- a brand new account still gets something worth reading;
- nothing they may not see, or should not be shown, ever appears;
- no single creator or topic takes the page over, and discovery has room;
- paging is stable, and the work done does not grow with the content.
"""

import io
import json
from unittest import mock

from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.db import connection
from django.test import SimpleTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APITestCase
from rest_framework.throttling import SimpleRateThrottle

from . import recommendations
from .fixtures_for_tests import (
    FAST_PASSWORDS,
    aged,
    make_account,
    meal_post,
    planner_post,
    workout_post,
)
from .models import (
    Block,
    Follow,
    Personalization,
    Post,
    PostComment,
    PostFeedback,
    PostLike,
    PostReport,
    PostVideo,
    PostView,
    UserDiscipline,
)
from .recommendations import (
    Candidate,
    FeedWeights,
    InterestProfile,
    build_profile,
    eligible_posts,
    primary_topic,
    rank,
    score,
    topics_for,
)
from .tests import RepbaseAPITestMixin

FOR_YOU = "/api/v1/social/for-you/"
IMPRESSIONS = "/api/v1/social/impressions/"


def candidate(pk, author_id, topics, score_value=1.0, **counts):
    item = Candidate(
        pk=pk, author_id=author_id, created_at=timezone.now(), topics=topics, has_video=False,
        open_reports=0, likes=0, comments=0, shares=0, saves=0, impressions=0, long_views=0,
        completions=0,
    )
    for name, value in counts.items():
        setattr(item, name, value)
    item.score = score_value
    return item


# ------------------------------------------------------------------ units

class TopicTests(SimpleTestCase):
    def test_a_workout_is_its_kind_its_type_and_half_its_authors_disciplines(self):
        topics = topics_for("workout", "running", None, False, ["triathlon"])
        self.assertEqual(
            topics, {"kind:workout": 1.0, "type:running": 1.0, "discipline:triathlon": 0.5}
        )

    def test_an_untyped_session_counts_as_lifting(self):
        self.assertIn("type:lifting", topics_for("workout", None, None, False, []))

    def test_planner_posts_carry_their_category_and_clips_carry_a_format(self):
        topics = topics_for("planner", None, "study", True, [])
        self.assertEqual(topics["category:study"], 1.0)
        self.assertEqual(topics["format:video"], 0.5)

    def test_the_main_topic_is_the_most_specific(self):
        self.assertEqual(primary_topic({"kind:workout": 1, "type:running": 1}), "type:running")
        self.assertEqual(primary_topic({"kind:meal": 1, "discipline:running": 0.5}), "kind:meal")


class ScoringTests(SimpleTestCase):
    weights = FeedWeights()

    def test_every_part_of_a_score_is_reported(self):
        total, parts = score(candidate(1, 5, {"kind:meal": 1.0}), InterestProfile(), self.weights, timezone.now())
        self.assertEqual(
            set(parts), {"topic", "author", "follows", "quality", "popularity", "recency", "watch", "seen", "reported"}
        )
        self.assertAlmostEqual(total, sum(parts.values()))

    def test_a_matching_topic_outscores_a_disliked_one(self):
        profile = InterestProfile(topics={"kind:meal": 0.9, "type:running": -0.8})
        now = timezone.now()
        liked, _ = score(candidate(1, 5, {"kind:meal": 1.0}), profile, self.weights, now)
        disliked, _ = score(candidate(2, 6, {"kind:workout": 1.0, "type:running": 1.0}), profile, self.weights, now)
        self.assertGreater(liked, disliked)

    def test_one_like_on_one_view_is_not_a_perfect_post(self):
        """Quality is smoothed toward a prior until there are views to judge by."""
        now = timezone.now()
        lucky, _ = score(candidate(1, 5, {"kind:meal": 1}, likes=1, impressions=1), InterestProfile(), self.weights, now)
        proven, _ = score(candidate(2, 6, {"kind:meal": 1}, likes=40, impressions=100), InterestProfile(), self.weights, now)
        self.assertGreater(proven, lucky)

    def test_something_already_seen_is_pushed_down_and_a_skip_more_so(self):
        now = timezone.now()
        fresh, _ = score(candidate(1, 5, {"kind:meal": 1}), InterestProfile(), self.weights, now)
        seen, _ = score(candidate(1, 5, {"kind:meal": 1}), InterestProfile(seen={1: False}), self.weights, now)
        skipped, _ = score(candidate(1, 5, {"kind:meal": 1}), InterestProfile(seen={1: True}), self.weights, now)
        self.assertGreater(fresh, seen)
        self.assertGreater(seen, skipped)

    @override_settings(FOR_YOU_WEIGHTS={"topic": 0.0})
    def test_weights_come_from_settings(self):
        self.assertEqual(FeedWeights.from_settings().topic, 0.0)

    @override_settings(FOR_YOU_WEIGHTS={"topik": 1.0})
    def test_a_misspelt_weight_is_an_error_not_a_silent_default(self):
        with self.assertRaises(ImproperlyConfigured):
            FeedWeights.from_settings()


class RankingTests(SimpleTestCase):
    weights = FeedWeights(discovery_every=0)

    def rng(self):
        import random
        return random.Random(7)

    def test_one_prolific_author_cannot_take_over(self):
        """Even scoring ten times higher, an author gets one slot in four --
        as long as there is anybody else to show."""
        found = [candidate(n, 1, {"kind:meal": 1}, 10.0 - n * 0.01) for n in range(20)]
        found += [candidate(100 + n, 2 + n % 5, {"kind:meal": 1}, 1.0) for n in range(10)]
        ordered = rank(found, InterestProfile(), self.weights, self.rng(), limit=10)
        prolific = [index for index, item in enumerate(ordered) if item.pk < 100]
        self.assertLessEqual(len(prolific), 3)
        self.assertEqual(prolific[0], 0, "the best post still leads")

    def test_with_nothing_else_to_show_the_page_is_still_full(self):
        found = [candidate(n, 1, {"kind:meal": 1}, 1.0) for n in range(6)]
        self.assertEqual(len(rank(found, InterestProfile(), self.weights, self.rng(), limit=6)), 6)

    def test_the_same_author_never_appears_twice_in_four_when_others_exist(self):
        found = [candidate(n, n % 6, {"kind:meal": 1}, 10 - n * 0.1) for n in range(36)]
        authors = [next(c.author_id for c in found if c.pk == item.pk)
                   for item in rank(found, InterestProfile(), self.weights, self.rng(), limit=30)]
        for start in range(len(authors) - 3):
            window = authors[start:start + 4]
            self.assertEqual(len(window), len(set(window)), window)

    def test_one_topic_cannot_take_over_either(self):
        found = [candidate(n, n, {"kind:workout": 1, "type:running": 1}, 5.0 - n * 0.01) for n in range(20)]
        found += [candidate(100 + n, 100 + n, {"kind:meal": 1}, 4.5) for n in range(5)]
        ordered = rank(found, InterestProfile(), self.weights, self.rng(), limit=10)
        self.assertTrue(any(item.pk >= 100 for item in ordered))

    def test_one_slot_in_five_goes_to_somebody_new(self):
        profile = InterestProfile(
            topics={"type:running": 0.9}, authors={1: 0.9}, engaged_authors=frozenset({1, 2, 3})
        )
        found = [candidate(n, 1 + n % 3, {"type:running": 1}, 10 - n * 0.01) for n in range(30)]
        found += [candidate(100 + n, 50 + n, {"kind:meal": 1}, 0.5) for n in range(10)]
        ordered = rank(found, profile, FeedWeights(), self.rng(), limit=15)
        discoveries = [item for item in ordered if item.reason == "discovery"]
        self.assertGreaterEqual(len(discoveries), 2)
        self.assertTrue(all(item.pk >= 100 for item in discoveries))
        self.assertTrue(all(index % 5 == 4 for index, item in enumerate(ordered) if item.reason == "discovery"))

    def test_discovery_never_brings_back_what_was_dismissed(self):
        profile = InterestProfile(
            topics={"type:running": -0.9}, authors={9: -0.7}, engaged_authors=frozenset()
        )
        found = [candidate(n, 9, {"type:running": 1}, 3.0) for n in range(10)]
        found += [candidate(100 + n, 20 + n, {"kind:meal": 1}, 1.0) for n in range(10)]
        ordered = rank(found, profile, FeedWeights(), self.rng(), limit=10)
        self.assertFalse(any(item.reason == "discovery" and item.pk < 100 for item in ordered))

    def test_discovery_can_be_switched_off(self):
        found = [candidate(n, n, {"kind:meal": 1}, 1.0) for n in range(20)]
        ordered = rank(found, InterestProfile(), FeedWeights(discovery_every=0), self.rng(), limit=20)
        self.assertFalse(any(item.reason == "discovery" for item in ordered))


class CursorTests(SimpleTestCase):
    def test_a_cursor_round_trips_and_a_forged_one_does_not(self):
        cursor = recommendations.make_cursor("abc", 40)
        self.assertEqual(recommendations.read_cursor(cursor), ("abc", 40))
        for forged in ["garbage", cursor[:-2] + "xx", cursor + "x", ""]:
            with self.assertRaises(recommendations.BadCursor):
                recommendations.read_cursor(forged)


# -------------------------------------------------------------- end to end

@FAST_PASSWORDS
class ForYouTestCase(RepbaseAPITestMixin, APITestCase):
    """A small community: four runners, four lifters, four cooks.

    Built once per class. Each test's own changes are rolled back around it,
    so tests can like, follow, hide and dismiss freely.
    """

    @classmethod
    def setUpTestData(cls):
        cls.runners = [make_account(f"runner{n}")[1] for n in range(4)]
        cls.lifters = [make_account(f"lifter{n}")[1] for n in range(4)]
        cls.cooks = [make_account(f"cook{n}")[1] for n in range(4)]
        cls.running, cls.lifting, cls.meals = [], [], []
        for index in range(4):
            for round_ in range(2):
                hours = 1 + index + round_ * 5
                cls.running.append(workout_post(cls.runners[index], "running", hours_ago=hours))
                cls.lifting.append(workout_post(cls.lifters[index], "lifting", hours_ago=hours))
                cls.meals.append(meal_post(cls.cooks[index], hours_ago=hours))

    def reader(self, name, training_types=None, emphasis=""):
        _, profile, token = make_account(name)
        if training_types is not None:
            Personalization.objects.create(
                repbase_user=profile, training_types=training_types, emphasis=emphasis
            )
        return profile, token

    def feed(self, token, **params):
        self.authenticate(token)
        response = self.client.get(FOR_YOU, params)
        self.assertEqual(response.status_code, 200, getattr(response, "data", response))
        return response

    def ids(self, response):
        return [item["id"] for item in response.data["results"]]

    def top(self, token, count=5, **params):
        return self.ids(self.feed(token, page_size=count, **params))

    def share(self, ids, posts):
        wanted = {post.pk for post in posts}
        return sum(1 for pk in ids if pk in wanted)


class PersonalisationTests(ForYouTestCase):
    def test_two_people_with_different_interests_get_different_pages(self):
        _, runner_token = self.reader("fan_of_running", ["Running"])
        _, lifter_token = self.reader("fan_of_lifting", ["Strength"])
        for_runner = self.top(runner_token, 8)
        for_lifter = self.top(lifter_token, 8)
        self.assertNotEqual(for_runner, for_lifter)
        self.assertGreater(self.share(for_runner, self.running), self.share(for_runner, self.lifting))
        self.assertGreater(self.share(for_lifter, self.lifting), self.share(for_lifter, self.running))
        self.assertIn(for_runner[0], {post.pk for post in self.running})
        self.assertIn(for_lifter[0], {post.pk for post in self.lifting})

    def test_profile_disciplines_count_as_interests(self):
        profile, token = self.reader("climber_who_runs")
        UserDiscipline.objects.create(profile=profile, discipline="running")
        self.assertIn(self.top(token, 1)[0], {post.pk for post in self.running})

    def test_behaviour_teaches_the_page(self):
        profile, token = self.reader("undecided", [])
        before = self.share(self.top(token, 8), self.meals)
        for post in self.meals[:4]:
            PostLike.objects.create(post=post, user=profile)
        PostComment.objects.create(post=self.meals[4], author=profile, body="recipe?")
        after_ids = self.top(token, 8)
        self.assertGreater(self.share(after_ids, self.meals), before)
        reasons = {item["recommendation"]["reason"] for item in self.feed(token, page_size=8).data["results"]}
        self.assertIn("interest", reasons)

    def test_following_somebody_lifts_their_posts(self):
        profile, token = self.reader("follower", [])
        Follow.objects.create(follower=profile, following=self.cooks[3])
        results = self.feed(token, page_size=6).data["results"]
        self.assertEqual(results[0]["author"]["id"], self.cooks[3].pk)
        self.assertEqual(results[0]["recommendation"]["reason"], "following")


class RespondingToFeedbackTests(ForYouTestCase):
    def test_not_interested_removes_a_post_at_once_even_mid_session(self):
        profile, token = self.reader("picky", ["Running"])
        first = self.feed(token, page_size=3)
        upcoming = self.ids(self.feed(token, page_size=50))
        target = upcoming[5]
        self.assertEqual(self.client.post(f"/api/v1/social/posts/{target}/not-interested/").status_code, 204)
        # The cached ranking from before still holds it; serving must not.
        second = self.client.get(first.data["next"].replace("http://testserver", ""))
        later = [item["id"] for item in second.data["results"]]
        fresh = self.ids(self.feed(token, page_size=50))
        self.assertNotIn(target, later)
        self.assertNotIn(target, fresh)

    def test_dismissing_a_topic_lowers_it(self):
        profile, token = self.reader("changed_mind", ["Running"])
        before = self.share(self.top(token, 6), self.running)
        for post in self.running[:4]:
            self.client.post(f"/api/v1/social/posts/{post.pk}/not-interested/")
        after = self.share(self.top(token, 6), self.running)
        self.assertLess(after, before)

    def test_not_interested_can_be_taken_back(self):
        profile, token = self.reader("undo")
        self.authenticate(token)
        post = self.meals[0]
        self.client.post(f"/api/v1/social/posts/{post.pk}/not-interested/")
        self.client.post(f"/api/v1/social/posts/{post.pk}/not-interested/")
        self.assertEqual(PostFeedback.objects.filter(viewer=profile).count(), 1)
        self.assertEqual(self.client.delete(f"/api/v1/social/posts/{post.pk}/not-interested/").status_code, 204)
        self.assertTrue(eligible_posts(profile).filter(pk=post.pk).exists())

    def test_not_interested_on_your_own_or_an_invisible_post(self):
        profile, token = self.reader("self_aware")
        own = meal_post(profile)
        private = meal_post(self.cooks[0], visibility="private")
        self.authenticate(token)
        self.assertEqual(self.client.post(f"/api/v1/social/posts/{own.pk}/not-interested/").status_code, 400)
        self.assertEqual(self.client.post(f"/api/v1/social/posts/{private.pk}/not-interested/").status_code, 404)

    def test_what_was_seen_moves_down_next_time(self):
        profile, token = self.reader("scroller", [])
        first = self.top(token, 4)
        self.client.post(
            IMPRESSIONS,
            {"events": [{"post": pk, "dwell_ms": 400, "surface": "for_you"} for pk in first]},
            format="json",
        )
        again = self.top(token, 4)
        self.assertLess(len(set(first) & set(again)), len(first))


class NewAccountTests(ForYouTestCase):
    def test_a_brand_new_account_gets_a_full_varied_page(self):
        _, token = self.reader("newcomer")
        results = self.feed(token, page_size=10).data["results"]
        self.assertEqual(len(results), 10)
        authors = [item["author"]["id"] for item in results]
        self.assertGreaterEqual(len(set(authors)), 6)
        kinds = {item["kind"] for item in results}
        self.assertEqual(kinds, {"workout", "meal"})

    def test_popular_posts_lead_for_somebody_with_no_history(self):
        _, token = self.reader("blank_slate")
        favourite = self.meals[-1]  # the oldest meal
        for fan in self.runners + self.lifters:
            PostLike.objects.create(post=favourite, user=fan)
        self.assertIn(favourite.pk, self.top(token, 3))

    def test_onboarding_answers_shape_the_very_first_page(self):
        _, token = self.reader("onboarded", ["Swimming", "Strength"], emphasis="Nutrition")
        first = self.top(token, 6)
        self.assertGreaterEqual(self.share(first, self.lifting + self.meals), 4)


class EligibilityTests(ForYouTestCase):
    def setUp(self):
        super().setUp()
        self.profile, self.token = self.reader("careful", [])
        self.author = self.cooks[0]

    def assert_never_shown(self, post):
        self.assertFalse(eligible_posts(self.profile).filter(pk=post.pk).exists())
        self.assertNotIn(post.pk, self.ids(self.feed(self.token, page_size=50)))

    def test_hidden_posts(self):
        post = meal_post(self.author)
        Post.objects.filter(pk=post.pk).update(is_hidden=True)
        self.assert_never_shown(post)

    def test_private_and_followers_only_posts_from_strangers(self):
        self.assert_never_shown(meal_post(self.author, visibility="private"))
        self.assert_never_shown(meal_post(self.author, visibility="followers"))

    def test_closed_profiles(self):
        post = meal_post(self.author)
        type(self.author).objects.filter(pk=self.author.pk).update(is_profile_public=False)
        self.assert_never_shown(post)

    def test_blocks_in_either_direction(self):
        mine = meal_post(self.cooks[1])
        theirs = meal_post(self.cooks[2])
        Block.objects.create(blocker=self.profile, blocked=self.cooks[1])
        Block.objects.create(blocker=self.cooks[2], blocked=self.profile)
        self.assert_never_shown(mine)
        self.assert_never_shown(theirs)

    def test_suspended_accounts(self):
        post = meal_post(self.author)
        self.author.user.is_active = False
        self.author.user.save(update_fields=["is_active"])
        self.assert_never_shown(post)

    def test_the_readers_own_posts_and_reposts(self):
        self.assert_never_shown(meal_post(self.profile))
        repost = Post.objects.create(author=self.author, kind=Post.Kind.REPOST, repost_of=self.running[0])
        self.assert_never_shown(repost)

    def test_posts_the_reader_reported_or_dismissed(self):
        reported = meal_post(self.author)
        PostReport.objects.create(post=reported, reporter=self.profile, reason="spam")
        dismissed = meal_post(self.author)
        PostFeedback.objects.create(viewer=self.profile, post=dismissed)
        self.assert_never_shown(reported)
        self.assert_never_shown(dismissed)

    def test_posts_enough_people_reported_wait_for_a_moderator(self):
        post = meal_post(self.author)
        for reporter in self.runners[:3]:
            PostReport.objects.create(post=post, reporter=reporter, reason="spam")
        self.assert_never_shown(post)
        PostReport.objects.filter(post=post).update(reviewed_at=timezone.now())
        self.assertTrue(eligible_posts(self.profile).filter(pk=post.pk).exists())

    def test_clips_waiting_for_review(self):
        post = meal_post(self.author)
        PostVideo.objects.create(
            post=post, file="post-videos/x.mp4", content_type="video/mp4", codec="avc1",
            duration_ms=1000, width=64, height=48, size_bytes=10, status=PostVideo.Status.PENDING,
        )
        self.assert_never_shown(post)

    def test_posts_older_than_the_window(self):
        old = aged(meal_post(self.author), hours=24 * (recommendations.WINDOW_DAYS + 1))
        self.assert_never_shown(old)

    def test_a_post_hidden_after_ranking_is_not_served_from_the_session(self):
        first = self.feed(self.token, page_size=2)
        remaining = self.ids(self.feed(self.token, page_size=50))
        Post.objects.filter(pk__in=remaining).update(is_hidden=True)
        later = self.client.get(first.data["next"].replace("http://testserver", ""))
        self.assertEqual(later.status_code, 200)
        self.assertFalse(set(item["id"] for item in later.data["results"]) & set(remaining))


class PagingTests(ForYouTestCase):
    def setUp(self):
        super().setUp()
        self.profile, self.token = self.reader("pager", [])

    def test_pages_never_repeat_and_end_with_no_next_link(self):
        seen = []
        response = self.feed(self.token, page_size=7)
        while True:
            seen.extend(self.ids(response))
            if response.data["next"] is None:
                break
            response = self.client.get(response.data["next"].replace("http://testserver", ""))
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(len(seen), 24)

    def test_page_size_is_validated(self):
        self.authenticate(self.token)
        for value in ("0", "51", "lots"):
            self.assertEqual(self.client.get(FOR_YOU, {"page_size": value}).status_code, 400)

    def test_a_forged_cursor_is_refused(self):
        self.authenticate(self.token)
        self.assertEqual(self.client.get(FOR_YOU, {"cursor": "made-up"}).status_code, 400)

    def test_somebody_elses_cursor_opens_nothing_of_theirs(self):
        first = self.feed(self.token, page_size=3)
        stranger_profile, stranger_token = self.reader("stranger", [])
        PostFeedback.objects.create(viewer=stranger_profile, post_id=self.ids(self.feed(self.token, page_size=50))[3])
        self.authenticate(stranger_token)
        response = self.client.get(first.data["next"].replace("http://testserver", ""))
        self.assertEqual(response.status_code, 200)
        dismissed = PostFeedback.objects.get(viewer=stranger_profile).post_id
        self.assertNotIn(dismissed, self.ids(response))

    def test_an_expired_ranking_is_made_again_rather_than_failing(self):
        first = self.feed(self.token, page_size=5)
        cache.clear()
        response = self.client.get(first.data["next"].replace("http://testserver", ""))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["results"])

    def test_signing_in_is_required(self):
        self.client.credentials()
        self.assertEqual(self.client.get(FOR_YOU).status_code, 401)
        self.assertEqual(self.client.post(IMPRESSIONS, {}, format="json").status_code, 401)


class ScaleTests(ForYouTestCase):
    def test_the_number_of_queries_does_not_grow_with_the_content(self):
        profile, token = self.reader("measured", ["Running"])
        Follow.objects.create(follower=profile, following=self.cooks[0])
        PostLike.objects.create(post=self.meals[0], user=profile)

        def count_queries():
            with CaptureQueriesContext(connection) as captured:
                recommendations.build_ranking(profile, seed=1)
            return len(captured)

        small = count_queries()
        for author in self.runners + self.lifters + self.cooks:
            for _ in range(5):
                meal_post(author)
                workout_post(author, "running")
        self.assertEqual(count_queries(), small)

    def test_the_candidate_pool_is_capped(self):
        profile, _ = self.reader("capped", ["Running"])
        with mock.patch.object(recommendations, "POOL_SIZE", 5), \
                mock.patch.object(recommendations, "SOURCE_LIMIT", 3):
            found = recommendations.candidates(profile, build_profile(profile))
        self.assertLessEqual(len(found), 5 + 3)

    def test_latency_is_measured_and_reported(self):
        _, token = self.reader("timed")
        response = self.feed(token)
        self.assertRegex(response["Server-Timing"], r"^rank;dur=\d+(\.\d+)?$")
        self.assertGreaterEqual(recommendations.latency_summary(hours=1)["count"], 1)


class ImpressionTests(ForYouTestCase):
    def setUp(self):
        super().setUp()
        self.profile, self.token = self.reader("watcher", [])
        self.authenticate(self.token)

    def send(self, *events):
        return self.client.post(IMPRESSIONS, {"events": list(events)}, format="json")

    def test_views_are_folded_into_one_row_per_post(self):
        post = self.meals[0]
        self.assertEqual(self.send({"post": post.pk, "dwell_ms": 300}).status_code, 202)
        row = PostView.objects.get(viewer=self.profile, post=post)
        self.assertTrue(row.skipped)
        self.send({"post": post.pk, "dwell_ms": 9000, "surface": "for_you"},
                  {"post": post.pk, "dwell_ms": 1000, "watch_ms": 4000, "completed": True})
        row.refresh_from_db()
        self.assertEqual((row.view_count, row.total_dwell_ms), (3, 10300))
        self.assertFalse(row.skipped)
        self.assertTrue(row.completed)
        self.assertEqual(row.max_watch_ms, 4000)
        self.assertEqual(PostView.objects.count(), 1)

    def test_invisible_and_own_posts_are_ignored_quietly(self):
        private = meal_post(self.cooks[0], visibility="private")
        own = meal_post(self.profile)
        response = self.send({"post": private.pk}, {"post": own.pk}, {"post": 999_999},
                             {"post": self.meals[1].pk})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["recorded"], 1)
        self.assertEqual(list(PostView.objects.values_list("post_id", flat=True)), [self.meals[1].pk])

    def test_the_batch_is_validated(self):
        for body in [{}, {"events": []}, {"events": [{"post": 1}] * 51},
                     {"events": [{"post": self.meals[0].pk, "dwell_ms": -1}]},
                     {"events": [{"post": self.meals[0].pk, "dwell_ms": 10 ** 9}]},
                     {"events": [{"post": self.meals[0].pk, "surface": "somewhere"}]},
                     {"events": [{"post": "first"}]}]:
            with self.subTest(body=str(body)[:60]):
                self.assertEqual(self.client.post(IMPRESSIONS, body, format="json").status_code, 400)
        self.assertFalse(PostView.objects.exists())

    def test_impressions_are_rate_limited(self):
        with mock.patch.dict(SimpleRateThrottle.THROTTLE_RATES, {"impression": "2/hour"}):
            codes = [self.send({"post": self.meals[0].pk}).status_code for _ in range(3)]
        self.assertEqual(codes, [202, 202, 429])


class FeedReportTests(ForYouTestCase):
    def test_the_report_counts_without_naming_anyone(self):
        profile, token = self.reader("measured_reader", [])
        self.authenticate(token)
        self.client.get(FOR_YOU)
        self.client.post(
            IMPRESSIONS,
            {"events": [{"post": self.meals[0].pk, "dwell_ms": 6000, "surface": "for_you"},
                        {"post": self.meals[1].pk, "dwell_ms": 200, "surface": "for_you"}]},
            format="json",
        )
        PostLike.objects.create(post=self.meals[0], user=profile)
        output = io.StringIO()
        call_command("feed_report", "--json", stdout=output)
        report = json.loads(output.getvalue())
        self.assertEqual(report["impressions"], 2)
        self.assertEqual(report["engagement_rate"], 0.5)
        self.assertEqual(report["skip_rate"], 0.5)
        self.assertGreaterEqual(report["latency"]["count"], 1)
        self.assertNotIn("measured_reader", output.getvalue())


class ForYouContractTests(ForYouTestCase):
    """The page as sent matches the page as documented, with data in it."""

    def test_a_populated_page_matches_the_schema(self):
        from drf_spectacular.generators import SchemaGenerator

        from .contract_check import response_schema, to_json_schema, violations

        profile, token = self.reader("contract_reader", ["Running"])
        Follow.objects.create(follower=profile, following=self.cooks[0])
        PostVideo.objects.create(
            post=self.meals[0], file="post-videos/x.mp4", content_type="video/mp4", codec="avc1",
            duration_ms=1000, width=64, height=48, size_bytes=10, status=PostVideo.Status.APPROVED,
        )
        planner_post(self.cooks[1])
        body = json.loads(json.dumps(self.feed(token, page_size=20).data, default=str))
        spec = to_json_schema(SchemaGenerator().get_schema(request=None, public=True))
        schema = response_schema(spec, FOR_YOU)
        self.assertIsNotNone(schema)
        self.assertEqual(violations(schema, body), [])
        self.assertTrue(any(item["video"] for item in body["results"]))
