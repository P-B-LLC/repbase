"""The For You page counts only engagement it can trust, and keeps what it
should not recommend off it -- whatever that engagement says.

- whose likes count, and for how much: suspended, brand-new, bursting and
  single-minded accounts, and the author's own;
- comments, reposts and saves count once per person, never the author's,
  never one a moderator hid or from somebody the author blocked;
- a ring of fresh accounts liking one person's posts does not buy them the
  page, the trend or anybody's taste;
- an author a moderator acted against lately is not shown to strangers;
- bait and sales pitches are pushed down, plain questions are not;
- all of it can be switched off by weight, so it can be run as an
  experiment.
"""

from datetime import timedelta

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from . import feed_integrity, recommendations
from .feed_integrity import Trust, caption_flags, focus_factor, trust_by_id
from .fixtures_for_tests import aged, make_account, meal_post, workout_post
from .models import (
    Block,
    Follow,
    Post,
    PostComment,
    PostFeedback,
    PostLike,
    PostVideo,
    PostView,
    RepbaseUser,
    WorkoutTemplate,
)
from .recommendations import FeedWeights, InterestProfile, build_profile, engagement_for, score
from .test_recommendations import ForYouTestCase, candidate, video


def backdate(profile, days):
    RepbaseUser.objects.filter(pk=profile.pk).update(created_at=timezone.now() - timedelta(days=days))


def suspend(profile):
    profile.user.is_active = False
    profile.user.save(update_fields=["is_active"])


# ------------------------------------------------------------------ units

class TrustUnitTests(SimpleTestCase):
    def test_single_mindedness_costs_only_past_the_threshold(self):
        self.assertEqual(focus_factor(0.5), 1.0)
        self.assertEqual(focus_factor(feed_integrity.FOCUS_SHARE), 1.0)
        self.assertAlmostEqual(focus_factor(1.0), feed_integrity.FOCUS_FLOOR)
        self.assertLess(focus_factor(0.9), focus_factor(0.7))

    def test_focus_only_discounts_likes_on_the_one_author(self):
        trust = Trust(base=0.5, focus_author=7, focus_share=1.0)
        self.assertAlmostEqual(trust.for_author(7), 0.5 * feed_integrity.FOCUS_FLOOR)
        self.assertEqual(trust.for_author(8), 0.5)


class CaptionTests(SimpleTestCase):
    def test_bait_and_pitches_are_recognised(self):
        for caption in (
            "Like this if you trained legs today", "Double tap if you agree", "Tag a friend who skips legs",
            "tag 3 friends", "f4f", "follow for more", "Like and follow!",
        ):
            with self.subTest(caption=caption):
                self.assertEqual(caption_flags(caption), feed_integrity.BAIT_SHARE)
        for caption in (
            "DM me for coaching plans", "use my code RYT10", "Promo code in comments",
            "lose 10 lbs in 7 days", "Giveaway!", "best fat burners",
        ):
            with self.subTest(caption=caption):
                self.assertEqual(caption_flags(caption), feed_integrity.PITCH_SHARE)

    def test_ordinary_captions_and_real_questions_are_left_alone(self):
        for caption in (
            "", "Leg day done", "Comment your PR below?", "Which shoes do you like?",
            "I like this program", "Followed the plan for 12 weeks", "Shared a meal with my brother",
        ):
            with self.subTest(caption=caption):
                self.assertEqual(caption_flags(caption), 0.0)


