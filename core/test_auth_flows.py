"""Registration, sign-in, sign-out and token rotation, end to end.

None of these four had a test of the request a client actually sends: the one
registration test checked the happy path, and sign-in, sign-out and rotation
had none at all. These go through the real stack -- routing, throttles,
serializers, the database -- so they fail the way a client would see it fail.
"""

import logging
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase
from rest_framework.throttling import SimpleRateThrottle

from .models import RepbaseUser
from .tests import RepbaseAPITestMixin

User = get_user_model()

REGISTER = "/api/v1/auth/register/"
LOGIN = "/api/v1/auth/login/"
LOGOUT = "/api/v1/auth/logout/"
ROTATE = "/api/v1/auth/rotate-token/"
ME = "/api/v1/me/"


def registration(**changes):
    body = {
        "username": "new_lifter",
        "email": "new.lifter@example.com",
        "password": "Correct-Horse-Battery-9",
        "first_name": "New",
        "last_name": "Lifter",
    }
    body.update(changes)
    return body


class RegistrationTests(RepbaseAPITestMixin, APITestCase):
    def register(self, **changes):
        return self.client.post(REGISTER, registration(**changes), format="json")

    def test_a_new_account_gets_a_token_that_works(self):
        response = self.register()
        self.assertEqual(response.status_code, 201, response.data)
        token = response.data["token"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        me = self.client.get(ME)
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.data["username"], "new_lifter")
        self.assertTrue(RepbaseUser.objects.filter(user__username="new_lifter").exists())

    def test_the_password_is_stored_hashed_and_never_echoed(self):
        response = self.register()
        self.assertNotIn("Correct-Horse-Battery-9", str(response.content))
        user = User.objects.get(username="new_lifter")
        self.assertNotEqual(user.password, "Correct-Horse-Battery-9")
        self.assertTrue(user.check_password("Correct-Horse-Battery-9"))

    def test_an_email_is_stored_lowercased(self):
        self.register(email="Mixed.Case@Example.COM")
        self.assertEqual(User.objects.get(username="new_lifter").email, "mixed.case@example.com")

    def test_every_field_is_required(self):
        for field in ("username", "email", "password", "first_name", "last_name"):
            with self.subTest(field=field):
                body = registration()
                body.pop(field)
                response = self.client.post(REGISTER, body, format="json")
                self.assertEqual(response.status_code, 400)
                self.assertIn(field, response.data)

    def test_weak_passwords_are_refused(self):
        for password, why in [
            ("short1!", "too short"),
            ("password123", "too common"),
            ("12345678901", "entirely numeric"),
            ("new_lifter_1", "too similar to the username"),
        ]:
            with self.subTest(why=why):
                response = self.register(password=password)
                self.assertEqual(response.status_code, 400, why)
                self.assertFalse(User.objects.filter(username="new_lifter").exists())

    def test_an_absurdly_long_password_is_refused_before_it_is_compared(self):
        response = self.register(password="x9!" * 50_000)
        self.assertEqual(response.status_code, 400)
        self.assertIn("password", response.data)

    def test_a_malformed_email_is_refused(self):
        response = self.register(email="not-an-address")
        self.assertEqual(response.status_code, 400)
        self.assertIn("email", response.data)

    def test_usernames_are_held_to_a_safe_alphabet(self):
        # "\u0430dmin" starts with a Cyrillic a: it renders exactly as "admin".
        for username in ["../admin", "two words", "<script>", "bob\u202eevil", "\u0430dmin", "semi;colon"]:
            with self.subTest(username=username):
                response = self.register(username=username, email=f"{len(username)}@example.com")
                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn("username", response.data)

    def test_ordinary_handles_are_still_accepted(self):
        for index, username in enumerate(["lifter", "Lifter.2024", "run-fast", "a_b", "me+gym@home"]):
            with self.subTest(username=username):
                response = self.register(username=username, email=f"ok{index}@example.com")
                self.assertEqual(response.status_code, 201, response.data)

    def test_service_names_are_reserved(self):
        for username in ["admin", "Support", "RYTIVO", "moderator"]:
            with self.subTest(username=username):
                response = self.register(username=username, email=f"{username}@example.com")
                self.assertEqual(response.status_code, 400)

    def test_names_cannot_hide_formatting_characters(self):
        response = self.register(first_name="Bob\u202e")
        self.assertEqual(response.status_code, 400)
        self.assertIn("first_name", response.data)
        response = self.register(last_name="Line\nBreak")
        self.assertEqual(response.status_code, 400)

    def test_emoji_sequences_in_names_are_fine(self):
        response = self.register(first_name="Ana 👩‍💻")
        self.assertEqual(response.status_code, 201, response.data)

    def test_duplicates_are_refused_whatever_the_case(self):
        self.assertEqual(self.register().status_code, 201)
        again = self.register(username="NEW_LIFTER", email="other@example.com")
        self.assertEqual(again.status_code, 400)
        self.assertIn("username", again.data)
        again = self.register(username="someone_else", email="NEW.LIFTER@example.com")
        self.assertEqual(again.status_code, 400)
        self.assertIn("email", again.data)

    def test_a_token_in_the_request_is_ignored(self):
        """Registration is anonymous: a stale token must not sign the request."""
        _, _, token = self.create_account("existing")
        self.authenticate(token)
        response = self.register()
        self.assertEqual(response.status_code, 201)
        self.assertNotEqual(response.data["token"], token.key)

    def test_registration_is_throttled_per_address(self):
        with mock.patch.dict(SimpleRateThrottle.THROTTLE_RATES, {"register": "3/day"}):
            codes = [
                self.register(username=f"bulk{n}", email=f"bulk{n}@example.com").status_code
                for n in range(5)
            ]
        self.assertEqual(codes[:3], [201, 201, 201])
        self.assertEqual(codes[3:], [429, 429])


