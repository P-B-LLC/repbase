"""Attack-shaped tests: what an adversarial client tries, and what it gets.

Each class is one attack surface. Where a test pins a fix made during the
security audit, its docstring says what was wrong before.
"""

import os
import subprocess
import sys
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import RequestFactory, SimpleTestCase
from django.utils import timezone
from rest_framework.test import APITestCase

from .fixtures_for_tests import (
    TemporaryMediaMixin,
    finished_session,
    meal_with_foods,
    planner_entry,
    workout_post,
)
from .models import (
    BodyWeightEntry,
    Block,
    Follow,
    PostComment,
    ProfileHighlight,
    SavedFoodMeal,
    WorkoutTemplate,
)
from .security import RedactingExceptionReporterFilter
from .tests import RepbaseAPITestMixin

User = get_user_model()


class MemberListTests(RepbaseAPITestMixin, APITestCase):
    """/api/repbase/ used to print every member's email, height and weight to
    anybody who asked, signed in or not."""

    def setUp(self):
        self.user, self.profile, self.token = self.create_account("private_person")

    def test_anonymous_visitors_are_sent_to_sign_in(self):
        response = self.client.get("/api/repbase/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])
        self.assertNotIn(b"private_person@example.com", response.content)

    def test_an_ordinary_member_is_not_staff(self):
        self.client.force_login(self.user)
        response = self.client.get("/api/repbase/")
        self.assertEqual(response.status_code, 302)

    def test_staff_can_still_use_it(self):
        staff = User.objects.create_user("staffer", "staff@example.com", "StrongPass!234", is_staff=True)
        self.client.force_login(staff)
        response = self.client.get("/api/repbase/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"private_person", response.content)


class ClosedProfileTests(RepbaseAPITestMixin, APITestCase):
    """A closed profile's lifts and connections used to be readable by any
    signed-in stranger who knew its id, though the profile itself was not."""

    def setUp(self):
        _, self.owner, self.owner_token = self.create_account("closed")
        _, self.stranger, self.stranger_token = self.create_account("stranger")
        _, self.friend, self.friend_token = self.create_account("friend")
        self.owner.is_profile_public = False
        self.owner.save(update_fields=["is_profile_public"])
        Follow.objects.create(follower=self.friend, following=self.owner)
        Follow.objects.create(follower=self.owner, following=self.friend)
        ProfileHighlight.objects.create(owner=self.owner, lift=ProfileHighlight.Lift.BENCH, manual_weight_kg=Decimal("100"), manual_reps=1, position=1)

    def lists(self, token):
        self.authenticate(token)
        base = f"/api/v1/users/{self.owner.pk}"
        highlights = self.client.get(f"{base}/highlights/")
        followers = self.client.get(f"{base}/followers/")
        following = self.client.get(f"{base}/following/")
        for response in (highlights, followers, following):
            self.assertEqual(response.status_code, 200)
        return highlights.data, followers.data["results"], following.data["results"]

    def test_a_stranger_gets_empty_lists_in_the_usual_shape(self):
        self.assertEqual(self.lists(self.stranger_token), ([], [], []))

    def test_a_follower_and_the_owner_see_them(self):
        for token in (self.friend_token, self.owner_token):
            highlights, followers, following = self.lists(token)
            self.assertEqual(len(highlights), 1)
            self.assertEqual(len(followers), 1)
            self.assertEqual(len(following), 1)

    def test_a_block_closes_even_an_open_profile(self):
        self.owner.is_profile_public = True
        self.owner.save(update_fields=["is_profile_public"])
        Block.objects.create(blocker=self.owner, blocked=self.stranger)
        self.assertEqual(self.lists(self.stranger_token), ([], [], []))


class SomebodyElsesRowsTests(RepbaseAPITestMixin, APITestCase):
    """Every owner-scoped resource, attacked by id from another account.

    The answer is 404 throughout, never 403: a 403 would confirm the row
    exists and is somebody's.
    """

    def setUp(self):
        _, self.victim, self.victim_token = self.create_account("victim")
        _, self.attacker, self.attacker_token = self.create_account("attacker")
        self.rows = {
            "sessions": finished_session(self.victim).pk,
            "workouts": WorkoutTemplate.objects.create(owner=self.victim, name="Secret plan").pk,
            "planner": planner_entry(self.victim).pk,
            "food/meals": meal_with_foods(self.victim).pk,
            "food/saved-meals": SavedFoodMeal.objects.create(owner=self.victim, name="Secret recipe").pk,
            "body-weight": BodyWeightEntry.objects.create(
                owner=self.victim, weight_kg=Decimal("70.00"), recorded_at=timezone.now()
            ).pk,
        }
        self.authenticate(self.attacker_token)

    def test_reading_editing_and_deleting_are_all_not_found(self):
        for resource, pk in self.rows.items():
            url = f"/api/v1/{resource}/{pk}/"
            with self.subTest(resource=resource):
                self.assertEqual(self.client.get(url).status_code, 404)
                self.assertEqual(self.client.patch(url, {"name": "pwned", "title": "pwned"}, format="json").status_code, 404)
                self.assertEqual(self.client.delete(url).status_code, 404)

    def test_lists_never_include_them(self):
        for resource, pk in self.rows.items():
            with self.subTest(resource=resource):
                response = self.client.get(f"/api/v1/{resource}/")
                if response.status_code != 200:
                    continue  # some lists require a filter; the detail test covers them
                rows = response.data["results"] if isinstance(response.data, dict) else response.data
                self.assertNotIn(pk, [row["id"] for row in rows])

    def test_a_session_cannot_be_logged_into_or_finished_by_somebody_else(self):
        session = finished_session(self.victim)
        line = session.session_exercises.first()
        response = self.client.post(
            "/api/v1/set-entries/",
            {"session_exercise": line.pk, "set_number": 9, "weight_kg": "1.00", "reps": 1},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.post(f"/api/v1/sessions/{session.pk}/end/").status_code, 404)

    def test_ownership_fields_in_a_body_are_ignored(self):
        response = self.client.post(
            "/api/v1/workouts/", {"name": "Mine", "workout_type": "lifting", "owner": self.victim.pk},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(WorkoutTemplate.objects.get(pk=response.data["id"]).owner, self.attacker)

    def test_somebody_elses_comment_cannot_be_edited_or_deleted(self):
        comment = PostComment.objects.create(post=workout_post(self.victim), author=self.victim, body="mine")
        url = f"/api/v1/social/comments/{comment.pk}/"
        self.assertEqual(self.client.patch(url, {"body": "pwned"}, format="json").status_code, 404)
        self.assertEqual(self.client.delete(url).status_code, 404)

    def test_a_reported_user_cannot_be_reported_as_someone_else(self):
        """The reporter is always the token's owner, whatever the body says."""
        from .models import PostReport

        post = workout_post(self.victim)
        self.client.post(
            f"/api/v1/social/posts/{post.pk}/report/",
            {"reason": "spam", "reporter": self.victim.pk},
            format="json",
        )
        self.assertEqual(PostReport.objects.get(post=post).reporter, self.attacker)


class MalformedInputTests(RepbaseAPITestMixin, APITestCase):
    """Bad input is a 400 that names the field, never a 500.

    Eight list filters used to pass their raw text to the database and raise
    on anything that was not a number or a date.
    """

    def setUp(self):
        _, self.profile, self.token = self.create_account("fuzzer")
        self.authenticate(self.token)

    def test_ids_and_dates_in_filters_are_validated(self):
        for path, name in [
            ("/api/v1/social/posts/?author=abc", "author"),
            ("/api/v1/social/posts/?author=99999999999999999999999", "author"),
            ("/api/v1/social/comments/?post=1.5", "post"),
            ("/api/v1/session-exercises/?session=abc", "session"),
            ("/api/v1/set-entries/?session_exercise=abc", "session_exercise"),
            ("/api/v1/set-entries/?session=-4", "session"),
            ("/api/v1/food/entries/?meal=abc", "meal"),
            ("/api/v1/planner/?start=bad", "start"),
            ("/api/v1/planner/?end=2026-02-30", "end"),
            ("/api/v1/planner/?parent=x", "parent"),
            ("/api/v1/schedules/?scheduled_date=bad", "scheduled_date"),
            ("/api/v1/food/meals/?start=yesterday", "start"),
            ("/api/v1/sessions/?workout=abc", "workout"),
            ("/api/v1/sessions/previous-sets/?exclude_session=z", "exclude_session"),
        ]:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 400)
                self.assertIn(name, response.data)

    def test_injection_shaped_search_text_is_just_text(self):
        for term in ["' OR 1=1 --", "%' ;--", "\\", "\x00", "a" * 2000, "<script>alert(1)</script>"]:
            with self.subTest(term=term[:20]):
                for path in ("/api/v1/users/", "/api/v1/social/posts/", "/api/v1/gyms/"):
                    response = self.client.get(path, {"search": term})
                    self.assertIn(response.status_code, (200, 400))

    def test_a_nul_byte_in_any_query_parameter_is_a_400(self):
        """PostgreSQL raises on NUL in text, so `?search=%00` was a 500 on
        every search in production -- and passed on SQLite, which does not."""
        for path in ("/api/v1/users/?search=a%00b", "/api/v1/social/posts/?search=%00",
                     "/api/v1/gyms/?search=%00", "/api/v1/exercises/?name=%00",
                     "/api/v1/planner/?category=%00", "/api/v1/food/search/?q=%00",
                     "/api/v1/social/posts/?a%00=1"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 400)
                self.assertIn("NUL", response.json()["detail"])

    def test_bodies_that_are_not_objects_are_refused(self):
        for path in ("/api/v1/workouts/", "/api/v1/social/posts/", "/api/v1/social/comments/"):
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path, [1, 2, 3], format="json").status_code, 400)
                self.assertEqual(
                    self.client.post(path, "{not json", content_type="application/json").status_code, 400
                )


