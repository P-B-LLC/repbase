from unittest import mock
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from .models import (
    Exercise,
    Gym,
    ProfileSocialLink,
    PasswordResetCode,
    RepbaseUser,
    SessionExercise,
    SetEntry,
    WorkoutExercise,
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