class SignInTests(RepbaseAPITestMixin, APITestCase):
    def setUp(self):
        self.user, self.profile, self.token = self.create_account("signer")

    def sign_in(self, username="signer", password="StrongPass!234", **extra):
        return self.client.post(LOGIN, {"username": username, "password": password}, format="json", **extra)

    def test_the_right_password_returns_the_account_and_a_working_token(self):
        response = self.sign_in()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["user"]["id"], self.profile.id)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {response.data['token']}")
        self.assertEqual(self.client.get(ME).status_code, 200)

    def test_a_wrong_password_and_an_unknown_user_read_the_same(self):
        """No answer may say which half was wrong: that is an account oracle."""
        wrong = self.sign_in(password="not-it")
        unknown = self.sign_in(username="nobody-here")
        self.assertEqual(wrong.status_code, 400)
        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(wrong.data, unknown.data)

    def test_missing_and_malformed_bodies_are_refused(self):
        self.assertEqual(self.client.post(LOGIN, {}, format="json").status_code, 400)
        self.assertEqual(self.client.post(LOGIN, {"username": "signer"}, format="json").status_code, 400)
        self.assertEqual(self.client.post(LOGIN, [1, 2], format="json").status_code, 400)
        self.assertEqual(
            self.client.post(LOGIN, "not json", content_type="application/json").status_code, 400
        )

    def test_a_suspended_account_cannot_sign_in_and_its_token_stops_working(self):
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        self.assertEqual(self.sign_in().status_code, 400)
        self.authenticate(self.token)
        self.assertEqual(self.client.get(ME).status_code, 401)

    def test_sign_in_is_throttled_per_address(self):
        with mock.patch.dict(SimpleRateThrottle.THROTTLE_RATES, {"login": "4/hour"}):
            codes = [self.sign_in(password="wrong").status_code for _ in range(6)]
        self.assertEqual(codes, [400, 400, 400, 400, 429, 429])

    def test_a_spoofed_forwarded_address_does_not_reset_the_limit(self):
        """The bypass that was reproduced: a new made-up X-Forwarded-For per
        request used to give every attempt a fresh bucket. With the proxy
        count production sets, only the address nginx wrote is believed."""
        rest = {**settings.REST_FRAMEWORK, "NUM_PROXIES": 1}
        with override_settings(REST_FRAMEWORK=rest), \
                mock.patch.dict(SimpleRateThrottle.THROTTLE_RATES, {"login": "4/hour"}):
            codes = [
                self.sign_in(
                    password="wrong",
                    HTTP_X_FORWARDED_FOR=f"10.0.0.{attempt}, 203.0.113.7",
                ).status_code
                for attempt in range(6)
            ]
        self.assertEqual(codes[-2:], [429, 429])

    def test_failures_against_one_username_are_limited_from_every_address(self):
        rest = {**settings.REST_FRAMEWORK, "NUM_PROXIES": 1}
        with override_settings(REST_FRAMEWORK=rest), mock.patch.dict(
            SimpleRateThrottle.THROTTLE_RATES, {"login": "1000/hour", "login_username": "3/hour"}
        ):
            codes = [
                self.sign_in(password="wrong", HTTP_X_FORWARDED_FOR=f"198.51.100.{n}").status_code
                for n in range(3)
            ]
            # The right password, from a new address, after the limit: still
            # refused, so guessing learns nothing past the limit.
            locked = self.sign_in(HTTP_X_FORWARDED_FOR="198.51.100.200")
            # Another account is unaffected.
            self.create_account("bystander")
            other = self.sign_in(username="bystander", HTTP_X_FORWARDED_FOR="198.51.100.201")
        self.assertEqual(codes, [400, 400, 400])
        self.assertEqual(locked.status_code, 429)
        self.assertEqual(other.status_code, 200)

    def test_successful_sign_ins_do_not_count_toward_the_username_limit(self):
        with mock.patch.dict(
            SimpleRateThrottle.THROTTLE_RATES, {"login": "1000/hour", "login_username": "2/hour"}
        ):
            codes = [self.sign_in().status_code for _ in range(5)]
        self.assertEqual(codes, [200] * 5)

    def test_the_username_limit_ignores_case(self):
        with mock.patch.dict(
            SimpleRateThrottle.THROTTLE_RATES, {"login": "1000/hour", "login_username": "2/hour"}
        ):
            self.sign_in(username="SIGNER", password="x")
            self.sign_in(username="Signer", password="x")
            response = self.sign_in()
        self.assertEqual(response.status_code, 429)

    def test_a_password_reset_lets_the_owner_back_in(self):
        """Somebody else's failures must not keep the owner out once they have
        proved they hold the mailbox."""
        sent = []

        def capture(**kwargs):
            sent.append(kwargs)
            return 1

        with mock.patch.dict(
            SimpleRateThrottle.THROTTLE_RATES, {"login": "1000/hour", "login_username": "2/hour"}
        ):
            self.sign_in(password="attacker-1")
            self.sign_in(password="attacker-2")
            self.assertEqual(self.sign_in().status_code, 429)

            with mock.patch("core.views.send_mail", side_effect=capture):
                self.client.post(
                    "/api/v1/auth/password-reset/", {"email": self.user.email}, format="json"
                )
            code = sent[0]["message"].split("code is ")[1][:6]
            reset = self.client.post(
                "/api/v1/auth/password-reset/confirm/",
                {"email": self.user.email, "code": code, "new_password": "Brand-New-Secret-77"},
                format="json",
            )
            self.assertEqual(reset.status_code, 200, reset.data)
            self.assertEqual(self.sign_in(password="Brand-New-Secret-77").status_code, 200)

    def test_the_password_never_reaches_the_logs(self):
        with self.assertLogs(level=logging.DEBUG) as captured:
            logging.getLogger("core").debug("marker")
            self.sign_in(password="Hunter2-SuperSecret")
            self.sign_in()
        self.assertNotIn("Hunter2-SuperSecret", "\n".join(captured.output))
        self.assertNotIn(self.token.key, "\n".join(captured.output))


