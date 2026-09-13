"""Two accounts must not be able to share an address, and a track must not be
measured four times to produce one number.

Unrelated fixes, one test file, because both are about the database doing
work the application was doing badly on its own.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from rest_framework.test import APITestCase

from .models import SessionRoutePoint, WorkoutSession
from .tests import RepbaseAPITestMixin

User = get_user_model()


class TheDatabaseEnforcesAccountIdentityTests(TestCase):
    """The serializer checks these too. That check is a read followed by a
    write, and between the two another request can commit the same address --
    which SQLite hides by allowing one writer at a time and PostgreSQL will
    not. This is the half that holds under concurrency.
    """

    def setUp(self):
        User.objects.create_user(
            username="taken", email="Person@Example.test", password="x" * 12
        )

    def test_the_same_email_in_a_different_case_is_refused(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                User.objects.create_user(
                    username="other", email="person@example.TEST", password="x" * 12
                )

    def test_the_same_username_in_a_different_case_is_refused(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                User.objects.create_user(
                    username="TAKEN", email="nobody@example.test", password="x" * 12
                )

    def test_accounts_without_an_email_do_not_collide(self):
        """The reason the email index is partial. Two superusers made without
        an address are the live data this was written against, and an empty
        string is not a duplicate of another empty string.
        """
        User.objects.create_user(username="first", email="", password="x" * 12)
        User.objects.create_user(username="second", email="", password="x" * 12)
        self.assertEqual(User.objects.filter(email="").count(), 2)

    def test_a_genuinely_different_address_is_still_allowed(self):
        User.objects.create_user(
            username="fine", email="someone.else@example.test", password="x" * 12
        )
        self.assertEqual(User.objects.count(), 2)


class TheTrackIsMeasuredOnceTests(RepbaseAPITestMixin, APITestCase):
    """Pace, average speed and moving pace all read route_distance_km, so one
    session used to run the haversine loop four times over every point.
    """

    def setUp(self):
        _, self.profile, _ = self.create_account("runner")
        self.session = WorkoutSession.objects.create(repbase_user=self.profile)
        start = timezone.now() - timedelta(minutes=30)
        SessionRoutePoint.objects.bulk_create([
            SessionRoutePoint(
                session=self.session,
                recorded_at=start + timedelta(seconds=10 * step),
                latitude=40.0 + step * 0.0005,
                longitude=-105.0,
                speed_mps=3.0,
            )
            for step in range(40)
        ])

    def test_reading_the_derived_values_walks_the_track_once(self):
        session = WorkoutSession.objects.get(pk=self.session.pk)
        first = session.route_distance_km
        self.assertIsNotNone(first)

        # Everything that depends on it, on the same instance, the way a
        # serializer reads them.
        session.pace_seconds_per_km
        session.average_speed_kmh
        session.moving_pace_seconds_per_km

        self.assertIs(session.route_distance_km, first)

    def test_a_fresh_instance_sees_points_added_since(self):
        """What the caching gives up, and why the upload endpoint re-fetches
        under select_for_update rather than reusing an instance."""
        session = WorkoutSession.objects.get(pk=self.session.pk)
        before = session.route_distance_km

        SessionRoutePoint.objects.create(
            session=self.session,
            recorded_at=timezone.now(),
            latitude=41.0,
            longitude=-105.0,
            speed_mps=3.0,
        )

        self.assertEqual(session.route_distance_km, before, "the old instance is a snapshot")
        reloaded = WorkoutSession.objects.get(pk=self.session.pk)
        self.assertGreater(reloaded.route_distance_km, before)

    def test_the_upload_endpoint_still_records_the_new_distance(self):
        """The one place that adds points and then reads the total. If
        caching had broken anything it would be here."""
        _, uploader, token = self.create_account("uploader")
        self.authenticate(token)
        mine = WorkoutSession.objects.create(repbase_user=uploader)
        start = timezone.now() - timedelta(minutes=5)
        response = self.client.post(
            f"/api/v1/sessions/{mine.id}/route/",
            {
                "points": [
                    {
                        "recorded_at": (start + timedelta(seconds=10 * n)).isoformat(),
                        "latitude": 40.0 + n * 0.001,
                        "longitude": -105.0,
                        "speed_mps": 3.0,
                    }
                    for n in range(10)
                ]
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        mine.refresh_from_db()
        self.assertIsNotNone(mine.recorded_distance_km)
        self.assertGreater(float(mine.recorded_distance_km), 0)
