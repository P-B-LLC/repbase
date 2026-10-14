"""Regression tests for the third security audit, October 2026.

As in `test_security_audit`, each class pins one finding, its docstring says
what could be done before the fix, and the tests are written as the attack:
they failed against the code as it was and pass against the fix.
"""

import os
from unittest import mock

from django.conf import settings
from django.core.files.storage import default_storage
from rest_framework.test import APITestCase
from rest_framework.throttling import SimpleRateThrottle

from .access_models import AccountAccess
from .fixtures_for_tests import TemporaryMediaMixin, as_base64, meal_post, photo_bytes
from .models import (
    Block,
    Follow,
    FollowRequest,
    Gym,
    Notification,
    PendingMediaDeletion,
    PostComment,
    PostReport,
    SessionRoutePoint,
    WorkoutSession,
)
from .tests import RepbaseAPITestMixin


class ImpossibleDateTests(RepbaseAPITestMixin, APITestCase):
    """Three filters answered 500 to a date in the right shape that does not
    exist, such as 30 February.

    They used Django's `parse_date`, which raises ValueError for those rather
    than returning None; the eight filters fixed in the first audit had moved
    to `core.params` and these three had not.
    """

    PATHS = (
        ("/api/v1/sessions/", "since"),
        ("/api/v1/step-counts/", "since"),
        ("/api/v1/sessions/training-stats/", "today"),
    )

    def setUp(self):
        self.user, self.profile, self.token = self.create_account("dates")
        self.authenticate(self.token)

    def test_an_impossible_date_is_a_400_naming_the_parameter(self):
        for path, name in self.PATHS:
            with self.subTest(path=path):
                response = self.client.get(path, {name: "2026-02-30"})
                self.assertEqual(response.status_code, 400)
                self.assertIn(name, response.data)

    def test_a_real_date_still_filters(self):
        for path, name in self.PATHS:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path, {name: "2026-02-28"}).status_code, 200)


class RouteCeilingTests(RepbaseAPITestMixin, APITestCase):
    """A session took 20,000 more GPS points on every upload, without end.

    Each upload also re-read every point already stored to recompute the
    summary, so uploads grew slower as the track grew: against a dev server,
    20,000 points took 2.7 s and the fifth batch 9 s, with the per-user
    ceiling allowing 1,200 such requests an hour. Any account could fill the
    database and hold workers.
    """

    def setUp(self):
        self.user, self.profile, self.token = self.create_account("runner")
        self.authenticate(self.token)
        self.session = WorkoutSession.objects.create(
            repbase_user=self.profile, status=WorkoutSession.Status.ACTIVE
        )
        self.url = f"/api/v1/sessions/{self.session.pk}/route/"

    def points(self, start, count):
        return [
            {
                "latitude": "41.000000",
                "longitude": f"-87.{index:06d}",
                "recorded_at": f"2026-10-01T10:{index // 60 % 60:02d}:{index % 60:02d}Z",
            }
            for index in range(start, start + count)
        ]

    def upload(self, points):
        return self.client.post(self.url, {"points": points}, format="json")

    def test_a_session_cannot_grow_past_the_ceiling(self):
        with mock.patch("core.views.MAX_ROUTE_POINTS_PER_SESSION", 10):
            self.assertEqual(self.upload(self.points(0, 6)).status_code, 200)
            response = self.upload(self.points(6, 6))
        self.assertEqual(response.status_code, 400)
        self.assertIn("points", response.data)
        self.assertEqual(SessionRoutePoint.objects.filter(session=self.session).count(), 6)

    def test_a_retry_of_stored_points_does_not_reread_the_track(self):
        track = self.points(0, 5)
        self.assertEqual(self.upload(track).status_code, 200)
        with mock.patch.object(WorkoutSession, "recompute_route_summary") as recompute:
            response = self.upload(track)
        self.assertEqual(response.status_code, 200)
        recompute.assert_not_called()

    def test_uploads_have_a_limit_of_their_own_and_reads_do_not(self):
        with mock.patch.dict(SimpleRateThrottle.THROTTLE_RATES, {"route": "2/hour"}):
            self.assertEqual(self.upload(self.points(0, 2)).status_code, 200)
            self.assertEqual(self.upload(self.points(2, 2)).status_code, 200)
            self.assertEqual(self.upload(self.points(4, 2)).status_code, 429)
            for _ in range(3):
                self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_the_production_limits_are_declared(self):
        rates = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]
        self.assertIn("route", rates)
        self.assertIn("photo", rates)


