"""The For You page: personal, responsive, safe, varied, and bounded.

Grouped by the promise each test keeps:

- different people get different pages, from what they said and did;
- the page changes when they act on it -- on the next page, not only the
  next visit;
- a brand new account still gets something worth reading, and so does a new
  post or a new creator;
- nothing they may not see, or should not be shown, ever appears -- and
  nothing they already acted on comes back;
- no single creator or topic takes the page over, and discovery has room;
- the reader can see what the page thinks, change it, or turn it off;
- viewing history is kept only as long as it is used;
- paging is stable, and the work done does not grow with the content;
- what it shows can be measured, per reason and per experiment arm.
"""

import io
import json
import random
import statistics
from datetime import timedelta
from unittest import mock

from django.core import checks
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
from .analytics_models import DailyActiveAccount
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
    RecommendationPreferences,
    RepbaseUser,
    SavedFoodMeal,
    SuggestionDismissal,
    UserDiscipline,
    WorkoutTemplate,
)
from .recommendations import (
    Candidate,
    FeedWeights,
    InterestProfile,
    RankState,
    build_profile,
    eligible_posts,
    experiment_variant,
    primary_topic,
    rank,
    score,
    topics_for,
)
from .tests import RepbaseAPITestMixin

FOR_YOU = "/api/v1/social/for-you/"
IMPRESSIONS = "/api/v1/social/impressions/"
PREFERENCES = "/api/v1/social/for-you/preferences/"
HISTORY = "/api/v1/social/for-you/history/"

EVERY_PART = {
    "topic", "momentum", "detail", "author", "follows", "similar", "quality", "popularity",
    "recency", "watch", "newcomer", "skip_rate", "hide_rate", "seen", "fatigue", "reported", "bait",
}


def candidate(pk, author_id, topics, score_value=1.0, **counts):
    item = Candidate(
        pk=pk, author_id=author_id, created_at=timezone.now(), topics=topics, has_video=False,
        open_reports=0, likes=0, comments=0, shares=0, saves=0, impressions=0, long_views=0,
        completions=0,
    )
    for name, value in counts.items():
        setattr(item, name, value)
        if name in ("likes", "comments", "shares", "saves") and f"trusted_{name}" not in counts:
            # Engagement from accounts that can be trusted, unless a test
            # says otherwise.
            setattr(item, f"trusted_{name}", value)
    item.score = score_value
    return item


def video(post, status=PostVideo.Status.APPROVED, duration_ms=10_000):
    return PostVideo.objects.create(
        post=post, file=f"post-videos/{post.pk}.mp4", content_type="video/mp4", codec="avc1",
        duration_ms=duration_ms, width=64, height=48, size_bytes=10, status=status,
    )


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

    def test_every_hideable_topic_has_a_name_to_show(self):
        for topic in recommendations.HIDEABLE_TOPICS:
            label = recommendations.topic_label(topic)
            self.assertTrue(label[0].isupper(), label)
            self.assertNotIn(":", label)


class ScoringTests(SimpleTestCase):
    weights = FeedWeights()

    def test_every_part_of_a_score_is_reported(self):
        total, parts = score(candidate(1, 5, {"kind:meal": 1.0}), InterestProfile(), self.weights, timezone.now())
        self.assertEqual(set(parts), EVERY_PART)
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
        seen_once = recommendations._seen_weight(1, 0, False)
        skipped = recommendations._seen_weight(1, 0, True)
        fresh, _ = score(candidate(1, 5, {"kind:meal": 1}), InterestProfile(), self.weights, now)
        seen, _ = score(candidate(1, 5, {"kind:meal": 1}), InterestProfile(seen={1: seen_once}), self.weights, now)
        passed, _ = score(candidate(1, 5, {"kind:meal": 1}), InterestProfile(seen={1: skipped}), self.weights, now)
        self.assertGreater(fresh, seen)
        self.assertGreater(seen, passed)

    def test_having_seen_something_fades_and_seeing_it_again_counts_more(self):
        weight = recommendations._seen_weight
        half_life = recommendations.SEEN_HALF_LIFE_DAYS
        self.assertAlmostEqual(weight(1, 0, False), 1.0)
        self.assertAlmostEqual(weight(1, half_life, False), 0.5)
        self.assertGreater(weight(4, 0, False), weight(2, 0, False))
        self.assertLessEqual(weight(1000, 0, False), 2.0)
        self.assertAlmostEqual(weight(1, 0, True), 1.5)

    def test_what_was_engaged_with_lately_counts_as_momentum(self):
        now = timezone.now()
        steady, _ = score(candidate(1, 5, {"kind:meal": 1}), InterestProfile(topics={"kind:meal": 0.3}), self.weights, now)
        hot, parts = score(
            candidate(1, 5, {"kind:meal": 1}),
            InterestProfile(topics={"kind:meal": 0.3}, momentum={"kind:meal": 0.9}), self.weights, now,
        )
        self.assertGreater(hot, steady)
        self.assertAlmostEqual(parts["momentum"], self.weights.momentum * 0.9)

    def test_similar_taste_and_new_creators_lift_a_post(self):
        now = timezone.now()
        plain, _ = score(candidate(1, 5, {"kind:meal": 1}), InterestProfile(), self.weights, now)
        liked_by_peers, _ = score(candidate(1, 5, {"kind:meal": 1}, similar=0.8), InterestProfile(), self.weights, now)
        newcomer, _ = score(candidate(1, 5, {"kind:meal": 1}, newcomer=1.0), InterestProfile(), self.weights, now)
        self.assertAlmostEqual(liked_by_peers - plain, self.weights.similar * 0.8)
        self.assertAlmostEqual(newcomer - plain, self.weights.newcomer)

    def test_exploration_is_centred_on_the_average(self):
        """Sampling quality moves a post up or down, not up on average."""
        now = timezone.now()
        weights = FeedWeights(jitter=0.0)
        item = candidate(1, 5, {"kind:meal": 1}, likes=5, impressions=40)
        _, average = score(item, InterestProfile(), weights, now)
        rng = random.Random(3)
        draws = [score(item, InterestProfile(), weights, now, rng)[1]["quality"] for _ in range(4000)]
        self.assertAlmostEqual(statistics.mean(draws), average["quality"], delta=0.02)

    def test_a_post_with_few_views_is_explored_more_than_a_proven_one(self):
        now = timezone.now()
        weights = FeedWeights(jitter=0.0, explore=1.0)
        new = candidate(1, 5, {"kind:meal": 1})
        proven = candidate(2, 6, {"kind:meal": 1}, likes=100, impressions=1000)
        rng = random.Random(5)
        spread_new = statistics.pstdev(score(new, InterestProfile(), weights, now, rng)[0] for _ in range(500))
        spread_proven = statistics.pstdev(score(proven, InterestProfile(), weights, now, rng)[0] for _ in range(500))
        self.assertGreater(spread_new, 3 * spread_proven)

    def test_exploration_can_be_switched_off(self):
        now = timezone.now()
        weights = FeedWeights(jitter=0.0, explore=0.0)
        item = candidate(1, 5, {"kind:meal": 1}, likes=2, impressions=10)
        values = {score(item, InterestProfile(), weights, now, random.Random(seed))[0] for seed in range(20)}
        self.assertEqual(len(values), 1)

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

    def test_the_rules_hold_across_a_page_boundary(self):
        """A page ranked in two goes keeps the author gap and the discovery
        cadence as though it had been ranked in one."""
        profile = InterestProfile(topics={"type:running": 0.9}, engaged_authors=frozenset({1, 2, 3}))
        found = [candidate(n, 1 + n % 3, {"type:running": 1}, 10 - n * 0.01) for n in range(30)]
        found += [candidate(100 + n, 50 + n, {"kind:meal": 1}, 0.5) for n in range(10)]
        state = RankState()
        first = rank(found, profile, FeedWeights(), self.rng(), limit=3, state=state)
        rest = [item for item in found if item.pk not in {chosen.pk for chosen in first}]
        second = rank(rest, profile, FeedWeights(), self.rng(), limit=9, state=state)
        ordered = first + second
        by_pk = {item.pk: item.author_id for item in found}
        authors = [by_pk[item.pk] for item in ordered]
        for start in range(len(authors) - 3):
            window = [author for author in authors[start:start + 4] if author < 50]
            self.assertEqual(len(window), len(set(window)), authors)
        self.assertEqual([index for index, item in enumerate(ordered) if item.reason == "discovery"], [4, 9])

    def test_a_rank_state_survives_the_cache(self):
        """The cache pickles what it keeps; a state must come back as it went."""
        import pickle

        state = RankState()
        rank([candidate(n, n, {"kind:meal": 1}) for n in range(6)], InterestProfile(), self.weights,
             self.rng(), limit=4, state=state)
        again = RankState.load(pickle.loads(pickle.dumps(state.dump())))
        self.assertEqual(again.position, 4)
        self.assertEqual(again.authors_used, state.authors_used)
        self.assertEqual(again.recent_authors, state.recent_authors)
        self.assertEqual(again.recent_topics, state.recent_topics)


