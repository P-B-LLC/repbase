"""People worth following: who is suggested, why, and who never is.

The promises, each a class below:

- every source finds the people it should, and says so in a way that names
  nobody else;
- the people a reader must not be shown -- themselves, people they follow or
  asked to, blocks, closed or suspended profiles, dismissed suggestions --
  never are;
- a dismissal can be made and taken back, and is forgotten in time;
- with personalisation off, nothing the reader did picks who is suggested;
- the work is bounded and the response is what the contract says.
"""

import json
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APITestCase
from rest_framework.throttling import SimpleRateThrottle

from . import suggestions
from .fixtures_for_tests import FAST_PASSWORDS, aged, make_account, meal_post, workout_post
from .models import (
    Block,
    Follow,
    FollowRequest,
    PostLike,
    RecommendationPreferences,
    RepbaseUser,
    SuggestionDismissal,
    UserDiscipline,
)
from .tests import RepbaseAPITestMixin

SUGGESTED = "/api/v1/social/suggested-people/"


def dismiss_url(person):
    return f"{SUGGESTED}{person.pk}/dismiss/"


@FAST_PASSWORDS
class SuggestionTestCase(RepbaseAPITestMixin, APITestCase):
    """A reader and eight people who have all posted something lately.

    Everybody joined long ago unless a test says otherwise, so being new is
    a reason only where a test makes it one."""

    NAMES = ("ana", "ben", "cai", "dee", "eli", "fay", "gus", "hal")

    @classmethod
    def setUpTestData(cls):
        _, cls.reader, cls.token = make_account("seeker")
        cls.people = {name: make_account(name)[1] for name in cls.NAMES}
        long_ago = timezone.now() - timedelta(days=400)
        RepbaseUser.objects.filter(pk__in=[p.pk for p in cls.people.values()] + [cls.reader.pk]).update(
            created_at=long_ago
        )
        for person in cls.people.values():
            person.refresh_from_db()
            meal_post(person, hours_ago=30)

    def setUp(self):
        super().setUp()
        self.authenticate(self.token)

    def suggest(self, **params):
        response = self.client.get(SUGGESTED, params)
        self.assertEqual(response.status_code, 200, getattr(response, "data", response))
        return [(row["person"]["id"], row["reason"], row["label"]) for row in response.data]

    def reasons(self, **params):
        return {person: (reason, label) for person, reason, label in self.suggest(**params)}

    def like_posts_of(self, person, count=3, liker=None):
        for _ in range(count):
            PostLike.objects.create(post=meal_post(person), user=liker or self.reader)


