"""Email that cannot deliver must fail loudly at deploy, quietly at runtime.

Those two halves pull in opposite directions on purpose.

At deploy time the wrong configuration should stop everything, because it is
cheap to fix then and there is nobody to hurt yet. At request time the same
failure must change nothing a caller can observe, because the only requests
that reach the send are the ones for addresses that have an account -- so any
difference between "sent" and "failed" is a way to ask the server whether a
given person is registered.
"""

from unittest import mock

from django.contrib.auth import get_user_model
from django.core.checks import Error, Warning
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from .checks import email_can_reach_a_real_person
from .models import PasswordResetCode, RepbaseUser

User = get_user_model()

SMTP = 'django.core.mail.backends.smtp.EmailBackend'
CONSOLE = 'django.core.mail.backends.console.EmailBackend'


def mailer(**options):
    """A default mailer over SMTP, configured well unless a test breaks it."""
    return {
        'default': {
            'BACKEND': SMTP,
            'OPTIONS': {
                'host': 'smtp.example.com',
                'port': 587,
                'username': 'rytivo',
                'password': 'hunter2',
                'use_tls': True,
                'use_ssl': False,
                'timeout': 10,
                **options,
            },
        }
    }


def ids(problems):
    return sorted(problem.id for problem in problems)


class TheDeployCheckTests(SimpleTestCase):
    @override_settings(
        MAILERS={'default': {'BACKEND': CONSOLE}},
        DEFAULT_FROM_EMAIL='noreply@repbase.local',
    )
    def test_a_backend_that_delivers_nowhere_on_purpose_is_left_alone(self):
        """Console and locmem are doing what they were asked to. Reporting
        their settings would be noise in every local `check --deploy`."""
        self.assertEqual(email_can_reach_a_real_person(None), [])

    @override_settings(
        MAILERS=mailer(), DEFAULT_FROM_EMAIL='noreply@rytivo.app'
    )
    def test_a_configured_provider_passes(self):
        self.assertEqual(email_can_reach_a_real_person(None), [])

    @override_settings(
        MAILERS=mailer(host='localhost'),
        DEFAULT_FROM_EMAIL='noreply@repbase.local',
    )
    def test_the_defaults_this_repo_shipped_are_both_refused(self):
        """The actual state of the deploy before this change: Django's own
        localhost fallback, and a From address on a reserved mDNS suffix."""
        self.assertEqual(ids(email_can_reach_a_real_person(None)), ['core.E002', 'core.E003'])

    @override_settings(MAILERS=mailer(), DEFAULT_FROM_EMAIL='rytivo.app')
    def test_a_from_address_that_is_not_an_address(self):
        self.assertEqual(ids(email_can_reach_a_real_person(None)), ['core.E001'])

    @override_settings(
        MAILERS=mailer(use_tls=False, use_ssl=False),
        DEFAULT_FROM_EMAIL='noreply@rytivo.app',
    )
    def test_smtp_without_tls_is_refused(self):
        """The password would cross the network in the clear."""
        self.assertEqual(ids(email_can_reach_a_real_person(None)), ['core.E004'])

    @override_settings(
        MAILERS=mailer(use_tls=False, use_ssl=True),
        DEFAULT_FROM_EMAIL='noreply@rytivo.app',
    )
    def test_implicit_tls_counts_as_tls(self):
        self.assertEqual(email_can_reach_a_real_person(None), [])

    @override_settings(
        MAILERS=mailer(username=''), DEFAULT_FROM_EMAIL='noreply@rytivo.app'
    )
    def test_no_credentials_is_a_warning_not_an_error(self):
        """A relay on a private network may authorise by address, so this one
        is a judgement the operator gets to make."""
        problems = email_can_reach_a_real_person(None)
        self.assertEqual(ids(problems), ['core.W005'])
        self.assertIsInstance(problems[0], Warning)

    @override_settings(
        MAILERS=mailer(host=''), DEFAULT_FROM_EMAIL='noreply@rytivo.app'
    )
    def test_nothing_else_is_reported_once_there_is_no_host(self):
        """Complaining about the TLS settings of a server that was never
        named would bury the one problem worth reading."""
        problems = email_can_reach_a_real_person(None)
        self.assertEqual(ids(problems), ['core.E003'])
        self.assertIsInstance(problems[0], Error)


class WhenTheProviderRefusesTheMessageTests(APITestCase):
    REQUEST = "/api/v1/auth/password-reset/"
    EMAIL = "forgetful@example.invalid"

    def setUp(self):
        self.user = User.objects.create_user(
            username="forgetful", email=self.EMAIL, password="the-old-one-12345"
        )
        RepbaseUser.objects.get_or_create(user=self.user)

    def ask(self, email=None, broken=True):
        """Ask for a code, with the provider refusing the message by default.

        The failure is asserted through assertLogs rather than merely
        allowed: an operator finding out is the entire compensation for the
        caller being told nothing, so a silent failure here would defeat the
        design instead of implementing it. It also keeps the traceback out of
        the test output, where it reads like a broken test.
        """
        side_effect = OSError("connection refused") if broken else None
        with mock.patch("core.views.send_mail", side_effect=side_effect) as sent:
            if broken and email is None:
                with self.assertLogs("core.views", level="ERROR") as logged:
                    response = self.client.post(
                        self.REQUEST, {"email": self.EMAIL}, format="json"
                    )
                self.assertIn("could not be sent", logged.output[0])
            else:
                response = self.client.post(
                    self.REQUEST, {"email": email or self.EMAIL}, format="json"
                )
        return response, sent

    def live_codes(self):
        return PasswordResetCode.objects.filter(
            user=self.user, used_at__isnull=True, expires_at__gt=timezone.now()
        )

    def test_the_answer_is_the_same_204_as_always(self):
        """The bug this replaces: send_mail raised, so a real address
        answered 500 and an unknown one answered 204. Anyone could tell the
        two apart, which is the whole thing the endpoint is built to prevent.
        """
        refused, _ = self.ask()
        self.assertEqual(refused.status_code, 204)

        unknown, sent = self.ask("nobody@example.invalid")
        self.assertEqual(unknown.status_code, 204)
        self.assertFalse(sent.called, "an unknown address should send nothing")

    def test_no_live_code_is_left_behind(self):
        """A code that was issued and never delivered is a reset the account
        holder cannot use and an attacker still could."""
        self.ask()
        self.assertEqual(self.live_codes().count(), 0)
        self.assertTrue(
            PasswordResetCode.objects.filter(user=self.user).exists(),
            "the row should be spent, not deleted -- the attempt happened",
        )

    def test_asking_again_once_the_provider_recovers_still_works(self):
        self.ask()
        response, sent = self.ask(broken=False)
        self.assertEqual(response.status_code, 204)
        self.assertTrue(sent.called)
        self.assertEqual(self.live_codes().count(), 1)
