"""Reporting content and acting on reports.

Comment reports had tests; post reports, the more common case, had none.
These follow a report from the button to the moderator's decision and out to
everywhere the post could otherwise still be seen.
"""

from unittest import mock

from django.contrib.admin import AdminSite
from django.test import RequestFactory
from rest_framework.test import APITestCase
from rest_framework.throttling import SimpleRateThrottle

from .admin import PostReportAdmin
from .fixtures_for_tests import TemporaryMediaMixin, as_base64, photo_bytes, planner_entry, workout_post
from .models import CommentReport, Follow, Post, PostComment, PostReport
from .recommendations import eligible_posts
from .tests import RepbaseAPITestMixin


class ReportingAPostTests(RepbaseAPITestMixin, APITestCase):
    def setUp(self):
        _, self.author, self.author_token = self.create_account("reported_author")
        _, self.reporter, self.reporter_token = self.create_account("reporter")
        self.post = workout_post(self.author)
        self.authenticate(self.reporter_token)

    def report(self, post=None, **body):
        body.setdefault("reason", "harassment")
        return self.client.post(f"/api/v1/social/posts/{(post or self.post).pk}/report/", body, format="json")

    def test_a_report_is_filed_once_and_a_second_press_is_not_an_error(self):
        first = self.report(detail="They keep messaging me")
        second = self.report(reason="spam")
        self.assertEqual((first.status_code, second.status_code), (201, 200))
        self.assertFalse(first.data["already_reported"])
        self.assertTrue(second.data["already_reported"])
        stored = PostReport.objects.get()
        self.assertEqual((stored.reason, stored.reporter), ("harassment", self.reporter))

    def test_the_detail_stays_private(self):
        response = self.report(detail="private context for the moderator")
        self.assertNotIn("private context", str(response.data))
        self.authenticate(self.author_token)
        card = self.client.get(f"/api/v1/social/posts/{self.post.pk}/")
        self.assertNotIn("private context", str(card.content))

    def test_the_reason_must_come_from_the_list_and_the_detail_is_bounded(self):
        self.assertEqual(self.report(reason="i_dont_like_it").status_code, 400)
        self.assertEqual(self.report(detail="x" * 501).status_code, 400)
        self.assertEqual(self.client.post(f"/api/v1/social/posts/{self.post.pk}/report/", {}, format="json").status_code, 400)
        self.assertFalse(PostReport.objects.exists())

    def test_your_own_post_cannot_be_reported(self):
        self.authenticate(self.author_token)
        self.assertEqual(self.report().status_code, 400)

    def test_a_post_you_cannot_see_cannot_be_reported(self):
        private = workout_post(self.author, visibility="private")
        self.assertEqual(self.report(post=private).status_code, 404)

    def test_reporting_needs_a_session(self):
        self.client.credentials()
        self.assertEqual(self.report().status_code, 401)

    def test_reporting_is_rate_limited(self):
        posts = [workout_post(self.author) for _ in range(3)]
        with mock.patch.dict(SimpleRateThrottle.THROTTLE_RATES, {"report": "2/hour"}):
            codes = [self.report(post=post).status_code for post in posts]
        self.assertEqual(codes, [201, 201, 429])

    def test_a_report_takes_the_post_off_the_reporters_recommendations(self):
        self.assertTrue(eligible_posts(self.reporter).filter(pk=self.post.pk).exists())
        self.report()
        self.assertFalse(eligible_posts(self.reporter).filter(pk=self.post.pk).exists())

    def test_reports_are_not_moderated_themselves(self):
        """Quoting abuse in a report must not get the report refused."""
        with mock.patch("core.moderation.check_public_content", side_effect=AssertionError("must not run")):
            self.assertEqual(self.report(detail="quoted slur here").status_code, 201)


