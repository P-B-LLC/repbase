"""Milestone 5: the ranking reads measured attention, tags, sounds and
formats -- each only where a weight turns it on, so the ranking in use is
the control.

Three kinds of test: that with every new weight at zero the page is the one
it was; that each new signal moves what it should and no further than its
cap; and that eligibility is never something a score can buy back.
"""

from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from . import recommendations
from .fixtures_for_tests import meal_post
from .models import (
    Follow, Post, PostEngagementStats, PostHashtag, PostLike, PostView, Sound,
)
from .recommendations import (
    CANDIDATE_SHARES, MEASURED_VARIANT, TAG_CEILING, FeedWeights, InterestProfile,
    RankState, build_profile, build_ranking, candidates, eligible_posts, experiment_config,
    format_baselines, rank, score,
)
from .test_recommendations import ForYouTestCase, candidate, video

ON = FeedWeights.from_settings(MEASURED_VARIANT)
OFF = FeedWeights.from_settings()


def measured_view(viewer, post, *, hours_ago=1, **figures):
    """A PostView row as a version 2 app leaves it."""
    when = timezone.now() - timedelta(hours=hours_ago)
    values = dict(
        view_count=1, qualified_views=1, active_dwell_ms=2000, surface="for_you",
        last_seen_at=when, last_observed_at=when, last_observed_key="s:1",
    )
    values.update(figures)
    return PostView.objects.create(viewer=viewer, post=post, **values)


def tagged(post, *tags, sound=None):
    Post.objects.filter(pk=post.pk).update(hashtags=list(tags), sound=sound)
    PostHashtag.objects.bulk_create([PostHashtag(post=post, tag=tag) for tag in tags])
    return post


class ControlTests(SimpleTestCase):
    """What "retain the current ranker as the control" means, exactly."""

    def test_every_new_signal_is_off_by_default(self):
        for name in ("measured", "tag", "sound", "format_prior", "candidate_cap"):
            self.assertEqual(getattr(OFF, name), 0, name)

    def test_the_variant_is_a_valid_experiment_and_turns_them_on(self):
        with override_settings(FOR_YOU_EXPERIMENT={
            "name": "measured", "variants": {"control": {}, "measured": MEASURED_VARIANT},
        }):
            self.assertEqual(set(experiment_config()["variants"]), {"control", "measured"})
        with override_settings(FOR_YOU_EXPERIMENT={
            "name": "typo", "variants": {"control": {}, "measured": {"mesured": 1}},
        }), self.assertRaises(ImproperlyConfigured):
            experiment_config()
        self.assertEqual((ON.measured, ON.candidate_cap), (1.0, 500))

    def test_the_shares_are_the_plans_and_add_up(self):
        self.assertAlmostEqual(sum(CANDIDATE_SHARES.values()), 1.0)
        self.assertEqual(
            {name: round(500 * share) for name, share in CANDIDATE_SHARES.items()},
            {"interest": 160, "following": 100, "fresh": 100, "similar": 60, "trending": 60, "own": 20},
        )

    def test_a_score_has_no_new_parts_until_a_weight_asks_for_them(self):
        now = timezone.now()
        item = candidate(1, 5, {"kind:meal": 1})
        item.hashtags, item.sound_id, item.format = ("mealprep",), 7, "meal:still"
        profile = InterestProfile(tags={"mealprep": 0.9}, sounds={7: 0.9})
        _, off = score(item, profile, OFF, now)
        self.assertNotIn("tags", off)
        self.assertNotIn("sound", off)
        with mock.patch.object(recommendations, "format_baselines", return_value={"*": 0.1}):
            total_on, on = score(item, profile, ON, now)
        self.assertEqual({"tags", "sound"} - set(on), set())
        for name, value in off.items():
            self.assertAlmostEqual(on[name], value, msg=name)


