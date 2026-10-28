"""Like mutations read back visible friends' posts, never just one's own."""

from unittest import mock

from django.core.cache import cache

from rest_framework.test import APITestCase

from .fixtures_for_tests import FAST_PASSWORDS, make_account, workout_post
from .models import Block, Follow, Notification, Post, PostLike


@FAST_PASSWORDS
class PostLikeTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author, cls.author_profile, cls.author_token = make_account("like-author")
        cls.reader, cls.reader_profile, cls.reader_token = make_account("like-reader")
        cls.post = workout_post(cls.author_profile, workout_type="cycling")
        cls.url = f"/api/v1/social/posts/{cls.post.pk}/like/"

    def setUp(self):
        cache.clear()
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.reader_token.key}")

    def test_a_like_returns_the_saved_state_and_notifies_once(self):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["viewer_has_liked"])
        self.assertEqual(response.data["like_count"], 1)
        self.assertEqual(PostLike.objects.filter(post=self.post, user=self.reader_profile).count(), 1)
        self.assertEqual(Notification.objects.filter(post=self.post, kind=Notification.Kind.LIKE).count(), 1)

    def test_repeated_like_and_unlike_are_idempotent(self):
        for _ in range(2):
            response = self.client.post(self.url)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.data["viewer_has_liked"])
            self.assertEqual(response.data["like_count"], 1)
        for _ in range(2):
            response = self.client.delete(self.url)
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.data["viewer_has_liked"])
            self.assertEqual(response.data["like_count"], 0)
        self.assertEqual(Notification.objects.filter(post=self.post, kind=Notification.Kind.LIKE).count(), 1)

    def test_unlike_preserves_other_peoples_likes(self):
        PostLike.objects.create(post=self.post, user=self.author_profile)
        PostLike.objects.create(post=self.post, user=self.reader_profile)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["viewer_has_liked"])
        self.assertEqual(response.data["like_count"], 1)
        self.assertTrue(PostLike.objects.filter(post=self.post, user=self.author_profile).exists())

    def test_own_like_works_without_notifying_yourself(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.author_token.key}")
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["viewer_has_liked"])
        self.assertTrue(response.data["viewer_is_author"])
        self.assertFalse(Notification.objects.filter(post=self.post).exists())

    def test_private_unpublished_and_moderated_posts_are_not_likeable(self):
        original = {
            "visibility": self.post.visibility,
            "moderation_status": self.post.moderation_status,
            "published_at": self.post.published_at,
            "moderation_child_hold": self.post.moderation_child_hold,
            "is_hidden": self.post.is_hidden,
        }
        for change in (
            {"visibility": Post.Visibility.PRIVATE},
            {"moderation_status": "pending"},
            {"moderation_status": "rejected"},
            {"published_at": None},
            {"moderation_child_hold": True},
            {"is_hidden": True},
        ):
            with self.subTest(change=change):
                Post.objects.filter(pk=self.post.pk).update(**change)
                response = self.client.post(self.url)
                self.assertEqual(response.status_code, 404)
                self.assertFalse(PostLike.objects.filter(post=self.post).exists())
                self.assertFalse(Notification.objects.filter(post=self.post).exists())
                Post.objects.filter(pk=self.post.pk).update(**original)

    def test_followers_only_post_uses_follow_permission_not_authorship(self):
        Post.objects.filter(pk=self.post.pk).update(visibility=Post.Visibility.FOLLOWERS)
        denied = self.client.post(self.url)
        self.assertEqual(denied.status_code, 404)
        self.assertFalse(PostLike.objects.filter(post=self.post).exists())
        Follow.objects.create(follower=self.reader_profile, following=self.author_profile)
        permitted = self.client.post(self.url)
        self.assertEqual(permitted.status_code, 200)
        self.assertTrue(permitted.data["viewer_has_liked"])
        self.assertFalse(permitted.data["viewer_is_author"])

    def test_block_in_either_direction_refuses_likes(self):
        for blocker, blocked in (
            (self.reader_profile, self.author_profile),
            (self.author_profile, self.reader_profile),
        ):
            with self.subTest(blocker=blocker.pk):
                block = Block.objects.create(blocker=blocker, blocked=blocked)
                response = self.client.post(self.url)
                self.assertEqual(response.status_code, 404)
                self.assertFalse(PostLike.objects.filter(post=self.post).exists())
                self.assertFalse(Notification.objects.filter(post=self.post).exists())
                block.delete()

    def test_readback_rechecks_privacy_if_it_changes_during_a_like(self):
        # A notification is made between the write and response readback. Model
        # the author making the post private in that window: return 404, never
        # an unhandled DoesNotExist or the now-private post body.
        def make_private(*args, **kwargs):
            Post.objects.filter(pk=self.post.pk).update(visibility=Post.Visibility.PRIVATE)

        with mock.patch("core.views.notify", side_effect=make_private):
            response = self.client.post(self.url)
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("caption", response.data)
        self.assertTrue(PostLike.objects.filter(post=self.post, user=self.reader_profile).exists())

    def test_deleted_post_is_not_an_unhandled_exception(self):
        self.post.delete()
        self.assertEqual(self.client.post(self.url).status_code, 404)
        self.assertEqual(self.client.delete(self.url).status_code, 404)

    def test_anonymous_like_is_refused_without_writing(self):
        self.client.credentials()
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 401)
        self.assertFalse(PostLike.objects.filter(post=self.post).exists())
        self.assertFalse(Notification.objects.filter(post=self.post).exists())
