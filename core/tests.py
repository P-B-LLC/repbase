import io
import json
import uuid
import tempfile
import warnings
from unittest import mock
from datetime import timedelta
from zoneinfo import ZoneInfo
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient, APITestCase

from . import food_sources
from .photos import FEED_PHOTO_BOX, feed_variant
from .models import (
    Block,
    Exercise,
    FoodEntry,
    FoodMeal,
    FoodSearchCache,
    Follow,
    FollowRequest,
    Gym,
    Notification,
    ProfileSocialLink,
    PasswordResetCode,
    PlannerEntry,
    Post,
    PostMeal,
    PostMealEntry,
    PostWorkout,
    PostWorkoutExercise,
    RepbaseUser,
    SavedFoodMeal,
    SessionExercise,
    SetEntry,
    WorkoutExercise,
    WorkoutSchedule,
    WorkoutSession,
    WorkoutTemplate,
)


User = get_user_model()


class RepbaseAPITestMixin:
    """Account and token plumbing, shared by every test class here.

    Split out because the class below holds real tests as well as these two
    helpers: anything inheriting it to reach `create_account` also inherited
    eight tests and ran them again under its own name.
    """

    @classmethod
    def _pre_setup(cls):
        """Start every test with the throttle counters empty.

        They live in the cache, and no transaction rolls a cache back. Test
        users are created fresh per test but their primary keys repeat, and
        the throttle key is the primary key -- so two unrelated classes whose
        first account is pk 5 share a bucket, and the count is whatever every
        test before them left behind.

        That is how it announced itself: adding tests that post made three
        tests in a different class start failing with 429, having changed
        nothing about what they exercise. Whichever class ran last lost.

        In `_pre_setup` rather than `setUp` because Django calls it whatever
        a subclass does, and most subclasses here define `setUp` without
        chaining to super.
        """
        super()._pre_setup()
        cache.clear()

    def create_account(self, username):
        user = User.objects.create_user(
            username=username,
            email=f"{username}@example.com",
            password="StrongPass!234",
            first_name=username.title(),
            last_name="Tester",
        )
        profile = RepbaseUser.objects.create(
            user=user,
            height_cm=180,
            weight_kg=Decimal("80.00"),
        )
        token = Token.objects.create(user=user)
        return user, profile, token

    def authenticate(self, token):
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")


class FoodDayCopyTests(RepbaseAPITestMixin, APITestCase):
    """Repeating yesterday's eating on today."""

    COPY = "/api/v1/food/meals/copy-day/"

    def setUp(self):
        self.user, self.profile, self.token = self.create_account("eater")
        self.authenticate(self.token)
        self.today = timezone.now().date()
        self.yesterday = self.today - timedelta(days=1)

    def meal(self, date, name, position, foods=()):
        meal = FoodMeal.objects.create(
            owner=self.profile, date=date, name=name, position=position
        )
        for index, (food, calories) in enumerate(foods, start=1):
            FoodEntry.objects.create(
                meal=meal, name=food, calories=calories, position=index
            )
        return meal

    def copy(self, source=None, target=None):
        return self.client.post(
            self.COPY,
            {
                "source_date": str(source or self.yesterday),
                "target_date": str(target or self.today),
            },
            format="json",
        )

    def foods_on(self, date):
        return sorted(
            FoodEntry.objects.filter(
                meal__owner=self.profile, meal__date=date
            ).values_list("name", flat=True)
        )

    def test_yesterdays_food_lands_on_today(self):
        self.meal(self.yesterday, "Meal 1", 1, [("Oats", 300), ("Banana", 90)])
        self.meal(self.yesterday, "Meal 2", 2, [("Chicken", 450)])

        response = self.copy()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.foods_on(self.today), ["Banana", "Chicken", "Oats"])
        # And yesterday is still exactly as it was.
        self.assertEqual(self.foods_on(self.yesterday), ["Banana", "Chicken", "Oats"])

    def test_meal_names_travel(self):
        self.meal(self.yesterday, "Chicken bowl", 1, [("Chicken", 450)])
        self.copy()
        self.assertEqual(
            FoodMeal.objects.filter(owner=self.profile, date=self.today)
            .first()
            .name,
            "Chicken bowl",
        )

    def test_the_copy_is_its_own_food(self):
        # Copied, not linked: editing today must leave yesterday alone.
        self.meal(self.yesterday, "Meal 1", 1, [("Oats", 300)])
        self.copy()

        copy = FoodEntry.objects.get(meal__date=self.today, meal__owner=self.profile)
        copy.name = "Oats and honey"
        copy.save(update_fields=["name"])

        self.assertEqual(self.foods_on(self.yesterday), ["Oats"])

    def test_a_day_with_food_is_refused(self):
        self.meal(self.yesterday, "Meal 1", 1, [("Oats", 300)])
        self.meal(self.today, "Meal 1", 1, [("Toast", 200)])

        response = self.copy()
        self.assertEqual(response.status_code, 400)
        self.assertIn("target_date", response.data)
        # Refused means untouched, not partly applied.
        self.assertEqual(self.foods_on(self.today), ["Toast"])

    def test_empty_meal_slots_do_not_count_as_food(self):
        # A day that was merely opened has empty meals on it, and that must
        # still read as "nothing logged" or the button could never be offered.
        self.meal(self.yesterday, "Meal 1", 1, [("Oats", 300)])
        self.meal(self.today, "Meal 1", 1)
        self.meal(self.today, "Meal 2", 2)

        response = self.copy()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.foods_on(self.today), ["Oats"])

    def test_copying_an_empty_day_is_refused(self):
        response = self.copy()
        self.assertEqual(response.status_code, 400)
        self.assertIn("source_date", response.data)

    def test_a_day_cannot_be_copied_onto_itself(self):
        self.meal(self.today, "Meal 1", 1, [("Oats", 300)])
        response = self.copy(source=self.today, target=self.today)
        self.assertEqual(response.status_code, 400)
        self.assertIn("target_date", response.data)

    def test_only_your_own_days_are_visible(self):
        other_user, other_profile, other_token = self.create_account("stranger")
        FoodEntry.objects.create(
            meal=FoodMeal.objects.create(
                owner=other_profile, date=self.yesterday, name="Meal 1", position=1
            ),
            name="Not mine",
            calories=100,
            position=1,
        )
        # Their day is not a source this account can reach.
        response = self.copy()
        self.assertEqual(response.status_code, 400)
        self.assertIn("source_date", response.data)
        self.assertEqual(self.foods_on(self.today), [])


class SavedWorkoutDeletionTests(RepbaseAPITestMixin, APITestCase):
    """Deleting a saved workout, and what that is allowed to take."""

    WORKOUTS = "/api/v1/workouts/"
    CYCLES = "/api/v1/cycles/"

    def setUp(self):
        self.user, self.profile, self.token = self.create_account("lifter")
        self.authenticate(self.token)
        self.push = self.workout("Push Day")
        self.pull = self.workout("Pull Day")

    def workout(self, name):
        response = self.client.post(
            self.WORKOUTS, {"name": name, "workout_type": "lifting"},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        return response.data["id"]

    def test_a_saved_workout_can_be_deleted(self):
        response = self.client.delete(f"{self.WORKOUTS}{self.push}/")
        self.assertEqual(response.status_code, 204, getattr(response, "data", None))
        self.assertFalse(WorkoutTemplate.objects.filter(id=self.push).exists())

    def test_the_days_it_was_planned_on_go_with_it(self):
        # A planned day is made of the workout and means nothing without it.
        WorkoutSchedule.objects.create(
            owner=self.profile,
            workout_id=self.push,
            scheduled_date=timezone.now().date() + timedelta(days=2),
        )
        self.client.delete(f"{self.WORKOUTS}{self.push}/")
        self.assertEqual(
            WorkoutSchedule.objects.filter(workout_id=self.push).count(), 0
        )

    def test_training_already_done_survives_it(self):
        # History is not a plan. A session says what happened, and deleting
        # the template it was built from must not rewrite that.
        session = WorkoutSession.objects.create(
            repbase_user=self.profile,
            workout_id=self.push,
            status=WorkoutSession.Status.COMPLETED,
        )
        self.client.delete(f"{self.WORKOUTS}{self.push}/")
        session.refresh_from_db()
        self.assertIsNone(session.workout_id)

    def test_a_workout_inside_a_rotation_is_refused(self):
        response = self.client.post(
            self.CYCLES,
            {
                "name": "PPL", "length": 2,
                "anchor_date": str(timezone.now().date()),
                "slots": [
                    {"position": 1, "workout": self.push},
                    {"position": 2, "workout": self.pull},
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)

        refused = self.client.delete(f"{self.WORKOUTS}{self.push}/")
        self.assertEqual(refused.status_code, 400)
        self.assertIn("workout", refused.data)
        self.assertIn("PPL", str(refused.data["workout"]))
        # Still there, and so is the rotation it holds up.
        self.assertTrue(WorkoutTemplate.objects.filter(id=self.push).exists())

    def test_deleting_is_only_ever_your_own(self):
        other_user, other_profile, other_token = self.create_account("stranger")
        self.authenticate(other_token)
        response = self.client.delete(f"{self.WORKOUTS}{self.push}/")
        self.assertEqual(response.status_code, 404)
        self.assertTrue(WorkoutTemplate.objects.filter(id=self.push).exists())


class NotificationTests(RepbaseAPITestMixin, APITestCase):
    """What gets recorded when somebody does something, and who hears about it."""

    NOTIFS = "/api/v1/social/notifications/"
    COMMENTS = "/api/v1/social/comments/"

    def setUp(self):
        self.author, self.author_profile, self.author_token = (
            self.create_account("author")
        )
        self.other, self.other_profile, self.other_token = (
            self.create_account("other")
        )
        self.post = Post.objects.create(
            author=self.author_profile,
            kind=Post.Kind.MEAL,
            caption="lunch",
            visibility=Post.Visibility.PUBLIC,
        )
        PostMeal.objects.create(
            post=self.post, name="Meal 1", date=timezone.localdate()
        )

    def mine(self, token):
        self.authenticate(token)
        response = self.client.get(self.NOTIFS)
        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data
        return rows["results"] if isinstance(rows, dict) else rows

    def kinds_for(self, token):
        return [row["kind"] for row in self.mine(token)]

    def like_as(self, token):
        self.authenticate(token)
        return self.client.post(f"/api/v1/social/posts/{self.post.id}/like/")

    # ------------------------------------------------------------- who hears

    def test_a_like_reaches_the_author(self):
        self.like_as(self.other_token)
        rows = self.mine(self.author_token)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "like")
        self.assertEqual(rows[0]["actor_username"], self.other.username)
        self.assertEqual(rows[0]["post"], self.post.id)

    def test_liking_your_own_post_tells_nobody(self):
        self.like_as(self.author_token)
        self.assertEqual(self.mine(self.author_token), [])

    def test_liking_twice_over_is_still_one_line(self):
        # Unliking and liking again is the same person saying the same thing.
        self.like_as(self.other_token)
        self.authenticate(self.other_token)
        self.client.delete(f"/api/v1/social/posts/{self.post.id}/like/")
        self.like_as(self.other_token)
        self.assertEqual(len(self.mine(self.author_token)), 1)

    def test_a_follow_reaches_the_followed(self):
        self.authenticate(self.other_token)
        self.client.post(f"/api/v1/users/{self.author_profile.id}/follow/")
        self.assertEqual(self.kinds_for(self.author_token), ["follow"])

    def test_a_repost_reaches_the_author(self):
        self.authenticate(self.other_token)
        response = self.client.post(f"/api/v1/social/posts/{self.post.id}/repost/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.kinds_for(self.author_token), ["repost"])

    def test_a_comment_reaches_the_author_and_carries_what_was_said(self):
        self.authenticate(self.other_token)
        response = self.client.post(
            self.COMMENTS,
            {"post": self.post.id, "body": "looks good"},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        rows = self.mine(self.author_token)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "comment")
        self.assertEqual(rows[0]["comment_body"], "looks good")

    def test_a_reply_reaches_the_person_replied_to(self):
        # The author is told because it is their post; the person being
        # answered is told because it is their comment.
        self.authenticate(self.other_token)
        parent = self.client.post(
            self.COMMENTS, {"post": self.post.id, "body": "nice"}, format="json"
        ).data

        third, third_profile, third_token = self.create_account("third")
        self.authenticate(third_token)
        self.client.post(
            self.COMMENTS,
            {"post": self.post.id, "body": "agreed", "parent": parent["id"]},
            format="json",
        )
        self.assertIn("comment", self.kinds_for(self.other_token))

    def test_a_request_to_follow_reaches_them_and_goes_once_answered(self):
        self.author_profile.is_profile_public = False
        self.author_profile.save(update_fields=["is_profile_public"])

        self.authenticate(self.other_token)
        self.client.post(f"/api/v1/users/{self.author_profile.id}/follow/")
        self.assertEqual(self.kinds_for(self.author_token), ["follow_request"])

        row = FollowRequest.objects.get(target=self.author_profile)
        self.authenticate(self.author_token)
        self.client.post(f"/api/v1/social/follow-requests/{row.id}/approve/")
        # Answered, so the line offering to answer it goes.
        self.assertEqual(self.mine(self.author_token), [])
        # And the person who asked is told, or their end says "Requested"
        # until they think to look again.
        self.assertEqual(self.kinds_for(self.other_token), ["follow_approved"])

    def test_declining_tells_the_asker_nothing(self):
        # Being turned down is not news anybody needs delivered. Their end
        # simply stops saying "Requested" the next time it is read.
        self.author_profile.is_profile_public = False
        self.author_profile.save(update_fields=["is_profile_public"])

        self.authenticate(self.other_token)
        self.client.post(f"/api/v1/users/{self.author_profile.id}/follow/")
        row = FollowRequest.objects.get(target=self.author_profile)

        self.authenticate(self.author_token)
        self.client.delete(f"/api/v1/social/follow-requests/{row.id}/")
        self.assertEqual(self.mine(self.other_token), [])
        self.assertEqual(self.mine(self.author_token), [])

    # ------------------------------------------------------------ reading

    def test_notifications_are_only_ever_your_own(self):
        self.like_as(self.other_token)
        self.assertEqual(self.mine(self.other_token), [])

    def test_unread_is_counted_and_can_be_cleared(self):
        self.like_as(self.other_token)
        self.authenticate(self.author_token)

        counted = self.client.get(f"{self.NOTIFS}unread-count/")
        self.assertEqual(counted.status_code, 200, counted.data)
        self.assertEqual(counted.data["unread"], 1)

        read = self.client.post(f"{self.NOTIFS}read/")
        self.assertEqual(read.status_code, 200, read.data)
        self.assertEqual(read.data["unread"], 0)
        self.assertTrue(self.mine(self.author_token)[0]["is_read"])

    def test_deleting_a_comment_takes_its_line_with_it(self):
        # There is nothing left to read, so there is nothing left to say.
        self.authenticate(self.other_token)
        comment = self.client.post(
            self.COMMENTS, {"post": self.post.id, "body": "gone soon"},
            format="json",
        ).data
        self.assertEqual(len(self.mine(self.author_token)), 1)

        self.authenticate(self.other_token)
        self.client.delete(f"{self.COMMENTS}{comment['id']}/")
        self.assertEqual(self.mine(self.author_token), [])


class FollowRequestTests(RepbaseAPITestMixin, APITestCase):
    """Asking to follow a closed profile, and being answered."""

    REQUESTS = "/api/v1/social/follow-requests/"

    def setUp(self):
        self.owner, self.owner_profile, self.owner_token = (
            self.create_account("closed")
        )
        self.asker, self.asker_profile, self.asker_token = (
            self.create_account("asker")
        )
        self.owner_profile.bio = "I lift things"
        self.owner_profile.is_profile_public = False
        self.owner_profile.save(update_fields=["bio", "is_profile_public"])

    def ask(self, token=None):
        self.authenticate(token or self.asker_token)
        return self.client.post(f"/api/v1/users/{self.owner_profile.id}/follow/")

    def pending(self):
        return FollowRequest.objects.filter(target=self.owner_profile)

    def follows(self):
        return Follow.objects.filter(
            follower=self.asker_profile, following=self.owner_profile
        )

    # ------------------------------------------------------------ asking

    def test_following_a_closed_profile_asks_rather_than_follows(self):
        response = self.ask()
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(self.pending().count(), 1)
        self.assertFalse(self.follows().exists())

    def test_asking_twice_is_still_one_request(self):
        self.assertEqual(self.ask().status_code, 202)
        self.assertEqual(self.ask().status_code, 202)
        self.assertEqual(self.pending().count(), 1)

    def test_asking_does_not_open_the_profile(self):
        # The whole point: a request is not a way in on its own.
        self.ask()
        self.authenticate(self.asker_token)
        response = self.client.get(f"/api/v1/users/{self.owner_profile.id}/")
        self.assertFalse(response.data["is_readable"])
        self.assertEqual(response.data["bio"], "")

    def test_the_profile_says_a_request_is_outstanding(self):
        # Survives the emptying, or the button could not say "Requested" and
        # would offer to ask all over again.
        self.ask()
        self.authenticate(self.asker_token)
        data = self.client.get(f"/api/v1/users/{self.owner_profile.id}/").data
        self.assertTrue(data["viewer_has_requested"])
        self.assertFalse(data["viewer_follows"])

    def test_an_open_profile_is_still_followed_outright(self):
        self.owner_profile.is_profile_public = True
        self.owner_profile.save(update_fields=["is_profile_public"])
        self.assertEqual(self.ask().status_code, 201)
        self.assertTrue(self.follows().exists())
        self.assertEqual(self.pending().count(), 0)

    def test_withdrawing_takes_the_request_back(self):
        self.ask()
        self.authenticate(self.asker_token)
        response = self.client.delete(
            f"/api/v1/users/{self.owner_profile.id}/follow/"
        )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.pending().count(), 0)

    # --------------------------------------------------------- answering

    def test_the_owner_sees_who_is_asking(self):
        self.ask()
        self.authenticate(self.owner_token)
        response = self.client.get(self.REQUESTS)
        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data["results"] if isinstance(response.data, dict) else response.data
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["username"], self.asker.username)

    def test_approving_makes_it_a_follow(self):
        self.ask()
        row = self.pending().first()
        self.authenticate(self.owner_token)
        response = self.client.post(f"{self.REQUESTS}{row.id}/approve/")
        self.assertEqual(response.status_code, 204, response.data)
        self.assertTrue(self.follows().exists())
        self.assertEqual(self.pending().count(), 0)

    def test_an_approved_asker_can_read_the_profile(self):
        self.ask()
        row = self.pending().first()
        self.authenticate(self.owner_token)
        self.client.post(f"{self.REQUESTS}{row.id}/approve/")

        self.authenticate(self.asker_token)
        data = self.client.get(f"/api/v1/users/{self.owner_profile.id}/").data
        self.assertTrue(data["is_readable"])
        self.assertTrue(data["viewer_follows"])
        self.assertEqual(data["bio"], "I lift things")

    def test_declining_leaves_no_follow_and_no_record(self):
        self.ask()
        row = self.pending().first()
        self.authenticate(self.owner_token)
        response = self.client.delete(f"{self.REQUESTS}{row.id}/")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.pending().count(), 0)
        self.assertFalse(self.follows().exists())

    def test_somebody_elses_request_is_not_yours_to_answer(self):
        self.ask()
        row = self.pending().first()
        stranger, _, stranger_token = self.create_account("stranger")
        self.authenticate(stranger_token)
        self.assertEqual(
            self.client.post(f"{self.REQUESTS}{row.id}/approve/").status_code, 404
        )
        self.assertEqual(
            self.client.delete(f"{self.REQUESTS}{row.id}/").status_code, 404
        )
        self.assertEqual(self.pending().count(), 1)

    def test_the_asker_cannot_approve_their_own_request(self):
        self.ask()
        row = self.pending().first()
        self.authenticate(self.asker_token)
        self.assertEqual(
            self.client.post(f"{self.REQUESTS}{row.id}/approve/").status_code, 404
        )
        self.assertFalse(self.follows().exists())

    def test_opening_the_profile_grants_what_was_waiting(self):
        # Anybody could follow an open profile without asking, so holding the
        # requests would keep people out of something no longer shut.
        self.ask()
        self.authenticate(self.owner_token)
        response = self.client.patch(
            "/api/v1/me/", {"is_profile_public": True}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(self.follows().exists())
        self.assertEqual(self.pending().count(), 0)

    def test_an_unrelated_profile_edit_leaves_requests_alone(self):
        self.ask()
        self.authenticate(self.owner_token)
        self.client.patch("/api/v1/me/", {"bio": "still shut"}, format="json")
        self.assertEqual(self.pending().count(), 1)
        self.assertFalse(self.follows().exists())


class PrivateProfileReachTests(RepbaseAPITestMixin, APITestCase):
    """Who a closed profile is closed to, and what goes with it."""

    def setUp(self):
        self.author, self.author_profile, self.author_token = (
            self.create_account("closed")
        )
        self.fan, self.fan_profile, self.fan_token = self.create_account("fan")
        self.passer, self.passer_profile, self.passer_token = (
            self.create_account("passerby")
        )

        self.author_profile.bio = "I lift things"
        self.author_profile.is_profile_public = False
        self.author_profile.save(update_fields=["bio", "is_profile_public"])
        Follow.objects.create(
            follower=self.fan_profile, following=self.author_profile
        )
        self.post = self.meal_post()

    def meal_post(self):
        post = Post.objects.create(
            author=self.author_profile,
            kind=Post.Kind.MEAL,
            caption="lunch",
            visibility=Post.Visibility.PUBLIC,
        )
        meal = PostMeal.objects.create(
            post=post, name="Meal 1", date=timezone.localdate()
        )
        PostMealEntry.objects.create(
            post_meal=meal,
            name="Chicken Bowl",
            servings=1,
            calories=800,
            protein_grams=50,
        )
        return post

    def profile_as(self, token):
        self.authenticate(token)
        response = self.client.get(f"/api/v1/users/{self.author_profile.id}/")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def post_ids_as(self, token):
        self.authenticate(token)
        response = self.client.get("/api/v1/social/posts/")
        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data
        if isinstance(rows, dict):
            rows = rows["results"]
        return {row["id"] for row in rows}

    def open_the_profile(self):
        self.author_profile.is_profile_public = True
        self.author_profile.save(update_fields=["is_profile_public"])

    # --------------------------------------------------------- the profile

    def test_a_follower_reads_the_whole_profile(self):
        data = self.profile_as(self.fan_token)
        self.assertTrue(data["is_readable"])
        self.assertEqual(data["bio"], "I lift things")
        # Still closed. Readable describes the reader, not the profile, and
        # the two must not be collapsed into one flag.
        self.assertFalse(data["is_profile_public"])

    def test_somebody_who_does_not_follow_gets_the_closed_door(self):
        data = self.profile_as(self.passer_token)
        self.assertFalse(data["is_readable"])
        self.assertEqual(data["bio"], "")
        self.assertEqual(data["first_name"], "")

    def test_the_owner_always_reads_their_own(self):
        data = self.profile_as(self.author_token)
        self.assertTrue(data["is_readable"])
        self.assertEqual(data["bio"], "I lift things")

    def test_an_open_profile_is_readable_by_anybody(self):
        self.open_the_profile()
        data = self.profile_as(self.passer_token)
        self.assertTrue(data["is_readable"])
        self.assertEqual(data["bio"], "I lift things")

    # ----------------------------------------------------------- the posts

    def test_a_closed_authors_posts_are_hidden_from_a_passerby(self):
        self.assertNotIn(self.post.id, self.post_ids_as(self.passer_token))

    def test_a_follower_still_sees_them(self):
        self.assertIn(self.post.id, self.post_ids_as(self.fan_token))

    def test_the_author_still_sees_their_own(self):
        self.assertIn(self.post.id, self.post_ids_as(self.author_token))

    def test_the_detail_route_hides_it_too(self):
        # The list is not the only way in, and a rule enforced in one place
        # and not the other is not a rule.
        self.authenticate(self.passer_token)
        response = self.client.get(f"/api/v1/social/posts/{self.post.id}/")
        self.assertEqual(response.status_code, 404)

    def test_opening_the_profile_puts_the_posts_back(self):
        self.open_the_profile()
        self.assertIn(self.post.id, self.post_ids_as(self.passer_token))

    def test_opening_a_profile_does_not_widen_a_narrowed_post(self):
        # The two rules stack. A post the author marked followers-only stays
        # followers-only however open the profile around it is.
        self.open_the_profile()
        self.post.visibility = Post.Visibility.FOLLOWERS
        self.post.save(update_fields=["visibility"])
        self.assertNotIn(self.post.id, self.post_ids_as(self.passer_token))
        self.assertIn(self.post.id, self.post_ids_as(self.fan_token))


class PrivateProfileTests(RepbaseAPITestMixin, APITestCase):
    """A profile that is not public says almost nothing to a stranger."""

    def setUp(self):
        self.user, self.profile, self.token = self.create_account("shy")
        self.stranger, self.stranger_profile, self.stranger_token = (
            self.create_account("stranger")
        )
        self.user.first_name = "Ada"
        self.user.save(update_fields=["first_name"])
        self.profile.bio = "I lift things"
        self.profile.is_profile_public = False
        self.profile.save(update_fields=["bio", "is_profile_public"])

    def fetch_as(self, token):
        self.authenticate(token)
        response = self.client.get(f"/api/v1/users/{self.profile.id}/")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def test_profiles_are_public_until_somebody_says_otherwise(self):
        # Every account that existed before the switch did was public, and the
        # migration must not quietly change that.
        self.assertTrue(self.stranger_profile.is_profile_public)

    def test_a_stranger_is_told_it_is_private_and_little_else(self):
        data = self.fetch_as(self.stranger_token)
        self.assertFalse(data["is_profile_public"])
        self.assertEqual(data["username"], self.user.username)
        self.assertEqual(data["bio"], "")
        self.assertEqual(data["first_name"], "")
        self.assertEqual(data["last_name"], "")
        self.assertIsNone(data["profile_photo_url"])
        self.assertEqual(data["disciplines"], [])
        self.assertEqual(data["prompts"], [])
        self.assertEqual(data["social_links"], [])

    def test_the_shape_of_a_private_profile_is_the_same_shape(self):
        # The generated client requires every documented field, so a key that
        # goes missing when a profile closes is a profile the app cannot
        # decode rather than one it draws as private.
        closed = set(self.fetch_as(self.stranger_token))
        self.profile.is_profile_public = True
        self.profile.save(update_fields=["is_profile_public"])
        self.assertEqual(closed, set(self.fetch_as(self.stranger_token)))

    def test_your_own_closed_profile_is_not_hidden_from_you(self):
        data = self.fetch_as(self.token)
        self.assertEqual(data["bio"], "I lift things")
        self.assertEqual(data["first_name"], "Ada")

    def test_an_open_profile_is_untouched(self):
        self.profile.is_profile_public = True
        self.profile.save(update_fields=["is_profile_public"])
        data = self.fetch_as(self.stranger_token)
        self.assertTrue(data["is_profile_public"])
        self.assertEqual(data["bio"], "I lift things")
        self.assertEqual(data["first_name"], "Ada")

    def test_the_switch_is_read_and_written_through_me(self):
        self.authenticate(self.token)
        response = self.client.patch(
            "/api/v1/me/", {"is_profile_public": True}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["is_profile_public"])
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.is_profile_public)