class ResponseHeaderTests(RepbaseAPITestMixin, APITestCase):
    def setUp(self):
        _, self.profile, self.token = self.create_account("headers")
        self.authenticate(self.token)

    def test_api_answers_carry_the_strict_policy_and_are_not_cached(self):
        response = self.client.get("/api/v1/me/")
        self.assertEqual(
            response["Content-Security-Policy"],
            "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        )
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response["X-Frame-Options"], "DENY")
        self.assertEqual(response["Referrer-Policy"], "same-origin")
        self.assertEqual(response["Cross-Origin-Opener-Policy"], "same-origin")
        self.assertEqual(response["Cross-Origin-Resource-Policy"], "same-origin")
        self.assertIn("camera=()", response["Permissions-Policy"])

    def test_pages_may_not_run_inline_scripts(self):
        response = self.client.get("/api/docs/")
        policy = response["Content-Security-Policy"]
        script = next(part for part in policy.split(";") if part.strip().startswith("script-src"))
        self.assertNotIn("unsafe-inline", script)
        self.assertIn("frame-ancestors 'none'", policy)
        self.assertNotIn(b"<script>", response.content)
        self.assertIn(b"swagger-ui-dist@5.17.14", response.content, "the docs CDN must be pinned")

    def test_no_other_origin_is_granted_access(self):
        """The web app is served from the API's own origin, so nothing needs
        CORS -- and an Access-Control-Allow-Origin here would let any site
        read responses with a visitor's credentials."""
        response = self.client.get("/api/v1/me/", HTTP_ORIGIN="https://evil.example")
        self.assertNotIn("Access-Control-Allow-Origin", response)
        preflight = self.client.options(
            "/api/v1/me/", HTTP_ORIGIN="https://evil.example",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="PATCH",
        )
        self.assertNotIn("Access-Control-Allow-Origin", preflight)
        self.assertNotIn("Access-Control-Allow-Credentials", preflight)

    def test_session_cookie_writes_still_need_a_csrf_token(self):
        """Token clients are exempt; a browser session riding on a cookie is not."""
        from rest_framework.test import APIClient

        client = APIClient(enforce_csrf_checks=True)
        client.force_login(self.profile.user)
        response = client.patch("/api/v1/me/", {"bio": "csrf"}, format="json")
        self.assertEqual(response.status_code, 403)


