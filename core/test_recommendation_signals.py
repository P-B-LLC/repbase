"""What the For You page reads into what people do, and how it orders a page.

- the value of a post to strangers: saves and shares ahead of likes, per
  view, less how often other people turned it down;
- what a reader's own viewing says: coming back to a post, clips watched
  (only real clips, only as far as they go);
- the exercises in a workout as interests of their own, from a known
  vocabulary only;
- an author shown again and again to no effect stops being insisted on,
  without touching the people the reader follows or sought out;
- people the reader follows do not take the whole page while anybody else
  is there, and somebody who has said nothing yet gets a wider spread.
"""

from datetime import timedelta

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from . import recommendations
from .fixtures_for_tests import meal_post, workout_post
from .models import (
    Exercise,
    Follow,
    PostLike,
    PostView,
    PostWorkoutExercise,
)
from .recommendations import (
    CONNECTED_WINDOW,
    FeedWeights,
    InterestProfile,
    RankState,
    build_profile,
    rank,
    score,
)
from .test_recommendations import IMPRESSIONS, ForYouTestCase, candidate, video


def views(viewer, post, *, count=1, dwell_ms=0, skipped=False, surface=PostView.Surface.FOR_YOU, hours_ago=1):
    when = timezone.now() - timedelta(hours=hours_ago)
    return PostView.objects.create(
        viewer=viewer, post=post, view_count=count, total_dwell_ms=dwell_ms, skipped=skipped,
        surface=surface, last_seen_at=when,
    )


def lifting_with(author, *exercises, hours_ago=2):
    post = workout_post(author, "lifting", hours_ago=hours_ago, exercise=exercises[0])
    for order, name in enumerate(exercises[1:], start=2):
        PostWorkoutExercise.objects.create(
            post_workout=post.workout, name=name, order=order, set_count=2,
        )
    return post


# ------------------------------------------------------------------ units

class ValueTests(SimpleTestCase):
    weights = FeedWeights()

    def test_a_save_is_worth_more_than_a_like_for_the_same_reach(self):
        now = timezone.now()
        saved, _ = score(candidate(1, 5, {"kind:meal": 1}, saves=5, impressions=50), InterestProfile(), self.weights, now)
        liked, _ = score(candidate(2, 6, {"kind:meal": 1}, likes=5, impressions=50), InterestProfile(), self.weights, now)
        shared, _ = score(candidate(3, 7, {"kind:meal": 1}, shares=5, impressions=50), InterestProfile(), self.weights, now)
        self.assertGreater(saved, liked)
        self.assertGreater(shared, liked)

    def test_engagement_is_judged_per_view(self):
        """Ten likes from ten views beat twenty from five hundred: a post
        shown everywhere is not thereby a good one."""
        now = timezone.now()
        efficient, _ = score(candidate(1, 5, {"kind:meal": 1}, likes=10, impressions=10), InterestProfile(), self.weights, now)
        overexposed, _ = score(candidate(2, 6, {"kind:meal": 1}, likes=20, impressions=500), InterestProfile(), self.weights, now)
        self.assertGreater(efficient, overexposed)

    def test_a_post_with_no_record_is_neither_helped_nor_hurt_by_rejection_rates(self):
        _, parts = score(candidate(1, 5, {"kind:meal": 1}), InterestProfile(), self.weights, timezone.now())
        self.assertAlmostEqual(parts["skip_rate"], 0.0)
        self.assertAlmostEqual(parts["hide_rate"], 0.0)

    def test_what_other_people_scroll_past_is_pushed_down(self):
        now = timezone.now()
        stopped_on, parts_kept = score(candidate(1, 5, {"kind:meal": 1}, impressions=80, skips=8), InterestProfile(), self.weights, now)
        scrolled_past, parts_skipped = score(candidate(2, 6, {"kind:meal": 1}, impressions=80, skips=70), InterestProfile(), self.weights, now)
        self.assertGreater(parts_kept["skip_rate"], 0)
        self.assertLess(parts_skipped["skip_rate"], -0.4)
        self.assertGreater(stopped_on, scrolled_past)

    def test_a_little_not_interested_costs_a_lot(self):
        now = timezone.now()
        _, parts = score(candidate(1, 5, {"kind:meal": 1}, impressions=100, dismissals=5), InterestProfile(), self.weights, now)
        self.assertLess(parts["hide_rate"], -0.3)

    def test_rejection_can_bury_a_post_only_so_far(self):
        """However many dismissals arrive, they never count for more than
        the views behind them, nor past the ceiling."""
        now = timezone.now()
        _, flood = score(candidate(1, 5, {"kind:meal": 1}, impressions=4, dismissals=1000), InterestProfile(), self.weights, now)
        _, all_of_them = score(candidate(2, 5, {"kind:meal": 1}, impressions=4, dismissals=4), InterestProfile(), self.weights, now)
        self.assertEqual(flood["hide_rate"], all_of_them["hide_rate"])
        self.assertGreaterEqual(flood["hide_rate"], -self.weights.hide_rate * recommendations.HIDE_RATE_CEILING)

    def test_fatigue_and_detail_are_read_from_the_profile(self):
        profile = InterestProfile(
            topics={"exercise:deadlift": 0.8}, fatigue={9: 0.5},
        )
        item = candidate(1, 9, {"kind:workout": 1, "type:lifting": 1}, details={"exercise:deadlift": 1.0})
        _, parts = score(item, profile, self.weights, timezone.now())
        self.assertAlmostEqual(parts["detail"], self.weights.detail * 0.8)
        self.assertAlmostEqual(parts["fatigue"], -self.weights.fatigue * 0.5)


