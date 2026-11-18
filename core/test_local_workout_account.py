"""The launcher's minimal local account can use the real authenticated API."""
import secrets

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APIClient, APITestCase

from .models import AccountAgreementAcceptance, RepbaseUser, WorkoutTemplate


@override_settings(MODERATION_ENABLED=False,
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class LocalWorkoutAccountTests(APITestCase):
    def test_local_account_signs_in_accepts_agreements_and_reads_capability(self):
        # Deliberately match the launcher, not registration/fixture helpers:
        # no email verification, acceptance, token or workout is pre-created.
        password = secrets.token_urlsafe(24)
        user = get_user_model()(username="local_workout_tester", email="local-workout@example.invalid",
                                first_name="Local", last_name="Tester")
        user.set_password(password)
        user.save()
        profile, _ = RepbaseUser.objects.get_or_create(user=user)
        self.assertFalse(AccountAgreementAcceptance.objects.filter(user=user).exists())
        self.assertFalse(WorkoutTemplate.objects.filter(owner=profile).exists())

        response = self.client.post("/api/v1/auth/login/", {
            "username": user.username, "password": password,
        }, format="json")
        self.assertEqual(response.status_code, 200)
        token = response.data["token"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        agreements = self.client.get("/api/v1/me/agreements/")
        self.assertEqual(agreements.status_code, 200)
        self.assertFalse(agreements.data["accepted"])
        self.assertEqual(self.client.put("/api/v1/me/agreements/", {
            "version": settings.ACCOUNT_AGREEMENT_VERSION, "accepted": True,
        }, format="json").status_code, 200)

        # A fresh client request uses stored authentication/acceptance rather
        # than view state or force_authenticate bypassing real token lookup.
        reopened = APIClient()
        reopened.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        self.assertTrue(reopened.get("/api/v1/me/agreements/").data["accepted"])
        capability = reopened.get("/api/v1/workouts/capabilities/")
        self.assertEqual(capability.status_code, 200)
        self.assertEqual(capability.data, {"planned_sets_version": 1})
        self.assertFalse(WorkoutTemplate.objects.filter(owner=profile).exists())
