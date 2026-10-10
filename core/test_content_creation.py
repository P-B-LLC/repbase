"""Publishing through the API: posts, their photos, and comments.

Most of the existing post tests build rows directly and then read them back.
These make them the way a client does, through POST /social/posts/, so the
validation, the ownership checks and the upload handling are what is under
test -- including the inputs a client should never send.
"""

import io
from unittest import mock

from django.core.files.storage import default_storage
from rest_framework.test import APITestCase

from .fixtures_for_tests import (
    TemporaryMediaMixin,
    as_base64,
    finished_session,
    gps_exif,
    meal_with_foods,
    photo_bytes,
    planner_entry,
    workout_post,
)
from .models import Follow, Post, PostComment, WorkoutSession
from .tests import RepbaseAPITestMixin

POSTS = "/api/v1/social/posts/"
COMMENTS = "/api/v1/social/comments/"


class CreatingPostsTests(RepbaseAPITestMixin, APITestCase):
    def setUp(self):
        self.user, self.author, self.token = self.create_account("poster")
        self.authenticate(self.token)

    def post(self, **body):
        return self.client.post(POSTS, body, format="json")

    def test_a_finished_workout_becomes_a_post_built_from_the_servers_numbers(self):
        session = finished_session(self.author, weight="140.00", reps=3)
        response = self.post(kind="workout", source_id=session.pk, caption="  New PR  ")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["caption"], "New PR")
        lift = response.data["workout"]["exercises"][0]
        self.assertEqual(lift["name"], "Back Squat")
        self.assertEqual(lift["top_set_weight_kg"], "140.00")
        self.assertEqual(response.data["source_id"], session.pk)

    def test_numbers_sent_by_the_client_are_ignored(self):
        session = finished_session(self.author, weight="60.00")
        response = self.post(
            kind="workout", source_id=session.pk,
            workout={"exercises": [{"name": "Squat", "top_set_weight_kg": "500.00"}]},
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["workout"]["exercises"][0]["top_set_weight_kg"], "60.00")

    def test_weights_can_be_held_back(self):
        session = finished_session(self.author)
        response = self.post(kind="workout", source_id=session.pk, shows_weights=False)
        self.assertEqual(response.status_code, 201)
        self.assertIsNone(response.data["workout"]["exercises"][0]["top_set_weight_kg"])

    def test_an_unfinished_session_cannot_be_posted(self):
        session = finished_session(self.author)
        WorkoutSession.objects.filter(pk=session.pk).update(status=WorkoutSession.Status.ACTIVE)
        response = self.post(kind="workout", source_id=session.pk)
        self.assertEqual(response.status_code, 400)
        self.assertIn("source_id", response.data)

    def test_somebody_elses_session_reads_exactly_like_a_missing_one(self):
        _, stranger, _ = self.create_account("stranger")
        theirs = finished_session(stranger)
        borrowed = self.post(kind="workout", source_id=theirs.pk)
        missing = self.post(kind="workout", source_id=999_999)
        self.assertEqual(borrowed.status_code, 400)
        self.assertEqual(borrowed.data, missing.data)
        self.assertFalse(Post.objects.filter(author=self.author).exists())

    def test_a_meal_and_a_planner_entry_can_be_posted(self):
        meal = meal_with_foods(self.author)
        response = self.post(kind="meal", source_id=meal.pk, cooking_instructions="Stir.")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(len(response.data["meal"]["entries"]), 2)
        entry = planner_entry(self.author)
        response = self.post(kind="planner", source_id=entry.pk)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["planner"]["title"], "Deload week")

    def test_an_empty_meal_has_nothing_to_show(self):
        meal = meal_with_foods(self.author, foods=())
        response = self.post(kind="meal", source_id=meal.pk)
        self.assertEqual(response.status_code, 400)

    def test_a_repost_cannot_be_made_through_create(self):
        """This used to build a repost of nothing and answer 500."""
        entry = planner_entry(self.author)
        response = self.post(kind="repost", source_id=entry.pk)
        self.assertEqual(response.status_code, 400)
        self.assertIn("kind", response.data)
        self.assertFalse(Post.objects.exists())

    def test_invalid_fields_are_refused_with_the_field_named(self):
        session = finished_session(self.author)
        for body, field in [
            ({"kind": "story", "source_id": session.pk}, "kind"),
            ({"kind": "workout", "source_id": 0}, "source_id"),
            ({"kind": "workout", "source_id": "abc"}, "source_id"),
            ({"kind": "workout", "source_id": session.pk, "caption": "x" * 301}, "caption"),
            ({"kind": "workout", "source_id": session.pk, "visibility": "everyone"}, "visibility"),
            ({"kind": "meal", "source_id": session.pk, "cooking_instructions": "x" * 4001},
             "cooking_instructions"),
        ]:
            with self.subTest(field=field, body=str(body)[:40]):
                response = self.post(**body)
                self.assertEqual(response.status_code, 400)
                self.assertIn(field, response.data)
        self.assertFalse(Post.objects.exists())

    def test_publishing_needs_a_session(self):
        self.client.credentials()
        self.assertEqual(self.post(kind="workout", source_id=1).status_code, 401)

    def test_publishing_is_rate_limited(self):
        from rest_framework.throttling import SimpleRateThrottle

        entry = planner_entry(self.author)
        with mock.patch.dict(SimpleRateThrottle.THROTTLE_RATES, {"post": "2/hour"}):
            codes = [self.post(kind="planner", source_id=entry.pk).status_code for _ in range(3)]
            # Reading is not publishing, and does not spend the same limit.
            reads = [self.client.get(POSTS).status_code for _ in range(3)]
        self.assertEqual(codes, [201, 201, 429])
        self.assertEqual(reads, [200, 200, 200])