class TagAndSoundScoreTests(SimpleTestCase):
    def part(self, name, item, profile):
        with mock.patch.object(recommendations, "format_baselines", return_value={}):
            return score(item, profile, ON, timezone.now())[1].get(name, 0.0)

    def test_stuffing_tags_buys_nothing(self):
        profile = InterestProfile(tags={"legday": 0.5, "squat": 0.4, "gym": 0.3, "fit": 0.9})
        one = candidate(1, 5, {"kind:workout": 1})
        one.hashtags = ("legday",)
        many = candidate(2, 6, {"kind:workout": 1})
        many.hashtags = ("legday", "squat", "gym", "fit", "a", "b", "c", "d", "e", "f")
        self.assertEqual(self.part("tags", one, profile), self.part("tags", many, profile),
                         "the best of the first three, not a sum, and not the tenth")

    def test_a_match_is_capped_and_explicit_interest_outweighs_a_viral_sound(self):
        profile = InterestProfile(tags={"x": 1.0}, sounds={7: 1.0}, topics={"kind:meal": 1.0})
        item = candidate(1, 5, {"kind:meal": 1})
        item.hashtags, item.sound_id = ("x",), 7
        self.assertAlmostEqual(self.part("tags", item, profile), ON.tag * TAG_CEILING)
        self.assertAlmostEqual(self.part("sound", item, profile), ON.sound * TAG_CEILING)
        self.assertGreater(self.part("topic", item, profile), 5 * self.part("sound", item, profile))

    def test_a_declared_topic_counts_like_a_tag_and_disliked_tags_count_against(self):
        profile = InterestProfile(topics={"discipline:powerlifting": 0.5}, tags={"spam": -0.9})
        declared = candidate(1, 5, {"kind:workout": 1})
        declared.declared = ("discipline:powerlifting",)
        self.assertAlmostEqual(self.part("tags", declared, profile), ON.tag * 0.5)
        disliked = candidate(2, 6, {"kind:workout": 1})
        disliked.hashtags = ("spam",)
        self.assertAlmostEqual(self.part("tags", disliked, profile), -ON.tag * TAG_CEILING)


class FormatBaselineTests(SimpleTestCase):
    def quality(self, item, baselines, weights=ON):
        with mock.patch.object(recommendations, "format_baselines", return_value=baselines):
            score(item, InterestProfile(), weights, timezone.now())
        return item.raw["quality"]

    def test_a_clip_is_not_marked_down_for_having_nothing_to_save(self):
        """Same views, the usual engagement for each format: the same
        quality, though the recipe's usual is three times the clip's."""
        baselines = {"media:video": 0.05, "meal:still": 0.15, "*": 0.1}
        clip = candidate(1, 5, {"kind:media": 1}, likes=10, impressions=200)
        clip.format = "media:video"
        recipe = candidate(2, 6, {"kind:meal": 1}, likes=30, impressions=200)
        recipe.format = "meal:still"
        self.assertAlmostEqual(self.quality(clip, baselines), self.quality(recipe, baselines), places=3)
        # Under one bar for everything, the clip looked a third as good.
        self.assertLess(self.quality(clip, baselines, OFF), self.quality(recipe, baselines, OFF) / 2)

    def test_a_post_with_no_record_is_average_for_its_format(self):
        baselines = {"media:video": 0.05, "meal:still": 0.15}
        fresh_clip = candidate(1, 5, {"kind:media": 1})
        fresh_clip.format = "media:video"
        fresh_recipe = candidate(2, 6, {"kind:meal": 1})
        fresh_recipe.format = "meal:still"
        self.assertAlmostEqual(self.quality(fresh_clip, baselines), self.quality(fresh_recipe, baselines))
        self.assertAlmostEqual(self.quality(fresh_clip, baselines), self.quality(candidate(3, 7, {}), {}, OFF))

    def test_an_unknown_format_falls_back_to_the_general_rate(self):
        item = candidate(1, 5, {"kind:media": 1}, likes=5, impressions=50)
        item.format = "media:video"
        self.assertAlmostEqual(self.quality(item, {"*": 0.1}), self.quality(item, {}, OFF))