class ReRankingTests(SimpleTestCase):
    weights = FeedWeights(discovery_every=0)

    def test_followed_authors_take_at_most_their_share_while_others_are_there(self):
        followed = frozenset(range(100, 120))
        profile = InterestProfile(followed=followed, engaged_authors=followed, declared=frozenset({"kind:meal"}))
        found = [candidate(n, 100 + n, {"kind:meal": 1.0}, score_value=5 - n * 0.01) for n in range(20)]
        found += [candidate(100 + n, 300 + n, {"kind:meal": 1.0}, score_value=1 - n * 0.01) for n in range(20)]
        page = rank(found, profile, self.weights, __import__("random").Random(1), limit=20)
        for start in range(0, len(page) - CONNECTED_WINDOW + 1):
            window = page[start:start + CONNECTED_WINDOW]
            self.assertLessEqual(
                sum(1 for item in window if item.pk < 100),
                int(self.weights.connected_share * CONNECTED_WINDOW),
            )

    def test_with_only_followed_authors_the_page_is_still_full(self):
        followed = frozenset(range(100, 120))
        profile = InterestProfile(followed=followed, engaged_authors=followed)
        found = [candidate(n, 100 + n, {"kind:meal": 1.0}) for n in range(20)]
        self.assertEqual(len(rank(found, profile, self.weights, __import__("random").Random(1), limit=20)), 20)

    def test_the_limit_can_be_lifted(self):
        weights = FeedWeights(discovery_every=0, connected_share=1.0)
        followed = frozenset(range(100, 120))
        profile = InterestProfile(followed=followed, engaged_authors=followed)
        found = [candidate(n, 100 + n, {"kind:meal": 1.0}, score_value=5) for n in range(10)]
        found += [candidate(100 + n, 300 + n, {"kind:meal": 1.0}, score_value=1) for n in range(10)]
        page = rank(found, profile, weights, __import__("random").Random(1), limit=10)
        self.assertTrue(all(item.pk < 100 for item in page))

    def test_somebody_who_said_nothing_yet_gets_a_wider_spread(self):
        topics = ("type:running", "type:lifting", "kind:meal", "type:swimming")

        def first_page(weights):
            # The best posts are nearly all running; everything else trails a little.
            found = [
                candidate(n, n, {"kind:workout": 1.0, topics[0]: 1.0}, score_value=3.0 - n * 0.01)
                for n in range(12)
            ] + [
                candidate(100 + n, 100 + n, {"kind:workout": 1.0, topics[1 + n % 3]: 1.0}, score_value=2.4 - n * 0.01)
                for n in range(12)
            ]
            page = rank(found, InterestProfile(), weights, __import__("random").Random(3), limit=10)
            return sum(1 for item in page if item.pk < 100)

        self.assertLess(first_page(FeedWeights(discovery_every=0)), first_page(FeedWeights(discovery_every=0, cold_variety=0)))

    def test_a_rank_state_saved_before_followed_slots_were_tracked_still_loads(self):
        for used in ({3: 1}, {"3": 1}):
            state = RankState.load([7, used, [3], ["kind:meal"]])
            self.assertEqual((state.position, state.authors_used[3], state.recent_followed), (7, 1, []))
            again = RankState.load(state.dump())
            self.assertEqual(again.dump(), state.dump())

    def test_the_other_version_never_reads_a_session(self):
        """Sessions are kept under a versioned key, so during a rolling
        deploy or a rollback a cursor from the other version opens nothing
        and ranks afresh, rather than meeting a state it cannot read."""
        self.assertIn(":v3:", recommendations._session_key(1, "abc"))
        self.assertNotEqual(recommendations._session_key(1, "abc"), "for-you:1:abc")