class TrustedScoringTests(SimpleTestCase):
    weights = FeedWeights()

    def test_untrusted_engagement_earns_nothing(self):
        now = timezone.now()
        farmed = candidate(1, 5, {"kind:meal": 1.0}, likes=40, trusted_likes=2.0, impressions=60)
        earned = candidate(2, 6, {"kind:meal": 1.0}, likes=10, impressions=60)
        self.assertGreater(
            score(earned, InterestProfile(), self.weights, now)[0],
            score(farmed, InterestProfile(), self.weights, now)[0],
        )

    def test_trust_can_be_switched_off_for_an_experiment(self):
        now = timezone.now()
        farmed = candidate(1, 5, {"kind:meal": 1.0}, likes=40, trusted_likes=2.0, impressions=60)
        earned = candidate(2, 6, {"kind:meal": 1.0}, likes=10, impressions=60)
        raw = FeedWeights(trust=0.0)
        self.assertGreater(
            score(farmed, InterestProfile(), raw, now)[0],
            score(earned, InterestProfile(), raw, now)[0],
        )

    def test_bait_is_pushed_down(self):
        now = timezone.now()
        honest = candidate(1, 5, {"kind:meal": 1.0}, likes=10, impressions=40)
        bait = candidate(2, 6, {"kind:meal": 1.0}, likes=10, impressions=40, bait=1.0)
        honest_score, _ = score(honest, InterestProfile(), self.weights, now)
        bait_score, parts = score(bait, InterestProfile(), self.weights, now)
        self.assertAlmostEqual(parts["bait"], -self.weights.bait)
        self.assertGreater(honest_score, bait_score)


# ------------------------------------------------------------- whose likes