class SourceTests(SuggestionTestCase):
    def test_people_whose_posts_you_keep_liking(self):
        self.like_posts_of(self.people["ana"])
        self.assertEqual(
            self.reasons()[self.people["ana"].pk], ("engaged", "You often like their posts")
        )

    def test_followed_by_people_you_follow_counts_only_open_profiles(self):
        ben, cai, dee = self.people["ben"], self.people["cai"], self.people["dee"]
        Follow.objects.create(follower=self.reader, following=ben)
        Follow.objects.create(follower=self.reader, following=cai)
        Follow.objects.create(follower=ben, following=dee)
        Follow.objects.create(follower=cai, following=dee)
        self.assertEqual(self.reasons()[dee.pk], ("mutual", "Followed by 2 people you follow"))
        # Closing cai's profile closes who cai follows, too.
        RepbaseUser.objects.filter(pk=cai.pk).update(is_profile_public=False)
        self.assertEqual(self.reasons()[dee.pk], ("mutual", "Followed by someone you follow"))

    def test_people_who_train_the_same_way(self):
        UserDiscipline.objects.create(profile=self.reader, discipline="running")
        eli = self.people["eli"]
        UserDiscipline.objects.create(profile=eli, discipline="running")
        workout_post(eli, "running")
        self.assertEqual(self.reasons()[eli.pk], ("discipline", "Also into running"))

    def test_new_creators_with_a_post_out(self):
        _, newcomer, _ = make_account("just_joined")
        meal_post(newcomer)
        _, lurker, _ = make_account("never_posted")
        found = self.reasons()
        self.assertEqual(found[newcomer.pk], ("new_creator", "New to Rytivo"))
        self.assertNotIn(lurker.pk, found)

    def test_popular_this_week(self):
        gus = self.people["gus"]
        hit = meal_post(gus)
        for name in ("ana", "ben", "cai", "dee"):
            PostLike.objects.create(post=hit, user=self.people[name])
        cache.clear()
        self.assertEqual(self.reasons()[gus.pk], ("popular", "Popular on Rytivo this week"))

    def test_taste_points_at_authors_never_at_the_people_who_share_it(self):
        """Two people like what the reader likes, and both like hal. Hal is
        suggested; the two are not -- that would make their likes the
        reason, and so make them visible."""
        twins = [make_account(f"twin{n}")[1] for n in range(2)]
        RepbaseUser.objects.filter(pk__in=[t.pk for t in twins]).update(created_at=timezone.now() - timedelta(days=400))
        for twin in twins:
            meal_post(twin)  # posting, so nothing but the rule keeps them out
        shared = [meal_post(self.people["ana"]) for _ in range(3)]
        for person in (self.reader, *twins):
            for post in shared:
                PostLike.objects.create(post=post, user=person)
        tip = meal_post(self.people["hal"])
        for twin in twins:
            PostLike.objects.create(post=tip, user=twin)
        found = self.reasons()
        self.assertEqual(found[self.people["hal"].pk][0], "similar")
        for twin in twins:
            self.assertNotIn(twin.pk, found)

    def test_the_most_personal_reason_that_carries_weight_is_given(self):
        ana = self.people["ana"]
        self.like_posts_of(ana, count=4)
        Follow.objects.create(follower=self.reader, following=self.people["ben"])
        Follow.objects.create(follower=self.people["ben"], following=ana)
        self.assertEqual(self.reasons()[ana.pk][0], "engaged")

    def test_the_limit_is_respected(self):
        for name in self.NAMES:
            self.like_posts_of(self.people[name], count=2)
        self.assertEqual(len(self.suggest(limit=3)), 3)
        self.assertEqual(len(self.suggest()), 8)


class ExclusionTests(SuggestionTestCase):
    def setUp(self):
        super().setUp()
        # Make everybody a candidate, so leaving someone out is a decision.
        for name in self.NAMES:
            self.like_posts_of(self.people[name], count=2)

    def assert_left_out(self, person):
        self.assertNotIn(person.pk, self.reasons())

    def test_everyone_is_a_candidate_to_begin_with(self):
        self.assertEqual(set(self.reasons()), {person.pk for person in self.people.values()})

    def test_the_reader_and_people_they_follow_or_asked_to(self):
        Follow.objects.create(follower=self.reader, following=self.people["ana"])
        FollowRequest.objects.create(requester=self.reader, target=self.people["ben"])
        found = self.reasons()
        self.assertNotIn(self.reader.pk, found)
        self.assertNotIn(self.people["ana"].pk, found)
        self.assertNotIn(self.people["ben"].pk, found)

    def test_blocks_in_either_direction(self):
        Block.objects.create(blocker=self.reader, blocked=self.people["cai"])
        Block.objects.create(blocker=self.people["dee"], blocked=self.reader)
        self.assert_left_out(self.people["cai"])
        self.assert_left_out(self.people["dee"])

    def test_closed_and_suspended_profiles(self):
        RepbaseUser.objects.filter(pk=self.people["eli"].pk).update(is_profile_public=False)
        fay = self.people["fay"]
        fay.user.is_active = False
        fay.user.save(update_fields=["is_active"])
        self.assert_left_out(self.people["eli"])
        self.assert_left_out(fay)

    def test_people_who_stopped_posting(self):
        gus = self.people["gus"]
        for post in gus.posts.all():
            aged(post, hours=24 * (suggestions.ACTIVE_DAYS + 1))
        self.assert_left_out(gus)

    def test_people_whose_only_recent_posts_are_private(self):
        hal = self.people["hal"]
        for post in hal.posts.all():
            aged(post, hours=24 * (suggestions.ACTIVE_DAYS + 1))
        meal_post(hal, visibility="private")
        self.assert_left_out(hal)

    def test_a_dismissal_lasts_six_months(self):
        hal = self.people["hal"]
        dismissal = SuggestionDismissal.objects.create(viewer=self.reader, person=hal)
        self.assert_left_out(hal)
        SuggestionDismissal.objects.filter(pk=dismissal.pk).update(created_at=timezone.now() - timedelta(days=181))
        self.assertIn(hal.pk, self.reasons())


