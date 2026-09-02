from unittest import mock
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import SimpleTestCase
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient, APITestCase

from . import food_sources
from .models import (
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

        anonymous_response = self.client.get("/api/v1/users/")
        self.assertEqual(anonymous_response.status_code, 401)

        self.authenticate(token)
        response = self.client.get("/api/v1/users/")
        self.assertEqual(response.status_code, 200)
        profile = response.data["results"][0]
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