class EditingPostsTests(RepbaseAPITestMixin, APITestCase):
    def setUp(self):
        _, self.author, self.author_token = self.create_account("owner")
        _, self.other, self.other_token = self.create_account("other")
        self.post = workout_post(self.author)

    def test_the_author_can_edit_the_caption_and_visibility(self):
        self.authenticate(self.author_token)
        response = self.client.patch(
            f"{POSTS}{self.post.pk}/", {"caption": "edited", "visibility": "followers"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.post.refresh_from_db()
        self.assertEqual((self.post.caption, self.post.visibility), ("edited", "followers"))

    def test_nobody_else_can_edit_or_delete_it(self):
        self.authenticate(self.other_token)
        edit = self.client.patch(f"{POSTS}{self.post.pk}/", {"caption": "hijacked"}, format="json")
        delete = self.client.delete(f"{POSTS}{self.post.pk}/")
        self.assertEqual((edit.status_code, delete.status_code), (404, 404))
        self.post.refresh_from_db()
        self.assertNotEqual(self.post.caption, "hijacked")

    def test_the_snapshot_cannot_be_replaced_with_put(self):
        self.authenticate(self.author_token)
        response = self.client.put(f"{POSTS}{self.post.pk}/", {"caption": "x"}, format="json")
        self.assertEqual(response.status_code, 405)

    def test_the_author_can_delete_it(self):
        self.authenticate(self.author_token)
        self.assertEqual(self.client.delete(f"{POSTS}{self.post.pk}/").status_code, 204)
        self.assertFalse(Post.objects.filter(pk=self.post.pk).exists())


class PostPhotoUploadTests(TemporaryMediaMixin, RepbaseAPITestMixin, APITestCase):
    def setUp(self):
        self.user, self.author, self.token = self.create_account("photographer")
        self.authenticate(self.token)
        self.entry = planner_entry(self.author)

    def publish(self, data, content_type="image/jpeg"):
        return self.client.post(
            POSTS,
            {"kind": "planner", "source_id": self.entry.pk, "content_type": content_type,
             "image_base64": as_base64(data)},
            format="json",
        )

    def stored(self, post_id):
        post = Post.objects.get(pk=post_id)
        with default_storage.open(post.image.name, "rb") as handle:
            return post.image.name, handle.read()

    def test_a_photo_is_stored_without_its_location(self):
        from PIL import Image

        response = self.publish(photo_bytes(exif=gps_exif(orientation=6)))
        self.assertEqual(response.status_code, 201, response.data)
        name, stored = self.stored(response.data["id"])
        with Image.open(io.BytesIO(stored)) as image:
            exif = image.getexif()
            self.assertEqual(exif.get_ifd(0x8825), {}, "GPS must be gone")
            self.assertEqual(exif.get(0x0112), 6, "orientation must survive")
        self.assertNotIn(b"Model-With-Serial", stored)
        self.assertTrue(response.data["image_url"])

    def test_the_stored_type_comes_from_the_bytes_not_the_label(self):
        response = self.publish(photo_bytes(fmt="PNG"), content_type="image/jpeg")
        self.assertEqual(response.status_code, 201, response.data)
        name, _ = self.stored(response.data["id"])
        self.assertTrue(name.endswith(".png"), name)

    def test_files_that_are_not_images_are_refused(self):
        for data in [b"%PDF-1.4 not an image", b"<svg onload=alert(1)>", b"MZ\x90\x00 an executable",
                     photo_bytes()[:200]]:
            with self.subTest(head=data[:10]):
                response = self.publish(data)
                self.assertEqual(response.status_code, 400)
        self.assertFalse(Post.objects.exists())

    def test_unsupported_formats_are_refused(self):
        response = self.publish(photo_bytes(fmt="GIF"), content_type="image/png")
        self.assertEqual(response.status_code, 400)

    def test_oversized_and_malformed_uploads_are_refused(self):
        too_big = self.client.post(
            POSTS,
            {"kind": "planner", "source_id": self.entry.pk, "content_type": "image/jpeg",
             "image_base64": as_base64(b"\xff\xd8" + b"\x00" * (5 * 1024 * 1024 + 1))},
            format="json",
        )
        self.assertEqual(too_big.status_code, 400)
        not_base64 = self.client.post(
            POSTS,
            {"kind": "planner", "source_id": self.entry.pk, "content_type": "image/jpeg",
             "image_base64": "@@@not base64@@@"},
            format="json",
        )
        self.assertEqual(not_base64.status_code, 400)
        no_type = self.client.post(
            POSTS,
            {"kind": "planner", "source_id": self.entry.pk, "image_base64": as_base64(photo_bytes())},
            format="json",
        )
        self.assertEqual(no_type.status_code, 400)
        self.assertIn("content_type", no_type.data)

    def test_a_decompression_bomb_is_refused_from_its_header(self):
        """A tiny file can declare an enormous canvas; it must be refused
        before anything decodes it."""
        from PIL import Image

        buffer = io.BytesIO()
        Image.new("1", (9000, 9000)).save(buffer, format="PNG")
        self.assertLess(len(buffer.getvalue()), 5 * 1024 * 1024)
        response = self.publish(buffer.getvalue(), content_type="image/png")
        self.assertEqual(response.status_code, 400)

    def test_a_profile_photo_loses_its_location_too(self):
        response = self.client.put(
            "/api/v1/me/photo/",
            {"content_type": "image/jpeg", "image_base64": as_base64(photo_bytes(exif=gps_exif()))},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.author.refresh_from_db()
        with default_storage.open(self.author.profile_photo.name, "rb") as handle:
            stored = handle.read()
        self.assertNotIn(b"Model-With-Serial", stored)
        self.assertNotIn(b"GPS", stored)


class CommentTests(RepbaseAPITestMixin, APITestCase):
    def setUp(self):
        _, self.author, self.author_token = self.create_account("author")
        _, self.reader, self.reader_token = self.create_account("reader")
        self.post = workout_post(self.author)
        self.authenticate(self.reader_token)

    def comment(self, body="Nice lift", **extra):
        return self.client.post(COMMENTS, {"post": self.post.pk, "body": body, **extra}, format="json")

    def test_a_reader_can_comment_and_reply(self):
        first = self.comment()
        self.assertEqual(first.status_code, 201, first.data)
        reply = self.comment(body="Thanks", parent=first.data["id"])
        self.assertEqual(reply.status_code, 201, reply.data)

    def test_a_reply_to_a_reply_is_refused(self):
        first = self.comment()
        reply = self.comment(parent=first.data["id"])
        deeper = self.comment(parent=reply.data["id"])
        self.assertEqual(deeper.status_code, 400)

    def test_commenting_on_a_post_you_cannot_see_reads_as_missing(self):
        private = workout_post(self.author, visibility="private")
        response = self.client.post(COMMENTS, {"post": private.pk, "body": "hi"}, format="json")
        self.assertIn(response.status_code, (400, 404))
        self.assertFalse(PostComment.objects.filter(post=private).exists())

    def test_followers_only_posts_need_a_follow(self):
        closed = workout_post(self.author, visibility="followers")
        before = self.client.post(COMMENTS, {"post": closed.pk, "body": "hi"}, format="json")
        Follow.objects.create(follower=self.reader, following=self.author)
        after = self.client.post(COMMENTS, {"post": closed.pk, "body": "hi"}, format="json")
        self.assertIn(before.status_code, (400, 404))
        self.assertEqual(after.status_code, 201)

    def test_bodies_are_bounded_and_required(self):
        self.assertEqual(self.comment(body="").status_code, 400)
        self.assertEqual(self.comment(body="x" * 1001).status_code, 400)

    def test_only_the_commenter_can_edit_or_delete(self):
        created = self.comment().data["id"]
        self.authenticate(self.author_token)
        edit = self.client.patch(f"{COMMENTS}{created}/", {"body": "changed"}, format="json")
        delete = self.client.delete(f"{COMMENTS}{created}/")
        self.assertEqual((edit.status_code, delete.status_code), (404, 404))

    def test_a_comment_cannot_be_moved_to_another_post(self):
        created = self.comment().data["id"]
        elsewhere = workout_post(self.author)
        response = self.client.patch(f"{COMMENTS}{created}/", {"post": elsewhere.pk}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_commenting_is_rate_limited(self):
        from rest_framework.throttling import SimpleRateThrottle

        with mock.patch.dict(SimpleRateThrottle.THROTTLE_RATES, {"comment": "2/hour"}):
            codes = [self.comment().status_code for _ in range(3)]
        self.assertEqual(codes, [201, 201, 429])