# ------------------------------------------------------------- end to end

class ViewingSignalTests(ForYouTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()

    def test_coming_back_to_a_post_says_more_than_one_look(self):
        once, _ = self.reader("looked_once", [])
        twice, _ = self.reader("came_back", [])
        views(once, self.meals[0], count=1, dwell_ms=8000)
        views(twice, self.meals[0], count=2, dwell_ms=16000)
        self.assertGreater(
            build_profile(twice).topics["kind:meal"], build_profile(once).topics["kind:meal"]
        )

    def test_watching_is_only_recorded_for_a_real_clip_and_never_past_its_end(self):
        profile, token = self.reader("watcher", [])
        self.authenticate(token)
        photo, clip = self.meals[0], self.meals[1]
        video(clip, duration_ms=10_000)
        response = self.client.post(IMPRESSIONS, {"events": [
            {"post": photo.pk, "dwell_ms": 3000, "watch_ms": 9000, "completed": True},
            {"post": clip.pk, "dwell_ms": 3000, "watch_ms": 3_600_000, "completed": True},
        ]}, format="json")
        self.assertEqual(response.status_code, 202)
        on_photo = PostView.objects.get(viewer=profile, post=photo)
        on_clip = PostView.objects.get(viewer=profile, post=clip)
        self.assertEqual((on_photo.max_watch_ms, on_photo.completed), (None, False))
        self.assertEqual((on_clip.max_watch_ms, on_clip.completed), (10_000, True))


class ExerciseInterestTests(ForYouTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        Exercise.objects.create(name="Deadlift", created_by=None)
        Exercise.objects.create(name="Overhead Press", created_by=None)
        cls.deadlifts = [lifting_with(lifter, "Deadlift", "Barbell Row") for lifter in cls.lifters]
        cls.presses = [lifting_with(lifter, "Overhead Press", "Lateral Raise") for lifter in cls.lifters]
        cls.secret = lifting_with(cls.lifters[0], "My Secret Finisher")

    def setUp(self):
        super().setUp()
        cache.clear()

    def test_a_workout_is_about_its_exercises(self):
        details = recommendations.details_by_post([self.deadlifts[0].pk, self.secret.pk])
        self.assertIn("exercise:deadlift", details[self.deadlifts[0].pk])
        self.assertNotIn(self.secret.pk, details, "a name nobody else uses is nobody's topic")

    def test_a_name_enough_people_use_is_a_topic_without_being_in_the_catalogue(self):
        details = recommendations.details_by_post([self.deadlifts[1].pk])
        self.assertIn("exercise:barbell row", details[self.deadlifts[1].pk])

    def test_liking_deadlifts_brings_deadlifts_ahead_of_presses(self):
        """From the same two lifters, whom the reader has never engaged with:
        the deadlifts come first, because deadlifts are what they liked."""
        reader, token = self.reader("puller", ["Strength"])
        for post in self.deadlifts[:2]:
            PostLike.objects.create(post=post, user=reader)
        with override_settings(FOR_YOU_WEIGHTS={"discovery_every": 0}):
            page = self.ids(self.feed(token, page_size=50))
        for deadlift, press in zip(self.deadlifts[2:], self.presses[2:]):
            self.assertLess(page.index(deadlift.pk), page.index(press.pk))

    def test_without_the_detail_weight_they_are_not_told_apart(self):
        reader, _ = self.reader("puller_without", ["Strength"])
        for post in self.deadlifts[:2]:
            PostLike.objects.create(post=post, user=reader)
        profile = build_profile(reader)
        found = {item.pk: item for item in recommendations.candidates(reader, profile)}
        weights = FeedWeights(detail=0.0, jitter=0.0, explore=0.0)
        now = timezone.now()
        deadlift = score(found[self.deadlifts[2].pk], profile, weights, now)[1]
        press = score(found[self.presses[2].pk], profile, weights, now)[1]
        self.assertEqual(deadlift["detail"], press["detail"])
        self.assertGreater(score(found[self.deadlifts[2].pk], profile, FeedWeights(), now)[1]["detail"], 0)

    def test_the_reason_names_the_exercise(self):
        reader, token = self.reader("puller_reasons", [])
        for post in self.deadlifts[:2]:
            PostLike.objects.create(post=post, user=reader)
        with override_settings(FOR_YOU_WEIGHTS={"discovery_every": 0}):
            results = self.feed(token, page_size=30).data["results"]
        reasons = {item["id"]: item["recommendation"]["label"] for item in results}
        self.assertIn("Because you like deadlift", {reasons.get(post.pk) for post in self.deadlifts[2:]})

    def test_one_person_spelling_a_name_three_ways_is_still_one_person(self):
        for spelling in ("Zercher Squat", "zercher squat", "ZERCHER SQUAT"):
            lifting_with(self.lifters[1], spelling)
        cache.clear()
        self.assertNotIn("zercher squat", recommendations.exercise_vocabulary())
        for lifter in self.lifters[2:]:
            lifting_with(lifter, "Zercher squat")
        cache.clear()
        self.assertIn("zercher squat", recommendations.exercise_vocabulary())

    def test_favourite_lifts_never_push_other_interests_out(self):
        reader, _ = self.reader("lifts_and_eats", [], emphasis="Nutrition")
        for post in self.deadlifts + self.presses:
            PostLike.objects.create(post=post, user=reader)
        profile = build_profile(reader)
        self.assertIn("kind:meal", profile.top_topics(5))
        self.assertTrue(all(not topic.startswith("exercise:") for topic in profile.top_topics(5)))
        self.assertTrue(profile.top_topics(3, exercises=True))

    def test_the_exercise_shows_among_what_the_page_picked_up(self):
        reader, token = self.reader("puller_interests", [])
        for post in self.deadlifts:
            PostLike.objects.create(post=post, user=reader)
        self.authenticate(token)
        labels = [item["label"] for item in self.client.get("/api/v1/social/for-you/preferences/").data["interests"]]
        self.assertIn("Deadlift", labels)
        self.assertNotIn("My secret finisher", labels)


class FatigueTests(ForYouTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.author = self.cooks[0]
        self.extra = [meal_post(self.author, hours_ago=1) for _ in range(3)]
        self.shown = [post for post in self.meals if post.author == self.author]

    def test_an_author_shown_again_and_again_to_no_effect_is_noted(self):
        reader, _ = self.reader("unmoved", [])
        for post in self.shown + self.extra[:1]:
            views(reader, post, dwell_ms=2500)
        fatigue = build_profile(reader).fatigue
        self.assertGreater(fatigue[self.author.pk], 0.3)

    def test_one_look_is_free(self):
        reader, _ = self.reader("glanced", [])
        views(reader, self.shown[0], dwell_ms=2500)
        self.assertNotIn(self.author.pk, build_profile(reader).fatigue)

    def test_not_for_people_they_follow_engage_with_or_sought_out(self):
        follower, _ = self.reader("follows_them", [])
        Follow.objects.create(follower=follower, following=self.author)
        fan, _ = self.reader("liked_once", [])
        PostLike.objects.create(post=self.shown[0], user=fan)
        visitor, _ = self.reader("visited_profile", [])
        for reader in (follower, fan):
            for post in self.shown + self.extra[:1]:
                views(reader, post, dwell_ms=2500)
        for post in self.shown + self.extra[:1]:
            views(visitor, post, dwell_ms=2500, surface=PostView.Surface.PROFILE)
        for reader in (follower, fan, visitor):
            self.assertNotIn(self.author.pk, build_profile(reader).fatigue, reader.user.username)

    def test_their_next_post_comes_lower(self):
        reader, token = self.reader("unmoved_scroller", [], emphasis="Nutrition")
        for post in self.shown + self.extra[:2]:
            views(reader, post, dwell_ms=2500)
        fresh = self.extra[2]
        with override_settings(FOR_YOU_WEIGHTS={"fatigue": 0}):
            before = self.ids(self.feed(token, page_size=40))
        after = self.ids(self.feed(token, page_size=40))
        self.assertGreater(after.index(fresh.pk), before.index(fresh.pk))


class ConnectedShareTests(ForYouTestCase):
    def test_people_you_follow_do_not_take_the_whole_page(self):
        reader, token = self.reader("follows_everyone_who_cooks", ["Running"])
        for cook in self.cooks:
            Follow.objects.create(follower=reader, following=cook)
            for _ in range(4):
                meal_post(cook)
        cache.clear()
        page = self.ids(self.feed(token, page_size=20))
        followed_posts = set(
            recommendations.Post.objects.filter(author__in=self.cooks).values_list("pk", flat=True)
        )
        for start in range(0, len(page) - CONNECTED_WINDOW + 1):
            window = page[start:start + CONNECTED_WINDOW]
            self.assertLessEqual(sum(pk in followed_posts for pk in window), CONNECTED_WINDOW // 2)


class DiversityReportTests(ForYouTestCase):
    def report(self):
        import io
        import json

        from django.core.management import call_command

        output = io.StringIO()
        call_command("feed_report", "--json", stdout=output)
        return json.loads(output.getvalue()), output.getvalue()

    def test_where_attention_went_is_reported_without_naming_anyone(self):
        reader, _ = self.reader("spread_reader", [])
        Follow.objects.create(follower=reader, following=self.cooks[0])
        shown = [post for post in self.meals if post.author == self.cooks[0]][:1] + self.running[:3]
        for post in shown:
            views(reader, post, dwell_ms=3000)
        PostView.objects.filter(viewer=reader, post=self.running[0]).update(view_count=2)
        report, raw = self.report()
        figures = report["diversity"]
        self.assertEqual(figures["unconnected_share"], 0.75)
        self.assertEqual(figures["repeat_impression_rate"], 0.25)
        self.assertEqual(figures["top_ten_author_share"], 1.0)
        self.assertEqual(figures["new_creator_share"], 1.0, "the community joined days ago")
        self.assertNotIn("spread_reader", raw)

    def test_an_empty_window_reports_nothing_rather_than_zero(self):
        report, _ = self.report()
        self.assertIsNone(report["diversity"]["unconnected_share"])