class FamilyAndSoundVarietyTests(SimpleTestCase):
    def page(self, found, weights=OFF):
        import random
        return [item.pk for item in rank(found, InterestProfile(), weights, random.Random(1), limit=len(found))]

    def takes_and_others(self):
        family = []
        for pk in range(1, 6):
            item = candidate(pk, 100 + pk, {"kind:meal": 1}, score_value=3.0 - pk * 0.01)
            item.family = 1
            family.append(item)
        others = []
        for pk in range(6, 11):
            item = candidate(pk, 100 + pk, {"kind:meal": 1}, score_value=2.0)
            item.family = pk
            others.append(item)
        return family + others

    def test_one_family_does_not_fill_the_page(self):
        weights = FeedWeights.from_settings({"discovery_every": 0, "topic_repeat": 0})
        first_five = self.page(self.takes_and_others(), weights)[:5]
        self.assertLessEqual(sum(1 for pk in first_five if pk <= 5), 2)
        none = FeedWeights.from_settings({"discovery_every": 0, "topic_repeat": 0, "family_repeat": 0})
        self.assertEqual(self.page(self.takes_and_others(), none)[:5], [1, 2, 3, 4, 5])

    def test_with_nothing_else_to_show_the_family_is_still_shown(self):
        weights = FeedWeights.from_settings({"discovery_every": 0})
        only = self.takes_and_others()[:5]
        self.assertEqual(sorted(self.page(only, weights)), [1, 2, 3, 4, 5])

    def test_one_sound_does_not_run_down_the_page(self):
        weights = FeedWeights.from_settings({"discovery_every": 0, "topic_repeat": 0})
        found = []
        for pk in range(1, 9):
            item = candidate(pk, 100 + pk, {"kind:media": 1}, score_value=3.0 if pk <= 4 else 2.4)
            item.sound_id, item.family = (7 if pk <= 4 else None), pk
            found.append(item)
        first_four = self.page(found, weights)[:4]
        self.assertLess(sum(1 for pk in first_four if pk <= 4), 4)

    def test_a_session_saved_before_families_were_tracked_still_loads(self):
        state = RankState.load([3, {"5": 1}, [5], ["kind:meal"], [True]])
        self.assertEqual((state.recent_families, state.recent_sounds), ([], []))
        self.assertEqual(RankState.load(state.dump()).dump(), state.dump())


