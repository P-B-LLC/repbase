"""Apple Health import: the boundaries, the empty cases and the sync record.

The main module (test_health_integration) covers consent, retries, matching
and concurrency. This one pins the edges of what the import accepts and what
the connection reports afterwards.
"""
from datetime import timedelta

from django.utils import timezone
from rest_framework.test import APITestCase

from .fixtures_for_tests import FAST_PASSWORDS, make_account
from .health_models import AppleHealthConnection, HealthWorkoutRecord
from .health_serializers import HEALTH_CONSENT_VERSION
from .models import WorkoutSession
from .test_health_integration import IMPORT, LIST, STATUS, entry


@FAST_PASSWORDS
class HealthImportBoundaryTests(APITestCase):
    def setUp(self):
        _, self.owner, self.token = make_account("health-edges")
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")
        response = self.client.patch(STATUS, {"enabled": True, "consent_version": HEALTH_CONSENT_VERSION},
                                     format="json")
        self.assertEqual(response.status_code, 200, response.data)

    def send(self, *workouts):
        return self.client.post(IMPORT, {"workouts": list(workouts)}, format="json")

    def window(self, seconds, *, ago=timedelta(days=8)):
        start = timezone.now().replace(microsecond=0) - ago
        return {"started_at": start.isoformat(), "ended_at": (start + timedelta(seconds=seconds)).isoformat(),
                "duration_seconds": min(seconds, 7 * 24 * 3600)}

    # ---------------------------------------------------------- normal use

    def test_an_empty_batch_succeeds_and_records_a_sync_of_nothing(self):
        response = self.send()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["imported"], 0)
        self.assertEqual(response.data["already_imported"], 0)
        status = self.client.get(STATUS).data
        self.assertIsNotNone(status["last_sync_at"])
        self.assertEqual(status["last_sync_imported_count"], 0)
        self.assertEqual(status["last_sync_updated_count"], 0)
        self.assertFalse(WorkoutSession.objects.exists())

    def test_the_sync_counts_describe_the_latest_batch(self):
        self.send(entry(external_id="a", **self.window(1800, ago=timedelta(days=3))),
                  entry(external_id="b", **self.window(1800, ago=timedelta(days=2))))
        status = self.client.get(STATUS).data
        self.assertEqual(status["last_sync_imported_count"], 2)
        # The same two again: nothing new, so the latest sync imported none.
        self.send(entry(external_id="a", **self.window(1800, ago=timedelta(days=3))))
        status = self.client.get(STATUS).data
        self.assertEqual(status["last_sync_imported_count"], 0)

    def test_exactly_five_hundred_workouts_is_one_accepted_batch(self):
        workouts = [entry(external_id=f"w-{i}", **self.window(60, ago=timedelta(days=10, hours=i)))
                    for i in range(500)]
        response = self.send(*workouts)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["imported"], 500)
        self.assertEqual(HealthWorkoutRecord.objects.count(), 500)

    def test_since_keeps_workouts_from_that_day_on(self):
        old = entry(external_id="old", **self.window(1800, ago=timedelta(days=20)))
        recent = entry(external_id="recent", **self.window(1800, ago=timedelta(days=2)))
        self.assertEqual(self.send(old, recent).status_code, 200)
        cutoff = (timezone.now() - timedelta(days=5)).date().isoformat()
        response = self.client.get(LIST, {"since": cutoff})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["external_id"] for row in response.data["results"]], ["recent"])
        self.assertEqual(self.client.get(LIST, {"since": "2000-01-01"}).data["count"], 2)

    # --------------------------------------------------------- boundaries

    def test_a_workout_of_exactly_seven_days_is_accepted_and_longer_is_not(self):
        week = 7 * 24 * 3600
        accepted = entry(external_id="week", **self.window(week, ago=timedelta(days=8)))
        self.assertEqual(self.send(accepted).status_code, 200)
        refused = entry(external_id="long", **self.window(week + 1, ago=timedelta(days=9)))
        response = self.send(refused)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(HealthWorkoutRecord.objects.filter(external_id="long").count(), 0)

    def test_heart_rate_limits_are_inclusive(self):
        low = entry(external_id="low", average_heart_rate_bpm="20.00", max_heart_rate_bpm="20.00",
                    **self.window(1800, ago=timedelta(days=4)))
        high = entry(external_id="high", average_heart_rate_bpm="300.00", max_heart_rate_bpm="300.00",
                     **self.window(1800, ago=timedelta(days=3)))
        response = self.send(low, high)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["imported"], 2)
        for value in ("19.99", "300.01"):
            with self.subTest(value=value):
                bad = entry(external_id=f"bad-{value}", average_heart_rate_bpm=value, max_heart_rate_bpm="300.00",
                            **self.window(1800, ago=timedelta(days=5)))
                self.assertEqual(self.send(bad).status_code, 400)

    def test_external_ids_are_at_most_sixty_four_characters(self):
        self.assertEqual(self.send(entry(external_id="x" * 64, **self.window(600))).status_code, 200)
        self.assertEqual(self.send(entry(external_id="y" * 65, **self.window(600, ago=timedelta(days=9)))).status_code,
                         400)

    # --------------------------------------------------------- bad input

    def test_missing_required_fields_are_refused_by_name(self):
        for field in ("external_id", "activity", "started_at", "ended_at"):
            with self.subTest(field=field):
                value = entry()
                del value[field]
                response = self.send(value)
                self.assertEqual(response.status_code, 400)
                self.assertIn(field, str(response.data))
        self.assertFalse(HealthWorkoutRecord.objects.exists())

    def test_an_unknown_activity_family_is_refused(self):
        response = self.send(entry(activity="yoga"))
        self.assertEqual(response.status_code, 400)
        self.assertIn("activity", str(response.data))

    def test_a_body_that_is_not_a_batch_is_refused(self):
        for body in ({}, {"workouts": "nope"}, {"workouts": [None]}, {"workouts": [{"external_id": 5}]}):
            with self.subTest(body=body):
                self.assertEqual(self.client.post(IMPORT, body, format="json").status_code, 400)
        self.assertFalse(WorkoutSession.objects.exists())

    def test_one_bad_workout_refuses_the_whole_batch(self):
        good = entry(external_id="good", **self.window(1800, ago=timedelta(days=4)))
        bad = entry(external_id="bad", average_heart_rate_bpm="999")
        self.assertEqual(self.send(good, bad).status_code, 400)
        self.assertFalse(HealthWorkoutRecord.objects.exists())