class SignOutAndRotationTests(RepbaseAPITestMixin, APITestCase):
    def setUp(self):
        self.user, self.profile, self.token = self.create_account("leaver")
        self.authenticate(self.token)

    def test_signing_out_revokes_the_token(self):
        response = self.client.post(LOGOUT)
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Token.objects.filter(key=self.token.key).exists())
        self.assertEqual(self.client.get(ME).status_code, 401)

    def test_signing_out_needs_a_session(self):
        self.client.credentials()
        self.assertEqual(self.client.post(LOGOUT).status_code, 401)

    def test_rotation_replaces_the_token(self):
        response = self.client.post(ROTATE)
        self.assertEqual(response.status_code, 200)
        fresh = response.data["token"]
        self.assertNotEqual(fresh, self.token.key)
        self.assertEqual(self.client.get(ME).status_code, 401, "the old token must be dead")
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {fresh}")
        self.assertEqual(self.client.get(ME).status_code, 200)

    def test_a_garbage_token_is_refused(self):
        self.client.credentials(HTTP_AUTHORIZATION="Token " + "0" * 40)
        self.assertEqual(self.client.get(ME).status_code, 401)
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + self.token.key)
        self.assertEqual(self.client.get(ME).status_code, 401)

    def test_the_profile_response_carries_no_secrets(self):
        body = str(self.client.get(ME).content)
        self.assertNotIn(self.token.key, body)
        self.assertNotIn("password", body)


class ProfileEditValidationTests(RepbaseAPITestMixin, APITestCase):
    """The username and name rules, applied to edits only when they change."""

    def setUp(self):
        self.user, self.profile, self.token = self.create_account("editor")
        self.authenticate(self.token)

    def test_a_changed_username_is_held_to_the_rules(self):
        response = self.client.patch(ME, {"username": "bad name"}, format="json")
        self.assertEqual(response.status_code, 400)
        response = self.client.patch(ME, {"username": "admin"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_an_unchanged_legacy_username_does_not_block_other_edits(self):
        """The app sends the username back with every save. An account made
        before the rules must still be able to change its bio."""
        User.objects.filter(pk=self.user.pk).update(username="legacy name")
        response = self.client.patch(
            ME, {"username": "legacy name", "bio": "still here"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.bio, "still here")

    def test_an_unchanged_legacy_name_does_not_block_other_edits(self):
        User.objects.filter(pk=self.user.pk).update(first_name="Old\u200bName")
        response = self.client.patch(
            ME, {"first_name": "Old\u200bName", "bio": "fine"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        response = self.client.patch(ME, {"first_name": "New\u202eName"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_read_only_fields_cannot_be_written(self):
        response = self.client.patch(
            ME, {"id": 999, "created_at": "2000-01-01T00:00:00Z", "profile_photo_url": "x"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.profile.refresh_from_db()
        self.assertNotEqual(self.profile.pk, 999)
        self.assertNotEqual(self.profile.created_at.year, 2000)