class RepbaseAPITestCase(RepbaseAPITestMixin, APITestCase):

    def test_registration_returns_token_and_private_profile(self):
        response = self.client.post(
            "/api/v1/auth/register/",
            {
                "username": "new-lifter",
                "email": "lifter@example.com",
                "password": "StrongPass!234",
                "first_name": "New",
                "last_name": "Lifter",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertIn("token", response.data)
        self.assertEqual(response.data["user"]["email"], "lifter@example.com")
        self.assertTrue(User.objects.filter(username="new-lifter").exists())

    def test_public_profile_list_requires_auth_and_hides_private_fields(self):
        _, _, token = self.create_account("alice")
        # Somebody other than the viewer, because the list no longer carries
        # the viewer themself. Reading a stranger's row is the stronger test
        # in any case: what alice may see of alice was never the question.
        _, bob, _ = self.create_account("bob")

        anonymous_response = self.client.get("/api/v1/users/")
        self.assertEqual(anonymous_response.status_code, 401)

        self.authenticate(token)
        response = self.client.get("/api/v1/users/")
        self.assertEqual(response.status_code, 200)
        profile = response.data["results"][0]
        self.assertEqual(profile["id"], bob.id)
        self.assertNotIn("email", profile)
        self.assertNotIn("birthdate", profile)
        # Withheld as null rather than by dropping the key, so the shape of
        # the response does not change with the switch. The test predates that
        # decision and was asserting the old shape.
        self.assertIsNone(profile["weight_kg"])
        self.assertIsNone(profile["height_cm"])
        self.assertIsNone(profile["target_weight_kg"])

    def test_me_endpoint_updates_only_authenticated_profile(self):
        _, profile, token = self.create_account("alice")
        self.authenticate(token)

        # `gym` is a row now, not a string on the profile -- one gym shared by
        # everybody who trains there rather than a name retyped per person.
        # The test predates that and was still sending the name.
        gym = Gym.objects.create(name="Repbase Gym", city="Leeds")

        response = self.client.patch(
            "/api/v1/me/",
            {"gym": gym.id, "target_weight_kg": "75.00"},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        profile.refresh_from_db()
        self.assertEqual(profile.gym_id, gym.id)
        self.assertEqual(profile.target_weight_kg, Decimal("75.00"))

    def test_me_endpoint_deletes_account_and_token(self):
        user, _, token = self.create_account("delete-me")
        token_key = token.key
        self.authenticate(token)

        response = self.client.delete("/api/v1/me/")

        self.assertEqual(response.status_code, 204)
        self.assertFalse(User.objects.filter(pk=user.pk).exists())
        self.assertFalse(Token.objects.filter(key=token_key).exists())

    def test_sessions_are_scoped_to_the_authenticated_owner(self):
        _, first_profile, first_token = self.create_account("alice")
        _, second_profile, _ = self.create_account("bob")
        first_session = WorkoutSession.objects.create(repbase_user=first_profile)
        second_session = WorkoutSession.objects.create(repbase_user=second_profile)
        self.authenticate(first_token)

        response = self.client.get("/api/v1/sessions/")
        self.assertEqual(response.status_code, 200)
        returned_ids = {item["id"] for item in response.data["results"]}
        self.assertEqual(returned_ids, {first_session.id})

        response = self.client.get(f"/api/v1/sessions/{second_session.id}/")
        self.assertEqual(response.status_code, 404)

    def test_workout_session_flow_copies_exercises_and_logs_sets(self):
        _, profile, token = self.create_account("alice")
        exercise = Exercise.objects.create(name="Back Squat", created_by=profile)
        workout = WorkoutTemplate.objects.create(owner=profile, name="Leg Day")
        WorkoutExercise.objects.create(
            workout=workout,
            exercise=exercise,
            order=1,
            target_sets=3,
            target_reps=5,
        )
        self.authenticate(token)

        create_response = self.client.post(
            "/api/v1/sessions/",
            {"workout": workout.id},
            format="json",
        )
        self.assertEqual(create_response.status_code, 201)
        session_id = create_response.data["id"]
        session_exercise = SessionExercise.objects.get(session_id=session_id)

        start_response = self.client.post(f"/api/v1/sessions/{session_id}/start/")
        self.assertEqual(start_response.status_code, 200)
        self.assertEqual(start_response.data["status"], "active")

        set_response = self.client.post(
            "/api/v1/set-entries/",
            {
                "session_exercise": session_exercise.id,
                "set_number": 1,
                "weight_kg": "100.00",
                "reps": 5,
                "completed_at": timezone.now().isoformat(),
            },
            format="json",
        )
        self.assertEqual(set_response.status_code, 201)
        self.assertTrue(SetEntry.objects.filter(session_exercise=session_exercise).exists())

        end_response = self.client.post(f"/api/v1/sessions/{session_id}/end/")
        self.assertEqual(end_response.status_code, 200)
        self.assertEqual(end_response.data["status"], "completed")
        self.assertIsNotNone(end_response.data["duration_seconds"])

    def test_user_cannot_create_session_for_another_users_workout(self):
        _, _, first_token = self.create_account("alice")
        _, second_profile, _ = self.create_account("bob")
        workout = WorkoutTemplate.objects.create(owner=second_profile, name="Private")
        self.authenticate(first_token)

        response = self.client.post(
            "/api/v1/sessions/",
            {"workout": workout.id},
            format="json",
        )

        self.assertEqual(response.status_code, 400)

    def test_duration_is_computed_and_not_stored(self):
        _, profile, _ = self.create_account("alice")
        started_at = timezone.now()
        session = WorkoutSession.objects.create(
            repbase_user=profile,
            status=WorkoutSession.Status.COMPLETED,
            started_at=started_at,
            ended_at=started_at + timedelta(minutes=45),
        )

        self.assertEqual(session.duration_seconds, 2700.0)


class ProfileSocialLinkTests(RepbaseAPITestMixin, APITestCase):
    """The outbound accounts on a profile: writing them, and who may."""

    LINKS_URL = "/api/v1/me/social-links/"

    def setUp(self):
        self.user, self.profile, self.token = self.create_account("linker")
        self.other_user, self.other_profile, self.other_token = self.create_account(
            "onlooker"
        )

    def put(self, links, token=None):
        self.authenticate(token or self.token)
        return self.client.put(
            self.LINKS_URL, {"social_links": links}, format="json"
        )

    # ------------------------------------------------------------ creating

    def test_creates_links_from_urls_and_handles(self):
        response = self.put([
            {"platform": "instagram", "url": "https://www.instagram.com/example/"},
            {"platform": "x", "url": "@example"},
            {"platform": "website", "url": "example.com/about"},
        ])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            [(row["platform"], row["url"]) for row in response.data],
            [
                ("instagram", "https://www.instagram.com/example"),
                ("x", "https://x.com/example"),
                ("website", "https://example.com/about"),
            ],
        )

    def test_handle_is_kept_internally_but_never_sent(self):
        self.put([{"platform": "instagram", "url": "@example"}])
        link = ProfileSocialLink.objects.get(owner=self.profile)
        self.assertEqual(link.handle, "example")
        response = self.client.get(self.LINKS_URL)
        self.assertEqual(set(response.data[0]), {"platform", "url"})

    def test_order_is_the_order_sent(self):
        self.put([
            {"platform": "youtube", "url": "@second"},
            {"platform": "instagram", "url": "@first"},
        ])
        self.assertEqual(
            list(
                ProfileSocialLink.objects.filter(owner=self.profile)
                .values_list("platform", "position")
            ),
            [("youtube", 1), ("instagram", 2)],
        )

    # ------------------------------------------------- updating and deleting

    def test_put_replaces_the_whole_set(self):
        self.put([
            {"platform": "instagram", "url": "@one"},
            {"platform": "tiktok", "url": "@two"},
        ])
        response = self.put([{"platform": "instagram", "url": "@changed"}])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            [(row["platform"], row["url"]) for row in response.data],
            [("instagram", "https://www.instagram.com/changed")],
        )
        # The one left out is gone, not merely hidden.
        self.assertFalse(
            ProfileSocialLink.objects.filter(
                owner=self.profile, platform="tiktok"
            ).exists()
        )

    def test_empty_list_removes_them_all(self):
        self.put([{"platform": "instagram", "url": "@one"}])
        response = self.put([])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data, [])
        self.assertEqual(ProfileSocialLink.objects.filter(owner=self.profile).count(), 0)

    # ---------------------------------------------------------- duplicates

    def test_two_links_for_one_platform_are_refused(self):
        response = self.put([
            {"platform": "instagram", "url": "@one"},
            {"platform": "instagram", "url": "@two"},
        ])
        self.assertEqual(response.status_code, 400)
        self.assertIn("social_links", response.data)
        self.assertEqual(ProfileSocialLink.objects.filter(owner=self.profile).count(), 0)

    def test_more_links_than_platforms_are_refused(self):
        response = self.put([
            {"platform": "instagram", "url": f"@a{index}"} for index in range(7)
        ])
        self.assertEqual(response.status_code, 400)

    # --------------------------------------------------------- bad addresses

    def test_unsafe_and_malformed_urls_are_refused(self):
        for platform, value in [
            ("website", "javascript:alert(1)"),
            ("website", "data:text/html;base64,PHNjcmlwdD4="),
            ("website", "http://example.com"),
            ("website", "ftp://example.com"),
            ("website", "https://user:pass@example.com"),
            ("website", "not a url"),
            ("website", "https://192.168.0.1/admin"),
            ("instagram", "https://evil.example.com/example"),
            ("instagram", "@not a handle"),
            ("instagram", "@" + "a" * 200),
        ]:
            with self.subTest(platform=platform, value=value):
                response = self.put([{"platform": platform, "url": value}])
                self.assertEqual(response.status_code, 400, f"{value} was accepted")
                self.assertEqual(
                    ProfileSocialLink.objects.filter(owner=self.profile).count(),
                    0,
                    "a refused request must write nothing",
                )

    def test_a_link_that_lies_about_its_platform_is_refused(self):
        # The label is what a reader trusts before they tap, so an Instagram
        # badge pointing anywhere else is the case this exists to stop.
        response = self.put([
            {"platform": "instagram", "url": "https://tiktok.com/@example"}
        ])
        self.assertEqual(response.status_code, 400)

    def test_the_old_twitter_host_is_still_accepted_for_x(self):
        response = self.put([
            {"platform": "x", "url": "https://twitter.com/example"}
        ])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data[0]["url"], "https://twitter.com/example")

    def test_a_fragment_is_dropped_and_a_trailing_slash_trimmed(self):
        response = self.put([
            {"platform": "website", "url": "https://example.com/about/#top"}
        ])
        self.assertEqual(response.data[0]["url"], "https://example.com/about")

    # -------------------------------------------------------- authorization

    def test_a_user_only_ever_writes_their_own(self):
        self.put([{"platform": "instagram", "url": "@mine"}])
        # The other account writes its own set; the route carries no id, so
        # there is no request shape that could address somebody else's.
        self.put([{"platform": "tiktok", "url": "@theirs"}], token=self.other_token)

        self.assertEqual(
            list(
                ProfileSocialLink.objects.filter(owner=self.profile)
                .values_list("platform", flat=True)
            ),
            ["instagram"],
        )
        self.assertEqual(
            list(
                ProfileSocialLink.objects.filter(owner=self.other_profile)
                .values_list("platform", flat=True)
            ),
            ["tiktok"],
        )

    def test_signed_out_callers_are_refused(self):
        self.client.credentials()
        response = self.client.put(
            self.LINKS_URL,
            {"social_links": [{"platform": "instagram", "url": "@example"}]},
            format="json",
        )
        self.assertIn(response.status_code, (401, 403))

    # --------------------------------------------------------- serialisation

    def test_both_profile_responses_carry_the_links(self):
        self.put([{"platform": "instagram", "url": "@example"}])

        mine = self.client.get("/api/v1/me/")
        self.assertEqual(
            mine.data["social_links"],
            [{"platform": "instagram", "url": "https://www.instagram.com/example"}],
        )

        self.authenticate(self.other_token)
        public = self.client.get(f"/api/v1/users/{self.profile.id}/")
        self.assertEqual(
            public.data["social_links"],
            [{"platform": "instagram", "url": "https://www.instagram.com/example"}],
        )

    def test_a_profile_with_none_serialises_an_empty_list(self):
        # An empty list rather than a missing key, so a client never has to
        # tell "none" apart from "not sent".
        self.authenticate(self.token)
        mine = self.client.get("/api/v1/me/")
        self.assertEqual(mine.data["social_links"], [])

        self.authenticate(self.other_token)
        public = self.client.get(f"/api/v1/users/{self.profile.id}/")
        self.assertEqual(public.data["social_links"], [])