class MeasuredProfileTests(ForYouTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.viewer, _ = self.reader("measured_reader", [])

    def taste(self, weights=ON):
        return build_profile(self.viewer, weights).topics.get("kind:meal", 0.0)

    def test_a_seek_to_the_end_is_not_a_completion(self):
        """The old report: furthest position at the end, `completed` true.
        The measured one: a second and a half covered."""
        post = self.meals[0]
        video(post)
        measured_view(self.viewer, post, max_watch_ms=10_000, completed=True,
                      coverage_ms=1500, watched_through=False, active_dwell_ms=1600)
        self.assertGreater(self.taste(OFF), 0, "read the old way, it looks watched")
        self.assertEqual(self.taste(ON), 0.0)

    def test_watching_it_through_counts_and_half_of_it_counts_for_less(self):
        through, half = self.meals[0], self.meals[1]
        video(through)
        video(half)
        measured_view(self.viewer, through, coverage_ms=9800, watched_through=True)
        whole = self.taste()
        PostView.objects.all().delete()
        measured_view(self.viewer, half, coverage_ms=6000)
        self.assertGreater(whole, self.taste())
        self.assertGreater(self.taste(), 0)

    def test_a_loop_left_running_is_worth_one_rewatch(self):
        post = self.meals[0]
        video(post)
        measured_view(self.viewer, post, coverage_ms=10_000, watched_through=True, rewatch_ms=10_000)
        once = self.taste()
        PostView.objects.update(rewatch_ms=30_000)
        self.assertAlmostEqual(self.taste(), once)
        PostView.objects.update(rewatch_ms=0)
        self.assertLess(self.taste(), once)

    def test_reading_a_post_with_no_clip_counts_once(self):
        measured_view(self.viewer, self.meals[0], qualified_views=1, active_dwell_ms=8000)
        read = self.taste()
        self.assertGreater(read, 0)
        PostView.objects.update(qualified_views=4, active_dwell_ms=32_000, view_count=4)
        self.assertAlmostEqual(self.taste(), read)
        # A glance is not reading.
        PostView.objects.update(qualified_views=1, active_dwell_ms=1500, view_count=1)
        self.assertEqual(self.taste(), 0.0)

    def test_reading_the_recipe_counts_and_a_mis_tap_does_not(self):
        measured_view(self.viewer, self.meals[0], attachment_opens=1, attachment_read_ms=400)
        self.assertEqual(self.taste(), 0.0)
        PostView.objects.update(attachment_read_ms=12_000)
        self.assertGreater(self.taste(), 0)

    def test_a_skip_is_weak_and_a_broken_player_is_not_a_skip(self):
        measured_view(self.viewer, self.meals[0], skipped=True, active_dwell_ms=500, qualified_views=0)
        self.assertLess(self.taste(), 0)
        self.assertGreater(self.taste(), -0.2, "weak evidence")
        # core.attention never marks a failed playback skipped; the profile
        # reads the flag and nothing else.
        PostView.objects.update(skipped=False, playback_failures=1)
        self.assertEqual(self.taste(), 0.0)

    def test_a_row_from_an_old_app_is_read_the_old_way_under_either_ranking(self):
        post = self.meals[0]
        video(post)
        PostView.objects.create(viewer=self.viewer, post=post, view_count=1, completed=True,
                                max_watch_ms=10_000, total_dwell_ms=9000)
        self.assertGreater(self.taste(ON), 0)
        self.assertAlmostEqual(self.taste(ON), self.taste(OFF))

    def test_posting_a_take_is_the_strongest_thing_a_reader_can_do(self):
        liked, taken = self.meals[0], self.meals[1]
        PostLike.objects.create(post=liked, user=self.viewer)
        from_like = build_profile(self.viewer, ON).authors[liked.author_id]
        Post.objects.create(author=self.viewer, kind=Post.Kind.MEAL, is_take=True,
                            source_post=taken, original_post=taken)
        profile = build_profile(self.viewer, ON)
        self.assertGreater(profile.authors[taken.author_id], from_like)
        self.assertIn(taken.pk, profile.engaged_posts)

    def test_tags_and_sounds_are_learned_from_what_was_engaged_with(self):
        sound = Sound.objects.create(owner=self.cooks[0], title="s")
        tagged(self.meals[0], "mealprep", "chicken", "bowl", "fourth", sound=sound)
        PostLike.objects.create(post=self.meals[0], user=self.viewer)
        profile = build_profile(self.viewer, ON)
        self.assertEqual(set(profile.tags), {"mealprep", "chicken", "bowl"}, "the first three only")
        self.assertGreater(profile.sounds[sound.pk], 0)
        off = build_profile(self.viewer, OFF)
        self.assertEqual((off.tags, off.sounds), ({}, {}))


class EligibilityTests(ForYouTestCase):
    """A score never buys back what eligibility took away."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.viewer, self.token = self.reader("eligible_reader", ["Running"])

    def test_a_post_on_a_removed_sound_is_not_recommended_but_is_still_readable(self):
        sound = Sound.objects.create(owner=self.cooks[0], title="taken down")
        post = tagged(self.meals[0], sound=sound)
        self.assertIn(post.pk, set(eligible_posts(self.viewer).values_list("pk", flat=True)))
        Sound.objects.update(status=Sound.Status.REMOVED)
        self.assertNotIn(post.pk, set(eligible_posts(self.viewer).values_list("pk", flat=True)))
        # However well it does, under either ranking.
        PostEngagementStats.objects.update_or_create(post=post, defaults=dict(
            likes=500, trusted_likes=500, saves=500, trusted_saves=500, impressions=600,
            computed_at=timezone.now(), version=recommendations.STATS_VERSION
            if hasattr(recommendations, "STATS_VERSION") else 1,
        ))
        for overrides in ({}, MEASURED_VARIANT):
            with override_settings(FOR_YOU_WEIGHTS=overrides):
                cache.clear()
                self.assertNotIn(post.pk, [item.pk for item in build_ranking(self.viewer, seed=3).items])
        self.authenticate(self.token)
        self.assertEqual(self.client.get(f"/api/v1/social/posts/{post.pk}/").status_code, 200)

    def test_a_take_is_as_eligible_as_any_post_and_no_more(self):
        take = meal_post(self.cooks[1])
        Post.objects.filter(pk=take.pk).update(is_take=True, source_post=self.meals[0],
                                               original_post=self.meals[0])
        self.assertIn(take.pk, set(eligible_posts(self.viewer).values_list("pk", flat=True)))
        Post.objects.filter(pk=take.pk).update(visibility="private")
        self.assertNotIn(take.pk, set(eligible_posts(self.viewer).values_list("pk", flat=True)))

    def test_the_page_is_revalidated_after_ranking(self):
        """Ranked, then taken down before the page is read: not served."""
        first = self.feed(self.token, page_size=5).data["results"]
        self.assertTrue(first)
        cache.clear()
        with mock.patch.object(recommendations, "eligible_posts", wraps=eligible_posts):
            ranked = build_ranking(self.viewer, seed=11).items[0].pk
        Post.objects.filter(pk=ranked).update(is_hidden=True)
        served = [post["id"] for post in self.feed(self.token, page_size=20).data["results"]]
        self.assertNotIn(ranked, served)


class CandidateBudgetTests(ForYouTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.viewer, _ = self.reader("budget_reader", ["Running"])
        Follow.objects.create(follower=self.viewer, following=self.cooks[0])

    def found(self, **overrides):
        weights = FeedWeights.from_settings(overrides)
        profile = build_profile(self.viewer, weights)
        return candidates(self.viewer, profile, weights=weights)

    def test_the_cap_holds_and_unused_room_is_filled_with_fresh_posts(self):
        everything = {item.pk for item in self.found()}
        self.assertEqual(len(everything), 24)
        capped = self.found(candidate_cap=10)
        self.assertEqual(len(capped), 10)
        self.assertEqual(len({item.pk for item in capped}), 10, "merged, not repeated")
        roomy = self.found(candidate_cap=500)
        self.assertEqual({item.pk for item in roomy}, everything, "with room for all, all are there")

    def test_what_the_reader_asked_for_is_in_before_what_is_merely_new(self):
        # Their interest is running and they follow one cook; the newest
        # posts overall are a mix. With room for ten, those come first.
        capped = self.found(candidate_cap=10)
        wanted = [item for item in capped if item.sources & {"interest", "following"}]
        self.assertGreaterEqual(len(wanted), 5)
        self.assertTrue(any("following" in item.sources for item in capped))

    def test_tag_and_sound_matches_join_the_interest_source(self):
        sound = Sound.objects.create(owner=self.lifters[0], title="s")
        liked = tagged(self.lifting[0], "legday", sound=sound)
        same_tag = tagged(self.lifting[5], "legday")
        same_sound = tagged(self.meals[6], sound=sound)
        PostLike.objects.create(post=liked, user=self.viewer)
        found = {item.pk: item for item in self.found(**MEASURED_VARIANT)}
        self.assertIn("interest", found[same_tag.pk].sources)
        self.assertIn("interest", found[same_sound.pk].sources)
        self.assertEqual(found[same_tag.pk].hashtags, ("legday",))
        self.assertEqual(found[same_sound.pk].sound_id, sound.pk)

    def test_a_candidate_knows_its_format_and_family(self):
        video(self.meals[0])
        Post.objects.filter(pk=self.meals[1].pk).update(original_post=self.meals[0])
        found = {item.pk: item for item in self.found()}
        self.assertEqual(found[self.meals[0].pk].format, "meal:video")
        self.assertEqual(found[self.meals[2].pk].format, "meal:still")
        self.assertEqual(found[self.meals[1].pk].family, self.meals[0].pk)
        self.assertEqual(found[self.meals[2].pk].family, self.meals[2].pk)


class FormatBaselineDataTests(ForYouTestCase):
    def test_baselines_come_from_the_stored_counts_and_stay_in_range(self):
        cache.clear()
        now = timezone.now()
        video(self.meals[0])
        for post, likes, views in ((self.meals[0], 2, 1000), (self.meals[1], 900, 1000),
                                   (self.running[0], 100, 1000)):
            PostEngagementStats.objects.create(
                post=post, likes=likes, trusted_likes=likes, impressions=views, computed_at=now
            )
        found = format_baselines(now)
        self.assertEqual(set(found), {"meal:video", "meal:still", "workout:still", "*"})
        low, high = recommendations.FORMAT_BASELINE_RANGE
        self.assertTrue(all(low <= value <= high for value in found.values()), found)
        self.assertLess(found["meal:video"], found["workout:still"])
        self.assertEqual(found["meal:still"], high, "an unusually good week does not move the bar out of reach")
        with self.assertNumQueries(0):
            self.assertEqual(format_baselines(now), found)