class DismissalTests(SuggestionTestCase):
    def test_dismissing_and_taking_it_back(self):
        ana = self.people["ana"]
        self.like_posts_of(ana)
        self.assertEqual(self.client.post(dismiss_url(ana)).status_code, 204)
        self.assertEqual(self.client.post(dismiss_url(ana)).status_code, 204)
        self.assertEqual(SuggestionDismissal.objects.filter(viewer=self.reader).count(), 1)
        self.assertNotIn(ana.pk, self.reasons())
        self.assertEqual(self.client.delete(dismiss_url(ana)).status_code, 204)
        self.assertIn(ana.pk, self.reasons())

    def test_a_dismissal_is_the_readers_alone(self):
        ana = self.people["ana"]
        _, bystander, bystander_token = make_account("bystander")
        self.like_posts_of(ana)
        self.like_posts_of(ana, liker=bystander)
        self.client.post(dismiss_url(ana))
        self.authenticate(bystander_token)
        self.assertEqual(self.reasons()[ana.pk][0], "engaged")

    def test_only_real_reachable_people_can_be_dismissed(self):
        Block.objects.create(blocker=self.people["ben"], blocked=self.reader)
        for target in (self.reader, self.people["ben"]):
            self.assertEqual(self.client.post(dismiss_url(target)).status_code, 404)
        self.assertEqual(self.client.post(f"{SUGGESTED}999999/dismiss/").status_code, 404)
        self.assertFalse(SuggestionDismissal.objects.exists())

    def test_dismissing_is_rate_limited_and_needs_signing_in(self):
        with mock.patch.dict(SimpleRateThrottle.THROTTLE_RATES, {"social": "1/hour"}):
            self.assertEqual(self.client.post(dismiss_url(self.people["ana"])).status_code, 204)
            self.assertEqual(self.client.post(dismiss_url(self.people["ben"])).status_code, 429)
        self.client.credentials()
        self.assertEqual(self.client.get(SUGGESTED).status_code, 401)
        self.assertEqual(self.client.post(dismiss_url(self.people["cai"])).status_code, 401)


class PrivacyTests(SuggestionTestCase):
    def test_with_personalisation_off_activity_picks_nobody(self):
        self.like_posts_of(self.people["ana"])
        Follow.objects.create(follower=self.reader, following=self.people["ben"])
        Follow.objects.create(follower=self.people["ben"], following=self.people["cai"])
        UserDiscipline.objects.create(profile=self.reader, discipline="running")
        UserDiscipline.objects.create(profile=self.people["dee"], discipline="running")
        RecommendationPreferences.objects.create(viewer=self.reader, personalized=False)
        cache.clear()
        reasons = {reason for _, reason, _ in self.suggest()}
        self.assertFalse(reasons & {"engaged", "mutual", "discipline", "similar"}, reasons)

    def test_the_limit_is_checked(self):
        for value in ("0", "21", "many"):
            self.assertEqual(self.client.get(SUGGESTED, {"limit": value}).status_code, 400)


class ScaleTests(SuggestionTestCase):
    def test_the_work_does_not_grow_with_the_community(self):
        UserDiscipline.objects.create(profile=self.reader, discipline="running")
        Follow.objects.create(follower=self.reader, following=self.people["ben"])
        self.like_posts_of(self.people["ana"])

        def count():
            cache.clear()
            with CaptureQueriesContext(connection) as captured:
                suggestions.suggest_people(self.reader)
            return len(captured)

        small = count()
        for n in range(15):
            person = make_account(f"extra{n}")[1]
            UserDiscipline.objects.create(profile=person, discipline="running")
            Follow.objects.create(follower=self.people["ben"], following=person)
            meal_post(person)
        self.assertEqual(count(), small)


class ContractTests(SuggestionTestCase):
    def test_a_populated_response_matches_the_schema(self):
        from drf_spectacular.generators import SchemaGenerator

        from .contract_check import response_schema, to_json_schema, violations

        self.like_posts_of(self.people["ana"])
        body = json.loads(json.dumps(self.client.get(SUGGESTED).data, default=str))
        self.assertTrue(body)
        spec = to_json_schema(SchemaGenerator().get_schema(request=None, public=True))
        schema = response_schema(spec, SUGGESTED)
        self.assertIsNotNone(schema)
        self.assertEqual(violations(schema, body), [])