class PasswordResetTests(APITestCase):
    """Finding 04: a forgotten password used to lock the account forever.

    The cases worth writing down are the ones where the endpoint has to give
    nothing away. A wrong code and an address nobody has registered must be
    indistinguishable, or the reset flow becomes the account enumerator the
    login endpoint deliberately is not.
    """

    REQUEST = "/api/v1/auth/password-reset/"
    CONFIRM = "/api/v1/auth/password-reset/confirm/"
    EMAIL = "forgetful@example.invalid"
    OLD_PASSWORD = "the-old-one-12345"
    NEW_PASSWORD = "a-brand-new-one-98765"

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username="forgetful", email=self.EMAIL, password=self.OLD_PASSWORD
        )
        RepbaseUser.objects.get_or_create(user=self.user)
        self.sent = []

    def tearDown(self):
        cache.clear()

    def ask(self, email=None):
        """Ask for a code and read it back out of the email that was sent."""
        self.sent = []

        def capture(**kwargs):
            self.sent.append(kwargs)
            return 1

        with mock.patch("core.views.send_mail", side_effect=capture):
            response = self.client.post(
                self.REQUEST, {"email": email or self.EMAIL}, format="json"
            )
        code = self.sent[0]["message"].split("code is ")[1][:6] if self.sent else None
        return response, code

    def live_code(self):
        return PasswordResetCode.objects.filter(
            user=self.user, used_at__isnull=True
        ).first()

    # --- asking ---------------------------------------------------------

    def test_unknown_address_is_indistinguishable(self):
        response, code = self.ask("nobody@example.invalid")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.sent, [])
        self.assertFalse(PasswordResetCode.objects.exists())

    def test_known_address_is_emailed_six_digits(self):
        response, code = self.ask()
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.sent[0]["recipient_list"], [self.EMAIL])
        self.assertRegex(code, r"^\d{6}$")

    def test_the_code_is_not_stored_in_the_clear(self):
        _, code = self.ask()
        self.assertFalse(
            PasswordResetCode.objects.filter(code_hash=code).exists(),
            "a database dump should not be a list of live reset codes",
        )

    def test_asking_twice_leaves_only_the_newer_code_live(self):
        _, first = self.ask()
        _, second = self.ask()
        self.assertNotEqual(first, second)
        self.assertEqual(
            PasswordResetCode.objects.filter(
                user=self.user, used_at__isnull=True
            ).count(),
            1,
        )
        stale = self.client.post(
            self.CONFIRM,
            {"email": self.EMAIL, "code": first, "new_password": self.NEW_PASSWORD},
            format="json",
        )
        self.assertEqual(stale.status_code, 400)

    # --- confirming -----------------------------------------------------

    def test_the_right_code_sets_the_password(self):
        _, code = self.ask()
        response = self.client.post(
            self.CONFIRM,
            {"email": self.EMAIL, "code": code, "new_password": self.NEW_PASSWORD},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.NEW_PASSWORD))
        self.assertFalse(self.user.check_password(self.OLD_PASSWORD))

    def test_a_reset_signs_the_other_sessions_out(self):
        stale = Token.objects.create(user=self.user).key
        _, code = self.ask()
        response = self.client.post(
            self.CONFIRM,
            {"email": self.EMAIL, "code": code, "new_password": self.NEW_PASSWORD},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Token.objects.filter(key=stale).exists())
        self.assertEqual(Token.objects.filter(user=self.user).count(), 1)
        self.assertNotEqual(response.data["token"], stale)
        self.assertTrue(response.data["user"]["username"])

    def test_a_code_works_once(self):
        _, code = self.ask()
        self.client.post(
            self.CONFIRM,
            {"email": self.EMAIL, "code": code, "new_password": self.NEW_PASSWORD},
            format="json",
        )
        again = self.client.post(
            self.CONFIRM,
            {"email": self.EMAIL, "code": code, "new_password": "different-one-4321"},
            format="json",
        )
        self.assertEqual(again.status_code, 400)

    def test_an_expired_code_is_refused(self):
        _, code = self.ask()
        entry = self.live_code()
        entry.expires_at = timezone.now() - timedelta(seconds=1)
        entry.save(update_fields=["expires_at"])
        response = self.client.post(
            self.CONFIRM,
            {"email": self.EMAIL, "code": code, "new_password": self.NEW_PASSWORD},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_a_wrong_guess_is_counted(self):
        """The counter has to survive the refusal that records it.

        It did not, once: the increment was saved inside the same atomic block
        the ValidationError unwound, so every wrong guess rolled back its own
        tally and the ceiling below could never be reached.
        """
        _, code = self.ask()
        wrong = "000000" if code != "000000" else "111111"
        response = self.client.post(
            self.CONFIRM,
            {"email": self.EMAIL, "code": wrong, "new_password": self.NEW_PASSWORD},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.live_code().attempts, 1)

    def test_enough_wrong_guesses_spend_the_code(self):
        _, code = self.ask()
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(PasswordResetCode.MAX_ATTEMPTS):
            self.client.post(
                self.CONFIRM,
                {"email": self.EMAIL, "code": wrong, "new_password": self.NEW_PASSWORD},
                format="json",
            )
        self.assertIsNone(self.live_code())
        burned = self.client.post(
            self.CONFIRM,
            {"email": self.EMAIL, "code": code, "new_password": self.NEW_PASSWORD},
            format="json",
        )
        self.assertEqual(burned.status_code, 400, "even the right code is dead now")

    def test_an_unknown_address_reads_like_a_wrong_code(self):
        _, code = self.ask()
        wrong = "000000" if code != "000000" else "111111"
        mistyped = self.client.post(
            self.CONFIRM,
            {"email": self.EMAIL, "code": wrong, "new_password": self.NEW_PASSWORD},
            format="json",
        )
        stranger = self.client.post(
            self.CONFIRM,
            {
                "email": "nobody@example.invalid",
                "code": wrong,
                "new_password": self.NEW_PASSWORD,
            },
            format="json",
        )
        self.assertEqual(stranger.status_code, mistyped.status_code)
        self.assertEqual(stranger.data, mistyped.data)

    def test_a_reset_is_not_a_way_round_the_password_rules(self):
        _, code = self.ask()
        response = self.client.post(
            self.CONFIRM,
            {"email": self.EMAIL, "code": code, "new_password": "password"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIsNotNone(self.live_code(), "a refused password should not burn it")

    # --- throttling -----------------------------------------------------

    def test_asking_over_and_over_is_refused(self):
        codes = []
        with mock.patch("core.views.send_mail", return_value=1):
            for _ in range(9):
                codes.append(
                    self.client.post(
                        self.REQUEST, {"email": self.EMAIL}, format="json"
                    ).status_code
                )
        self.assertIn(429, codes)
        self.assertEqual(codes[0], 204)

    def test_confirming_is_not_capped_below_the_attempt_ceiling(self):
        """Both endpoints once shared one budget, which made MAX_ATTEMPTS moot.

        Five wrong guesses cannot be made if the sixth call of any kind is
        refused, so the two scopes are separate.
        """
        _, code = self.ask()
        wrong = "000000" if code != "000000" else "111111"
        codes = [
            self.client.post(
                self.CONFIRM,
                {"email": self.EMAIL, "code": wrong, "new_password": self.NEW_PASSWORD},
                format="json",
            ).status_code
            for _ in range(PasswordResetCode.MAX_ATTEMPTS)
        ]
        self.assertNotIn(429, codes)


class FoodSearchNormalisationTests(SimpleTestCase):
    """Reading FoodData Central without getting the numbers wrong.

    Every case here is one that returns a plausible number rather than an
    error, which is why it is worth a test: nothing crashes, the food log is
    just quietly incorrect.
    """

    def test_energy_is_matched_by_id_not_by_name(self):
        """The trap that would make every calorie 4.2 times too high.

        Two nutrients on the same food are both called Energy: 1008 in
        kilocalories and 1062 in kilojoules. The search endpoint lists the
        kilojoules, so anything matching on the name reads 1101 where the
        answer is 263.
        """
        food = food_sources.normalize({
            "fdcId": 171515,
            "description": "Chicken breast tenders, breaded, uncooked",
            "dataType": "SR Legacy",
            "foodNutrients": [
                {"nutrientId": 1062, "nutrientName": "Energy", "value": 1101.0},
                {"nutrientId": 1008, "nutrientName": "Energy", "value": 263.0},
                {"nutrientId": 1003, "value": 14.73},
                {"nutrientId": 1005, "value": 15.01},
                {"nutrientId": 1004, "value": 15.75},
            ],
        })
        self.assertEqual(food["calories"], Decimal("263.00"))

    def test_energy_order_does_not_matter(self):
        """Same food with the kilojoules listed second."""
        food = food_sources.normalize({
            "fdcId": 1,
            "description": "Anything",
            "dataType": "SR Legacy",
            "foodNutrients": [
                {"nutrientId": 1008, "nutrientName": "Energy", "value": 263.0},
                {"nutrientId": 1062, "nutrientName": "Energy", "value": 1101.0},
            ],
        })
        self.assertEqual(food["calories"], Decimal("263.00"))

    def test_kilojoules_are_converted_when_calories_are_absent(self):
        food = food_sources.normalize({
            "fdcId": 2,
            "description": "Only kilojoules",
            "dataType": "SR Legacy",
            "foodNutrients": [{"nutrientId": 1062, "value": 1101.0}],
        })
        # 1101 / 4.184
        self.assertEqual(food["calories"], Decimal("263.15"))

    def test_calories_are_burned_from_macros_as_a_last_resort(self):
        """Raw skinless chicken breast states protein and fat and no energy."""
        food = food_sources.normalize({
            "fdcId": 2646170,
            "description": "Chicken, breast, boneless, skinless, raw",
            "dataType": "Foundation",
            "foodNutrients": [
                {"nutrientId": 1003, "value": 22.5},
                {"nutrientId": 1005, "value": 0.0},
                {"nutrientId": 1004, "value": 1.93},
            ],
        })
        # 22.5 x 4 + 0 x 4 + 1.93 x 9
        self.assertEqual(food["calories"], Decimal("107.37"))

    def test_a_food_with_nothing_in_it_is_dropped(self):
        self.assertIsNone(food_sources.normalize({
            "fdcId": 3,
            "description": "Nothing known",
            "dataType": "Foundation",
            "foodNutrients": [],
        }))

    def test_negative_carbohydrate_is_clamped(self):
        """Carbohydrate by difference can land under zero on a wet food."""
        food = food_sources.normalize({
            "fdcId": 4,
            "description": "Chicken, breast, meat and skin, raw",
            "dataType": "Foundation",
            "foodNutrients": [
                {"nutrientId": 1008, "value": 126.9},
                {"nutrientId": 1003, "value": 21.4},
                {"nutrientId": 1005, "value": -0.43},
                {"nutrientId": 1004, "value": 4.78},
            ],
        })
        self.assertEqual(food["carbohydrate_grams"], Decimal("0"))

    def test_a_branded_label_is_read_per_serving(self):
        """165 kcal per 100 g and 469 per serving are both right.

        The packet says 469, so that is what a person reading the packet
        should be offered, and the serving it means is said out loud.
        """
        food = food_sources.normalize({
            "fdcId": 2187885,
            "description": "CHICKEN BREAST",
            "dataType": "Branded",
            "brandName": "GIANT EAGLE",
            "servingSize": 284.0,
            "servingSizeUnit": "g",
            "householdServingFullText": "1 CHICKEN BREAST",
            "labelNutrients": {
                "calories": {"value": 469},
                "protein": {"value": 58.0},
                "carbohydrates": {"value": 3.01},
                "fat": {"value": 23.0},
            },
            "foodNutrients": [{"nutrientId": 1008, "value": 165.0}],
        })
        self.assertEqual(food["calories"], Decimal("469.00"))
        self.assertEqual(food["serving_description"], "1 CHICKEN BREAST (284 g)")

    def test_a_branded_food_is_scaled_to_its_own_serving(self):
        """The usual case, because search never returns labelNutrients.

        foodNutrients is per 100 g and the response says a serving weighs
        284 g, which is enough to state the food as one of its own servings
        rather than as a weight nobody eats in. The same packet carries the
        label figures on the single-food endpoint, so the arithmetic can be
        checked against them: 469 kcal, 58 g of protein, 3.01 and 23.0.
        """
        food = food_sources.normalize({
            "fdcId": 2187885,
            "description": "CHICKEN BREAST",
            "dataType": "Branded",
            "brandName": "GIANT EAGLE",
            "servingSize": 284.0,
            "servingSizeUnit": "g",
            "householdServingFullText": "1 CHICKEN BREAST",
            "foodNutrients": [
                {"nutrientId": 1008, "value": 165.0},
                {"nutrientId": 1003, "value": 20.42},
                {"nutrientId": 1005, "value": 1.06},
                {"nutrientId": 1004, "value": 8.1},
            ],
        })
        self.assertEqual(food["serving_description"], "1 CHICKEN BREAST (284 g)")
        # Within the rounding the packet itself does.
        self.assertEqual(food["calories"], Decimal("468.60"))
        self.assertEqual(food["protein_grams"], Decimal("57.99"))
        self.assertEqual(food["carbohydrate_grams"], Decimal("3.01"))
        self.assertEqual(food["fat_grams"], Decimal("23.00"))

    def test_a_branded_food_with_no_serving_weight_stays_per_100g(self):
        """Nothing to scale by, so nothing is invented."""
        food = food_sources.normalize({
            "fdcId": 6,
            "description": "MYSTERY SNACK",
            "dataType": "Branded",
            "foodNutrients": [{"nutrientId": 1008, "value": 165.0}],
        })
        self.assertEqual(food["calories"], Decimal("165.00"))
        self.assertEqual(food["serving_description"], "100 g")

    def test_a_serving_measured_in_millilitres_is_not_treated_as_grams(self):
        """A drink states its serving in ml, and ml are not grams.

        Scaling per-100g figures by a volume assumes the food weighs a gram
        per millilitre, which is true of water and of very little else.
        """
        food = food_sources.normalize({
            "fdcId": 7,
            "description": "ORANGE JUICE",
            "dataType": "Branded",
            "servingSize": 240.0,
            "servingSizeUnit": "ml",
            "householdServingFullText": "1 CUP",
            "foodNutrients": [{"nutrientId": 1008, "value": 45.0}],
        })
        self.assertEqual(food["calories"], Decimal("45.00"))
        self.assertEqual(food["serving_description"], "100 g")

    def test_a_generic_food_stays_per_100g(self):
        """FoodData Central holds portions for these and does not return them
        from search, so there is nothing to scale by."""
        food = food_sources.normalize({
            "fdcId": 8,
            "description": "Chicken, breast, boneless, skinless, raw",
            "dataType": "Foundation",
            "foodNutrients": [{"nutrientId": 1008, "value": 120.0}],
        })
        self.assertEqual(food["serving_description"], "100 g")

    def test_shouted_names_are_made_readable(self):
        food = food_sources.normalize({
            "fdcId": 5,
            "description": "CHICKEN BREAST",
            "dataType": "Branded",
            "foodNutrients": [{"nutrientId": 1008, "value": 165.0}],
        })
        self.assertEqual(food["name"], "Chicken Breast")


class FoodSearchViewTests(RepbaseAPITestMixin, APITestCase):
    """The endpoint in front of it: caching, and what happens when it is down."""

    URL = "/api/v1/food/search/"

    SAMPLE = [{
        "source_id": "usda:1",
        "name": "Oats",
        "brand": "",
        "serving_description": "100 g",
        "calories": Decimal("389.00"),
        "protein_grams": Decimal("16.90"),
        "carbohydrate_grams": Decimal("66.30"),
        "fat_grams": Decimal("6.90"),
    }]

    def setUp(self):
        cache.clear()
        FoodSearchCache.objects.all().delete()
        _, _, token = self.create_account('food_probe')
        self.authenticate(token)

    def test_a_short_query_asks_nobody(self):
        with mock.patch("core.views.food_sources.search") as upstream:
            response = self.client.get(self.URL, {"q": "o"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, [])
        upstream.assert_not_called()

    def test_a_search_is_fetched_once_and_then_cached(self):
        with mock.patch(
            "core.views.food_sources.search", return_value=self.SAMPLE
        ) as upstream:
            first = self.client.get(self.URL, {"q": "oats"})
            second = self.client.get(self.URL, {"q": "  OATS  "})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.data, second.data)
        # Case and spacing fold, so this is one term and not three.
        upstream.assert_called_once()
        self.assertEqual(FoodSearchCache.objects.count(), 1)

    def test_a_stale_answer_beats_no_answer(self):
        """Reference nutrition does not go off. A week-old figure for oats is
        still the figure for oats, and is worth more than an error."""
        with mock.patch("core.views.food_sources.search", return_value=self.SAMPLE):
            self.client.get(self.URL, {"q": "oats"})

        entry = FoodSearchCache.objects.get(term="oats")
        entry.fetched_at = timezone.now() - FoodSearchCache.LIFETIME - timedelta(days=1)
        entry.save(update_fields=["fetched_at"])

        with mock.patch(
            "core.views.food_sources.search",
            side_effect=food_sources.FoodSourceUnavailable("down"),
        ):
            response = self.client.get(self.URL, {"q": "oats"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data[0]["name"], "Oats")

    def test_upstream_down_with_nothing_cached_is_503(self):
        """Not 500. The request was fine; the answer is elsewhere and away."""
        with mock.patch(
            "core.views.food_sources.search",
            side_effect=food_sources.FoodSourceUnavailable("down"),
        ):
            response = self.client.get(self.URL, {"q": "oats"})
        self.assertEqual(response.status_code, 503)

    def test_signing_in_is_required(self):
        anonymous = APIClient()
        response = anonymous.get(self.URL, {"q": "oats"})
        self.assertIn(response.status_code, (401, 403))


class PersonalizationTests(RepbaseAPITestMixin, APITestCase):
    """The five answers the first-run flow asks for, kept on the account.

    They used to go to UserDefaults, which meant they belonged to a phone
    rather than to a person.
    """

    URL = "/api/v1/me/personalization/"

    def setUp(self):
        _, _, token = self.create_account("personal_probe")
        self.authenticate(token)

    def test_an_account_that_has_never_answered_reads_defaults(self):
        """Not a 404. An account that has not finished the flow has answers
        anyway -- the ones the flow starts on -- and a screen should not have
        to tell the difference."""
        response = self.client.get(self.URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["training_types"], [])
        self.assertEqual(response.data["weekly_target"], 3)

    def test_answers_are_saved_and_read_back(self):
        saved = self.client.patch(
            self.URL,
            {
                "training_types": ["Running", "Cycling"],
                "weekly_target": 5,
                "emphasis": "Nutrition",
            },
            format="json",
        )
        self.assertEqual(saved.status_code, 200)

        read = self.client.get(self.URL)
        self.assertEqual(read.data["training_types"], ["Running", "Cycling"])
        self.assertEqual(read.data["weekly_target"], 5)
        self.assertEqual(read.data["emphasis"], "Nutrition")

    def test_a_partial_update_leaves_the_rest_alone(self):
        self.client.patch(
            self.URL,
            {"training_types": ["Swimming"], "weekly_target": 6},
            format="json",
        )
        self.client.patch(self.URL, {"weekly_target": 2}, format="json")

        read = self.client.get(self.URL)
        self.assertEqual(read.data["weekly_target"], 2)
        self.assertEqual(read.data["training_types"], ["Swimming"])

    def test_a_weekly_target_outside_a_week_is_refused(self):
        for value in (8, 100):
            response = self.client.patch(
                self.URL, {"weekly_target": value}, format="json"
            )
            self.assertEqual(response.status_code, 400, value)

    def test_a_choice_the_app_no_longer_offers_still_reads(self):
        """Retiring an option from the app should not break the accounts that
        chose it. The list is checked for shape, not for membership."""
        self.client.patch(
            self.URL, {"training_types": ["Something Retired"]}, format="json"
        )
        read = self.client.get(self.URL)
        self.assertEqual(read.data["training_types"], ["Something Retired"])

    def test_answers_belong_to_the_account_that_gave_them(self):
        self.client.patch(self.URL, {"weekly_target": 7}, format="json")

        _, _, other = self.create_account("personal_other")
        self.authenticate(other)
        response = self.client.get(self.URL)
        self.assertEqual(response.data["weekly_target"], 3)

    def test_signing_in_is_required(self):
        anonymous = APIClient()
        self.assertIn(anonymous.get(self.URL).status_code, (401, 403))


class WeeklyGoalTests(RepbaseAPITestMixin, APITestCase):
    """Where the number on the weekly goal card comes from.

    It used to be a count of what was already scheduled, which is a goal that
    is met the moment the week is planned.
    """

    URL = "/api/v1/sessions/training-stats/"

    def setUp(self):
        self.user, self.profile, token = self.create_account("goal_probe")
        self.authenticate(token)

    def _stats(self):
        response = self.client.get(self.URL)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def _schedule(self, count):
        template = WorkoutTemplate.objects.create(
            owner=self.profile, name="Session", workout_type="lifting"
        )
        monday = timezone.localdate() - timedelta(days=timezone.localdate().weekday())
        for offset in range(count):
            WorkoutSchedule.objects.create(
                owner=self.profile,
                workout=template,
                scheduled_date=monday + timedelta(days=offset),
            )

    def test_the_chosen_target_is_the_goal(self):
        self.client.patch(
            "/api/v1/me/personalization/", {"weekly_target": 5}, format="json"
        )
        self.assertEqual(self._stats()["weekly_goal"], 5)

    def test_the_target_wins_over_what_is_scheduled(self):
        """The whole point. Planning two sessions against a target of five is
        three short, and the card should say so rather than call it done."""
        self._schedule(2)
        self.client.patch(
            "/api/v1/me/personalization/", {"weekly_target": 5}, format="json"
        )
        self.assertEqual(self._stats()["weekly_goal"], 5)

    def test_a_target_of_none_is_respected(self):
        """Nought is an answer -- somebody who is not aiming at a count -- and
        must not fall through to the old behaviour."""
        self._schedule(3)
        self.client.patch(
            "/api/v1/me/personalization/", {"weekly_target": 0}, format="json"
        )
        self.assertEqual(self._stats()["weekly_goal"], 0)

    def test_an_account_that_never_answered_keeps_the_old_behaviour(self):
        """No dashboard changes under somebody because of a question they were
        never asked."""
        self._schedule(3)
        self.assertEqual(self._stats()["weekly_goal"], 3)

    def test_one_accounts_target_is_not_anothers(self):
        self.client.patch(
            "/api/v1/me/personalization/", {"weekly_target": 7}, format="json"
        )
        _, _, other = self.create_account("goal_other")
        self.authenticate(other)
        self.assertEqual(self._stats()["weekly_goal"], 0)


class RotationSwitchingTests(RepbaseAPITestMixin, APITestCase):
    """One rotation at a time, chosen deliberately, writing the calendar."""

    CYCLES = "/api/v1/cycles/"

    def setUp(self):
        self.user, self.profile, self.token = self.create_account("rotator")
        self.authenticate(self.token)
        self.push = self.workout("Push Day")
        self.pull = self.workout("Pull Day")
        self.legs = self.workout("Leg Day")

    def workout(self, name):
        response = self.client.post(
            "/api/v1/workouts/", {"name": name, "workout_type": "lifting"},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        return response.data["id"]

    def rotation(self, name, workout_ids, **extra):
        body = {
            "name": name,
            "length": len(workout_ids),
            "anchor_date": str(timezone.now().date()),
            "slots": [
                {"position": index + 1, "workout": workout_id}
                for index, workout_id in enumerate(workout_ids)
            ],
        }
        body.update(extra)
        return self.client.post(self.CYCLES, body, format="json")

    # ------------------------------------------------- saving writes the days

    def test_saving_a_rotation_fills_the_calendar(self):
        # The whole complaint: a rotation saved and the calendar stayed empty,
        # because only plan-ahead ever wrote rows.
        response = self.rotation("PPL", [self.push, self.pull, self.legs])
        self.assertEqual(response.status_code, 201, response.data)

        today = timezone.now().date()
        rows = WorkoutSchedule.objects.filter(owner=self.profile)
        self.assertGreater(rows.count(), 0, "saving wrote no schedule rows")
        self.assertEqual(
            rows.filter(scheduled_date=today).first().workout_id,
            self.push,
            "day 1 of the rotation should land on today",
        )
        # Written for every day of the horizon, not just the first turn.
        self.assertGreaterEqual(rows.count(), 50)
        self.assertTrue(all(r.source_cycle_id for r in rows), "rows must name their cycle")

    # --------------------------------------------------- one at a time

    def test_a_second_rotation_takes_over_from_the_first(self):
        first = self.rotation("PPL", [self.push, self.pull, self.legs])
        second = self.rotation("Upper/Lower", [self.push, self.legs])
        self.assertEqual(second.status_code, 201, second.data)

        cycles = {c["id"]: c for c in [first.data, second.data]}
        self.assertEqual(len(cycles), 2)

        from core.models import WorkoutCycle
        self.assertFalse(
            WorkoutCycle.objects.get(id=first.data["id"]).is_active,
            "the first rotation should have been closed",
        )
        self.assertTrue(WorkoutCycle.objects.get(id=second.data["id"]).is_active)

        # And the first one's future days are gone, not left in next week.
        self.assertEqual(
            WorkoutSchedule.objects.filter(
                owner=self.profile, source_cycle_id=first.data["id"],
                scheduled_date__gte=timezone.now().date(),
            ).count(),
            0,
        )

    def test_switching_back_restarts_the_rotation_today(self):
        first = self.rotation("PPL", [self.push, self.pull, self.legs])
        self.rotation("Upper/Lower", [self.push, self.legs])

        response = self.client.post(f"{self.CYCLES}{first.data['id']}/activate/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["is_active"])

        today = timezone.now().date()
        from core.models import WorkoutCycle
        revived = WorkoutCycle.objects.get(id=first.data["id"])
        self.assertEqual(revived.anchor_date, today, "day 1 should land on today")
        self.assertIsNone(revived.effective_until)

        self.assertEqual(
            WorkoutSchedule.objects.filter(
                owner=self.profile, scheduled_date=today
            ).first().workout_id,
            self.push,
        )

    def test_only_one_rotation_is_ever_active(self):
        for index in range(3):
            self.rotation(f"Block {index}", [self.push, self.pull])
        from core.models import WorkoutCycle
        self.assertEqual(
            WorkoutCycle.objects.filter(
                owner=self.profile, effective_until__isnull=True
            ).count(),
            1,
        )

    def test_an_ended_rotation_is_still_listed_and_switchable(self):
        first = self.rotation("PPL", [self.push, self.pull, self.legs])
        self.rotation("Upper/Lower", [self.push, self.legs])

        # Hidden by default, which is why activate looks past the filter.
        default = self.client.get(self.CYCLES)
        ids = [c["id"] for c in default.data["results"]]
        self.assertNotIn(first.data["id"], ids)

        including = self.client.get(f"{self.CYCLES}?include_ended=true")
        ids = [c["id"] for c in including.data["results"]]
        self.assertIn(first.data["id"], ids)

    # -------------------------------------------- the weekly repeat clash

    def test_a_repeating_workout_is_refused_and_named(self):
        rule = self.client.post(
            "/api/v1/recurrences/",
            {"workout": self.push, "weekday": 0},
            format="json",
        )
        self.assertEqual(rule.status_code, 201, rule.data)

        response = self.rotation("PPL", [self.push, self.pull])
        self.assertEqual(response.status_code, 400)
        # Named, so the app can offer to stop exactly these rather than
        # sending somebody off to find them.
        self.assertIn("conflicting_workouts", response.data)
        self.assertEqual(response.data["conflicting_workouts"], ["Push Day"])

    def test_agreeing_to_stop_the_repeat_saves_the_rotation(self):
        self.client.post(
            "/api/v1/recurrences/", {"workout": self.push, "weekday": 0},
            format="json",
        )

        response = self.rotation(
            "PPL", [self.push, self.pull], stop_conflicting_repeats=True
        )
        self.assertEqual(response.status_code, 201, response.data)

        from core.models import WorkoutRecurrence
        self.assertEqual(
            WorkoutRecurrence.objects.filter(
                owner=self.profile, workout_id=self.push,
                effective_until__isnull=True,
            ).count(),
            0,
            "the weekly repeat should have been closed",
        )
        # Closed, not deleted: weeks already planned keep resolving.
        self.assertEqual(
            WorkoutRecurrence.objects.filter(owner=self.profile).count(), 1
        )

    def test_a_refused_rotation_leaves_the_repeat_alone(self):
        # The stop happens inside the save's transaction, so a rotation that
        # fails must not have ended somebody's weekly repeat on the way.
        self.client.post(
            "/api/v1/recurrences/", {"workout": self.push, "weekday": 0},
            format="json",
        )
        response = self.rotation("PPL", [self.push, self.pull])
        self.assertEqual(response.status_code, 400)

        from core.models import WorkoutRecurrence
        self.assertEqual(
            WorkoutRecurrence.objects.filter(
                owner=self.profile, effective_until__isnull=True
            ).count(),
            1,
            "a refused rotation must not have stopped the repeat",
        )


class RotationHandoverTests(RepbaseAPITestMixin, APITestCase):
    """Switching rotations on a date you pick, not the moment you tap."""

    CYCLES = "/api/v1/cycles/"

    def setUp(self):
        self.user, self.profile, self.token = self.create_account("switcher")
        self.authenticate(self.token)
        self.push = self.workout("Push Day")
        self.pull = self.workout("Pull Day")
        self.squat = self.workout("Squat Day")

    def workout(self, name):
        r = self.client.post(
            "/api/v1/workouts/", {"name": name, "workout_type": "lifting"},
            format="json",
        )
        self.assertEqual(r.status_code, 201, r.data)
        return r.data["id"]

    def rotation(self, name, ids):
        r = self.client.post(
            self.CYCLES,
            {
                "name": name,
                "length": len(ids),
                "anchor_date": str(timezone.now().date()),
                "slots": [
                    {"position": i + 1, "workout": w} for i, w in enumerate(ids)
                ],
            },
            format="json",
        )
        self.assertEqual(r.status_code, 201, r.data)
        return r.data

    def scheduled_on(self, day):
        return set(
            WorkoutSchedule.objects.filter(
                owner=self.profile, scheduled_date=day
            ).values_list("workout_id", flat=True)
        )

    def test_the_old_rotation_runs_until_the_new_one_starts(self):
        today = timezone.now().date()
        handover = today + timedelta(days=14)

        old = self.rotation("Old block", [self.push, self.pull])
        new = self.rotation("New block", [self.squat])
        # Creating the second one took over immediately, so put the first back
        # and then schedule the real handover from it.
        self.client.post(f"{self.CYCLES}{old['id']}/activate/", {}, format="json")

        response = self.client.post(
            f"{self.CYCLES}{new['id']}/activate/",
            {"start_on": str(handover)},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)

        # The whole point: the fortnight in between still belongs to the old
        # rotation rather than being cleared the moment the switch was made.
        self.assertEqual(self.scheduled_on(today), {self.push})
        self.assertEqual(self.scheduled_on(today + timedelta(days=1)), {self.pull})
        # Day 13 of a two-day rotation anchored on today is slot 2.
        self.assertEqual(self.scheduled_on(handover - timedelta(days=1)), {self.pull})

        # And from the handover on, it is the new one, on its own day 1.
        self.assertEqual(self.scheduled_on(handover), {self.squat})
        self.assertEqual(self.scheduled_on(handover + timedelta(days=1)), {self.squat})

    def test_the_old_rotation_is_closed_at_the_handover_not_today(self):
        today = timezone.now().date()
        handover = today + timedelta(days=10)

        old = self.rotation("Old block", [self.push, self.pull])
        new = self.rotation("New block", [self.squat])
        self.client.post(f"{self.CYCLES}{old['id']}/activate/", {}, format="json")
        self.client.post(
            f"{self.CYCLES}{new['id']}/activate/",
            {"start_on": str(handover)}, format="json",
        )

        from core.models import WorkoutCycle
        self.assertEqual(
            WorkoutCycle.objects.get(id=old["id"]).effective_until, handover
        )
        revived = WorkoutCycle.objects.get(id=new["id"])
        self.assertEqual(revived.effective_from, handover)
        self.assertEqual(revived.anchor_date, handover)
        self.assertIsNone(revived.effective_until)

    def test_no_start_date_means_today(self):
        old = self.rotation("Old block", [self.push, self.pull])
        new = self.rotation("New block", [self.squat])
        self.client.post(f"{self.CYCLES}{old['id']}/activate/", {}, format="json")

        response = self.client.post(f"{self.CYCLES}{new['id']}/activate/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.scheduled_on(timezone.now().date()), {self.squat})

    def test_a_start_date_in_the_past_is_refused(self):
        new = self.rotation("New block", [self.squat])
        response = self.client.post(
            f"{self.CYCLES}{new['id']}/activate/",
            {"start_on": str(timezone.now().date() - timedelta(days=1))},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("start_on", response.data)

    def test_days_already_trained_are_left_alone(self):
        # A handover must never reach backwards into what has been done.
        today = timezone.now().date()
        old = self.rotation("Old block", [self.push, self.pull])
        past = WorkoutSchedule.objects.create(
            owner=self.profile,
            workout_id=self.push,
            scheduled_date=today - timedelta(days=3),
        )

        new = self.rotation("New block", [self.squat])
        self.client.post(
            f"{self.CYCLES}{new['id']}/activate/",
            {"start_on": str(today + timedelta(days=5))}, format="json",
        )
        self.assertTrue(WorkoutSchedule.objects.filter(id=past.id).exists())


class ScheduleClearingTests(RepbaseAPITestMixin, APITestCase):
    """A rotation owns the calendar it starts on, and clearing it by hand."""

    CYCLES = "/api/v1/cycles/"
    CLEAR = "/api/v1/schedules/clear/"

    def setUp(self):
        self.user, self.profile, self.token = self.create_account("clearer")
        self.authenticate(self.token)
        self.push = self.workout("Push Day")
        self.pull = self.workout("Pull Day")

    def workout(self, name):
        r = self.client.post(
            "/api/v1/workouts/", {"name": name, "workout_type": "lifting"},
            format="json",
        )
        self.assertEqual(r.status_code, 201, r.data)
        return r.data["id"]

    def booked(self, workout_id, day):
        return WorkoutSchedule.objects.create(
            owner=self.profile, workout_id=workout_id, scheduled_date=day
        )

    def rotation(self, name, ids):
        r = self.client.post(
            self.CYCLES,
            {
                "name": name, "length": len(ids),
                "anchor_date": str(timezone.now().date()),
                "slots": [{"position": i + 1, "workout": w} for i, w in enumerate(ids)],
            },
            format="json",
        )
        self.assertEqual(r.status_code, 201, r.data)
        return r.data

    # ------------------------------------- a rotation starts on a clean slate

    def test_starting_a_rotation_clears_what_was_booked(self):
        today = timezone.now().date()
        # Booked by hand, which the old behaviour left in place: rotation days
        # landed among them and the week read as two plans at once.
        by_hand = self.booked(self.pull, today + timedelta(days=2))

        self.rotation("PPL", [self.push])
        self.assertFalse(WorkoutSchedule.objects.filter(id=by_hand.id).exists())

        # And the rotation filled the day instead.
        self.assertEqual(
            WorkoutSchedule.objects.filter(
                owner=self.profile, scheduled_date=today + timedelta(days=2)
            ).first().workout_id,
            self.push,
        )

    def test_clearing_never_reaches_past_the_starting_week(self):
        # A first rotation owns the week it starts, so it does clear days
        # earlier in that week -- but never a week that is already finished.
        today = timezone.now().date()
        monday = today - timedelta(days=today.weekday())
        last_week = self.booked(self.pull, monday - timedelta(days=2))
        self.rotation("PPL", [self.push])
        self.assertTrue(WorkoutSchedule.objects.filter(id=last_week.id).exists())

    def test_switching_clears_only_from_the_handover(self):
        today = timezone.now().date()
        handover = today + timedelta(days=10)

        first = self.rotation("First", [self.push])
        second = self.rotation("Second", [self.pull])
        self.client.post(f"{self.CYCLES}{first['id']}/activate/", {}, format="json")

        # A day booked by hand between now and the handover survives it.
        keeper = self.booked(self.pull, today + timedelta(days=3))
        self.client.post(
            f"{self.CYCLES}{second['id']}/activate/",
            {"start_on": str(handover)}, format="json",
        )
        self.assertTrue(
            WorkoutSchedule.objects.filter(id=keeper.id).exists(),
            "days before the handover belong to the rotation still running",
        )
        # From the handover on, the new rotation owns it.
        self.assertEqual(
            WorkoutSchedule.objects.filter(
                owner=self.profile, scheduled_date=handover
            ).first().workout_id,
            self.pull,
        )

    # ------------------------------------------------- clearing by hand

    def test_clearing_empties_the_whole_schedule(self):
        today = timezone.now().date()
        self.booked(self.push, today - timedelta(days=2))
        self.booked(self.push, today)
        self.booked(self.pull, today + timedelta(days=5))

        response = self.client.post(self.CLEAR, {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        # Past days go too. Clearing by hand is "start from scratch", and a
        # calendar still showing last month's abandoned plan is not scratch.
        self.assertEqual(response.data["cleared"], 3)
        self.assertEqual(
            WorkoutSchedule.objects.filter(owner=self.profile).count(), 0
        )

    def test_clearing_keeps_workouts_already_logged(self):
        # The point of the feature: throwing away the plan must never throw
        # away the training. Sessions carry no link to the schedule row that
        # planned them, and this is the test that holds those two apart.
        today = timezone.now().date()
        self.booked(self.push, today - timedelta(days=3))
        self.booked(self.pull, today + timedelta(days=1))
        logged = WorkoutSession.objects.create(
            repbase_user=self.profile,
            workout_id=self.push,
            status=WorkoutSession.Status.COMPLETED,
        )

        response = self.client.post(self.CLEAR, {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            WorkoutSchedule.objects.filter(owner=self.profile).count(), 0
        )

        logged.refresh_from_db()
        self.assertEqual(logged.status, WorkoutSession.Status.COMPLETED)
        self.assertEqual(logged.workout_id, self.push)
        self.assertEqual(
            WorkoutSession.objects.filter(repbase_user=self.profile).count(), 1
        )

    def test_clearing_is_refused_while_a_rotation_is_running(self):
        # A rotation would write the days straight back, so the honest answer
        # is to say what actually needs changing.
        self.rotation("PPL", [self.push])
        response = self.client.post(self.CLEAR, {}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("schedule", response.data)
        self.assertGreater(
            WorkoutSchedule.objects.filter(owner=self.profile).count(), 0
        )

    def test_clearing_an_empty_schedule_is_fine(self):
        response = self.client.post(self.CLEAR, {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["cleared"], 0)

    def test_clearing_is_only_ever_your_own(self):
        other_user, other_profile, other_token = self.create_account("bystander")
        theirs = WorkoutSchedule.objects.create(
            owner=other_profile,
            workout_id=self.push,
            scheduled_date=timezone.now().date() + timedelta(days=1),
        )
        self.client.post(self.CLEAR, {}, format="json")
        self.assertTrue(WorkoutSchedule.objects.filter(id=theirs.id).exists())


class RotationStartingWeekTests(RepbaseAPITestMixin, APITestCase):
    """Which days a rotation clears depends on what it is replacing."""

    CYCLES = "/api/v1/cycles/"

    def setUp(self):
        self.user, self.profile, self.token = self.create_account("weeker")
        self.authenticate(self.token)
        self.push = self.workout("Push Day")
        self.pull = self.workout("Pull Day")

    def workout(self, name):
        r = self.client.post(
            "/api/v1/workouts/", {"name": name, "workout_type": "lifting"},
            format="json",
        )
        self.assertEqual(r.status_code, 201, r.data)
        return r.data["id"]

    def rotation(self, name, ids):
        r = self.client.post(
            self.CYCLES,
            {
                "name": name, "length": len(ids),
                "anchor_date": str(timezone.now().date()),
                "slots": [{"position": i + 1, "workout": w} for i, w in enumerate(ids)],
            },
            format="json",
        )
        self.assertEqual(r.status_code, 201, r.data)
        return r.data

    def test_a_first_rotation_empties_the_week_it_starts(self):
        # A hand-made plan filling Monday to Thursday, with the rotation
        # starting today: the week must not read as two plans at once.
        today = timezone.now().date()
        monday = today - timedelta(days=today.weekday())
        earlier = [
            WorkoutSchedule.objects.create(
                owner=self.profile, workout_id=self.pull,
                scheduled_date=monday + timedelta(days=offset),
            )
            for offset in range(today.weekday())
        ]

        self.rotation("PPL", [self.push])

        for row in earlier:
            self.assertFalse(
                WorkoutSchedule.objects.filter(id=row.id).exists(),
                f"{row.scheduled_date} should have been cleared",
            )
        # Nothing from the old plan is left anywhere in the week.
        week = WorkoutSchedule.objects.filter(
            owner=self.profile,
            scheduled_date__gte=monday,
            scheduled_date__lt=monday + timedelta(days=7),
        )
        self.assertTrue(all(r.source_cycle_id for r in week))

    def test_it_does_not_reach_into_the_week_before(self):
        today = timezone.now().date()
        monday = today - timedelta(days=today.weekday())
        last_week = WorkoutSchedule.objects.create(
            owner=self.profile, workout_id=self.pull,
            scheduled_date=monday - timedelta(days=1),
        )
        self.rotation("PPL", [self.push])
        self.assertTrue(WorkoutSchedule.objects.filter(id=last_week.id).exists())

    def test_a_handover_keeps_the_outgoing_rotation_days(self):
        # Rotation to rotation is the other case: the one on the way out keeps
        # every day up to the date, including earlier in the handover week.
        today = timezone.now().date()
        first = self.rotation("First", [self.push])
        second = self.rotation("Second", [self.pull])
        self.client.post(f"{self.CYCLES}{first['id']}/activate/", {}, format="json")

        handover = today + timedelta(days=9)
        monday_of_handover = handover - timedelta(days=handover.weekday())
        # A day the outgoing rotation owns, earlier in the handover's week.
        if monday_of_handover < handover:
            before = WorkoutSchedule.objects.filter(
                owner=self.profile, scheduled_date=monday_of_handover
            ).first()
            self.assertIsNotNone(before, "the first rotation should have filled it")

            self.client.post(
                f"{self.CYCLES}{second['id']}/activate/",
                {"start_on": str(handover)}, format="json",
            )
            self.assertTrue(
                WorkoutSchedule.objects.filter(id=before.id).exists(),
                "a handover must not clear back to the start of the week",
            )


class RotationShiftClearsPlannerTests(RepbaseAPITestMixin, APITestCase):
    """A day a rotation drops takes its planner task with it."""

    CYCLES = "/api/v1/cycles/"
    SYNC = "/api/v1/schedules/sync-planner/"

    def setUp(self):
        self.user, self.profile, self.token = self.create_account("shifter")
        self.authenticate(self.token)
        self.push = self.workout("Push Day")
        self.pull = self.workout("Pull Day")

    def workout(self, name):
        r = self.client.post(
            "/api/v1/workouts/",
            {"name": name, "workout_type": "lifting"},
            format="json",
        )
        self.assertEqual(r.status_code, 201, r.data)
        return r.data["id"]

    def test_shifting_a_rotation_removes_the_tasks_it_orphans(self):
        """The bug: six overdue workouts for somebody who missed two.

        Shifting a rotation deletes the future schedule rows it no longer
        implies, but PlannerEntry has no link back to the row it came from,
        so the tasks used to survive their own days and pile up past due.
        """
        today = timezone.localdate()
        created = self.client.post(
            self.CYCLES,
            {
                "name": "Rotation",
                "length": 2,
                "anchor_date": today.isoformat(),
                "slots": [
                    {"position": 1, "workout": self.push},
                    {"position": 2, "workout": self.pull},
                ],
            },
            format="json",
        )
        self.assertEqual(created.status_code, 201, created.data)
        cycle_id = created.data["id"]

        horizon = today + timedelta(days=13)
        self.client.post(
            self.SYNC,
            {"start": today.isoformat(), "end": horizon.isoformat()},
            format="json",
        )
        before = PlannerEntry.objects.filter(
            owner=self.profile, workout__isnull=False
        ).count()
        self.assertGreater(before, 0, "the rotation should have made tasks")

        shifted = self.client.post(
            f"{self.CYCLES}{cycle_id}/shift/", {"days": 1}, format="json"
        )
        self.assertEqual(shifted.status_code, 200, shifted.data)

        # Every remaining task must still have a scheduled day behind it.
        stranded = [
            entry
            for entry in PlannerEntry.objects.filter(
                owner=self.profile,
                workout__isnull=False,
                completed_at__isnull=True,
                scheduled_date__gte=today,
            )
            if not WorkoutSchedule.objects.filter(
                owner=self.profile,
                workout_id=entry.workout_id,
                scheduled_date=entry.scheduled_date,
            ).exists()
        ]
        self.assertEqual(
            stranded,
            [],
            f"tasks left with no scheduled day behind them: "
            f"{[(e.scheduled_date, e.title) for e in stranded]}",
        )

    def test_a_finished_day_survives_the_shift(self):
        """A ticked-off task is a record, not a plan that can be withdrawn."""
        today = timezone.localdate()
        created = self.client.post(
            self.CYCLES,
            {
                "name": "Rotation",
                "length": 2,
                "anchor_date": today.isoformat(),
                "slots": [
                    {"position": 1, "workout": self.push},
                    {"position": 2, "workout": self.pull},
                ],
            },
            format="json",
        )
        cycle_id = created.data["id"]
        horizon = today + timedelta(days=13)
        self.client.post(
            self.SYNC,
            {"start": today.isoformat(), "end": horizon.isoformat()},
            format="json",
        )

        done = PlannerEntry.objects.filter(
            owner=self.profile, workout__isnull=False, scheduled_date__gt=today
        ).first()
        self.assertIsNotNone(done)
        done.completed_at = timezone.now()
        done.save(update_fields=["completed_at"])

        self.client.post(f"{self.CYCLES}{cycle_id}/shift/", {"days": 1}, format="json")

        self.assertTrue(
            PlannerEntry.objects.filter(id=done.id).exists(),
            "a completed task must not be swept up with the rotation",
        )


class ContractMatchesResponsesTests(RepbaseAPITestMixin, APITestCase):
    """Every collection response must match the schema that describes it.

    Guards against the pair of bugs that prompted it, both of which were
    invisible from either side alone: a saved-meal result documented as a
    string that returned a number, and a created_by documented as an integer
    that returns null on anything the app shipped with. Each broke the
    generated client on a live screen, and neither broke a test, because
    every test until now checked what the server sends without checking it
    against what the server promises to send.
    """

    def setUp(self):
        self.user, self.profile, self.token = self.create_account("contract")
        self.authenticate(self.token)

    def _fetch(self, path):
        response = self.client.get(path)
        # A route that needs a query parameter answers 400 to a bare GET, and
        # a 400 body says nothing about whether the 200 body would match.
        if response.status_code != 200:
            self.skipped.append(f"{path} ({response.status_code})")
            return None
        return response.data

    def test_collection_responses_match_the_schema(self):
        from drf_spectacular.generators import SchemaGenerator

        from .contract_check import check, collection_paths, to_json_schema

        # Shapes chosen to be awkward rather than representative. A fixture
        # that fills in every optional field validates against a contract
        # that forbids null, which is exactly the bug that shipped.
        Gym.objects.create(name="Seeded Gym", city="Nowhere", created_by=None)
        Exercise.objects.create(name="Seeded Exercise", created_by=None)
        self.client.post(
            "/api/v1/workouts/",
            {"name": "Bare Workout", "workout_type": "lifting"},
            format="json",
        )

        spec = SchemaGenerator().get_schema(request=None, public=True)
        self.skipped = []

        paths = collection_paths(to_json_schema(spec))
        problems = check(spec, self._fetch, paths=paths)

        reached = len(paths) - len(self.skipped)
        self.assertGreater(
            reached,
            20,
            "too few endpoints answered to call this a sweep; "
            f"skipped {self.skipped}",
        )
        self.assertEqual(
            problems,
            [],
            "responses disagree with the contract:\n  " + "\n  ".join(problems),
        )

    def test_no_primary_key_field_documents_itself_as_a_string(self):
        """A pk on a plain Serializer is described as a string and sent as a number.

        The response sweep above cannot catch this one: it reads GET
        collections, and the two serializers it happened to on were both
        results of a POST. So the rule is checked at its source instead.

        On a ModelSerializer, spectacular resolves PrimaryKeyRelatedField
        against the model and correctly says integer. On a plain Serializer
        there is no model to resolve against and no queryset on a read-only
        field, so it falls back to string -- and the generated client then
        refuses to decode the number the view actually returns. That is what
        broke saving a meal from somebody else's post.
        """
        from rest_framework import serializers as drf

        from . import serializers as module

        offenders = []
        for name in dir(module):
            candidate = getattr(module, name)
            if not isinstance(candidate, type):
                continue
            if not issubclass(candidate, drf.Serializer):
                continue
            if issubclass(candidate, drf.ModelSerializer):
                continue
            for field_name, field in getattr(candidate, "_declared_fields", {}).items():
                if isinstance(field, drf.PrimaryKeyRelatedField) and field.queryset is None:
                    offenders.append(f"{name}.{field_name}")

        self.assertEqual(
            offenders,
            [],
            "these document as a string and return a number; use IntegerField:\n  "
            + "\n  ".join(offenders),
        )


class SavingTwiceSavesOnceTests(RepbaseAPITestMixin, APITestCase):
    """Tapping Save again hands back the copy already made.

    Three taps on one meal used to leave "Meal 1 (from @them)", the same
    again with a 2, and again with a 3, with nothing to say which was worth
    keeping. The numbering was there to stop a name collision, so it could
    not tell a second meal from the same meal twice.
    """

    def setUp(self):
        self.user, self.profile, self.token = self.create_account("saver")
        self.author, self.author_profile, self.author_token = self.create_account(
            "poster"
        )
        self.authenticate(self.token)

    def _meal_post(self):
        post = Post.objects.create(
            author=self.author_profile, kind=Post.Kind.MEAL, caption="lunch"
        )
        meal = PostMeal.objects.create(post=post, name="Meal 1", date=timezone.localdate())
        PostMealEntry.objects.create(
            post_meal=meal,
            name="Chicken Bowl",
            servings=1,
            calories=800,
            protein_grams=50,
            carbohydrate_grams=120,
            fat_grams=30,
            position=1,
        )
        return post

    def _workout_post(self):
        post = Post.objects.create(
            author=self.author_profile, kind=Post.Kind.WORKOUT, caption="pull"
        )
        workout = PostWorkout.objects.create(
            post=post, title="Pull day", performed_at=timezone.now()
        )
        PostWorkoutExercise.objects.create(
            post_workout=workout, name="Lat pulldown", order=1, set_count=3
        )
        return post

    def test_saving_a_meal_twice_leaves_one(self):
        post = self._meal_post()
        url = f"/api/v1/social/posts/{post.id}/save-meal/"

        first = self.client.post(url)
        self.assertEqual(first.status_code, 201, first.data)
        self.assertFalse(first.data["already_saved"])

        second = self.client.post(url)
        # 200, not 201: nothing was created the second time.
        self.assertEqual(second.status_code, 200, second.data)
        self.assertTrue(second.data["already_saved"])
        self.assertEqual(second.data["meal"], first.data["meal"])
        self.assertEqual(second.data["name"], first.data["name"])

        self.assertEqual(
            SavedFoodMeal.objects.filter(owner=self.profile).count(),
            1,
            "a second tap must not leave a second meal",
        )

    def test_saving_a_workout_twice_leaves_one(self):
        post = self._workout_post()
        url = f"/api/v1/social/posts/{post.id}/save-workout/"

        first = self.client.post(url)
        self.assertEqual(first.status_code, 201, first.data)
        self.assertFalse(first.data["already_saved"])

        second = self.client.post(url)
        self.assertEqual(second.status_code, 200, second.data)
        self.assertTrue(second.data["already_saved"])
        self.assertEqual(second.data["workout"], first.data["workout"])

        self.assertEqual(
            WorkoutTemplate.objects.filter(owner=self.profile).count(),
            1,
            "a second tap must not leave a second workout",
        )

    def test_ten_taps_still_leave_one(self):
        """The report was a flooded folder, so the guard is against flooding."""
        post = self._meal_post()
        url = f"/api/v1/social/posts/{post.id}/save-meal/"
        for _ in range(10):
            self.client.post(url)
        self.assertEqual(SavedFoodMeal.objects.filter(owner=self.profile).count(), 1)

    def test_two_different_posts_are_two_meals(self):
        """Deduplication must not become refusing to save a second meal."""
        first_post = self._meal_post()
        second_post = self._meal_post()

        self.client.post(f"/api/v1/social/posts/{first_post.id}/save-meal/")
        self.client.post(f"/api/v1/social/posts/{second_post.id}/save-meal/")

        self.assertEqual(
            SavedFoodMeal.objects.filter(owner=self.profile).count(),
            2,
            "two posts are two meals, even when they hold the same food",
        )

    def test_another_account_saving_the_same_post_is_unaffected(self):
        """The copy is per person: mine existing is not you having one."""
        post = self._meal_post()
        url = f"/api/v1/social/posts/{post.id}/save-meal/"
        self.client.post(url)

        other_user, other_profile, other_token = self.create_account("second")
        self.authenticate(other_token)
        theirs = self.client.post(url)

        self.assertEqual(theirs.status_code, 201, theirs.data)
        self.assertFalse(theirs.data["already_saved"])
        self.assertEqual(SavedFoodMeal.objects.filter(owner=other_profile).count(), 1)
        self.assertEqual(SavedFoodMeal.objects.filter(owner=self.profile).count(), 1)

    def test_the_feed_says_whether_the_reader_already_saved_it(self):
        """The Save button reads this to know it has nothing left to do.

        Session state is not enough: a post saved last week must still show
        as saved on the next launch, so the answer has to come from the
        server rather than from what this run happens to remember.
        """
        post = self._meal_post()

        before = self.client.get("/api/v1/social/posts/")
        mine = [p for p in before.data["results"] if p["id"] == post.id][0]
        self.assertFalse(mine["viewer_saved"])

        self.client.post(f"/api/v1/social/posts/{post.id}/save-meal/")

        after = self.client.get("/api/v1/social/posts/")
        mine = [p for p in after.data["results"] if p["id"] == post.id][0]
        self.assertTrue(mine["viewer_saved"])

    def test_one_reader_saving_does_not_mark_it_saved_for_another(self):
        post = self._meal_post()
        self.client.post(f"/api/v1/social/posts/{post.id}/save-meal/")

        _, _, other_token = self.create_account("onlooker")
        self.authenticate(other_token)

        theirs = self.client.get("/api/v1/social/posts/")
        mine = [p for p in theirs.data["results"] if p["id"] == post.id][0]
        self.assertFalse(mine["viewer_saved"], "saved is per reader, not per post")

    def test_a_deleted_post_leaves_the_copy_alone(self):
        """SET_NULL, not CASCADE: the copy is the readers own once made."""
        post = self._meal_post()
        self.client.post(f"/api/v1/social/posts/{post.id}/save-meal/")
        saved = SavedFoodMeal.objects.get(owner=self.profile)

        post.delete()

        saved.refresh_from_db()
        self.assertIsNone(saved.source_post_id)
        self.assertEqual(saved.ingredients.count(), 1, "the food must survive too")


class SearchingForPeopleTests(RepbaseAPITestMixin, APITestCase):
    """Finding somebody who has never posted.

    The app had a search box that filtered posts already downloaded, so the
    only people it could find were the ones already on screen. Somebody who
    had not posted was unreachable by search no matter how exactly their
    handle was typed, which is the opposite of what a search is for.
    """

    URL = "/api/v1/users/"

    def setUp(self):
        self.user, self.profile, self.token = self.create_account("seeker")
        # create_account gives first_name the title-cased username and
        # last_name "Tester", so these are "Aaron Tester" and "Bianca Tester".
        _, self.aaron, _ = self.create_account("aaron")
        _, self.bianca, _ = self.create_account("bianca")
        self.authenticate(self.token)

    def ids(self, response):
        return {row["id"] for row in response.data["results"]}

    def search(self, query):
        response = self.client.get(self.URL, {"search": query})
        self.assertEqual(response.status_code, 200, response.data)
        return self.ids(response)

    def test_finds_somebody_by_username(self):
        self.assertEqual(self.search("aaron"), {self.aaron.id})

    def test_finds_somebody_by_first_name(self):
        # Different case from the stored "Aaron", so this also pins the
        # matching as case-insensitive.
        self.assertEqual(self.search("AARON"), {self.aaron.id})

    def test_finds_somebody_by_full_name(self):
        """The whole name, which is two columns and one person."""
        self.assertEqual(self.search("aaron tester"), {self.aaron.id})

    def test_a_leading_at_sign_is_ignored(self):
        """Handles are written with one and stored without."""
        self.assertEqual(self.search("@aaron"), {self.aaron.id})

    def test_a_partial_match_still_finds_them(self):
        self.assertEqual(self.search("aar"), {self.aaron.id})

    def test_the_viewer_is_never_a_result(self):
        """Searching your own name finds the other people, not you."""
        self.assertNotIn(self.profile.id, self.search("tester"))
        self.assertEqual(self.search("tester"), {self.aaron.id, self.bianca.id})

    def test_no_search_is_everybody_else(self):
        response = self.client.get(self.URL)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.ids(response), {self.aaron.id, self.bianca.id})

    def test_somebody_the_viewer_blocked_is_not_found(self):
        Block.objects.create(blocker=self.profile, blocked=self.aaron)
        self.assertEqual(self.search("aaron"), set())

    def test_somebody_who_blocked_the_viewer_is_not_found(self):
        """A block hides its subject from the person it was aimed at."""
        Block.objects.create(blocker=self.aaron, blocked=self.profile)
        self.assertEqual(self.search("aaron"), set())

    def test_a_block_does_not_hide_everybody_else(self):
        Block.objects.create(blocker=self.profile, blocked=self.aaron)
        self.assertEqual(self.search("tester"), {self.bianca.id})

    def test_a_blocked_profile_can_still_be_opened_by_id(self):
        """Only the list is narrowed.

        The app can already navigate to a profile it holds an id for, and
        turning that into a 404 partway through a tap would be a worse
        answer than the page it draws today.
        """
        Block.objects.create(blocker=self.profile, blocked=self.aaron)
        response = self.client.get(f"{self.URL}{self.aaron.id}/")
        self.assertEqual(response.status_code, 200, response.data)

    def test_matching_nobody_is_an_empty_list_not_an_error(self):
        response = self.client.get(self.URL, {"search": "nobodyhasthisname"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["results"], [])

    def test_a_blank_search_is_the_same_as_none(self):
        """So clearing the box returns the list rather than nothing."""
        response = self.client.get(self.URL, {"search": "   "})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.ids(response), {self.aaron.id, self.bianca.id})


class SearchingForPostsTests(RepbaseAPITestMixin, APITestCase):
    """Searching posts, without it becoming a way around visibility.

    Search narrows what the reader could already see. The risk worth a test
    is the opposite: a filter written beside the visibility rules instead of
    after them would let somebody find a private post by guessing a word in
    it, and would do so quietly.
    """

    URL = "/api/v1/social/posts/"

    def setUp(self):
        self.user, self.profile, self.token = self.create_account("reader")
        _, self.author, _ = self.create_account("poster")
        self.authenticate(self.token)

    def post_with(self, caption, visibility=Post.Visibility.PUBLIC, author=None):
        return Post.objects.create(
            author=author or self.author,
            kind=Post.Kind.MEAL,
            caption=caption,
            visibility=visibility,
        )

    def ids(self, query):
        response = self.client.get(self.URL, query)
        self.assertEqual(response.status_code, 200, response.data)
        return {row["id"] for row in response.data["results"]}

    def test_finds_a_post_by_caption(self):
        wanted = self.post_with("leg day was brutal")
        self.post_with("rest day")
        self.assertEqual(self.ids({"search": "brutal"}), {wanted.id})

    def test_finds_a_post_by_author_username(self):
        wanted = self.post_with("anything")
        self.assertEqual(self.ids({"search": "poster"}), {wanted.id})

    def test_finds_a_post_by_the_meal_on_it(self):
        post = self.post_with("")
        PostMeal.objects.create(
            post=post, name="Chicken Bowl", date=timezone.localdate()
        )
        self.post_with("unrelated")
        self.assertEqual(self.ids({"search": "chicken"}), {post.id})

    def test_finds_a_post_by_the_workout_on_it(self):
        post = Post.objects.create(
            author=self.author, kind=Post.Kind.WORKOUT, caption=""
        )
        PostWorkout.objects.create(
            post=post, title="Pull Day", performed_at=timezone.now()
        )
        self.post_with("unrelated")
        self.assertEqual(self.ids({"search": "pull"}), {post.id})

    def test_a_private_post_is_not_findable_by_a_stranger(self):
        """The whole reason this test class exists."""
        self.post_with("secret leg day", visibility=Post.Visibility.PRIVATE)
        self.assertEqual(self.ids({"search": "secret"}), set())

    def test_a_followers_only_post_is_not_findable_before_following(self):
        self.post_with("just for my people", visibility=Post.Visibility.FOLLOWERS)
        self.assertEqual(self.ids({"search": "people"}), set())

    def test_a_followers_only_post_is_findable_once_following(self):
        wanted = self.post_with(
            "just for my people", visibility=Post.Visibility.FOLLOWERS
        )
        Follow.objects.create(follower=self.profile, following=self.author)
        self.assertEqual(self.ids({"search": "people"}), {wanted.id})

    def test_an_author_finds_their_own_private_post(self):
        """Visibility is not consulted against yourself."""
        mine = self.post_with(
            "secret leg day",
            visibility=Post.Visibility.PRIVATE,
            author=self.profile,
        )
        self.assertEqual(self.ids({"search": "secret"}), {mine.id})

    def test_a_blocked_authors_post_is_not_findable(self):
        self.post_with("leg day")
        Block.objects.create(blocker=self.profile, blocked=self.author)
        self.assertEqual(self.ids({"search": "leg"}), set())

    def test_a_hidden_post_is_not_findable(self):
        post = self.post_with("leg day")
        post.is_hidden = True
        post.save(update_fields=["is_hidden"])
        self.assertEqual(self.ids({"search": "leg"}), set())

    def test_search_combines_with_the_author_filter(self):
        _, other, _ = self.create_account("somebodyelse")
        mine = self.post_with("leg day")
        self.post_with("leg day", author=other)
        found = self.ids({"search": "leg", "author": self.author.id})
        self.assertEqual(found, {mine.id})

    def test_no_search_returns_the_list(self):
        first = self.post_with("one")
        second = self.post_with("two")
        self.assertEqual(self.ids({}), {first.id, second.id})

    def test_one_post_is_returned_once(self):
        """A match on two columns at once is still one row.

        The filter reaches author, workout and meal from the post. Written as
        joins rather than as conditions on the row, a post matching in two
        places would come back twice and the paginator would count it twice.
        """
        post = self.post_with("chicken")
        PostMeal.objects.create(
            post=post, name="Chicken Bowl", date=timezone.localdate()
        )
        response = self.client.get(self.URL, {"search": "chicken"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["count"], 1)

    def meal_post_holding(self, *foods, name="Meal 1"):
        """A meal posted under a name that says nothing about the food in it."""
        post = self.post_with("")
        meal = PostMeal.objects.create(
            post=post, name=name, date=timezone.localdate()
        )
        for position, food in enumerate(foods, start=1):
            PostMealEntry.objects.create(
                post_meal=meal,
                name=food,
                servings=1,
                calories=100,
                protein_grams=10,
                carbohydrate_grams=10,
                fat_grams=10,
                position=position,
            )
        return post

    def test_finds_a_meal_by_the_food_inside_it(self):
        """The name on the card is the one thing nobody would search for.

        Meals are posted as "Meal 1" and "Meal 4". Searching those finds a
        meal by its position in somebody's day, which is not a thing anybody
        wants; the food is what gets typed.
        """
        post = self.meal_post_holding("Chicken Bowl", "Rice")
        self.meal_post_holding("Oats", name="Meal 2")
        self.assertEqual(self.ids({"search": "chicken"}), {post.id})

    def test_finds_a_workout_by_an_exercise_inside_it(self):
        post = Post.objects.create(
            author=self.author, kind=Post.Kind.WORKOUT, caption=""
        )
        workout = PostWorkout.objects.create(
            post=post, title="Push Day", performed_at=timezone.now()
        )
        PostWorkoutExercise.objects.create(
            post_workout=workout, name="Incline Chest Press", order=1, set_count=3
        )
        self.post_with("unrelated")
        self.assertEqual(self.ids({"search": "incline"}), {post.id})

    def test_a_meal_with_several_matching_foods_is_returned_once(self):
        """The reason those two are subqueries and not joins.

        Three chickens in one meal is one post. Through a join it would be
        three rows, and `.distinct()` — the usual repair — then disagrees with
        the paginator's count.
        """
        post = self.meal_post_holding(
            "Chicken Breast", "Chicken Thigh", "Chicken Stock"
        )
        response = self.client.get(self.URL, {"search": "chicken"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([row["id"] for row in response.data["results"]], [post.id])
        self.assertEqual(response.data["count"], 1)

    def test_the_food_search_still_respects_visibility(self):
        """Reaching deeper must not reach around the rules."""
        post = self.meal_post_holding("Chicken Bowl")
        post.visibility = Post.Visibility.PRIVATE
        post.save(update_fields=["visibility"])
        self.assertEqual(self.ids({"search": "chicken"}), set())


class SplittingPostsByFollowTests(RepbaseAPITestMixin, APITestCase):
    """The two halves the Social tabs are built from.

    "For you" is the following feed and then everybody else; "Discover" is
    only everybody else. Both need the same list split the same way, so the
    split is the server's rather than each screen filtering a page it
    happened to be given.
    """

    URL = "/api/v1/social/posts/"

    def setUp(self):
        self.user, self.profile, self.token = self.create_account("reader")
        _, self.followed, _ = self.create_account("followed")
        _, self.stranger, _ = self.create_account("stranger")
        Follow.objects.create(follower=self.profile, following=self.followed)
        self.authenticate(self.token)

        self.mine = self.post_by(self.profile, "my own post")
        self.theirs = self.post_by(self.followed, "from somebody I follow")
        self.strangers = self.post_by(self.stranger, "from a stranger")

    def post_by(self, author, caption):
        return Post.objects.create(
            author=author, kind=Post.Kind.MEAL, caption=caption
        )

    def ids(self, query):
        response = self.client.get(self.URL, query)
        self.assertEqual(response.status_code, 200, response.data)
        return {row["id"] for row in response.data["results"]}

    def test_omitting_it_is_everybody(self):
        self.assertEqual(
            self.ids({}), {self.mine.id, self.theirs.id, self.strangers.id}
        )

    def test_true_is_the_people_being_followed(self):
        self.assertEqual(self.ids({"from_following": "true"}), {self.theirs.id})

    def test_false_is_the_people_not_being_followed(self):
        self.assertEqual(self.ids({"from_following": "false"}), {self.strangers.id})

    def test_false_leaves_out_the_readers_own_posts(self):
        """Discover is for finding other people, not for reading yourself."""
        self.assertNotIn(self.mine.id, self.ids({"from_following": "false"}))

    def test_the_two_halves_do_not_overlap(self):
        following = self.ids({"from_following": "true"})
        rest = self.ids({"from_following": "false"})
        self.assertEqual(following & rest, set())

    def test_following_somebody_moves_their_post_across(self):
        Follow.objects.create(follower=self.profile, following=self.stranger)
        self.assertIn(self.strangers.id, self.ids({"from_following": "true"}))
        self.assertNotIn(self.strangers.id, self.ids({"from_following": "false"}))

    def test_it_still_respects_visibility(self):
        """Splitting the list must not widen it."""
        hidden = self.post_by(self.stranger, "not for you")
        hidden.visibility = Post.Visibility.PRIVATE
        hidden.save(update_fields=["visibility"])
        self.assertNotIn(hidden.id, self.ids({"from_following": "false"}))

    def test_a_blocked_stranger_is_not_in_discover(self):
        Block.objects.create(blocker=self.profile, blocked=self.stranger)
        self.assertEqual(self.ids({"from_following": "false"}), set())

    def test_it_combines_with_search(self):
        self.post_by(self.stranger, "chicken and rice")
        found = self.ids({"from_following": "false", "search": "chicken"})
        self.assertEqual(len(found), 1)
        self.assertNotIn(self.strangers.id, found)

    def test_a_value_that_is_neither_is_refused(self):
        """Rather than quietly returning the opposite half."""
        response = self.client.get(self.URL, {"from_following": "banana"})
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("from_following", response.data)

    def test_one_and_zero_are_accepted(self):
        self.assertEqual(self.ids({"from_following": "1"}), {self.theirs.id})
        self.assertEqual(self.ids({"from_following": "0"}), {self.strangers.id})


class FeedSizedPhotoTests(SimpleTestCase):
    """Making a card-sized copy, and knowing when not to bother."""

    def photo_bytes(self, width, height, fmt="JPEG", mode="RGB"):
        from PIL import Image

        # Noise rather than flat colour: a solid image compresses to almost
        # nothing at any size, so a flat 4000px photo would "already be small
        # enough" and the size assertions below would prove nothing.
        import random

        random.seed(width * height)
        image = Image.new(mode, (width, height))
        image.putdata([
            tuple(random.randrange(256) for _ in range(len(mode)))
            for _ in range(width * height)
        ])
        buffer = io.BytesIO()
        image.save(buffer, format=fmt)
        return buffer.getvalue()

    def opened(self, raw):
        from PIL import Image

        return Image.open(io.BytesIO(raw))

    def test_a_large_photo_is_made_smaller(self):
        original = self.photo_bytes(2400, 1600)
        smaller = feed_variant(original)
        self.assertIsNotNone(smaller)
        self.assertLess(len(smaller), len(original))

    def test_it_fits_inside_the_box(self):
        smaller = feed_variant(self.photo_bytes(2400, 1600))
        image = self.opened(smaller)
        self.assertLessEqual(image.width, FEED_PHOTO_BOX[0])
        self.assertLessEqual(image.height, FEED_PHOTO_BOX[1])

    def test_the_shape_is_kept(self):
        """A bounding box, not a crop. The card decides its own framing."""
        original = self.photo_bytes(2400, 1600)
        image = self.opened(feed_variant(original))
        self.assertAlmostEqual(image.width / image.height, 2400 / 1600, places=2)

    def test_a_tall_photo_also_fits(self):
        image = self.opened(feed_variant(self.photo_bytes(1200, 3000)))
        self.assertLessEqual(image.width, FEED_PHOTO_BOX[0])
        self.assertLessEqual(image.height, FEED_PHOTO_BOX[1])

    def test_a_small_photo_gets_no_variant(self):
        """Re-encoding something already small stores a second copy for nothing."""
        self.assertIsNone(feed_variant(self.photo_bytes(200, 200)))

    def test_a_png_becomes_a_jpeg(self):
        """JPEG has no alpha; without the conversion the save raises."""
        smaller = feed_variant(self.photo_bytes(2000, 2000, fmt="PNG", mode="RGBA"))
        self.assertIsNotNone(smaller)
        self.assertEqual(self.opened(smaller).format, "JPEG")

    def test_nonsense_bytes_produce_no_variant_rather_than_an_error(self):
        """The reason every failure path returns None: a post must still post."""
        self.assertIsNone(feed_variant(b"this is not an image"))

    def test_empty_bytes_produce_no_variant(self):
        self.assertIsNone(feed_variant(b""))

    def test_a_truncated_file_produces_no_variant(self):
        """The 160-byte file already in the database is this shape."""
        self.assertIsNone(feed_variant(self.photo_bytes(2400, 1600)[:160]))


class PostsCarryBothPhotoSizesTests(RepbaseAPITestMixin, APITestCase):
    """What the two URLs on a post mean, and that neither is ever a surprise.

    Writes into a temporary MEDIA_ROOT. The test database is thrown away at
    the end of a run but uploaded files are not -- they go wherever the real
    settings point, which is the development server's own media directory.
    The first version of this class left seven files and nine megabytes of
    test photographs sitting among the real ones.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # ignore_cleanup_errors because removing the directory is housekeeping,
        # not the thing under test. A FieldFile still open on Windows makes the
        # file unremovable, and the error that raised named the temporary
        # directory rather than anything a reader could act on.
        cls._media = tempfile.TemporaryDirectory(
            prefix="repbase-test-media-", ignore_cleanup_errors=True
        )
        cls._media_override = override_settings(MEDIA_ROOT=cls._media.name)
        cls._media_override.enable()

    @classmethod
    def tearDownClass(cls):
        # Each step runs whatever the one before it did. These were three bare
        # statements, so a cleanup that raised skipped the base teardown
        # entirely -- leaving the class's database and connection state up and
        # failing the rest of the run somewhere else, a long way from the
        # unremovable file that actually caused it.
        try:
            cls._media_override.disable()
        finally:
            try:
                cls._media.cleanup()
            finally:
                super().tearDownClass()

    def setUp(self):
        self.user, self.profile, self.token = self.create_account("photographer")
        self.authenticate(self.token)

    def photo(self, width=2400, height=1600):
        from PIL import Image
        import random

        random.seed(width)
        image = Image.new("RGB", (width, height))
        image.putdata([
            (random.randrange(256), random.randrange(256), random.randrange(256))
            for _ in range(width * height)
        ])
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG")
        return buffer.getvalue()

    def post_with_photo(self, raw=None):
        post = Post.objects.create(
            author=self.profile, kind=Post.Kind.MEAL, caption="lunch"
        )
        post.image.save("original.jpg", ContentFile(raw or self.photo()), save=True)
        # Reads below open FieldFile handles; Windows cannot remove the test
        # media directory until those handles are explicitly closed.
        self.addCleanup(post.image.close)
        self.addCleanup(post.feed_image.close)
        return post

    def card(self, post):
        response = self.client.get(f"/api/v1/social/posts/{post.id}/")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def test_a_post_with_no_photo_answers_null_for_both(self):
        post = Post.objects.create(
            author=self.profile, kind=Post.Kind.MEAL, caption="no picture"
        )
        card = self.card(post)
        self.assertIsNone(card["image_url"])
        self.assertIsNone(card["feed_image_url"])

    def test_without_a_variant_the_feed_url_is_the_original(self):
        """The fallback that lets this ship without migrating what exists.

        Returning null here instead would take the picture off every post
        made before the variant existed.
        """
        post = self.post_with_photo()
        card = self.card(post)
        self.assertIsNotNone(card["image_url"])
        self.assertEqual(card["feed_image_url"], card["image_url"])

    def test_with_a_variant_the_two_urls_differ(self):
        post = self.post_with_photo()
        smaller = feed_variant(post.image.read())
        post.feed_image.save("small.jpg", ContentFile(smaller), save=True)

        card = self.card(post)
        self.assertNotEqual(card["feed_image_url"], card["image_url"])
        self.assertIn("post-photos/feed/", card["feed_image_url"])

    def test_the_original_is_never_replaced(self):
        """The detail view opens what was posted, at the size it was posted."""
        raw = self.photo()
        post = self.post_with_photo(raw)
        post.feed_image.save(
            "small.jpg", ContentFile(feed_variant(raw)), save=True
        )
        post.refresh_from_db()
        self.assertEqual(post.image.size, len(raw))

    def test_both_urls_are_absolute(self):
        """The app talks to the API from a different origin than the files."""
        post = self.post_with_photo()
        post.feed_image.save(
            "small.jpg", ContentFile(feed_variant(post.image.read())), save=True
        )
        card = self.card(post)
        self.assertTrue(card["image_url"].startswith("http"))
        self.assertTrue(card["feed_image_url"].startswith("http"))


class CookingInstructionsTests(RepbaseAPITestMixin, APITestCase):
    """The recipe half of a meal post.

    A meal is a list of foods; a recipe is what somebody did with them. The
    foods are copied from the meal, and this is written by the author at the
    moment they post.
    """

    URL = "/api/v1/social/posts/"

    def setUp(self):
        self.user, self.profile, self.token = self.create_account("cook")
        self.authenticate(self.token)
        self.meal = FoodMeal.objects.create(
            owner=self.profile,
            date=timezone.localdate(),
            name="Meal 2",
            position=2,
        )
        FoodEntry.objects.create(
            meal=self.meal, name="Chicken thigh", servings=2, calories=210, position=1
        )

    def post_meal(self, **extra):
        body = {"kind": "meal", "source_id": self.meal.id, **extra}
        response = self.client.post(self.URL, body, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return response.data

    def test_instructions_are_kept_on_the_post(self):
        card = self.post_meal(
            cooking_instructions="Sear skin down for six minutes, then turn."
        )
        self.assertEqual(
            card["meal"]["cooking_instructions"],
            "Sear skin down for six minutes, then turn.",
        )

    def test_a_meal_posted_without_them_reads_blank(self):
        """Most meals are assembled rather than cooked."""
        self.assertEqual(self.post_meal()["meal"]["cooking_instructions"], "")

    def test_surrounding_whitespace_is_dropped(self):
        card = self.post_meal(cooking_instructions="  Rest it for ten minutes.\n\n")
        self.assertEqual(card["meal"]["cooking_instructions"], "Rest it for ten minutes.")

    def test_line_breaks_inside_are_kept(self):
        """A recipe is a list of steps, and the steps are the line breaks."""
        recipe = "1. Season the thighs.\n2. Sear them.\n3. Rest."
        self.assertEqual(
            self.post_meal(cooking_instructions=recipe)["meal"]["cooking_instructions"],
            recipe,
        )

    def test_editing_the_meal_afterwards_does_not_rewrite_the_post(self):
        """The whole reason this lives on the snapshot.

        Somebody reads a recipe under a post. Renaming the meal, or logging it
        again tomorrow having cooked it differently, must not change what they
        read.
        """
        card = self.post_meal(cooking_instructions="Sear, then rest.")
        self.meal.name = "Something else"
        self.meal.save(update_fields=["name"])

        again = self.client.get(f"{self.URL}{card['id']}/")
        self.assertEqual(again.status_code, 200, again.data)
        self.assertEqual(again.data["meal"]["cooking_instructions"], "Sear, then rest.")
        self.assertEqual(again.data["meal"]["name"], "Meal 2")

    def test_they_cannot_be_set_by_editing_the_post(self):
        """PATCH takes the caption and the visibility, and nothing else.

        The snapshot is the record of what was posted. A recipe that could be
        rewritten after people had read it would be worth as little as a
        workout whose weights could.
        """
        card = self.post_meal(cooking_instructions="Sear, then rest.")
        response = self.client.patch(
            f"{self.URL}{card['id']}/",
            {"cooking_instructions": "Something different"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            response.data["meal"]["cooking_instructions"], "Sear, then rest."
        )

    def test_a_workout_post_ignores_them_rather_than_refusing(self):
        """One create endpoint serves three kinds; the field belongs to one."""
        # Finished, because an unfinished session is refused before this
        # field is ever looked at -- which is right, and not what is under
        # test here.
        session = WorkoutSession.objects.create(
            repbase_user=self.profile, status=WorkoutSession.Status.COMPLETED
        )
        response = self.client.post(
            self.URL,
            {
                "kind": "workout",
                "source_id": session.id,
                "cooking_instructions": "not applicable",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNone(response.data["meal"])

    def test_the_foods_still_come_from_the_meal(self):
        """Instructions are added alongside the copy, not instead of it."""
        card = self.post_meal(cooking_instructions="Sear, then rest.")
        self.assertEqual([row["name"] for row in card["meal"]["entries"]], ["Chicken thigh"])
        self.assertEqual(card["meal"]["entries"][0]["servings"], "2.00")


class SavingKeepsTheRecipeTests(RepbaseAPITestMixin, APITestCase):
    """Saving somebody's meal takes the method with the ingredients.

    A recipe and its food are one thing. Keeping the list and dropping the
    instructions saves the half that needs the other one -- and the post it
    came from can be edited or deleted, so pointing back at it is not a way
    of keeping it either.
    """

    def setUp(self):
        self.user, self.profile, self.token = self.create_account("saver")
        self.author, self.author_profile, self.author_token = self.create_account(
            "cook"
        )
        self.authenticate(self.token)

    def meal_post(self, instructions=""):
        post = Post.objects.create(
            author=self.author_profile, kind=Post.Kind.MEAL, caption="dinner"
        )
        meal = PostMeal.objects.create(
            post=post,
            name="Meal 1",
            date=timezone.localdate(),
            cooking_instructions=instructions,
        )
        PostMealEntry.objects.create(
            post_meal=meal,
            name="Chicken thigh",
            servings=2,
            calories=210,
            protein_grams=20,
            carbohydrate_grams=0,
            fat_grams=14,
            position=1,
        )
        return post

    def save(self, post):
        response = self.client.post(f"/api/v1/social/posts/{post.id}/save-meal/")
        self.assertIn(response.status_code, (200, 201), response.data)
        return SavedFoodMeal.objects.get(pk=response.data["meal"])

    def test_the_recipe_is_copied_with_the_food(self):
        recipe = "1. Season.\n2. Sear skin down.\n3. Rest."
        saved = self.save(self.meal_post(recipe))
        self.assertEqual(saved.cooking_instructions, recipe)
        self.assertEqual(saved.ingredients.count(), 1)

    def test_a_post_without_a_recipe_saves_blank(self):
        self.assertEqual(self.save(self.meal_post()).cooking_instructions, "")

    def test_the_copy_survives_the_post_being_deleted(self):
        """Which is why it is copied rather than pointed at."""
        post = self.meal_post("Sear, then rest.")
        saved = self.save(post)
        post.delete()

        saved.refresh_from_db()
        self.assertEqual(saved.cooking_instructions, "Sear, then rest.")
        self.assertIsNone(saved.source_post_id)
        self.assertEqual(saved.ingredients.count(), 1)

    def test_it_is_returned_when_the_library_is_read(self):
        recipe = "Sear, then rest."
        self.save(self.meal_post(recipe))
        response = self.client.get("/api/v1/food/saved-meals/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            response.data["results"][0]["cooking_instructions"], recipe
        )

    def test_saving_twice_still_leaves_one(self):
        """The recipe must not break the idempotence that was fixed before."""
        post = self.meal_post("Sear, then rest.")
        first = self.save(post)
        second = self.save(post)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(
            SavedFoodMeal.objects.filter(owner=self.profile).count(), 1
        )

    def test_a_meal_built_by_hand_can_carry_one_too(self):
        """The field is the saved meal's, not only the copier's."""
        response = self.client.post(
            "/api/v1/food/saved-meals/",
            {
                "name": "Overnight oats",
                "cooking_instructions": "Mix and leave until morning.",
                "ingredients": [
                    {
                        "name": "Oats",
                        "servings": "1",
                        "calories": "150",
                        "protein_grams": "5",
                        "carbohydrate_grams": "27",
                        "fat_grams": "3",
                        "position": 1,
                    }
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            response.data["cooking_instructions"], "Mix and leave until morning."
        )


class TheRecipeHasACeilingTests(RepbaseAPITestMixin, APITestCase):
    """A saved recipe is held to the same length as a posted one.

    The model field is a TextField, so nothing below this serializer puts a
    limit on it. A post's instructions have been capped at 4000 since they
    were added, and a recipe copied out of a post lands in this same field --
    so leaving the editor's own writes uncapped would mean the ceiling
    depended on which screen the text was typed on.
    """

    ceiling = 4000

    def setUp(self):
        self.user, self.profile, self.token = self.create_account("cook")
        self.authenticate(self.token)

    def write(self, instructions, name="Overnight oats"):
        return self.client.post(
            "/api/v1/food/saved-meals/",
            {
                "name": name,
                "cooking_instructions": instructions,
                "ingredients": [
                    {
                        "name": "Oats",
                        "servings": "1",
                        "calories": "150",
                        "protein_grams": "5",
                        "carbohydrate_grams": "27",
                        "fat_grams": "3",
                        "position": 1,
                    }
                ],
            },
            format="json",
        )

    def test_the_ceiling_itself_is_accepted(self):
        """The boundary belongs to the allowed side, as it does for a post."""
        response = self.write("x" * self.ceiling)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            len(SavedFoodMeal.objects.get(owner=self.profile).cooking_instructions),
            self.ceiling,
        )

    def test_one_character_over_is_refused(self):
        response = self.write("x" * (self.ceiling + 1))
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("cooking_instructions", response.data)
        self.assertFalse(SavedFoodMeal.objects.filter(owner=self.profile).exists())

    def test_editing_is_held_to_the_same_ceiling(self):
        """Otherwise the limit would only apply to the first save."""
        self.assertEqual(self.write("Mix and leave.").status_code, 201)
        saved = SavedFoodMeal.objects.get(owner=self.profile)

        response = self.client.patch(
            f"/api/v1/food/saved-meals/{saved.pk}/",
            {"cooking_instructions": "x" * (self.ceiling + 1)},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        saved.refresh_from_db()
        self.assertEqual(saved.cooking_instructions, "Mix and leave.")

    def test_surrounding_blank_space_is_not_counted_or_kept(self):
        """Trailing newlines are what a text view leaves behind."""
        response = self.write("  Mix and leave until morning.\n\n  ")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            response.data["cooking_instructions"], "Mix and leave until morning."
        )

    def test_a_recipe_is_still_optional(self):
        """The ceiling must not turn an absent recipe into a required one."""
        response = self.client.post(
            "/api/v1/food/saved-meals/",
            {
                "name": "Plain oats",
                "ingredients": [
                    {
                        "name": "Oats",
                        "servings": "1",
                        "calories": "150",
                        "protein_grams": "5",
                        "carbohydrate_grams": "27",
                        "fat_grams": "3",
                        "position": 1,
                    }
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["cooking_instructions"], "")


class PagingIsStableAcrossPagesTests(RepbaseAPITestMixin, APITestCase):
    """Every row shows up exactly once when a list is read page by page.

    Both of these lists annotate a Count, which puts a GROUP BY on the query.
    Django drops Meta.ordering when it does that and emits no ORDER BY at all,
    so the database was free to return rows in whatever order suited it. A
    paginated read is several reads of that table, and two reads of an
    unordered table need not agree: page two can repeat a row page one already
    showed and skip one it never did. Nothing about that is visible in a test
    that only ever asks for the first page, which is why these ask for all of
    them and compare the whole against what is stored.
    """

    #: settings.REST_FRAMEWORK["PAGE_SIZE"]. More than one page is the point.
    page_size = 50
    rows = 120

    def setUp(self):
        self.user, self.profile, self.token = self.create_account("pager")
        self.authenticate(self.token)

    def walk(self, url):
        """Every id a list gives, followed the way a client follows it."""
        seen = []
        pages = 0
        while url:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, response.data)
            seen.extend(row["id"] for row in response.data["results"])
            url = response.data["next"]
            pages += 1
            # A next link that never empties would otherwise hang the suite.
            self.assertLess(pages, 20, "paging did not terminate")
        return seen, pages

    def assertCoversExactly(self, seen, expected_ids):
        self.assertEqual(len(seen), len(set(seen)), "a row was served twice")
        self.assertEqual(set(seen), set(expected_ids), "a row was missed")

    # -- gyms ------------------------------------------------------------

    def make_gyms(self):
        """Inserted out of order, so insertion order cannot pass for sorted.

        Names are unique because the model requires it: a gym is unique per
        normalized name and city, so ORDER BY name, city cannot actually tie
        here. What is being tested is that the clause is emitted at all.
        """
        for index in sorted(range(self.rows), key=lambda n: (n * 7919) % self.rows):
            Gym.objects.create(name=f"Gym {index:03d}", city="Leeds")
        return list(Gym.objects.values_list("id", flat=True))

    def test_gyms_page_without_repeating_or_dropping_one(self):
        expected = self.make_gyms()
        seen, pages = self.walk("/api/v1/gyms/")
        self.assertGreater(pages, 1, "fewer rows than a page; nothing was paged")
        self.assertCoversExactly(seen, expected)

    def test_gyms_come_back_in_the_documented_order(self):
        self.make_gyms()
        seen, _ = self.walk("/api/v1/gyms/")
        names = list(
            Gym.objects.filter(id__in=seen).order_by("name", "city", "pk")
            .values_list("id", flat=True)
        )
        self.assertEqual(seen, names, "pages did not arrive in name order")

    def test_reading_the_gym_list_twice_gives_the_same_order(self):
        """An unordered read may happen to look sorted once."""
        self.make_gyms()
        first, _ = self.walk("/api/v1/gyms/")
        second, _ = self.walk("/api/v1/gyms/")
        self.assertEqual(first, second)

    # -- workout history -------------------------------------------------

    def make_sessions(self):
        """All sharing one created_at, which is what history sorts on.

        created_at is auto_now_add, so sessions written in the same instant --
        a health import, a fast client, a test -- genuinely carry the same
        value. When they do, the sort column cannot decide anything and the
        tiebreaker is the only thing keeping the pages apart.
        """
        sessions = [
            WorkoutSession.objects.create(repbase_user=self.profile)
            for _ in range(self.rows)
        ]
        moment = timezone.now()
        WorkoutSession.objects.filter(
            id__in=[session.id for session in sessions]
        ).update(created_at=moment)
        return [session.id for session in sessions]

    def test_workout_history_pages_without_repeating_or_dropping_one(self):
        expected = self.make_sessions()
        seen, pages = self.walk("/api/v1/sessions/")
        self.assertGreater(pages, 1, "fewer rows than a page; nothing was paged")
        self.assertCoversExactly(seen, expected)

    def test_history_breaks_a_tied_timestamp_by_the_key(self):
        """Every row here ties, so this is the tiebreaker on its own."""
        expected = self.make_sessions()
        seen, _ = self.walk("/api/v1/sessions/")
        self.assertEqual(seen, sorted(expected, reverse=True))

    def test_reading_the_history_twice_gives_the_same_order(self):
        self.make_sessions()
        first, _ = self.walk("/api/v1/sessions/")
        second, _ = self.walk("/api/v1/sessions/")
        self.assertEqual(first, second)

    # -- the warning itself ----------------------------------------------

    def test_neither_list_is_paginated_unordered(self):
        """DRF says so itself, and says it once per unordered list.

        Turned into an error because it is a warning nobody reads: it was
        printed on every run of this suite for as long as both lists have
        existed.
        """
        from django.core.paginator import UnorderedObjectListWarning

        Gym.objects.create(name="Gym 000", city="Leeds")
        WorkoutSession.objects.create(repbase_user=self.profile)
        with warnings.catch_warnings():
            warnings.simplefilter("error", UnorderedObjectListWarning)
            self.assertEqual(self.client.get("/api/v1/gyms/").status_code, 200)
            self.assertEqual(self.client.get("/api/v1/sessions/").status_code, 200)


class IdempotentSaveAcceptsACharsetTests(RepbaseAPITestMixin, APITestCase):
    """A JSON body is allowed to say which encoding it is in.

    ``Content-Type`` carries parameters, and DRF's ``request.content_type``
    hands the header back verbatim rather than parsed. The mixin compared that
    whole string to "application/json", so a client sending the perfectly
    ordinary "application/json; charset=utf-8" was told its body was not JSON.

    That client was the iOS app: swift-openapi-generator always sends the
    charset. Every save carrying an Idempotency-Key -- planner entries,
    workouts, sessions, set entries, food entries, saved meals -- came back
    400 for a week, while anything without a key went through, which is why
    signing in worked and nothing could be written. curl sends no charset by
    default and no key at all, so every hand-made request said it was fine.

    The header is the whole point of these tests. Sending a body and asserting
    on the row is not enough: it was never the body that was wrong.
    """

    def setUp(self):
        self.user, self.profile, self.token = self.create_account("charset")
        self.authenticate(self.token)
        self.entry = {
            "title": "study",
            "scheduled_date": str(timezone.localdate()),
            "kind": "task",
            "category": "home",
        }

    def _post(self, content_type, key=None):
        return self.client.post(
            "/api/v1/planner/",
            data=json.dumps(self.entry),
            content_type=content_type,
            **({"HTTP_IDEMPOTENCY_KEY": str(key)} if key else {}),
        )

    def test_a_charset_is_not_a_reason_to_refuse_the_save(self):
        response = self._post("application/json; charset=utf-8", key=uuid.uuid4())
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(PlannerEntry.objects.filter(owner=self.profile).count(), 1)

    def test_the_parameter_may_be_spelled_any_way_a_client_spells_it(self):
        # Media types are case-insensitive and the separator may carry spaces.
        for content_type in (
            "application/json;charset=utf-8",
            "Application/JSON; charset=UTF-8",
            "application/json ; charset=iso-8859-1",
        ):
            with self.subTest(content_type=content_type):
                response = self._post(content_type, key=uuid.uuid4())
                self.assertEqual(response.status_code, 201, response.data)

    def test_a_body_that_really_is_not_json_is_still_refused(self):
        # The check still has a job: this is what it was written to catch.
        response = self.client.post(
            "/api/v1/planner/",
            data="title=study",
            content_type="application/x-www-form-urlencoded",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(PlannerEntry.objects.filter(owner=self.profile).count(), 0)

    def test_a_charset_does_not_break_the_replay(self):
        # The point of the key: the same save twice leaves one row.
        key = uuid.uuid4()
        first = self._post("application/json; charset=utf-8", key=key)
        second = self._post("application/json; charset=utf-8", key=key)
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(second.status_code, 201, second.data)
        self.assertEqual(second.data["id"], first.data["id"])
        self.assertEqual(PlannerEntry.objects.filter(owner=self.profile).count(), 1)


class FinishingAWorkoutTicksItsTaskTests(RepbaseAPITestMixin, APITestCase):
    """The calendar has to agree that the training happened.

    A scheduled workout exists twice: as a WorkoutSession, which knows whether
    it was done, and as a PlannerEntry, which is what the calendar draws. Only
    the session was being finished, so after training the day still read
    "Push Day" unticked and "0 of 2 done" -- the planner reporting outstanding
    work that had just been completed, which is the one number it exists to
    get right.

    The zone cases are the ones worth having. `scheduled_date` is a date and
    `ended_at` is an instant, so the match depends on whose day is meant. The
    server keeps UTC; for anyone west of it an evening session converts to
    tomorrow, finds no task for that date, and silently ticks nothing -- the
    failure this would have shipped with, and the reason `zone_for` is asked
    rather than `timezone.localdate`.
    """

    def setUp(self):
        self.user, self.profile, self.token = self.create_account("trainee")
        self.authenticate(self.token)
        self.workout = WorkoutTemplate.objects.create(owner=self.profile, name="Push Day")

    def _task_on(self, day, workout=None):
        return PlannerEntry.objects.create(
            owner=self.profile,
            kind=PlannerEntry.Kind.TASK,
            title="Push Day",
            category="workout",
            scheduled_date=day,
            workout=self.workout if workout is None else workout,
        )

    def _session(self, started_at):
        return WorkoutSession.objects.create(
            repbase_user=self.profile,
            workout=self.workout,
            status=WorkoutSession.Status.ACTIVE,
            started_at=started_at,
        )

    def _end(self, session, ended_at):
        return self.client.post(
            f"/api/v1/sessions/{session.id}/end/",
            {"ended_at": ended_at.isoformat()},
            format="json",
        )

    def test_finishing_marks_the_day_task_complete(self):
        started = timezone.now() - timedelta(hours=1)
        task = self._task_on(timezone.localtime(started, ZoneInfo("UTC")).date())
        response = self._end(self._session(started), timezone.now())

        self.assertEqual(response.status_code, 200, response.data)
        task.refresh_from_db()
        self.assertIsNotNone(task.completed_at, "the calendar must show the workout done")

    def test_an_evening_session_ticks_its_own_day_not_tomorrow(self):
        # 21:00 in Chicago is the small hours of the next day in UTC. The task
        # belongs to the day the person trained, which is the earlier one.
        #
        # Taken relative to now, and in the past: the serializer refuses a
        # finish time in the future, so a fixed date here passes or fails
        # depending on when the suite is run.
        self.profile.time_zone = "America/Chicago"
        self.profile.save(update_fields=["time_zone"])
        chicago = ZoneInfo("America/Chicago")
        started = (
            timezone.localtime(timezone.now(), chicago) - timedelta(days=2)
        ).replace(hour=21, minute=0, second=0, microsecond=0)
        their_day = self._task_on(started.date())
        utc_day = self._task_on(started.astimezone(ZoneInfo("UTC")).date())
        self.assertNotEqual(their_day.scheduled_date, utc_day.scheduled_date)

        self._end(self._session(started), started + timedelta(hours=1))

        their_day.refresh_from_db()
        utc_day.refresh_from_db()
        self.assertIsNotNone(their_day.completed_at, "ticked the day they trained")
        self.assertIsNone(utc_day.completed_at, "must not tick the server's day")

    def test_a_session_running_past_midnight_belongs_to_the_day_it_began(self):
        # Past, for the same reason as above: a future finish time is refused.
        started = (timezone.now() - timedelta(days=2)).replace(
            hour=23, minute=30, second=0, microsecond=0
        )
        started_day = self._task_on(timezone.localtime(started, ZoneInfo("UTC")).date())
        self._end(self._session(started), started + timedelta(hours=1))

        started_day.refresh_from_db()
        self.assertIsNotNone(started_day.completed_at)

    def test_finishing_again_keeps_the_first_completion_time(self):
        started = timezone.now() - timedelta(hours=1)
        task = self._task_on(timezone.localtime(started, ZoneInfo("UTC")).date())
        session = self._session(started)
        self._end(session, timezone.now())
        task.refresh_from_db()
        first = task.completed_at

        self._end(session, timezone.now() + timedelta(minutes=5))
        task.refresh_from_db()
        self.assertEqual(task.completed_at, first, "a retry must not rewrite history")

    def test_somebody_elses_task_is_never_touched(self):
        other, other_profile, _ = self.create_account("stranger")
        started = timezone.now() - timedelta(hours=1)
        theirs = PlannerEntry.objects.create(
            owner=other_profile,
            kind=PlannerEntry.Kind.TASK,
            title="Push Day",
            category="workout",
            scheduled_date=timezone.localtime(started, ZoneInfo("UTC")).date(),
            workout=self.workout,
        )
        self._end(self._session(started), timezone.now())

        theirs.refresh_from_db()
        self.assertIsNone(theirs.completed_at)

    def test_a_session_with_no_workout_finishes_without_incident(self):
        loose = WorkoutSession.objects.create(
            repbase_user=self.profile,
            status=WorkoutSession.Status.ACTIVE,
            started_at=timezone.now() - timedelta(hours=1),
        )
        response = self._end(loose, timezone.now())
        self.assertEqual(response.status_code, 200, response.data)
