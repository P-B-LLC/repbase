"""Two accounts must not be able to share an address, and a track must not be
measured four times to produce one number.

Unrelated fixes, one test file, because both are about the database doing
work the application was doing badly on its own.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext
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


class TheSessionListDoesNotWalkAnyTrackTests(RepbaseAPITestMixin, APITestCase):
    """The cost this was about is a query count, so the test is a query count.

    Four of the track-derived values are read from list responses -- the app
    plots distance, pace and climb per session to show progress on a named
    workout -- so the list cannot simply stop reporting them. It can stop
    recomputing them, which is what the stored summary is for.
    """

    def setUp(self):
        _, self.profile, token = self.create_account("runner")
        self.authenticate(token)

    def session_with_a_track(self, points=60):
        session = WorkoutSession.objects.create(
            repbase_user=self.profile, status=WorkoutSession.Status.COMPLETED
        )
        start = timezone.now() - timedelta(minutes=40)
        SessionRoutePoint.objects.bulk_create([
            SessionRoutePoint(
                session=session,
                recorded_at=start + timedelta(seconds=10 * step),
                latitude=40.0 + step * 0.0004,
                longitude=-105.0,
                speed_mps=3.0,
                altitude_m=1600 + step,
            )
            for step in range(points)
        ])
        session.recompute_route_summary()
        session.save(update_fields=["route_summary"])
        return session

    def test_the_list_never_reads_the_point_table(self):
        """The assertion that actually holds.

        A query count alone would not have caught this: prefetch_related is
        one query no matter how many sessions, so the old code passed "more
        sessions, same number of queries" while pulling every GPS fix of every
        session in the page into memory. The cost was rows and arithmetic, not
        round trips. So this asserts the table is not read at all.
        """
        for _ in range(3):
            self.session_with_a_track()

        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.client.get("/api/v1/sessions/").status_code, 200)

        touched = [q["sql"] for q in queries.captured_queries if "sessionroutepoint" in q["sql"].lower()]
        self.assertEqual(touched, [], "the session list read the point table")

    def test_more_sessions_do_not_cost_more_queries(self):
        """And with the prefetch gone, nothing may quietly become an N+1."""
        self.session_with_a_track()
        with CaptureQueriesContext(connection) as first:
            self.assertEqual(self.client.get("/api/v1/sessions/").status_code, 200)

        for _ in range(4):
            self.session_with_a_track()
        with self.assertNumQueries(len(first.captured_queries)):
            response = self.client.get("/api/v1/sessions/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["results"]), 5)

    def test_the_list_still_reports_what_the_app_plots(self):
        """sessionHistory reads these four off the list to draw a progress
        chart. Storing the summary must not quietly empty them."""
        self.session_with_a_track()
        row = self.client.get("/api/v1/sessions/").data["results"][0]
        for field in (
            "route_distance_km",
            "pace_seconds_per_km",
            "moving_pace_seconds_per_km",
            "elevation_gain_m",
        ):
            self.assertIsNotNone(row[field], f"{field} went missing from the list")
        self.assertTrue(row["splits"])
        self.assertIsNotNone(row["max_speed_kmh"])

    def test_the_stored_summary_matches_what_the_points_say(self):
        """Otherwise this is a fast way to report the wrong number."""
        session = self.session_with_a_track()
        stored = dict(session.route_summary)

        fresh = WorkoutSession.objects.get(pk=session.pk)
        fresh.route_summary = None
        for field in WorkoutSession.ROUTE_SUMMARY_FIELDS:
            self.assertEqual(
                stored[field], getattr(fresh, field), f"{field} disagrees with the track"
            )

    def test_uploading_more_points_updates_the_summary(self):
        session = WorkoutSession.objects.create(repbase_user=self.profile)
        start = timezone.now() - timedelta(minutes=10)

        def upload(offset):
            return self.client.post(
                f"/api/v1/sessions/{session.id}/route/",
                {"points": [
                    {
                        "recorded_at": (start + timedelta(seconds=10 * (offset + n))).isoformat(),
                        "latitude": 40.0 + (offset + n) * 0.001,
                        "longitude": -105.0,
                        "speed_mps": 3.0,
                    }
                    for n in range(10)
                ]},
                format="json",
            )

        self.assertEqual(upload(0).status_code, 200)
        session.refresh_from_db()
        first = session.route_summary["route_distance_km"]

        self.assertEqual(upload(10).status_code, 200)
        session.refresh_from_db()
        self.assertGreater(session.route_summary["route_distance_km"], first)
        self.assertEqual(
            round(float(session.recorded_distance_km), 3),
            round(session.route_summary["route_distance_km"], 3),
        )


class ASessionWithNoTemplateStillDecodesTests(RepbaseAPITestMixin, APITestCase):
    """Deleting a workout template must not make its sessions unreadable.

    `WorkoutSession.workout` is SET_NULL, so removing a template nulls it on
    every session that used it. With a dotted source and no allow_null, DRF
    does not send null for such a field -- it omits the key -- and the
    generated Swift client decodes that key as required and throws. One
    deletion made a whole training history undecodable, and a route upload
    answered 200 and then failed in the app for points the server had stored.

    Found by running the GPS path end to end in the simulator, not by reading
    the code: the app always attaches a workout when it creates a session, so
    nothing it does produces this shape.
    """

    def setUp(self):
        _, self.profile, token = self.create_account("lifter")
        self.authenticate(token)
        self.session = WorkoutSession.objects.create(repbase_user=self.profile)

    def test_every_documented_key_is_present_when_there_is_no_template(self):
        body = self.client.get(f"/api/v1/sessions/{self.session.id}/").data
        for key in ("workout", "workout_name", "workout_type"):
            self.assertIn(key, body, f"{key} was dropped rather than sent as null")
            self.assertIsNone(body[key])

    def test_the_list_keeps_them_too(self):
        row = self.client.get("/api/v1/sessions/").data["results"][0]
        for key in ("workout_name", "workout_type"):
            self.assertIn(key, row, f"{key} was dropped from the list")

    def test_deleting_the_template_does_not_break_its_sessions(self):
        """The path a real user takes to reach this."""
        from .models import WorkoutTemplate

        template = WorkoutTemplate.objects.create(owner=self.profile, name="Long run")
        session = WorkoutSession.objects.create(
            repbase_user=self.profile, workout=template
        )
        named = self.client.get(f"/api/v1/sessions/{session.id}/").data
        self.assertEqual(named["workout_name"], "Long run")

        template.delete()

        orphaned = self.client.get(f"/api/v1/sessions/{session.id}/").data
        self.assertIn("workout_name", orphaned)
        self.assertIsNone(orphaned["workout_name"])

    def test_uploading_a_route_to_such_a_session_answers_something_readable(self):
        """What the probe actually hit: the upload succeeded and the response
        it returned could not be decoded."""
        start = timezone.now() - timedelta(minutes=5)
        response = self.client.post(
            f"/api/v1/sessions/{self.session.id}/route/",
            {"points": [
                {
                    "recorded_at": (start + timedelta(seconds=10 * n)).isoformat(),
                    "latitude": 40.0 + n * 0.0005,
                    "longitude": -105.27,
                    "speed_mps": 3.0,
                }
                for n in range(8)
            ]},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn("workout_name", response.data)
        self.assertIsNotNone(response.data["route_distance_km"])