class MediaEmbeddingTests(TemporaryMediaMixin, RepbaseAPITestMixin, APITestCase):
    """The web app's page and its media URLs do not always share an origin --
    Vite's port and Django's in development. `Cross-Origin-Resource-Policy:
    same-origin` on media made Chrome refuse every clip and photo in the
    running app, which no test of the API alone could see."""

    def test_media_may_be_embedded_from_another_origin(self):
        from django.core.files.base import ContentFile
        from django.core.files.storage import default_storage

        from .media import signed_media_path

        _, _, token = self.create_account("embedder")
        self.authenticate(token)
        name = default_storage.save("post-photos/corp-check.png", ContentFile(b"\x89PNG\r\n\x1a\n"))
        response = self.client.get(signed_media_path(name))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cross-Origin-Resource-Policy"], "cross-origin")
        self.assertNotIn("Content-Security-Policy", response)
        self.assertEqual(self.client.get("/api/v1/me/")["Cross-Origin-Resource-Policy"], "same-origin")


class ErrorReportTests(SimpleTestCase):
    def test_the_authorization_header_is_hidden_from_error_reports(self):
        request = RequestFactory().get("/", HTTP_AUTHORIZATION="Token abcdef0123456789")
        meta = RedactingExceptionReporterFilter().get_safe_request_meta(request)
        self.assertNotIn("abcdef0123456789", str(meta["HTTP_AUTHORIZATION"]))


