from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from .models import (
    Exercise,
    RepbaseUser,
    SessionExercise,
    SetEntry,
    WorkoutExercise,
    WorkoutSession,
    WorkoutTemplate,
)


User = get_user_model()


class RepbaseAPITestCase(APITestCase):
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
        self.assertNotIn("weight_kg", profile)
        self.assertNotIn("birthdate", profile)

    def test_me_endpoint_updates_only_authenticated_profile(self):
        _, profile, token = self.create_account("alice")
        self.authenticate(token)

        response = self.client.patch(
            "/api/v1/me/",
            {"gym": "Repbase Gym", "target_weight_kg": "75.00"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        profile.refresh_from_db()
        self.assertEqual(profile.gym, "Repbase Gym")
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