class ReasonTests(SimpleTestCase):
    def reason(self, item, profile=None):
        item.parts = item.parts or {"quality": 0, "popularity": 0, "recency": 1}
        return recommendations._reason(item, profile or InterestProfile(), discovery=False)[0]

    def test_similar_taste_is_only_claimed_with_more_than_one_person_behind_it(self):
        self.assertEqual(self.reason(candidate(1, 5, {"kind:meal": 1}, similar=0.9, similar_voters=2)), "similar")
        self.assertNotEqual(self.reason(candidate(1, 5, {"kind:meal": 1}, similar=0.9, similar_voters=1)), "similar")

    def test_personal_reasons_come_before_general_ones(self):
        profile = InterestProfile(topics={"kind:meal": 0.8}, followed=frozenset({7}))
        self.assertEqual(self.reason(candidate(1, 7, {"kind:meal": 1}, newcomer=1.0), profile), "following")
        self.assertEqual(self.reason(candidate(1, 5, {"kind:meal": 1}, newcomer=1.0), profile), "interest")
        self.assertEqual(self.reason(candidate(1, 5, {"type:running": 1}, newcomer=1.0), profile), "new_creator")

    def test_every_reason_is_a_documented_one(self):
        self.assertEqual(
            set(recommendations.REASONS),
            {"following", "interest", "similar", "discovery", "new_creator", "popular", "fresh"},
        )