class ProductionSettingsTests(SimpleTestCase):
    """Settings that only exist in config.production, checked in a process
    that actually loads it."""

    def environment(self, **extra):
        return {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "config.production", "DJANGO_DEBUG": "false",
            "DJANGO_SECRET_KEY": "unit-test-only-not-a-real-secret-" * 3,
            "DJANGO_ALLOWED_HOSTS": "api.example.com", "DJANGO_MEDIA_ROOT": "/persistent/media",
            "REDIS_URL": "rediss://:test-password@cache.example.com:6379/0",
            "DATABASE_URL": "postgresql://test:test@db.example.com/rytivo",
            "DATABASE_SSLMODE": "verify-full", **extra,
        }

    def settings_say(self, expression, **extra):
        result = subprocess.run(
            [sys.executable, "-c", f"from django.conf import settings as s; print({expression})"],
            env=self.environment(**extra), capture_output=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return result.stdout.decode().strip()

    def test_the_real_client_address_is_read_from_behind_one_proxy(self):
        self.assertEqual(self.settings_say("s.REST_FRAMEWORK['NUM_PROXIES']"), "1")
        self.assertEqual(self.settings_say("s.REST_FRAMEWORK['NUM_PROXIES']", REPBASE_NUM_PROXIES="2"), "2")

    def test_api_docs_are_for_staff_unless_published(self):
        self.assertIn("IsAdminUser", self.settings_say("s.SPECTACULAR_SETTINGS.get('SERVE_PERMISSIONS')"))
        published = self.settings_say(
            "s.SPECTACULAR_SETTINGS.get('SERVE_PERMISSIONS')", REPBASE_PUBLIC_API_DOCS="true"
        )
        self.assertEqual(published, "None")

    def test_clips_are_always_reviewed(self):
        self.assertEqual(self.settings_say("s.VIDEO_REVIEW_REQUIRED"), "True")