class FollowRequestBlockTests(RepbaseAPITestMixin, APITestCase):
    """A block left any waiting follow request standing.

    The blocked person stayed in the blocker's request list, and approving it
    made a follow straight through the block. Opening a closed profile grants
    every waiting request, so it did the same without anyone pressing
    approve -- which is how a blocked person ended up following the account
    that blocked them.
    """

    def setUp(self):
        self.owner_user, self.owner, self.owner_token = self.create_account("closed")
        self.asker_user, self.asker, self.asker_token = self.create_account("asker")
        self.owner.is_profile_public = False
        self.owner.save(update_fields=["is_profile_public"])

    def request_follow(self):
        self.authenticate(self.asker_token)
        response = self.client.post(f"/api/v1/users/{self.owner.pk}/follow/")
        self.assertEqual(response.status_code, 202)

    def block(self, blocker_token, blocked):
        self.authenticate(blocker_token)
        response = self.client.post("/api/v1/social/blocks/", {"blocked": blocked.pk}, format="json")
        self.assertEqual(response.status_code, 201, response.data)

    def test_blocking_removes_the_request_and_its_notification(self):
        self.request_follow()
        self.block(self.owner_token, self.asker)
        self.assertFalse(FollowRequest.objects.exists())
        self.assertFalse(
            Notification.objects.filter(kind=Notification.Kind.FOLLOW_REQUEST).exists()
        )
        self.assertEqual(self.client.get("/api/v1/social/follow-requests/").data["results"], [])

    def test_blocking_removes_the_blockers_own_request_too(self):
        self.owner.is_profile_public = True
        self.owner.save(update_fields=["is_profile_public"])
        self.asker.is_profile_public = False
        self.asker.save(update_fields=["is_profile_public"])
        self.authenticate(self.owner_token)
        self.assertEqual(self.client.post(f"/api/v1/users/{self.asker.pk}/follow/").status_code, 202)
        self.block(self.owner_token, self.asker)
        self.assertFalse(FollowRequest.objects.exists())

    def test_a_request_from_before_the_fix_cannot_be_approved_across_the_block(self):
        Block.objects.create(blocker=self.owner, blocked=self.asker)
        stale = FollowRequest.objects.create(requester=self.asker, target=self.owner)
        self.authenticate(self.owner_token)
        self.assertEqual(self.client.get("/api/v1/social/follow-requests/").data["results"], [])
        response = self.client.post(f"/api/v1/social/follow-requests/{stale.pk}/approve/")
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Follow.objects.filter(follower=self.asker, following=self.owner).exists())

    def test_opening_the_profile_does_not_grant_a_blocked_request(self):
        Block.objects.create(blocker=self.owner, blocked=self.asker)
        FollowRequest.objects.create(requester=self.asker, target=self.owner)
        _, other, _ = self.create_account("waiting")
        FollowRequest.objects.create(requester=other, target=self.owner)
        self.authenticate(self.owner_token)
        response = self.client.patch("/api/v1/me/", {"is_profile_public": True}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(Follow.objects.filter(follower=self.asker, following=self.owner).exists())
        self.assertTrue(Follow.objects.filter(follower=other, following=self.owner).exists())
        self.assertFalse(FollowRequest.objects.exists())


class SuspendedProfileTests(TemporaryMediaMixin, RepbaseAPITestMixin, APITestCase):
    """Suspending an account hid its posts and comments, not the account.

    Its name, photo and bio -- often the reason it was suspended -- were still
    in people search, served by id, listed among followers and gym members,
    and named in notifications and follow requests.
    """

    def setUp(self):
        self.reader_user, self.reader, self.reader_token = self.create_account("reader")
        self.suspended_user, self.suspended, _ = self.create_account("suspended")
        self.gym = Gym.objects.create(name="Iron Works", city="Springfield")
        self.suspended.gym = self.gym
        self.suspended.bio = "the reason for the suspension"
        self.suspended.save(update_fields=["gym", "bio"])
        Follow.objects.create(follower=self.suspended, following=self.reader)
        Follow.objects.create(follower=self.reader, following=self.suspended)
        Notification.objects.create(
            recipient=self.reader, actor=self.suspended, kind=Notification.Kind.FOLLOW
        )
        self.reader.is_profile_public = False
        self.reader.save(update_fields=["is_profile_public"])
        FollowRequest.objects.create(requester=self.suspended, target=self.reader)
        self.suspended_user.is_active = False
        self.suspended_user.save(update_fields=["is_active"])
        self.authenticate(self.reader_token)

    def ids(self, response):
        rows = response.data["results"] if isinstance(response.data, dict) else response.data
        return [row.get("id", row.get("requester_id")) for row in rows]

    def test_gone_from_search_and_by_id(self):
        self.assertNotIn(self.suspended.pk, self.ids(self.client.get("/api/v1/users/", {"search": "suspended"})))
        self.assertEqual(self.client.get(f"/api/v1/users/{self.suspended.pk}/").status_code, 404)

    def test_gone_from_follow_lists_and_gym_members(self):
        self.assertNotIn(self.suspended.pk, self.ids(self.client.get(f"/api/v1/users/{self.reader.pk}/followers/")))
        self.assertNotIn(self.suspended.pk, self.ids(self.client.get(f"/api/v1/users/{self.reader.pk}/following/")))
        self.assertNotIn(self.suspended.pk, self.ids(self.client.get(f"/api/v1/gyms/{self.gym.pk}/members/")))

    def test_gone_from_notifications_and_requests(self):
        response = self.client.get("/api/v1/social/notifications/")
        self.assertEqual(response.data["results"], [])
        self.assertEqual(self.client.get("/api/v1/social/notifications/unread-count/").data["unread"], 0)
        self.assertEqual(self.client.get("/api/v1/social/follow-requests/").data["results"], [])

    def test_reinstating_brings_it_back(self):
        self.suspended_user.is_active = True
        self.suspended_user.save(update_fields=["is_active"])
        self.assertEqual(self.client.get(f"/api/v1/users/{self.suspended.pk}/").status_code, 200)
        self.assertIn(self.suspended.pk, self.ids(self.client.get(f"/api/v1/users/{self.reader.pk}/followers/")))


class ProfilePhotoTests(TemporaryMediaMixin, RepbaseAPITestMixin, APITestCase):
    """Profile photos had only the per-user ceiling, and could orphan files.

    Each upload is decoded in full and, where moderation is on, sent to the
    classifier -- the work a post's photo costs, which is limited to sixty an
    hour. A profile photo allowed 1,200. And the old file was deleted and the
    new one saved with no lock: ten uploads sent at once against a dev server
    left ten files on disk with one referenced, out of reach of every cleanup.
    """

    URL = "/api/v1/me/photo/"

    def setUp(self):
        self.user, self.profile, self.token = self.create_account("selfie")
        self.authenticate(self.token)
        # The media folder lasts for the class and rollback does not empty it,
        # so only files that appear during this test are counted.
        self.before = set(self._photo_files())

    def _photo_files(self):
        folder = os.path.join(settings.MEDIA_ROOT, "profile-photos")
        return os.listdir(folder) if os.path.isdir(folder) else []

    def upload(self, color=(10, 20, 30)):
        return self.client.put(
            self.URL,
            {"content_type": "image/jpeg", "image_base64": as_base64(photo_bytes(color=color))},
            format="json",
        )

    def stored_photos(self):
        return sorted(set(self._photo_files()) - self.before)

    def test_uploads_have_a_limit_of_their_own(self):
        with mock.patch.dict(SimpleRateThrottle.THROTTLE_RATES, {"photo": "2/hour"}):
            self.assertEqual(self.upload().status_code, 200)
            self.assertEqual(self.upload().status_code, 200)
            self.assertEqual(self.upload().status_code, 429)
            self.assertEqual(self.client.delete(self.URL).status_code, 200)

    def test_replacing_a_photo_leaves_one_file(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.upload().status_code, 200)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.upload(color=(90, 90, 90)).status_code, 200)
        self.profile.refresh_from_db()
        self.assertEqual(self.stored_photos(), [os.path.basename(self.profile.profile_photo.name)])
        self.assertFalse(PendingMediaDeletion.objects.exists())

    def test_an_upload_racing_another_does_not_orphan_its_file(self):
        # The second request read the profile before the first one saved:
        # its copy still has no photo. The old code deleted what its copy
        # held (nothing) and so left the first upload's file on disk.
        from .post_publication import replace_profile_photo

        stale = type(self.profile).objects.get(pk=self.profile.pk)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.upload().status_code, 200)
        first = type(self.profile).objects.get(pk=self.profile.pk).profile_photo.name
        with self.captureOnCommitCallbacks(execute=True):
            replace_profile_photo(stale, photo_bytes(color=(1, 2, 3)), ".jpg")
        self.assertFalse(default_storage.exists(first))
        self.assertEqual(len(self.stored_photos()), 1)

    def test_removing_the_photo_deletes_the_file(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.upload()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.delete(self.URL)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data["profile_photo_url"])
        self.assertEqual(self.stored_photos(), [])