class ExperimentTests(SimpleTestCase):
    config = {"name": "fresher", "variants": {"control": {}, "fresher": {"recency": 1.6}}}

    def test_without_an_experiment_everyone_gets_the_settings(self):
        self.assertEqual(experiment_variant(7), (None, {}))
        self.assertEqual(FeedWeights.for_viewer(7), (FeedWeights.from_settings(), None))

    def test_readers_split_evenly_and_stay_put(self):
        with override_settings(FOR_YOU_EXPERIMENT=self.config):
            arms = [experiment_variant(viewer)[0] for viewer in range(1, 4001)]
            self.assertEqual(arms, [experiment_variant(viewer)[0] for viewer in range(1, 4001)])
        self.assertAlmostEqual(arms.count("fresher") / len(arms), 0.5, delta=0.04)

    def test_an_arm_moves_only_its_own_weights(self):
        with override_settings(FOR_YOU_EXPERIMENT=self.config):
            fresher = next(v for v in range(1, 100) if experiment_variant(v)[0] == "fresher")
            control = next(v for v in range(1, 100) if experiment_variant(v)[0] == "control")
            self.assertEqual(FeedWeights.for_viewer(fresher), (FeedWeights(recency=1.6), "fresher"))
            self.assertEqual(FeedWeights.for_viewer(control), (FeedWeights(), "control"))

    def test_renaming_the_experiment_reshuffles(self):
        renamed = {**self.config, "name": "fresher-2"}
        with override_settings(FOR_YOU_EXPERIMENT=self.config):
            before = [experiment_variant(v)[0] for v in range(1, 200)]
        with override_settings(FOR_YOU_EXPERIMENT=renamed):
            after = [experiment_variant(v)[0] for v in range(1, 200)]
        self.assertNotEqual(before, after)

    def test_a_broken_experiment_stops_the_server_starting(self):
        for broken in (
            {"name": "x", "variants": {"only": {}}},
            {"name": "", "variants": {"a": {}, "b": {}}},
            {"name": "x", "variants": {"a": {}, "b": {"recencee": 1}}},
            {"name": "x", "variants": {"a": {}, "b": {}}, "share": 0.1},
        ):
            with self.subTest(broken=broken), override_settings(FOR_YOU_EXPERIMENT=broken):
                with self.assertRaises(ImproperlyConfigured):
                    recommendations.experiment_config()
                found = [error.id for error in checks.run_checks() if error.id == "core.E009"]
                self.assertEqual(found, ["core.E009"])

    @override_settings(FOR_YOU_WEIGHTS={"topik": 1.0})
    def test_a_misspelt_weight_is_caught_at_startup(self):
        self.assertIn("core.E009", [error.id for error in checks.run_checks()])


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
    so tests can like, follow, hide and dismiss freely. Rankings start from
    a fixed seed, so a test sees the same page every run; how much the
    randomness moves things is tested on its own above.
    """

    @classmethod
    def setUpTestData(cls):
        cls.runners = [make_account(f"runner{n}")[1] for n in range(4)]
        cls.lifters = [make_account(f"lifter{n}")[1] for n in range(4)]
        cls.cooks = [make_account(f"cook{n}")[1] for n in range(4)]
        # The community has been here a few days: past the two days in
        # which a brand-new account's likes count for half
        # (core.feed_integrity), and still well inside the sixty that make
        # somebody a new creator. Readers made in a test are brand new.
        RepbaseUser.objects.filter(
            pk__in=[person.pk for person in cls.runners + cls.lifters + cls.cooks]
        ).update(created_at=timezone.now() - timedelta(days=3))
        cls.running, cls.lifting, cls.meals = [], [], []
        for index in range(4):
            for round_ in range(2):
                hours = 1 + index + round_ * 5
                cls.running.append(workout_post(cls.runners[index], "running", hours_ago=hours))
                cls.lifting.append(workout_post(cls.lifters[index], "lifting", hours_ago=hours))
                cls.meals.append(meal_post(cls.cooks[index], hours_ago=hours))

    def setUp(self):
        super().setUp()
        seed = mock.patch.object(recommendations, "new_seed", return_value=11)
        seed.start()
        self.addCleanup(seed.stop)

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

    def follow_next(self, response):
        return self.client.get(response.data["next"].replace("http://testserver", ""))

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

    def test_interest_wins_whatever_the_dice_say(self):
        """The pages above use one seed. Across many, exploration may tie a
        page now and then -- the diversity rules leave the slots after the
        first two lifting posts to everything else -- but never turns it."""
        lifter, _ = self.reader("fan_of_lifting", ["Strength"])
        runner, _ = self.reader("fan_of_running", ["Running"])
        lifting, running = {p.pk for p in self.lifting}, {p.pk for p in self.running}
        outcomes = []
        for seed in range(30):
            for reader, mine, theirs in ((lifter, lifting, running), (runner, running, lifting)):
                top = [item.pk for item in recommendations.build_ranking(reader, seed=seed, limit=8).items]
                outcomes.append(sum(pk in mine for pk in top) - sum(pk in theirs for pk in top))
        self.assertFalse([gap for gap in outcomes if gap < 0], outcomes)
        self.assertGreaterEqual(sum(gap > 0 for gap in outcomes) / len(outcomes), 0.85, outcomes)

    def test_profile_disciplines_count_as_interests(self):
        profile, token = self.reader("climber_who_runs")
        UserDiscipline.objects.create(profile=profile, discipline="running")
        self.assertIn(self.top(token, 1)[0], {post.pk for post in self.running})

    def test_behaviour_teaches_the_page(self):
        profile, token = self.reader("undecided", [])
        untouched = self.meals[3:]
        before = self.share(self.top(token, 8), untouched)
        for post in self.meals[:2]:
            PostLike.objects.create(post=post, user=profile)
        PostComment.objects.create(post=self.meals[2], author=profile, body="recipe?")
        after_ids = self.top(token, 8)
        self.assertGreater(self.share(after_ids, untouched), before)
        reasons = {item["recommendation"]["reason"] for item in self.feed(token, page_size=8).data["results"]}
        self.assertIn("interest", reasons)

    def test_following_somebody_lifts_their_posts(self):
        profile, token = self.reader("follower", [])
        Follow.objects.create(follower=profile, following=self.cooks[3])
        results = self.feed(token, page_size=6).data["results"]
        self.assertEqual(results[0]["author"]["id"], self.cooks[3].pk)
        self.assertEqual(results[0]["recommendation"]["reason"], "following")

    def test_what_someone_did_lately_counts_for_more_than_last_month(self):
        """Running liked three weeks ago and food liked this afternoon: the
        page leans to food now."""
        profile, token = self.reader("changing_tastes", [])
        for post in self.running[:4]:
            like = PostLike.objects.create(post=post, user=profile)
            PostLike.objects.filter(pk=like.pk).update(created_at=timezone.now() - timedelta(days=20))
        for post in self.meals[:2]:
            PostLike.objects.create(post=post, user=profile)
        built = build_profile(profile)
        self.assertGreater(built.momentum.get("kind:meal", 0), 0.5)
        self.assertNotIn("type:running", built.momentum)
        top = self.top(token, 4)
        self.assertGreater(self.share(top, self.meals[2:]), self.share(top, self.running[4:]))

    @override_settings(FOR_YOU_WEIGHTS={"discovery_every": 0})
    def test_people_with_similar_taste_point_somewhere_new(self):
        """Two people who like what this reader likes both liked a meal; it
        comes recommended for that reason, though the reader never looked at
        food."""
        reader, token = self.reader("lifts_only", [])
        twins = [self.reader(f"twin{n}", [])[0] for n in range(2)]
        # Established members: a brand-new account is nobody's taste twin
        # (core.feed_integrity.Trust.as_neighbour).
        RepbaseUser.objects.filter(pk__in=[twin.pk for twin in twins]).update(
            created_at=timezone.now() - timedelta(days=10)
        )
        for person in [reader, *twins]:
            for post in self.lifting[:3]:
                PostLike.objects.create(post=post, user=person)
        tip = self.meals[7]
        for twin in twins:
            PostLike.objects.create(post=tip, user=twin)
        page = {item["id"]: item for item in self.feed(token, page_size=50).data["results"]}
        self.assertIn(tip.pk, page)
        self.assertEqual(page[tip.pk]["recommendation"]["reason"], "similar")
        self.assertEqual(page[tip.pk]["recommendation"]["label"], "Liked by people with similar taste")

    @override_settings(FOR_YOU_WEIGHTS={"discovery_every": 0})
    def test_one_persons_likes_are_never_given_as_the_reason(self):
        reader, token = self.reader("lifts_only", [])
        twin = self.reader("only_twin", [])[0]
        for person in (reader, twin):
            for post in self.lifting[:3]:
                PostLike.objects.create(post=post, user=person)
        PostLike.objects.create(post=self.meals[7], user=twin)
        reasons = {item["id"]: item["recommendation"]["reason"] for item in self.feed(token, page_size=50).data["results"]}
        self.assertNotEqual(reasons.get(self.meals[7].pk), "similar")

    def test_blocked_and_suspended_people_are_nobodys_taste_twin(self):
        reader, _ = self.reader("lifts_only", [])
        blocked, suspended = self.reader("blocked_twin", [])[0], self.reader("gone_twin", [])[0]
        for person in (reader, blocked, suspended):
            for post in self.lifting[:3]:
                PostLike.objects.create(post=post, user=person)
        Block.objects.create(blocker=reader, blocked=blocked)
        suspended.user.is_active = False
        suspended.user.save(update_fields=["is_active"])
        for person in (blocked, suspended):
            PostLike.objects.create(post=self.meals[0], user=person)
        self.assertEqual(recommendations.similar_tastes(reader, build_profile(reader)), {})

    def test_new_creators_get_a_lift_that_fades_as_they_grow(self):
        reader, _ = self.reader("open_minded", [])
        first_timer = self.reader("first_timer", [])[0]
        debut = planner_post(first_timer, category="sleep", hours_ago=2)
        popular = self.cooks[0]
        for fan in self.runners + self.lifters + self.cooks[1:]:
            Follow.objects.create(follower=fan, following=popular)
        old_hand = self.lifters[0]
        type(old_hand).objects.filter(pk=old_hand.pk).update(created_at=timezone.now() - timedelta(days=400))

        found = {c.pk: c for c in recommendations.candidates(reader, build_profile(reader))}
        self.assertEqual(found[debut.pk].newcomer, 1.0, "joined today, nobody following yet")
        self.assertGreater(found[debut.pk].newcomer, found[self.meals[0].pk].newcomer, "eleven followers")
        self.assertGreater(found[self.meals[0].pk].newcomer, 0.0, "still small")
        self.assertEqual(found[self.lifting[0].pk].newcomer, 0.0, "joined over a year ago")

    @override_settings(FOR_YOU_WEIGHTS={"discovery_every": 0})
    def test_a_new_creator_is_named_as_one(self):
        _, token = self.reader("open_minded", [])
        debut = planner_post(self.reader("first_timer", [])[0], category="sleep", hours_ago=2)
        page = {item["id"]: item for item in self.feed(token, page_size=50).data["results"]}
        self.assertEqual(page[debut.pk]["recommendation"]["reason"], "new_creator")
        self.assertEqual(page[debut.pk]["recommendation"]["label"], "From a new creator")


class RespondingToFeedbackTests(ForYouTestCase):
    def test_not_interested_removes_a_post_at_once_even_mid_session(self):
        profile, token = self.reader("picky", ["Running"])
        first = self.feed(token, page_size=3)
        upcoming = self.ids(self.feed(token, page_size=50))
        target = upcoming[5]
        self.assertEqual(self.client.post(f"/api/v1/social/posts/{target}/not-interested/").status_code, 204)
        # The cached ranking from before still holds it; serving must not.
        second = self.follow_next(first)
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

    def test_the_next_page_follows_what_was_just_liked(self):
        """Two readers start identically. One likes two meals after the first
        page; their second page has more food on it than the other's."""
        liker, liker_token = self.reader("liker", [])
        _, idle_token = self.reader("idle", [])
        first_liker = self.feed(liker_token, page_size=4)
        first_idle = self.feed(idle_token, page_size=4)
        self.assertEqual(self.ids(first_liker), self.ids(first_idle))
        shown = set(self.ids(first_liker))
        for post in [meal for meal in self.meals if meal.pk not in shown][:2]:
            PostLike.objects.create(post=post, user=liker)
        self.authenticate(liker_token)
        second_liker = self.ids(self.follow_next(first_liker))
        self.authenticate(idle_token)
        second_idle = self.ids(self.follow_next(first_idle))
        self.assertGreater(self.share(second_liker, self.meals), self.share(second_idle, self.meals))
        self.assertFalse(set(second_liker) & shown, "a re-ranked page still never repeats")

    def test_a_dismissal_mid_session_reshapes_the_rest(self):
        reader, token = self.reader("tired_of_running", ["Running"])
        first = self.feed(token, page_size=2)
        for post in self.running:
            if post.pk not in self.ids(first):
                PostFeedback.objects.create(viewer=reader, post=post)
                break
        dismissed_author = next(p.author_id for p in self.running if PostFeedback.objects.filter(viewer=reader, post=p).exists())
        rest = []
        response = first
        while response.data["next"]:
            response = self.follow_next(response)
            rest.extend(response.data["results"])
        authors = [item["author"]["id"] for item in rest]
        self.assertNotIn(dismissed_author, authors[:6])

    def test_what_was_already_liked_or_saved_does_not_come_back(self):
        reader, token = self.reader("collector", [])
        liked, commented, reposted = self.meals[0], self.running[0], self.lifting[0]
        saved_meal, saved_workout = self.meals[1], self.lifting[1]
        PostLike.objects.create(post=liked, user=reader)
        PostComment.objects.create(post=commented, author=reader, body="nice")
        Post.objects.create(author=reader, kind=Post.Kind.REPOST, repost_of=reposted)
        SavedFoodMeal.objects.create(owner=reader, name="Copy", source_post=saved_meal)
        WorkoutTemplate.objects.create(owner=reader, name="Copy", workout_type="lifting", source_post=saved_workout)
        shown = set(self.ids(self.feed(token, page_size=50)))
        for post in (liked, commented, reposted, saved_meal, saved_workout):
            self.assertNotIn(post.pk, shown)
        self.assertEqual(len(shown), 24 - 5)


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

    def test_this_weeks_most_liked_are_candidates_however_old(self):
        """The newest posts are only five hundred deep; what everyone has been
        liking this week is looked at as well."""
        reader, _ = self.reader("catching_up")
        hit = aged(meal_post(self.cooks[1]), hours=24 * 20)
        for fan in self.runners + self.lifters:
            PostLike.objects.create(post=hit, user=fan)
        with mock.patch.object(recommendations, "POOL_SIZE", 3):
            found = {c.pk: c for c in recommendations.candidates(reader, build_profile(reader))}
        self.assertIn(hit.pk, found)
        self.assertIn("trending", found[hit.pk].sources)


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
        video(post, status=PostVideo.Status.PENDING, duration_ms=1000)
        self.assert_never_shown(post)

    def test_posts_older_than_the_window(self):
        old = aged(meal_post(self.author), hours=24 * (recommendations.WINDOW_DAYS + 1))
        self.assert_never_shown(old)

    def test_posts_already_acted_on(self):
        liked, commented = meal_post(self.author), meal_post(self.author)
        PostLike.objects.create(post=liked, user=self.profile)
        PostComment.objects.create(post=commented, author=self.profile, body="looks good")
        self.assert_never_shown(liked)
        self.assert_never_shown(commented)

    def test_topics_the_reader_hid(self):
        clip = meal_post(self.cooks[1])
        video(clip)
        RecommendationPreferences.objects.create(
            viewer=self.profile, hidden_topics=["type:running", "format:video"]
        )
        shown = set(self.ids(self.feed(self.token, page_size=50)))
        self.assertFalse(shown & {post.pk for post in self.running})
        self.assertNotIn(clip.pk, shown)
        self.assertTrue(shown & {post.pk for post in self.meals})

    def test_hiding_lifting_hides_the_sessions_drawn_as_lifting(self):
        untyped = workout_post(self.lifters[0], "lifting")
        untyped.workout.workout_type = None
        untyped.workout.save(update_fields=["workout_type"])
        RecommendationPreferences.objects.create(viewer=self.profile, hidden_topics=["type:lifting"])
        self.assertFalse(eligible_posts(self.profile, hidden=["type:lifting"]).filter(pk=untyped.pk).exists())
        self.assertNotIn(untyped.pk, self.ids(self.feed(self.token, page_size=50)))

    def test_a_post_hidden_after_ranking_is_not_served_from_the_session(self):
        first = self.feed(self.token, page_size=2)
        remaining = self.ids(self.feed(self.token, page_size=50))
        Post.objects.filter(pk__in=remaining).update(is_hidden=True)
        later = self.follow_next(first)
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
            response = self.follow_next(response)
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(len(seen), 24)

    def test_pages_are_full_even_after_posts_drop_out(self):
        first = self.feed(self.token, page_size=5)
        upcoming = [pk for pk in self.ids(self.feed(self.token, page_size=50)) if pk not in self.ids(first)]
        Post.objects.filter(pk__in=upcoming[:4]).update(is_hidden=True)
        self.authenticate(self.token)
        self.assertEqual(len(self.ids(self.follow_next(first))), 5)

    def test_a_retried_page_is_the_same_page(self):
        first = self.feed(self.token, page_size=5)
        once = self.ids(self.follow_next(first))
        PostLike.objects.create(post=self.meals[0], user=self.profile)
        again = self.ids(self.follow_next(first))
        self.assertEqual(once, again)

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
        response = self.follow_next(first)
        self.assertEqual(response.status_code, 200)
        dismissed = PostFeedback.objects.get(viewer=stranger_profile).post_id
        self.assertNotIn(dismissed, self.ids(response))

    def test_an_expired_ranking_starts_again_from_the_top(self):
        first = self.feed(self.token, page_size=5)
        cache.clear()
        response = self.follow_next(first)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["results"]), 5)
        self.assertIsNotNone(response.data["next"])

    def test_a_session_cached_by_the_previous_version_still_pages(self):
        self.authenticate(self.token)
        order = self.ids(self.feed(self.token, page_size=50))
        cache.set(
            recommendations._session_key(self.profile.pk, "old"),
            [(pk, "fresh", "New in the community") for pk in order],
            60,
        )
        response = self.client.get(FOR_YOU, {"cursor": recommendations.make_cursor("old", 5), "page_size": 5})
        self.assertEqual(self.ids(response), order[5:10])

    def test_signing_in_is_required(self):
        self.client.credentials()
        self.assertEqual(self.client.get(FOR_YOU).status_code, 401)
        self.assertEqual(self.client.post(IMPRESSIONS, {}, format="json").status_code, 401)