class ModeratorTakedownTests(TemporaryMediaMixin, RepbaseAPITestMixin, APITestCase):
    def setUp(self):
        _, self.author, self.author_token = self.create_account("offender")
        _, self.reporter, self.reporter_token = self.create_account("witness")
        _, self.follower, self.follower_token = self.create_account("fan")
        Follow.objects.create(follower=self.follower, following=self.author)
        self.authenticate(self.author_token)
        created = self.client.post(
            "/api/v1/social/posts/",
            {"kind": "planner", "source_id": planner_entry(self.author).pk,
             "content_type": "image/png", "image_base64": as_base64(photo_bytes(fmt="PNG"))},
            format="json",
        )
        self.assertEqual(created.status_code, 201, created.data)
        self.post = Post.objects.get(pk=created.data["id"])
        self.photo_url = created.data["image_url"].replace("http://testserver", "")
        self.authenticate(self.reporter_token)
        self.client.post(f"/api/v1/social/posts/{self.post.pk}/report/", {"reason": "hate"}, format="json")

    def hide_through_the_admin(self):
        moderator = self.create_account("moderator")[0]
        moderator.is_staff = moderator.is_superuser = True
        moderator.save()
        request = RequestFactory().post("/admin/")
        request.user = moderator
        request._messages = mock.MagicMock()
        PostReportAdmin(PostReport, AdminSite()).hide_reported_posts(request, PostReport.objects.all())
        return moderator

    def test_hiding_closes_the_report_and_takes_the_post_down_everywhere(self):
        self.assertEqual(self.client.get(self.photo_url).status_code, 200)
        moderator = self.hide_through_the_admin()

        report = PostReport.objects.get()
        self.assertEqual((report.resolution, report.reviewed_by), ("hidden", moderator))
        for token in (self.follower_token, self.reporter_token, self.author_token):
            self.authenticate(token)
            self.assertEqual(self.client.get(f"/api/v1/social/posts/{self.post.pk}/").status_code, 404)
            self.assertNotIn(self.post.pk, [row["id"] for row in self.client.get("/api/v1/social/feed/").data["results"]])
            self.assertNotIn(self.post.pk, [row["id"] for row in self.client.get("/api/v1/social/for-you/").data["results"]])
        self.assertEqual(self.client.get(self.photo_url).status_code, 403)


class SuspensionTests(RepbaseAPITestMixin, APITestCase):
    """Suspending an account used to stop its token and leave everything it
    had posted -- and every comment it had made -- in place for everyone."""

    def setUp(self):
        _, self.abuser, _ = self.create_account("abuser")
        _, self.reader, self.reader_token = self.create_account("reader")
        _, self.bystander, _ = self.create_account("bystander")
        Follow.objects.create(follower=self.reader, following=self.abuser)
        self.post = workout_post(self.abuser)
        self.victim_post = workout_post(self.bystander)
        self.comment = PostComment.objects.create(post=self.victim_post, author=self.abuser, body="abuse")
        self.authenticate(self.reader_token)

    def suspend(self):
        self.abuser.user.is_active = False
        self.abuser.user.save(update_fields=["is_active"])

    def test_their_posts_and_comments_disappear(self):
        self.suspend()
        self.assertEqual(self.client.get(f"/api/v1/social/posts/{self.post.pk}/").status_code, 404)
        self.assertNotIn(self.post.pk, [row["id"] for row in self.client.get("/api/v1/social/feed/").data["results"]])
        comments = self.client.get("/api/v1/social/comments/", {"post": self.victim_post.pk})
        self.assertEqual(comments.data["results"], [])
        card = self.client.get(f"/api/v1/social/posts/{self.victim_post.pk}/")
        self.assertEqual(card.data["comment_count"], 0)

    def test_reinstating_brings_it_all_back(self):
        self.suspend()
        self.abuser.user.is_active = True
        self.abuser.user.save(update_fields=["is_active"])
        self.assertEqual(self.client.get(f"/api/v1/social/posts/{self.post.pk}/").status_code, 200)

    def test_their_comments_can_still_be_reported_before_suspension(self):
        response = self.client.post(
            f"/api/v1/social/comments/{self.comment.pk}/report/", {"reason": "harassment"}, format="json"
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(CommentReport.objects.count(), 1)