class AccountTrustTests(ForYouTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()

    def test_an_established_account_counts_in_full(self):
        self.assertEqual(trust_by_id([self.runners[0].pk])[self.runners[0].pk].base, 1.0)

    def test_a_suspended_account_counts_for_nothing(self):
        suspend(self.runners[0])
        self.assertEqual(trust_by_id([self.runners[0].pk])[self.runners[0].pk].base, 0.0)

    def test_a_brand_new_account_counts_for_half(self):
        # Whatever was worked out a moment ago: an account made after the
        # shared lists were cached is still known to be new.
        trust_by_id([self.runners[0].pk])
        fresh = make_account("fresh_face")[1]
        self.assertEqual(trust_by_id([fresh.pk])[fresh.pk].base, feed_integrity.NEW_ACCOUNT_TRUST)

    def test_likes_made_right_after_signing_up_count_for_half_too(self):
        post = self.meals[2]
        engagement_for([post.pk])
        cache.delete(f"for-you:stats4:{post.pk}")
        fresh = make_account("instant_liker")[1]
        PostLike.objects.create(post=post, user=fresh)
        self.assertEqual(engagement_for([post.pk])[post.pk].trusted_likes, feed_integrity.NEW_ACCOUNT_TRUST)

    def test_a_burst_of_likes_counts_for_less(self):
        bot = self.lifters[0]
        authors = self.runners + self.cooks + self.lifters[1:]
        posts = []
        for author in authors:
            posts += [meal_post(author) for _ in range(12)]
        PostLike.objects.bulk_create([PostLike(post=post, user=bot) for post in posts[:120]])
        self.assertAlmostEqual(
            trust_by_id([bot.pk])[bot.pk].base, feed_integrity.LIKE_BURST / 120, places=3
        )

    def test_an_account_that_only_likes_one_author_is_single_minded_about_them(self):
        puppet = self.cooks[3]
        target = self.runners[2]
        for post in [workout_post(target, "running") for _ in range(6)]:
            PostLike.objects.create(post=post, user=puppet)
        trust = trust_by_id([puppet.pk])[puppet.pk]
        self.assertEqual(trust.focus_author, target.pk)
        self.assertAlmostEqual(trust.for_author(target.pk), feed_integrity.FOCUS_FLOOR)
        self.assertEqual(trust.for_author(self.lifters[0].pk), 1.0)

    def test_a_fan_who_likes_other_people_too_is_not(self):
        fan = self.cooks[3]
        target = self.runners[2]
        for post in [workout_post(target, "running") for _ in range(6)]:
            PostLike.objects.create(post=post, user=fan)
        for post in self.lifting[:6]:
            PostLike.objects.create(post=post, user=fan)
        self.assertIsNone(trust_by_id([fan.pk])[fan.pk].focus_author)

    def test_liking_your_own_posts_does_not_make_you_single_minded(self):
        author = self.runners[1]
        for post in [workout_post(author, "running") for _ in range(6)]:
            PostLike.objects.create(post=post, user=author)
        self.assertIsNone(trust_by_id([author.pk])[author.pk].focus_author)


# --------------------------------------------------------- what is counted

class TrustedCountTests(ForYouTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.post = self.meals[0]
        self.author = self.post.author

    def counts(self):
        cache.clear()
        return engagement_for([self.post.pk])[self.post.pk]

    def test_the_authors_own_like_is_a_row_but_not_a_vote(self):
        PostLike.objects.create(post=self.post, user=self.author)
        PostLike.objects.create(post=self.post, user=self.runners[0])
        counts = self.counts()
        self.assertEqual(counts.likes, 2)
        self.assertEqual(counts.trusted_likes, 1.0)

    def test_suspended_and_brand_new_likers_count_for_less(self):
        fresh = make_account("fresh_liker")[1]
        PostLike.objects.create(post=self.post, user=self.runners[0])
        PostLike.objects.create(post=self.post, user=self.runners[1])
        PostLike.objects.create(post=self.post, user=fresh)
        suspend(self.runners[1])
        counts = self.counts()
        self.assertEqual(counts.likes, 3)
        self.assertEqual(counts.trusted_likes, 1.0 + feed_integrity.NEW_ACCOUNT_TRUST)

    def test_a_comment_counts_once_per_person_and_never_the_authors(self):
        for _ in range(10):
            PostComment.objects.create(post=self.post, author=self.runners[0], body="nice")
        PostComment.objects.create(post=self.post, author=self.runners[1], body="nice")
        PostComment.objects.create(post=self.post, author=self.author, body="thanks all")
        PostComment.objects.create(post=self.post, author=self.runners[2], body="hidden", is_hidden=True)
        counts = self.counts()
        self.assertEqual(counts.comments, 12, "every visible row, as before")
        self.assertEqual(counts.trusted_comments, 2)

    def test_comments_from_someone_the_author_blocked_are_not_rewarded(self):
        PostComment.objects.create(post=self.post, author=self.runners[0], body="you again")
        PostComment.objects.create(post=self.post, author=self.runners[1], body="nice")
        Block.objects.create(blocker=self.author, blocked=self.runners[0])
        self.assertEqual(self.counts().trusted_comments, 1)

    def test_a_repost_and_a_save_count_once_per_person(self):
        workout = self.lifting[0]
        for number in range(3):
            Post.objects.create(author=self.runners[0], kind=Post.Kind.REPOST, repost_of=workout)
            WorkoutTemplate.objects.create(owner=self.runners[1], name=f"copy {number}", source_post=workout)
        Post.objects.create(author=workout.author, kind=Post.Kind.REPOST, repost_of=workout)
        cache.clear()
        counts = engagement_for([workout.pk])[workout.pk]
        self.assertEqual((counts.shares, counts.saves), (4, 3))
        self.assertEqual((counts.trusted_shares, counts.trusted_saves), (1, 1))

    def test_views_skips_and_dismissals_from_suspended_accounts_do_not_count(self):
        for viewer, skipped in ((self.runners[0], True), (self.runners[1], True), (self.runners[2], False)):
            PostView.objects.create(viewer=viewer, post=self.post, view_count=1, skipped=skipped)
        PostFeedback.objects.create(viewer=self.runners[0], post=self.post)
        PostFeedback.objects.create(viewer=self.runners[1], post=self.post)
        suspend(self.runners[1])
        counts = self.counts()
        self.assertEqual((counts.impressions, counts.skips, counts.dismissals), (2, 1, 1))

    def test_fresh_accounts_cannot_bury_a_post(self):
        """Twenty accounts made a minute ago, each viewing and dismissing a
        post: none of it counts toward how others see it."""
        crowd = [make_account(f"burier{n}")[1] for n in range(20)]
        for person in crowd:
            PostView.objects.create(viewer=person, post=self.post, view_count=1, skipped=True)
            PostFeedback.objects.create(viewer=person, post=self.post)
        counts = self.counts()
        self.assertEqual((counts.impressions, counts.skips, counts.dismissals), (0, 0, 0))

    def test_not_interested_counts_only_from_somebody_who_was_shown_it(self):
        PostFeedback.objects.create(viewer=self.runners[0], post=self.post)
        self.assertEqual(self.counts().dismissals, 0)
        PostView.objects.create(viewer=self.runners[0], post=self.post, view_count=1)
        self.assertEqual(self.counts().dismissals, 1)

    def test_counts_cached_by_an_earlier_version_are_never_read(self):
        for older in ("stats", "stats3"):
            cache.set(f"for-you:{older}:{self.post.pk}", "an older shape", 600)
        self.assertEqual(engagement_for([self.post.pk])[self.post.pk].likes, 0)


# ---------------------------------------------------- a ring buying a page

class LikeRingTests(ForYouTestCase):
    """Fifteen accounts made this afternoon that like nothing but one
    person's posts, against a post a handful of established members liked."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.promoter = make_account("promoter")[1]
        backdate(cls.promoter, 40)
        cls.puppets = [make_account(f"puppet{n}")[1] for n in range(15)]
        cls.farmed = [aged(meal_post(cls.promoter, caption="Protein pancakes"), hours=3) for _ in range(5)]
        PostLike.objects.bulk_create([
            PostLike(post=post, user=puppet) for post in cls.farmed for puppet in cls.puppets
        ])
        cls.earned = cls.meals[1]
        PostLike.objects.bulk_create([
            PostLike(post=cls.earned, user=fan) for fan in cls.runners + cls.lifters[:1]
        ])

    def setUp(self):
        super().setUp()
        cache.clear()

    def test_the_ring_counts_for_a_fraction_of_its_rows(self):
        counts = engagement_for([self.farmed[0].pk])[self.farmed[0].pk]
        self.assertEqual(counts.likes, 15)
        self.assertLess(counts.trusted_likes, 15 * feed_integrity.NEW_ACCOUNT_TRUST * feed_integrity.FOCUS_FLOOR + 0.01)

    def test_the_farmed_posts_do_not_lead_the_page(self):
        _, token = self.reader("anybody")
        top = self.top(token, 5)
        self.assertIn(self.earned.pk, top)
        self.assertNotIn(self.farmed[0].pk, top[:2])
        self.assertLessEqual(self.share(top, self.farmed), 1)

    def test_without_trust_the_ring_would_have_led(self):
        """What the same page looked like before: raw counts, rows as they
        stand. The reason trust is on by default."""
        with override_settings(FOR_YOU_WEIGHTS={"trust": 0.0}):
            _, token = self.reader("anybody_raw")
            top = self.top(token, 5)
        self.assertIn(top[0], {post.pk for post in self.farmed})

    def test_the_ring_does_not_make_a_trend(self):
        trending = recommendations.trending_post_ids()
        self.assertIn(self.earned.pk, trending)
        self.assertFalse({post.pk for post in self.farmed} & set(trending))

    def test_the_ring_is_nobodys_taste_twin_at_full_weight(self):
        reader, _ = self.reader("lookalike")
        backdate(reader, 10)
        for post in self.farmed[:3]:
            PostLike.objects.create(post=post, user=reader)
        cache.clear()
        twin = make_account("twin")[1]
        backdate(twin, 10)
        for post in self.farmed[:3]:
            PostLike.objects.create(post=post, user=twin)
        PostLike.objects.create(post=self.lifting[3], user=twin)
        for puppet in self.puppets:
            PostLike.objects.create(post=self.running[5], user=puppet)
        cache.clear()
        found = recommendations.similar_tastes(reader, build_profile(reader))
        self.assertIn(self.lifting[3].pk, found)
        self.assertNotIn(self.running[5].pk, found, "fifteen fresh accounts are not a taste")


# ------------------------------------------------------ moderation history

class ModerationCooldownTests(ForYouTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.offender = self.cooks[2]
        self.other_post = next(post for post in self.meals if post.author == self.offender)
        self.taken_down = meal_post(self.offender)
        Post.objects.filter(pk=self.taken_down.pk).update(is_hidden=True, hidden_at=timezone.now())

    def eligible(self, viewer):
        return set(recommendations.eligible_posts(viewer).values_list("pk", flat=True))

    def test_strangers_are_not_shown_somebody_just_moderated(self):
        stranger, _ = self.reader("stranger")
        self.assertNotIn(self.other_post.pk, self.eligible(stranger))
        self.assertIn(self.meals[0].pk, self.eligible(stranger))

    def test_their_followers_still_are(self):
        follower, _ = self.reader("loyal")
        Follow.objects.create(follower=follower, following=self.offender)
        self.assertIn(self.other_post.pk, self.eligible(follower))

    def test_the_cooldown_ends(self):
        stranger, _ = self.reader("patient")
        Post.objects.filter(pk=self.taken_down.pk).update(
            hidden_at=timezone.now() - timedelta(days=feed_integrity.MODERATION_COOLDOWN_DAYS + 1)
        )
        self.assertIn(self.other_post.pk, self.eligible(stranger))

    def test_undoing_the_takedown_ends_it_at_once(self):
        stranger, _ = self.reader("forgiving")
        Post.objects.filter(pk=self.taken_down.pk).update(is_hidden=False, hidden_at=None)
        self.assertIn(self.other_post.pk, self.eligible(stranger))

    def test_a_rejected_clip_counts_as_a_moderation_decision(self):
        Post.objects.filter(pk=self.taken_down.pk).update(is_hidden=False, hidden_at=None)
        clip_author = self.runners[3]
        clip = video(workout_post(clip_author, "running"), status=PostVideo.Status.REJECTED)
        PostVideo.objects.filter(pk=clip.pk).update(reviewed_at=timezone.now())
        stranger, _ = self.reader("cautious")
        eligible = self.eligible(stranger)
        self.assertFalse({post.pk for post in self.running if post.author == clip_author} & eligible)
        self.assertIn(self.other_post.pk, eligible)

    def test_deleting_the_hidden_post_does_not_end_a_portal_decision(self):
        from .access_models import AccessAudit

        AccessAudit.objects.create(
            target=self.offender.user, action="moderation_hide", subject=f"post:{self.taken_down.pk}",
        )
        self.taken_down.delete()
        cache.clear()
        stranger, _ = self.reader("still_careful")
        self.assertNotIn(self.other_post.pk, self.eligible(stranger))

    def test_a_portal_decision_that_was_undone_does_not_count(self):
        from .access_models import AccessAudit

        AccessAudit.objects.create(
            target=self.offender.user, action="moderation_hide", subject=f"post:{self.taken_down.pk}",
        )
        Post.objects.filter(pk=self.taken_down.pk).update(is_hidden=False, hidden_at=None)
        cache.clear()
        stranger, _ = self.reader("trusting")
        self.assertIn(self.other_post.pk, self.eligible(stranger))

    def test_a_decision_reaches_the_page_within_the_minute(self):
        stranger, _ = self.reader("timely")
        Post.objects.filter(pk=self.taken_down.pk).update(is_hidden=False, hidden_at=None)
        self.assertIn(self.other_post.pk, self.eligible(stranger))
        Post.objects.filter(pk=self.taken_down.pk).update(is_hidden=True, hidden_at=timezone.now())
        # Cached for MODERATED_TTL_SECONDS, then read again.
        self.assertIn(self.other_post.pk, self.eligible(stranger))
        cache.delete("for-you:moderated")
        self.assertNotIn(self.other_post.pk, self.eligible(stranger))

    def test_it_reaches_the_page_itself(self):
        _, token = self.reader("scroller")
        self.assertNotIn(self.other_post.pk, self.ids(self.feed(token, page_size=50)))


class BaitTests(ForYouTestCase):
    def test_a_bait_caption_ranks_below_the_same_post_without_it(self):
        cache.clear()
        honest = meal_post(self.cooks[0], caption="Chicken and rice, prepped for the week")
        bait = meal_post(self.cooks[1], caption="Like this if you meal prep! Tag a friend")
        for fan in self.runners[:3]:
            PostLike.objects.create(post=honest, user=fan)
            PostLike.objects.create(post=bait, user=fan)
        _, token = self.reader("meal_prepper", [], emphasis="Nutrition")
        top = self.ids(self.feed(token, page_size=30))
        self.assertLess(top.index(honest.pk), top.index(bait.pk))