class ControlTests(ForYouTestCase):
    """What the reader can see and change about their page."""

    def setUp(self):
        super().setUp()
        self.profile, self.token = self.reader("in_charge", ["Running"])
        self.authenticate(self.token)

    def test_preferences_start_at_the_defaults_without_writing_anything(self):
        body = self.client.get(PREFERENCES).data
        self.assertTrue(body["personalized"])
        self.assertEqual(body["hidden_topics"], [])
        self.assertIsNone(body["history_cleared_at"])
        self.assertEqual([t["topic"] for t in body["hideable_topics"]], list(recommendations.HIDEABLE_TOPICS))
        running = next(i for i in body["interests"] if i["topic"] == "type:running")
        self.assertTrue(running["declared"])
        self.assertEqual(running["label"], "Running")
        self.assertFalse(RecommendationPreferences.objects.exists())

    def test_each_interest_is_shown_once_and_only_choices_are_called_choices(self):
        """Running as a workout type and as a discipline are one interest to
        a person; triathlon is what the ranking read into "Running", not
        something the reader said."""
        UserDiscipline.objects.create(profile=self.profile, discipline="running")
        interests = self.client.get(PREFERENCES).data["interests"]
        labels = [interest["label"] for interest in interests]
        self.assertEqual(len(labels), len(set(labels)), labels)
        by_label = {interest["label"]: interest for interest in interests}
        self.assertTrue(by_label["Running"]["declared"])
        self.assertEqual(by_label["Running"]["topic"], "type:running", "the one that can be hidden")
        self.assertFalse(by_label["Triathlon"]["declared"])

    def test_interests_show_what_was_picked_up_and_never_who(self):
        for post in self.meals[:3]:
            PostLike.objects.create(post=post, user=self.profile)
        body = self.client.get(PREFERENCES).data
        meals = next(i for i in body["interests"] if i["topic"] == "kind:meal")
        self.assertFalse(meals["declared"])
        self.assertGreater(meals["strength"], 0)
        self.assertNotIn("cook", json.dumps(body))

    def test_turning_personalisation_off_stops_using_activity(self):
        Follow.objects.create(follower=self.profile, following=self.cooks[0])
        for post in self.meals[2:5]:
            PostLike.objects.create(post=post, user=self.profile)
        response = self.client.patch(PREFERENCES, {"personalized": False}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["personalized"])
        self.assertEqual(response.data["interests"], [])
        reasons = {item["recommendation"]["reason"] for item in self.feed(self.token, page_size=50).data["results"]}
        self.assertFalse(reasons & {"following", "interest", "similar"}, reasons)
        self.client.patch(PREFERENCES, {"personalized": True}, format="json")
        self.assertIn("following", {item["recommendation"]["reason"] for item in self.feed(self.token, page_size=50).data["results"]})

    def test_hiding_topics(self):
        response = self.client.patch(
            PREFERENCES, {"hidden_topics": ["format:video", "type:running", "type:running"]}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["hidden_topics"], ["type:running", "format:video"])
        self.assertFalse(set(self.top(self.token, 50)) & {post.pk for post in self.running})
        self.assertNotIn("type:running", [i["topic"] for i in self.client.get(PREFERENCES).data["interests"]])
        self.client.patch(PREFERENCES, {"hidden_topics": []}, format="json")
        self.assertTrue(set(self.top(self.token, 50)) & {post.pk for post in self.running})

    def test_only_known_topics_can_be_hidden(self):
        for body in ({"hidden_topics": ["discipline:running"]}, {"hidden_topics": "type:running"},
                     {"hidden_topics": ["x"] * 21}, {"personalized": "maybe"}):
            with self.subTest(body=body):
                self.assertEqual(self.client.patch(PREFERENCES, body, format="json").status_code, 400)
        self.assertFalse(RecommendationPreferences.objects.exists())

    def test_starting_fresh_forgets_what_was_learned_and_deletes_viewing_history(self):
        for post in self.meals[:3]:
            PostLike.objects.create(post=post, user=self.profile)
        self.client.post(
            IMPRESSIONS, {"events": [{"post": post.pk, "dwell_ms": 9000} for post in self.lifting]}, format="json"
        )
        self.assertTrue(PostView.objects.filter(viewer=self.profile).exists())
        response = self.client.delete(HISTORY)
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.data["history_cleared_at"])
        self.assertFalse(PostView.objects.filter(viewer=self.profile).exists())
        self.assertEqual(PostLike.objects.filter(user=self.profile).count(), 3, "likes are the reader's own")
        topics = [i["topic"] for i in response.data["interests"]]
        self.assertNotIn("kind:meal", topics)
        self.assertIn("type:running", topics, "what they told us still counts")
        built = build_profile(self.profile)
        self.assertEqual(built.engaged_posts, ())

    def test_the_controls_need_signing_in_and_writes_are_limited(self):
        with mock.patch.dict(SimpleRateThrottle.THROTTLE_RATES, {"social": "1/hour"}):
            self.assertEqual(self.client.patch(PREFERENCES, {"personalized": False}, format="json").status_code, 200)
            self.assertEqual(self.client.patch(PREFERENCES, {"personalized": True}, format="json").status_code, 429)
            self.assertEqual(self.client.get(PREFERENCES).status_code, 200)
        self.client.credentials()
        for method, url in (("get", PREFERENCES), ("patch", PREFERENCES), ("delete", HISTORY)):
            self.assertEqual(getattr(self.client, method)(url).status_code, 401)