class CommentEditAcrossBlockTests(RepbaseAPITestMixin, APITestCase):
    """Somebody blocked by a post's author could still rewrite their old
    comments under that post.

    The edit was invisible to the person who blocked them and shown to
    everyone else: against a dev server, a stranger read the rewritten text
    while the post's author saw nothing. Creating a comment was already
    refused across a block; editing was not.
    """

    def setUp(self):
        self.author_user, self.author, self.author_token = self.create_account("poster")
        self.commenter_user, self.commenter, self.commenter_token = self.create_account("commenter")
        self.post = meal_post(self.author)
        self.comment = PostComment.objects.create(post=self.post, author=self.commenter, body="nice lunch")
        self.authenticate(self.commenter_token)

    def edit(self, comment):
        return self.client.patch(f"/api/v1/social/comments/{comment.pk}/", {"body": "rewritten"}, format="json")

    def test_editing_works_until_the_block(self):
        self.assertEqual(self.edit(self.comment).status_code, 200)

    def test_the_posts_author_blocking_stops_edits(self):
        Block.objects.create(blocker=self.author, blocked=self.commenter)
        self.assertEqual(self.edit(self.comment).status_code, 404)
        self.comment.refresh_from_db()
        self.assertEqual(self.comment.body, "nice lunch")

    def test_the_replied_to_commenter_blocking_stops_edits_to_the_reply(self):
        _, third, _ = self.create_account("third")
        parent = PostComment.objects.create(post=self.post, author=third, body="first!")
        reply = PostComment.objects.create(post=self.post, author=self.commenter, parent=parent, body="hi")
        Block.objects.create(blocker=third, blocked=self.commenter)
        self.assertEqual(self.edit(reply).status_code, 404)

    def test_deleting_your_own_comment_still_works_after_a_block(self):
        Block.objects.create(blocker=self.author, blocked=self.commenter)
        self.assertEqual(self.client.delete(f"/api/v1/social/comments/{self.comment.pk}/").status_code, 204)


class ModeratorOwnContentTests(RepbaseAPITestMixin, APITestCase):
    """A moderator could decide reports about their own posts and comments --
    dismissing every complaint made about them, which also lifts the hold that
    open reports put on recommendations."""

    def setUp(self):
        self.mod_user, self.moderator, self.mod_token = self.create_account("moderator1")
        self.other_user, _, self.other_token = self.create_account("moderator2")
        _, reporter, _ = self.create_account("reporter")
        for user in (self.mod_user, self.other_user):
            AccountAccess.objects.create(user=user, moderator=True)
        self.post = meal_post(self.moderator)
        self.report = PostReport.objects.create(post=self.post, reporter=reporter, reason="spam")
        self.url = f"/api/v1/administration/reports/post/{self.report.pk}/decision/"

    def decide(self, token):
        self.authenticate(token)
        return self.client.post(self.url, {"decision": "no_action", "reason": "fine"}, format="json")

    def test_a_moderator_cannot_decide_a_report_about_themself(self):
        self.assertEqual(self.decide(self.mod_token).status_code, 403)
        self.report.refresh_from_db()
        self.assertIsNone(self.report.reviewed_at)

    def test_another_moderator_can(self):
        self.assertEqual(self.decide(self.other_token).status_code, 200)
