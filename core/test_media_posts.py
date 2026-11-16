"""Photo and video posts: a picture or a clip and a caption, made from nothing
recorded.

Made the way a client makes them, through POST /social/posts/ and then the
clip upload, so the refusals, the visibility rule and the clip's progress are
what is under test. The rule worth the most attention is the one a reader
never sees: a media post with nothing yet to show is its author's alone, and
reaches everybody else the moment its clip is ready, with no second publish.
"""

import io
import tempfile
from pathlib import Path
from unittest import mock

from django.test import override_settings
from rest_framework.test import APITestCase

from .fixtures_for_tests import (
    TemporaryMediaMixin,
    as_base64,
    finished_session,
    photo_bytes,
    video_fixture,
)
from .models import Follow, Post, PostVideo
from .tests import RepbaseAPITestMixin

POSTS = "/api/v1/social/posts/"
FEED = "/api/v1/social/feed/"
FOR_YOU = "/api/v1/social/for-you/"


class MediaPostTestMixin(TemporaryMediaMixin, RepbaseAPITestMixin):
    def setUp(self):
        _, self.author, self.author_token = self.create_account("photographer")
        _, self.follower, self.follower_token = self.create_account("follower")
        _, self.stranger, self.stranger_token = self.create_account("stranger")
        Follow.objects.create(follower=self.follower, following=self.author)
        self.authenticate(self.author_token)

    def publish(self, **body):
        self.authenticate(self.author_token)
        return self.client.post(POSTS, {"kind": "media", **body}, format="json")

    def publish_photo(self, **body):
        return self.publish(
            content_type="image/jpeg", image_base64=as_base64(photo_bytes()), **body
        )

    def upload(self, post_id, name="h264_faststart.mp4"):
        self.authenticate(self.author_token)
        return self.client.generic(
            "POST", f"{POSTS}{post_id}/video/", video_fixture(name), content_type="video/mp4"
        )

    def ids_in(self, url, token):
        self.authenticate(token)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.data)
        return {post["id"] for post in response.data["results"]}

    def seen_by(self, token, post_id):
        """Every place a reader could come across the post, and whether it
        was there."""
        self.authenticate(token)
        return {
            "feed": post_id in self.ids_in(FEED, token),
            "list": post_id in self.ids_in(POSTS, token),
            "profile": post_id in self.ids_in(f"{POSTS}?author={self.author.pk}", token),
            "detail": self.client.get(f"{POSTS}{post_id}/").status_code == 200,
        }

    def everywhere(self, value):
        return {"feed": value, "list": value, "profile": value, "detail": value}