class RetentionTests(ForYouTestCase):
    def setUp(self):
        super().setUp()
        self.profile, _ = self.reader("remembered", [])

    def age(self, model, pk, field, days):
        model.objects.filter(pk=pk).update(**{field: timezone.now() - timedelta(days=days)})

    def test_viewing_history_is_kept_only_as_long_as_it_is_used(self):
        keep_days = recommendations.HISTORY_DAYS
        old_view = PostView.objects.create(viewer=self.profile, post=self.meals[0])
        recent_view = PostView.objects.create(viewer=self.profile, post=self.meals[1])
        old_feedback = PostFeedback.objects.create(viewer=self.profile, post=self.meals[2])
        old_dismissal = SuggestionDismissal.objects.create(viewer=self.profile, person=self.cooks[0])
        recent_dismissal = SuggestionDismissal.objects.create(viewer=self.profile, person=self.cooks[1])
        self.age(PostView, old_view.pk, "last_seen_at", keep_days + 1)
        self.age(PostView, recent_view.pk, "last_seen_at", keep_days - 1)
        self.age(PostFeedback, old_feedback.pk, "created_at", keep_days + 1)
        self.age(SuggestionDismissal, old_dismissal.pk, "created_at", 181)
        self.age(SuggestionDismissal, recent_dismissal.pk, "created_at", 179)

        dry = io.StringIO()
        call_command("prune_expired_rows", "--dry-run", stdout=dry)
        self.assertIn("would delete 1 viewing-history rows", dry.getvalue())
        self.assertTrue(PostView.objects.filter(pk=old_view.pk).exists())

        call_command("prune_expired_rows", stdout=io.StringIO())
        self.assertEqual(list(PostView.objects.values_list("pk", flat=True)), [recent_view.pk])
        self.assertFalse(PostFeedback.objects.exists())
        self.assertEqual(list(SuggestionDismissal.objects.values_list("pk", flat=True)), [recent_dismissal.pk])

    def test_the_deploy_check_notices_history_kept_too_long(self):
        view = PostView.objects.create(viewer=self.profile, post=self.meals[0])
        self.age(PostView, view.pk, "last_seen_at", recommendations.HISTORY_DAYS + 2)
        warnings = [w for w in checks.run_checks(include_deployment_checks=True) if w.id == "core.W007"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("viewing-history", warnings[0].msg)


class ScaleTests(ForYouTestCase):
    def count_queries(self, work):
        with CaptureQueriesContext(connection) as captured:
            work()
        return len(captured)

    def test_the_number_of_queries_does_not_grow_with_the_content(self):
        profile, token = self.reader("measured", ["Running"])
        Follow.objects.create(follower=profile, following=self.cooks[0])
        PostLike.objects.create(post=self.meals[0], user=profile)

        def cold():
            cache.clear()
            return self.count_queries(lambda: recommendations.build_ranking(profile, seed=1))

        small = cold()
        for author in self.runners + self.lifters + self.cooks:
            for _ in range(5):
                meal_post(author)
                workout_post(author, "running")
        self.assertEqual(cold(), small)

    def test_shared_counts_spare_the_next_reader_the_work(self):
        first, _ = self.reader("first_reader", [])
        second, _ = self.reader("second_reader", [])
        cache.clear()
        cold = self.count_queries(lambda: recommendations.build_ranking(first, seed=1))
        warm = self.count_queries(lambda: recommendations.build_ranking(second, seed=1))
        self.assertLessEqual(warm, cold - 6, "the per-post counts came from the cache")

    def test_later_pages_cost_the_same_however_much_there_is(self):
        """The ranking side of a later page. Drawing the cards costs a
        prefetch per kind of post on the page, which is bounded and is not
        what grows."""
        profile, _ = self.reader("long_scroller", ["Running"])

        def next_page():
            cache.clear()
            first = recommendations.serve_page(profile, None, 0, 3)
            return self.count_queries(
                lambda: recommendations.serve_page(profile, first.session, first.next_offset, 3)
            )

        small = next_page()
        for author in self.runners + self.lifters + self.cooks:
            for _ in range(3):
                meal_post(author)
        self.assertEqual(next_page(), small)

    def test_the_candidate_pool_is_capped(self):
        profile, _ = self.reader("capped", ["Running"])
        Follow.objects.create(follower=profile, following=self.runners[0])
        with mock.patch.object(recommendations, "POOL_SIZE", 5), \
                mock.patch.object(recommendations, "SOURCE_LIMIT", 3):
            found = recommendations.candidates(profile, build_profile(profile))
        self.assertLessEqual(len(found), 5 + 4 * 3)

    def test_latency_is_measured_and_reported(self):
        _, token = self.reader("timed")
        response = self.feed(token, page_size=3)
        self.assertRegex(response["Server-Timing"], r"^rank;dur=\d+(\.\d+)?$")
        self.assertGreaterEqual(recommendations.latency_summary(hours=1)["count"], 1)
        later = self.follow_next(response)
        self.assertRegex(later["Server-Timing"], r"^rank;dur=\d+(\.\d+)?$")
        self.assertGreaterEqual(recommendations.latency_summary(hours=1, kind="next")["count"], 1)


class ImpressionTests(ForYouTestCase):
    def setUp(self):
        super().setUp()
        self.profile, self.token = self.reader("watcher", [])
        self.authenticate(self.token)

    def send(self, *events):
        return self.client.post(IMPRESSIONS, {"events": list(events)}, format="json")

    def test_views_are_folded_into_one_row_per_post(self):
        post = self.meals[0]
        video(post)
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

    def test_a_later_batch_keeps_the_furthest_watch_and_the_first_surface(self):
        post = self.meals[0]
        video(post)
        self.send({"post": post.pk, "dwell_ms": 5000, "watch_ms": 8000, "surface": "for_you"})
        self.send({"post": post.pk, "dwell_ms": 200, "watch_ms": 1000, "surface": "profile"},
                  {"post": self.meals[1].pk, "dwell_ms": 4000})
        row = PostView.objects.get(viewer=self.profile, post=post)
        self.assertEqual((row.view_count, row.total_dwell_ms, row.max_watch_ms), (2, 5200, 8000))
        self.assertEqual(row.surface, "for_you")
        self.assertTrue(row.skipped)
        self.assertFalse(row.completed)
        fresh = PostView.objects.get(viewer=self.profile, post=self.meals[1])
        self.assertEqual((fresh.view_count, fresh.total_dwell_ms, fresh.max_watch_ms), (1, 4000, None))
        self.assertEqual(fresh.surface, "other")
        self.assertIsNotNone(fresh.first_seen_at)

    def test_a_full_batch_is_written_in_one_statement(self):
        posts = self.running + self.lifting + self.meals
        PostView.objects.create(viewer=self.profile, post=posts[0], view_count=4)
        events = [{"post": post.pk, "dwell_ms": 2000} for post in posts]
        with CaptureQueriesContext(connection) as captured:
            recorded = recommendations.record_impressions(self.profile, events)
        self.assertEqual(recorded, len(posts))
        # One to decide what is visible, one to write it.
        self.assertEqual(len(captured), 2, [q["sql"][:80] for q in captured])
        self.assertEqual(PostView.objects.get(viewer=self.profile, post=posts[0]).view_count, 5)
        self.assertEqual(PostView.objects.filter(viewer=self.profile).count(), len(posts))

    def test_for_you_views_carry_the_reason_they_were_shown_for(self):
        page = self.feed(self.token, page_size=4).data["results"]
        shown, other = page[0], page[1]
        self.send({"post": shown["id"], "dwell_ms": 3000, "surface": "for_you"},
                  {"post": other["id"], "dwell_ms": 3000, "surface": "following"})
        self.assertEqual(
            PostView.objects.get(viewer=self.profile, post_id=shown["id"]).reason,
            shown["recommendation"]["reason"],
        )
        self.assertEqual(PostView.objects.get(viewer=self.profile, post_id=other["id"]).reason, "")
        # A view of something For You never served has no reason to give.
        unserved = next(p for p in self.meals if p.pk not in {item["id"] for item in page})
        self.send({"post": unserved.pk, "surface": "for_you"})
        self.assertEqual(PostView.objects.get(viewer=self.profile, post=unserved).reason, "")

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

    def test_half_a_clip_watched_counts_as_interest(self):
        clip = meal_post(self.cooks[2])
        video(clip, duration_ms=10_000)
        self.send({"post": clip.pk, "dwell_ms": 1000, "watch_ms": 6000, "surface": "for_you"})
        self.assertGreater(build_profile(self.profile).topics.get("format:video", 0), 0)


class FeedReportTests(ForYouTestCase):
    def report(self, *args):
        output = io.StringIO()
        call_command("feed_report", "--json", *args, stdout=output)
        return json.loads(output.getvalue()), output.getvalue()

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
        report, raw = self.report()
        self.assertEqual(report["impressions"], 2)
        self.assertEqual(report["engagement_rate"], 0.5)
        self.assertEqual(report["skip_rate"], 0.5)
        self.assertEqual(report["actions"]["like_rate"], 0.5)
        self.assertEqual(report["actions"]["save_rate"], 0.0)
        self.assertGreaterEqual(report["latency"]["count"], 1)
        self.assertNotIn("measured_reader", raw)

    def test_each_reason_is_measured_on_its_own(self):
        profile, token = self.reader("reasoned", [])
        page = self.feed(token, page_size=10).data["results"]
        self.client.post(
            IMPRESSIONS,
            {"events": [{"post": item["id"], "dwell_ms": 3000, "surface": "for_you"} for item in page]},
            format="json",
        )
        liked = page[0]
        PostLike.objects.create(post_id=liked["id"], user=profile)
        report, _ = self.report()
        by_reason = report["by_reason"]
        self.assertEqual(sum(row["impressions"] for row in by_reason.values()), 10)
        self.assertGreater(by_reason[liked["recommendation"]["reason"]]["engagement_rate"], 0)

    def test_clip_watching_is_reported(self):
        profile, token = self.reader("watching", [])
        clip = meal_post(self.cooks[3])
        video(clip, duration_ms=10_000)
        self.authenticate(token)
        self.client.post(IMPRESSIONS, {"events": [
            {"post": clip.pk, "dwell_ms": 9000, "watch_ms": 5000, "surface": "for_you"},
        ]}, format="json")
        report, _ = self.report()
        self.assertEqual(report["watch"]["clip_impressions"], 1)
        self.assertEqual(report["watch"]["average_watch_fraction"], 0.5)
        self.assertEqual(report["watch"]["watch_time_ms"], 5000)

    @override_settings(FOR_YOU_EXPERIMENT={"name": "fresher", "variants": {"control": {}, "fresher": {"recency": 1.6}}})
    def test_an_experiment_is_reported_arm_by_arm(self):
        for n in range(6):
            profile, token = self.reader(f"subject{n}", [])
            self.authenticate(token)
            self.client.post(IMPRESSIONS, {"events": [
                {"post": self.meals[n].pk, "dwell_ms": 3000, "surface": "for_you"},
            ]}, format="json")
        report, _ = self.report()
        self.assertEqual(report["experiment"], "fresher")
        self.assertEqual(sum(arm["viewers"] for arm in report["by_variant"].values()), 6)
        self.assertEqual(sum(arm["impressions"] for arm in report["by_variant"].values()), 6)

    @override_settings(ANALYTICS_ENABLED=True)
    def test_return_rates_come_from_daily_activity(self):
        came_back, _ = self.reader("came_back", [])
        stayed_away, _ = self.reader("stayed_away", [])
        eight_days_ago = timezone.now() - timedelta(days=8)
        for person in (came_back, stayed_away):
            row = PostView.objects.create(viewer=person, post=self.meals[0], surface="for_you")
            PostView.objects.filter(pk=row.pk).update(first_seen_at=eight_days_ago)
        DailyActiveAccount.objects.create(user=came_back.user, day=(eight_days_ago + timedelta(days=1)).date())
        report, _ = self.report("--days", "10")
        self.assertEqual(report["retention"]["cohort"], 2)
        self.assertEqual(report["retention"]["next_day_return_rate"], 0.5)
        self.assertEqual(report["retention"]["seven_day_return_rate"], 0.5)

    @override_settings(ANALYTICS_ENABLED=False)
    def test_without_analytics_return_rates_are_not_guessed(self):
        report, _ = self.report()
        self.assertIsNone(report["retention"])


class ForYouContractTests(ForYouTestCase):
    """The page as sent matches the page as documented, with data in it."""

    def schema_for(self, path):
        from drf_spectacular.generators import SchemaGenerator

        from .contract_check import response_schema, to_json_schema

        spec = to_json_schema(SchemaGenerator().get_schema(request=None, public=True))
        schema = response_schema(spec, path)
        self.assertIsNotNone(schema)
        return schema

    def test_a_populated_page_matches_the_schema(self):
        from .contract_check import violations

        profile, token = self.reader("contract_reader", ["Running"])
        Follow.objects.create(follower=profile, following=self.cooks[0])
        video(self.meals[0], duration_ms=1000)
        planner_post(self.cooks[1])
        body = json.loads(json.dumps(self.feed(token, page_size=20).data, default=str))
        self.assertEqual(violations(self.schema_for(FOR_YOU), body), [])
        self.assertTrue(any(item["video"] for item in body["results"]))

    def test_the_preferences_match_the_schema(self):
        from .contract_check import violations

        profile, token = self.reader("contract_controls", ["Running"])
        PostLike.objects.create(post=self.meals[0], user=profile)
        self.authenticate(token)
        self.client.patch(PREFERENCES, {"hidden_topics": ["kind:planner"]}, format="json")
        body = json.loads(json.dumps(self.client.get(PREFERENCES).data, default=str))
        self.assertTrue(body["interests"])
        self.assertEqual(violations(self.schema_for(PREFERENCES), body), [])