@FAST_PASSWORDS
class HealthConnectionSettingsTests(APITestCase):
    def setUp(self):
        _, self.owner, self.token = make_account("health-settings")
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")

    def test_enabled_must_be_given_as_a_boolean(self):
        for body in ({}, {"enabled": "maybe"}, {"enabled": None}, {"consent_version": HEALTH_CONSENT_VERSION}):
            with self.subTest(body=body):
                self.assertEqual(self.client.patch(STATUS, body, format="json").status_code, 400)
        self.assertFalse(AppleHealthConnection.objects.exists())

    def test_disabling_needs_no_consent_and_keeps_the_last_agreement(self):
        self.client.patch(STATUS, {"enabled": True, "consent_version": HEALTH_CONSENT_VERSION}, format="json")
        response = self.client.patch(STATUS, {"enabled": False}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["enabled"])
        self.assertEqual(response.data["consent_version"], HEALTH_CONSENT_VERSION)

    def test_enabling_again_refreshes_when_consent_was_given(self):
        self.client.patch(STATUS, {"enabled": True, "consent_version": HEALTH_CONSENT_VERSION}, format="json")
        first = AppleHealthConnection.objects.get(owner=self.owner).consented_at
        AppleHealthConnection.objects.filter(owner=self.owner).update(consented_at=first - timedelta(days=30))
        self.client.patch(STATUS, {"enabled": False}, format="json")
        self.client.patch(STATUS, {"enabled": True, "consent_version": HEALTH_CONSENT_VERSION}, format="json")
        again = AppleHealthConnection.objects.get(owner=self.owner).consented_at
        self.assertGreater(again, first - timedelta(days=30))

    def test_disabling_before_ever_connecting_makes_a_paused_connection(self):
        response = self.client.patch(STATUS, {"enabled": False}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["enabled"])
        self.assertIsNone(response.data["consent_version"])
        self.assertEqual(self.client.post(IMPORT, {"workouts": [entry()]}, format="json").status_code, 403)

    def test_another_account_never_sees_these_workouts(self):
        self.client.patch(STATUS, {"enabled": True, "consent_version": HEALTH_CONSENT_VERSION}, format="json")
        self.client.post(IMPORT, {"workouts": [entry()]}, format="json")
        _, _, other = make_account("health-settings-other")
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {other.key}")
        self.assertEqual(self.client.get(LIST).data["count"], 0)
        self.assertFalse(self.client.get(STATUS).data["enabled"])