class CreatingMediaPostsTests(MediaPostTestMixin, APITestCase):
    def test_a_photo_and_a_caption_are_a_post(self):
        response = self.publish_photo(caption="  Sunrise before the long run  ")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["kind"], "media")
        self.assertEqual(response.data["caption"], "Sunrise before the long run")
        self.assertTrue(response.data["image_url"])
        self.assertTrue(response.data["feed_image_url"])
        for snapshot in ("workout", "meal", "planner", "source_id", "video"):
            self.assertIsNone(response.data[snapshot], snapshot)
        post = Post.objects.get(pk=response.data["id"])
        self.assertIsNone(post.source_session_id)
        self.assertIsNone(post.source_meal_id)
        self.assertIsNone(post.source_planner_entry_id)

    def test_a_photo_post_is_seen_wherever_its_visibility_allows(self):
        post_id = self.publish_photo(caption="flowers").data["id"]
        self.assertEqual(self.seen_by(self.author_token, post_id), self.everywhere(True))
        self.assertEqual(self.seen_by(self.follower_token, post_id), self.everywhere(True))
        self.assertTrue(self.seen_by(self.stranger_token, post_id)["detail"])

    def test_visibility_still_applies(self):
        followers_only = self.publish_photo(visibility="followers").data["id"]
        private = self.publish_photo(visibility="private").data["id"]
        self.assertTrue(self.seen_by(self.follower_token, followers_only)["detail"])
        self.assertEqual(self.seen_by(self.stranger_token, followers_only), self.everywhere(False))
        self.assertEqual(self.seen_by(self.follower_token, private), self.everywhere(False))
        self.assertTrue(self.seen_by(self.author_token, private)["detail"])

    def test_nothing_to_show_is_refused(self):
        for body in ({}, {"caption": "just words"}, {"image_base64": ""},
                     {"expects_video": False, "caption": "x"}):
            with self.subTest(body=body):
                response = self.publish(**body)
                self.assertEqual(response.status_code, 400)
                self.assertIn("image_base64", response.data)
        self.assertFalse(Post.objects.exists())

    def test_a_media_post_is_not_made_from_a_source(self):
        session = finished_session(self.author)
        response = self.publish_photo(source_id=session.pk)
        self.assertEqual(response.status_code, 400)
        self.assertIn("source_id", response.data)
        self.assertFalse(Post.objects.exists())

    def test_the_other_kinds_still_need_their_source(self):
        for kind in ("workout", "meal", "planner"):
            with self.subTest(kind=kind):
                response = self.client.post(POSTS, {"kind": kind, "caption": "x"}, format="json")
                self.assertEqual(response.status_code, 400)
                self.assertIn("source_id", response.data)
        self.assertFalse(Post.objects.exists())

    def test_a_photo_on_a_workout_is_still_a_workout_post(self):
        session = finished_session(self.author)
        response = self.client.post(
            POSTS,
            {"kind": "workout", "source_id": session.pk, "content_type": "image/jpeg",
             "image_base64": as_base64(photo_bytes())},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["kind"], "workout")
        self.assertTrue(response.data["workout"])
        self.assertTrue(response.data["image_url"])

    def test_bad_photos_are_refused_as_they_are_on_any_post(self):
        response = self.publish(content_type="image/jpeg", image_base64=as_base64(b"%PDF-1.4"))
        self.assertEqual(response.status_code, 400)
        response = self.publish(image_base64=as_base64(photo_bytes()))
        self.assertEqual(response.status_code, 400)
        self.assertIn("content_type", response.data)
        self.assertFalse(Post.objects.exists())

    def test_a_repost_still_cannot_be_made_here(self):
        response = self.client.post(POSTS, {"kind": "repost"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("kind", response.data)

    def test_the_caption_can_be_found_by_search(self):
        post_id = self.publish_photo(caption="Trail at Mount Tam").data["id"]
        self.assertIn(post_id, self.ids_in(f"{POSTS}?search=mount%20tam", self.stranger_token))

    def test_its_caption_and_visibility_can_still_be_edited(self):
        post_id = self.publish_photo(caption="first").data["id"]
        response = self.client.patch(
            f"{POSTS}{post_id}/", {"caption": "second", "visibility": "followers"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["caption"], "second")
        self.assertEqual(response.data["visibility"], "followers")

    def test_it_can_be_reposted_and_the_repost_shows_the_photo(self):
        post_id = self.publish_photo(caption="flowers").data["id"]
        self.authenticate(self.follower_token)
        response = self.client.post(f"{POSTS}{post_id}/repost/")
        self.assertIn(response.status_code, (200, 201), response.data)
        repost = next(
            post for post in self.client.get(FEED).data["results"]
            if post["kind"] == "repost"
        )
        self.assertEqual(repost["repost_of"]["kind"], "media")
        self.assertTrue(repost["repost_of"]["image_url"])

    def test_it_can_reach_for_you(self):
        post_id = self.publish_photo(caption="flowers").data["id"]
        self.authenticate(self.stranger_token)
        response = self.client.get(FOR_YOU)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn(post_id, {post["id"] for post in response.data["results"]})


@override_settings(VIDEO_REVIEW_REQUIRED=False)
class ClipOnlyPostTests(MediaPostTestMixin, APITestCase):
    """A post that is only a clip, uploaded straight to storage."""

    def test_it_is_its_authors_alone_until_the_clip_arrives(self):
        created = self.publish(caption="first ride", expects_video=True)
        self.assertEqual(created.status_code, 201, created.data)
        post_id = created.data["id"]
        self.assertIsNone(created.data["image_url"])
        self.assertIsNone(created.data["video"])

        self.assertEqual(self.seen_by(self.author_token, post_id), self.everywhere(True))
        for token in (self.follower_token, self.stranger_token):
            self.assertEqual(self.seen_by(token, post_id), self.everywhere(False))

        uploaded = self.upload(post_id)
        self.assertEqual(uploaded.status_code, 200, uploaded.data)
        self.assertEqual(uploaded.data["video"]["status"], "approved")
        self.assertEqual(self.seen_by(self.follower_token, post_id), self.everywhere(True))
        self.assertTrue(self.seen_by(self.stranger_token, post_id)["detail"])
        self.authenticate(self.follower_token)
        self.assertTrue(self.client.get(f"{POSTS}{post_id}/").data["video"]["url"])

    def test_nobody_else_can_repost_or_report_or_like_it_meanwhile(self):
        post_id = self.publish(expects_video=True).data["id"]
        self.authenticate(self.follower_token)
        self.assertEqual(self.client.post(f"{POSTS}{post_id}/repost/").status_code, 404)
        self.assertEqual(self.client.post(f"{POSTS}{post_id}/like/").status_code, 404)
        self.assertEqual(
            self.client.post(f"{POSTS}{post_id}/report/", {"reason": "spam"}, format="json").status_code,
            404,
        )

    def test_removing_the_clip_takes_it_back_out_of_sight(self):
        post_id = self.publish(expects_video=True).data["id"]
        self.upload(post_id)
        self.assertTrue(self.seen_by(self.follower_token, post_id)["detail"])
        self.authenticate(self.author_token)
        self.assertEqual(self.client.delete(f"{POSTS}{post_id}/video/").status_code, 200)
        self.assertEqual(self.seen_by(self.follower_token, post_id), self.everywhere(False))
        self.assertTrue(self.seen_by(self.author_token, post_id)["detail"])

    def test_a_refused_clip_leaves_the_post_saved_for_a_retry(self):
        post_id = self.publish(expects_video=True).data["id"]
        refused = self.upload(post_id, name="vp9.webm")
        self.assertEqual(refused.status_code, 400)
        self.assertTrue(Post.objects.filter(pk=post_id).exists())
        self.assertEqual(self.seen_by(self.follower_token, post_id), self.everywhere(False))
        retried = self.upload(post_id)
        self.assertEqual(retried.status_code, 200, retried.data)
        self.assertEqual(Post.objects.filter(author=self.author).count(), 1)
        self.assertTrue(self.seen_by(self.follower_token, post_id)["detail"])

    def test_with_a_photo_as_well_the_photo_shows_at_once(self):
        post_id = self.publish_photo(expects_video=True).data["id"]
        self.assertEqual(self.seen_by(self.follower_token, post_id), self.everywhere(True))

    def test_the_clip_counts_as_a_video_post_for_for_you(self):
        post_id = self.publish(expects_video=True, caption="ride").data["id"]
        self.authenticate(self.stranger_token)
        self.assertNotIn(post_id, {p["id"] for p in self.client.get(FOR_YOU).data["results"]})
        self.upload(post_id)
        self.authenticate(self.stranger_token)
        self.assertIn(post_id, {p["id"] for p in self.client.get(FOR_YOU).data["results"]})


@override_settings(VIDEO_REVIEW_REQUIRED=True)
class ClipOnlyPostUnderReviewTests(MediaPostTestMixin, APITestCase):
    def test_it_waits_for_the_moderator_and_appears_on_approval(self):
        post_id = self.publish(expects_video=True).data["id"]
        uploaded = self.upload(post_id)
        self.assertEqual(uploaded.data["video"]["status"], "pending")
        self.assertEqual(self.seen_by(self.follower_token, post_id), self.everywhere(False))
        self.assertEqual(self.seen_by(self.author_token, post_id), self.everywhere(True))

        PostVideo.objects.filter(post_id=post_id).update(status=PostVideo.Status.APPROVED)
        self.assertEqual(self.seen_by(self.follower_token, post_id), self.everywhere(True))

    def test_a_rejected_clip_never_shows(self):
        post_id = self.publish(expects_video=True).data["id"]
        self.upload(post_id)
        PostVideo.objects.filter(post_id=post_id).update(status=PostVideo.Status.REJECTED)
        self.assertEqual(self.seen_by(self.follower_token, post_id), self.everywhere(False))
        self.authenticate(self.author_token)
        self.assertEqual(self.client.get(f"{POSTS}{post_id}/").data["video"]["status"], "rejected")


@override_settings(VIDEO_PIPELINE_ENABLED=True, VIDEO_STORAGE_BACKEND="local",
                   VIDEO_MODERATION_BYPASS=True, VIDEO_LOCAL_ACCEL_PREFIX="",
                   VIDEO_DIRECT_MAX_BYTES=15 * 1024 * 1024, VIDEO_REVIEW_REQUIRED=False)
class ClipOnlyPostPipelineTests(MediaPostTestMixin, APITestCase):
    """The deployed path: the upload is queued and encoded later."""

    def setUp(self):
        super().setUp()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        override = override_settings(VIDEO_LOCAL_ROOT=self.directory.name)
        override.enable()
        self.addCleanup(override.disable)

    def fake_transcode(self, source, directory):
        path = Path(directory) / "854.mp4"
        path.write_bytes(video_fixture("h264_faststart.mp4"))
        thumbnail = Path(directory) / "thumbnail.jpg"
        thumbnail.write_bytes(b"jpeg")
        return [{"path": path, "width": 64, "height": 48, "size_bytes": path.stat().st_size,
                 "duration_ms": 1000, "has_audio": True}], thumbnail

    def run_worker(self, transcode=None):
        from .video_pipeline import claim, process

        job = claim()
        self.assertIsNotNone(job)
        with mock.patch("core.video_pipeline.transcode", side_effect=transcode or self.fake_transcode):
            return process(job)

    def test_processing_then_ready_then_seen(self):
        post_id = self.publish(caption="ride", expects_video=True).data["id"]
        uploaded = self.upload(post_id)
        self.assertEqual(uploaded.status_code, 200, uploaded.data)
        self.assertEqual(uploaded.data["video"]["processing_state"], "processing")
        self.assertIsNone(uploaded.data["video"]["url"])
        self.assertEqual(self.seen_by(self.follower_token, post_id), self.everywhere(False))

        self.assertTrue(self.run_worker())
        self.assertEqual(self.seen_by(self.follower_token, post_id), self.everywhere(True))
        self.authenticate(self.follower_token)
        clip = self.client.get(f"{POSTS}{post_id}/").data["video"]
        self.assertEqual(clip["processing_state"], "ready")
        self.assertTrue(clip["url"])
        self.assertTrue(clip["thumbnail_url"])

    def test_a_failed_encode_stays_hidden_and_a_retry_reuses_the_post(self):
        from .video_transcode import InvalidVideo

        post_id = self.publish(expects_video=True).data["id"]
        self.upload(post_id)

        def broken(source, directory):
            raise InvalidVideo("unreadable")

        self.assertFalse(self.run_worker(transcode=broken))
        self.authenticate(self.author_token)
        mine = self.client.get(f"{POSTS}{post_id}/").data["video"]
        self.assertEqual(mine["processing_state"], "failed")
        self.assertEqual(self.seen_by(self.follower_token, post_id), self.everywhere(False))

        # What the clients do on "Retry video": take the failed clip off, then
        # upload again to the same post.
        self.authenticate(self.author_token)
        self.assertEqual(self.client.delete(f"{POSTS}{post_id}/video/").status_code, 200)
        self.assertEqual(self.upload(post_id).status_code, 200)
        self.assertTrue(self.run_worker())
        self.assertEqual(Post.objects.filter(author=self.author).count(), 1)
        self.assertEqual(self.seen_by(self.follower_token, post_id), self.everywhere(True))

    def test_a_clip_nobody_else_can_see_cannot_be_played_by_them(self):
        from django.test import RequestFactory

        from .video_pipeline_views import asset_url, serve_asset

        post_id = self.publish(expects_video=True).data["id"]
        self.upload(post_id)
        self.assertTrue(self.run_worker())
        video = PostVideo.objects.get(post_id=post_id)
        PostVideo.objects.filter(pk=video.pk).update(status=PostVideo.Status.PENDING)

        factory = RequestFactory()
        request = factory.get("/")
        request.user = self.follower.user
        url = asset_url(video, video.variants[0]["key"], request)
        playback = factory.get(url)
        self.assertEqual(serve_asset(playback).status_code, 403)
