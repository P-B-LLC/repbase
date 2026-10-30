"""Notification context stays private and reading affects only displayed rows."""

from unittest import mock

from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APITestCase

from .fixtures_for_tests import FAST_PASSWORDS, make_account, meal_post, workout_post
from .models import Block, Follow, Notification, Post, PostComment, PostVideo
from .serializers import NotificationSerializer


@FAST_PASSWORDS
class NotificationPreviewTests(APITestCase):
    URL = "/api/v1/social/notifications/"

    @classmethod
    def setUpTestData(cls):
        cls.author, cls.author_profile, cls.author_token = make_account("notice-author")
        cls.viewer, cls.viewer_profile, cls.viewer_token = make_account("notice-viewer")
        cls.actor, cls.actor_profile, cls.actor_token = make_account("notice-actor")
        cls.post = meal_post(cls.author_profile, caption="Lunch outside")
        cls.post.image = "post-photos/example.jpg"
        cls.post.feed_image = "post-photos/feed/example.jpg"
        cls.post.save(update_fields=["image", "feed_image"])

    def setUp(self):
        cache.clear()
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.viewer_token.key}")

    def notice(self, *, post=None, kind=Notification.Kind.LIKE, recipient=None, **kwargs):
        return Notification.objects.create(
            recipient=recipient or self.viewer_profile, actor=self.actor_profile,
            post=post if post is not None else self.post, kind=kind, **kwargs,
        )

    def rows(self):
        result = self.client.get(self.URL)
        self.assertEqual(result.status_code, 200, result.data)
        return result.data["results"]

    def test_authorized_photo_preview_uses_small_signed_photo(self):
        notice = self.notice()
        self.actor_profile.profile_photo = "profile-photos/actor.jpg"
        self.actor_profile.save(update_fields=["profile_photo"])
        row = self.rows()[0]
        self.assertEqual(row["id"], notice.id)
        self.assertTrue(row["supports_scoped_read"])
        self.assertIn("created_at", row)
        self.assertIn("/media/profile-photos/actor.jpg?", row["actor_photo_url"])
        preview = row["post_preview"]
        self.assertEqual(preview["id"], self.post.id)
        self.assertEqual(preview["kind"], "meal")
        self.assertEqual(preview["title"], "Lunch outside")
        self.assertEqual(preview["media_kind"], "photo")
        self.assertIn("/media/post-photos/feed/example.jpg?", preview["thumbnail_url"])
        self.assertIn("&s=", preview["thumbnail_url"])
        self.assertNotIn("source_meal", preview)

    def test_follow_has_no_post_preview(self):
        Notification.objects.create(recipient=self.viewer_profile, actor=self.actor_profile, kind="follow")
        self.assertIsNone(self.rows()[0]["post_preview"])

    def test_workout_without_photo_has_title_and_no_thumbnail(self):
        post = workout_post(self.author_profile, caption=" ")
        self.notice(post=post)
        preview = self.rows()[0]["post_preview"]
        self.assertEqual(preview["title"], "Lifting session")
        self.assertEqual(preview["media_kind"], "none")
        self.assertIsNone(preview["thumbnail_url"])

    def test_standalone_serializer_fails_closed_without_authorized_context(self):
        self.assertIsNone(NotificationSerializer(self.notice()).data["post_preview"])

    def test_schema_additions_are_optional_for_staged_client_rollout(self):
        from drf_spectacular.generators import SchemaGenerator

        spec = SchemaGenerator().get_schema(request=None, public=True)
        schemas = spec["components"]["schemas"]
        notification = schemas["Notification"]
        for field in ("post_preview", "supports_scoped_read"):
            self.assertIn(field, notification["properties"])
            self.assertNotIn(field, notification["required"])
        self.assertTrue(notification["properties"]["post_preview"]["nullable"])
        self.assertEqual(schemas["NotificationReadRequest"]["properties"]["ids"]["maxItems"], 500)
        self.assertFalse(spec["paths"][self.URL + "read/"]["post"]["requestBody"].get("required", False))

    def test_restricted_post_activity_and_comments_do_not_leak(self):
        comment = PostComment.objects.create(post=self.post, author=self.actor_profile, body="Private reply")
        self.notice(kind="comment", comment=comment)
        original = {
            "visibility": "public", "moderation_status": "approved", "is_hidden": False,
            "published_at": self.post.published_at, "moderation_child_hold": False,
        }
        for change in (
            {"visibility": "private"}, {"visibility": "followers"},
            {"moderation_status": "pending"}, {"moderation_status": "flagged"},
            {"moderation_status": "rejected"}, {"published_at": None},
            {"is_hidden": True}, {"moderation_child_hold": True},
        ):
            with self.subTest(change=change):
                Post.objects.filter(pk=self.post.pk).update(**(original | change))
                self.assertEqual(self.rows(), [])
                self.assertEqual(self.client.get(self.URL + "unread-count/").data["unread"], 0)

    def test_closed_profile_requires_actual_follow(self):
        self.notice()
        self.author_profile.is_profile_public = False
        self.author_profile.save(update_fields=["is_profile_public"])
        self.assertEqual(self.rows(), [])
        Follow.objects.create(follower=self.viewer_profile, following=self.author_profile)
        self.assertEqual(len(self.rows()), 1)
        Follow.objects.all().delete()
        self.assertEqual(self.rows(), [])

    def test_block_in_either_direction_for_actor_or_post_author_hides_activity(self):
        self.notice()
        for other in (self.actor_profile, self.author_profile):
            for blocker, blocked in ((self.viewer_profile, other), (other, self.viewer_profile)):
                with self.subTest(blocker=blocker.id, blocked=blocked.id):
                    block = Block.objects.create(blocker=blocker, blocked=blocked)
                    self.assertEqual(self.rows(), [])
                    block.delete()

    def test_suspended_actor_or_author_hides_activity(self):
        self.notice()
        for user in (self.actor, self.author):
            user.is_active = False
            user.save(update_fields=["is_active"])
            self.assertEqual(self.rows(), [])
            user.is_active = True
            user.save(update_fields=["is_active"])

    def test_hidden_comment_or_parent_is_not_disclosed(self):
        parent = PostComment.objects.create(post=self.post, author=self.viewer_profile, body="parent")
        comment = PostComment.objects.create(post=self.post, author=self.actor_profile, body="reply", parent=parent)
        self.notice(kind="comment", comment=comment)
        for row in (comment, parent):
            row.is_hidden = True
            row.save(update_fields=["is_hidden"])
            self.assertEqual(self.rows(), [])
            row.is_hidden = False
            row.save(update_fields=["is_hidden"])

    def test_inconsistent_comment_post_links_are_never_disclosed_or_marked_read(self):
        private_post = workout_post(self.author_profile, visibility="private")
        comment = PostComment.objects.create(post=private_post, author=self.actor_profile,
                                             body="Confidential private reply")
        # Invalid historical/internal rows are possible: no database check
        # can enforce equality across these two foreign-key targets.
        for post in (None, self.post):
            with self.subTest(post=post):
                row = Notification.objects.create(
                    recipient=self.viewer_profile, actor=self.actor_profile,
                    kind="comment", post=post, comment=comment,
                )
                self.assertEqual(self.rows(), [])
                self.assertEqual(self.client.get(self.URL + "unread-count/").data["unread"], 0)
                response = self.client.post(self.URL + "read/", {"ids": [row.id]}, format="json")
                self.assertEqual(response.status_code, 200)
                row.refresh_from_db()
                self.assertIsNone(row.read_at)
                row.delete()

    def test_rejection_notice_remains_generic_with_no_restricted_preview(self):
        Post.objects.filter(pk=self.post.pk).update(is_hidden=True, moderation_child_hold=True, moderation_status="rejected")
        Notification.objects.create(recipient=self.author_profile, actor=self.author_profile,
                                    post=self.post, kind="moderation_rejected")
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.author_token.key}")
        row = self.rows()[0]
        self.assertIsNone(row["post_preview"])
        self.assertIsNone(row["comment_body"])
        self.assertEqual(row["kind"], "moderation_rejected")
        self.assertNotIn("Lunch outside", str(row))

    def test_repost_preview_respects_original_privacy(self):
        repost = Post.objects.create(author=self.actor_profile, kind="repost", repost_of=self.post)
        self.notice(post=repost)
        preview = self.rows()[0]["post_preview"]
        self.assertEqual(preview["id"], repost.id)
        self.assertEqual(preview["kind"], "meal")
        self.assertEqual(preview["title"], "Lunch outside")
        Post.objects.filter(pk=self.post.pk).update(visibility="private")
        self.assertEqual(self.rows(), [])

    def test_video_thumbnail_requires_ready_approved_and_no_child_hold(self):
        video = PostVideo.objects.create(post=self.post, file="post-videos/test.mp4",
                                         status="approved", processing_state="ready",
                                         thumbnail_key="videos/thumb.jpg", duration_ms=1000,
                                         width=320, height=240, size_bytes=1024,
                                         content_type="video/mp4")
        self.notice()
        preview = self.rows()[0]["post_preview"]
        self.assertEqual(preview["media_kind"], "video")
        self.assertIn("/api/v1/video-assets/?token=", preview["thumbnail_url"])
        for change in ({"status": "pending"}, {"status": "rejected"}, {"processing_state": "processing"}):
            PostVideo.objects.filter(pk=video.pk).update(**({"status": "approved", "processing_state": "ready"} | change))
            self.assertIsNone(self.rows()[0]["post_preview"]["thumbnail_url"])
        Post.objects.filter(pk=self.post.pk).update(moderation_child_hold=True)
        self.assertEqual(self.rows(), [])

    def test_list_query_count_is_bounded_as_post_count_grows(self):
        self.notice()
        with CaptureQueriesContext(connection) as one:
            self.rows()
        for index in range(12):
            post = workout_post(self.author_profile, caption=f"Workout {index}")
            self.notice(post=post)
        with CaptureQueriesContext(connection) as many:
            self.assertEqual(len(self.rows()), 13)
        self.assertEqual(len(one), len(many))
        self.assertLessEqual(len(many), 8)

    def test_privacy_changed_before_preview_read_never_signs_thumbnail(self):
        from .views import NotificationViewSet
        self.notice()
        original = NotificationViewSet.paginate_queryset

        def change_privacy(view, queryset):
            page = original(view, queryset)
            Post.objects.filter(pk=self.post.pk).update(visibility="private")
            return page

        with mock.patch.object(NotificationViewSet, "paginate_queryset", change_privacy):
            self.assertEqual(self.rows(), [])

    def test_list_and_read_require_authentication(self):
        self.client.credentials()
        self.assertEqual(self.client.get(self.URL).status_code, 401)
        self.assertEqual(self.client.post(self.URL + "read/", {"ids": []}, format="json").status_code, 401)

    def test_other_recipient_cannot_read_or_mark_my_notification(self):
        row = self.notice(recipient=self.author_profile)
        self.assertEqual(self.rows(), [])
        response = self.client.post(self.URL + "read/", {"ids": [row.id]}, format="json")
        self.assertEqual(response.status_code, 200)
        row.refresh_from_db()
        self.assertIsNone(row.read_at)

    def test_scoped_read_preserves_unseen_and_newly_arrived_notifications(self):
        seen = self.notice()
        # A notification created after the displayed list was loaded stays unread.
        new = self.notice(kind="repost")
        response = self.client.post(self.URL + "read/", {"ids": [seen.id, seen.id]}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["unread"], 1)
        seen.refresh_from_db()
        new.refresh_from_db()
        self.assertIsNotNone(seen.read_at)
        self.assertIsNone(new.read_at)

    def test_empty_read_does_nothing_and_legacy_bodyless_read_still_works(self):
        row = self.notice()
        response = self.client.post(self.URL + "read/", {"ids": []}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["unread"], 1)
        row.refresh_from_db()
        self.assertIsNone(row.read_at)
        response = self.client.post(self.URL + "read/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["unread"], 0)

    def test_invalid_read_requests_do_not_mark_anything(self):
        row = self.notice()
        for body in ({"ids": [0]}, {"ids": [-1]}, {"ids": ["x"]}, {"ids": [True]},
                     {"ids": [1.5]}, {"ids": None}, {"ids": "all"},
                     {"ids": list(range(1, 502))}, {"id": row.id}, []):
            with self.subTest(body=body):
                result = self.client.post(self.URL + "read/", body, format="json")
                self.assertEqual(result.status_code, 400, result.data)
                row.refresh_from_db()
                self.assertIsNone(row.read_at)

    def test_unavailable_rows_are_not_marked_by_scoped_or_legacy_read(self):
        row = self.notice()
        Post.objects.filter(pk=self.post.pk).update(visibility="private")
        for body in ({"ids": [row.id]}, {}):
            result = self.client.post(self.URL + "read/", body, format="json")
            self.assertEqual(result.status_code, 200)
            row.refresh_from_db()
            self.assertIsNone(row.read_at)

    def test_order_and_pagination_keep_latest_event_first(self):
        rows = [self.notice() for _ in range(55)]
        response = self.client.get(self.URL)
        self.assertEqual(response.data["count"], 55)
        self.assertEqual(len(response.data["results"]), 50)
        self.assertEqual(response.data["results"][0]["id"], rows[-1].id)
        self.assertIsNotNone(response.data["next"])
        second = self.client.get(response.data["next"])
        self.assertEqual(len(second.data["results"]), 5)
        self.assertTrue(all(row["supports_scoped_read"] for row in second.data["results"]))
