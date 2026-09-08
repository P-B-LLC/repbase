from django.contrib.auth import get_user_model
from rest_framework.authtoken.models import Token
from rest_framework.test import APIRequestFactory, APITestCase, force_authenticate

from .models import RepbaseUser, WorkoutSession
from .views import LogoutView


class OfflineRecoveryTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="recovery-test")
        self.profile = RepbaseUser.objects.create(user=self.user)
        self.token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")
        self.session = WorkoutSession.objects.create(repbase_user=self.profile)
        self.url = f"/api/v1/sessions/{self.session.pk}/route/"
        self.point = {
            "latitude": "41.000000", "longitude": "-87.000000",
            "recorded_at": "2026-09-08T12:00:00.123Z", "speed_mps": "2.00",
        }

    def test_repeated_and_overlapping_uploads_are_idempotent(self):
        second = dict(self.point, recorded_at="2026-09-08T12:00:01.123Z")
        for points in ([self.point, self.point], [self.point], [self.point, second]):
            response = self.client.post(self.url, {"points": points}, format="json")
            self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.session.route_points.count(), 2)

    def test_route_cannot_be_written_by_another_account(self):
        other = get_user_model().objects.create_user(username="other-recovery")
        RepbaseUser.objects.create(user=other)
        token = Token.objects.create(user=other)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        response = self.client.post(self.url, {"points": [self.point]}, format="json")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.session.route_points.count(), 0)

    def test_route_read_matches_paginated_ios_contract(self):
        self.client.post(self.url, {"points": [self.point]}, format="json")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(len(response.data["results"]), 1)

    def test_delayed_logout_does_not_revoke_replacement_token(self):
        # Authentication happened before rotation; dispatch happens after it.
        old_key = self.token.key
        Token.objects.filter(pk=old_key).delete()
        replacement = Token.objects.create(user=self.user)
        request = APIRequestFactory().post("/api/v1/auth/logout/")
        force_authenticate(request, user=self.user, token=self.token)
        response = LogoutView.as_view()(request)
        self.assertEqual(response.status_code, 204)
        self.assertTrue(Token.objects.filter(pk=replacement.pk).exists())

    def test_logout_revokes_request_token(self):
        response = self.client.post("/api/v1/auth/logout/")
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Token.objects.filter(pk=self.token.pk).exists())
