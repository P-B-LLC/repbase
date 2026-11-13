"""Wearable consent, private summaries and retry-safe source imports."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier, Event
from unittest import skipUnless
from unittest.mock import patch

from django.db import close_old_connections, connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase, APITransactionTestCase

from .fixtures_for_tests import FAST_PASSWORDS, make_account
from .health_imports import import_health_workouts
from .health_models import AppleHealthConnection, HealthWorkoutRecord
from .health_serializers import HealthWorkoutImportSerializer
from .models import (DailyStepCount, Exercise, SessionExercise, SetEntry, WorkoutSession,
                     WorkoutTemplate, performed_sessions)

STATUS = "/api/v1/integrations/apple-health/"
IMPORT = "/api/v1/sessions/import-health/"
LIST = "/api/v1/integrations/workouts/"


def entry(**changes):
    start = timezone.now().replace(microsecond=0) - timedelta(hours=3)
    return {"external_id": "watch-1", "activity": "lifting", "workout_activity": "weightlifting",
            "started_at": start.isoformat(), "ended_at": (start + timedelta(hours=1)).isoformat(),
            "duration_seconds": 3000, "active_energy_kcal": "420.50", "average_heart_rate_bpm": "120.25",
            "max_heart_rate_bpm": "160.00", "source_name": "Apple Watch", "source_bundle_id": "com.apple.health",
            "device_name": "Apple Watch", **changes}


@FAST_PASSWORDS
class HealthIntegrationTests(APITestCase):
    def setUp(self):
        _, self.owner, self.token = make_account("health-owner")
        _, self.other, self.other_token = make_account("health-other")
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")

    def enable(self):
        result = self.client.patch(STATUS, {"enabled": True, "consent_version": "apple-health-v1"}, format="json")
        self.assertEqual(result.status_code, 200, result.data)

    def send(self, value=None):
        return self.client.post(IMPORT, {"workouts": [value or entry()]}, format="json")

    def test_status_starts_disconnected_without_creating_consent(self):
        response = self.client.get(STATUS)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["enabled"])
        self.assertFalse(response.data["garmin_direct_available"])
        self.assertEqual(response.data["schema_version"], 1)
        self.assertIsNone(response.data["last_sync_at"])
        self.assertFalse(AppleHealthConnection.objects.exists())

    def test_requires_current_explicit_consent(self):
        for value in ({"enabled": True}, {"enabled": True, "consent_version": "old"}):
            self.assertEqual(self.client.patch(STATUS, value, format="json").status_code, 400)
        self.assertEqual(self.send().status_code, 403)
        response = self.client.post("/api/v1/step-counts/record/", {"days": [{"day": "2026-01-01", "steps": 20}]}, format="json")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(DailyStepCount.objects.exists())
        self.assertFalse(WorkoutSession.objects.exists())

    def test_anonymous_requests_are_denied(self):
        self.client.credentials()
        self.assertEqual(self.client.get(STATUS).status_code, 401)
        self.assertEqual(self.client.get(LIST).status_code, 401)
        self.assertEqual(self.send().status_code, 401)
        self.assertEqual(self.client.patch(STATUS, {"enabled": False}, format="json").status_code, 401)

    def test_import_metrics_and_retry_are_idempotent(self):
        self.enable()
        value = entry()
        first = self.send(value)
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(first.data["imported"], 1)
        again = self.send(value)
        self.assertEqual(again.data["already_imported"], 1)
        self.assertEqual(HealthWorkoutRecord.objects.count(), 1)
        self.assertEqual(WorkoutSession.objects.count(), 1)
        summary = self.client.get(LIST).data["results"][0]
        self.assertEqual(summary["active_energy_kcal"], "420.50")
        self.assertEqual(summary["duration_seconds"], 3000)
        self.assertEqual(summary["provider"], "apple_health")
        session = self.client.get(f"/api/v1/sessions/{summary['session']}/").data
        self.assertEqual(session["health_summary"], summary)
        self.assertIsNotNone(self.client.get(STATUS).data["last_sync_at"])

    def test_legacy_payload_still_imports_after_consent(self):
        self.enable()
        value = entry()
        value = {key: value[key] for key in ("external_id", "activity", "started_at", "ended_at")}
        result = self.send(value)
        self.assertEqual(result.status_code, 200, result.data)
        record = HealthWorkoutRecord.objects.get()
        self.assertEqual(record.duration_seconds, 3600)
        self.assertIsNone(record.active_energy_kcal)
        self.assertEqual(record.workout_activity, "weightlifting")

    def test_distinct_accounts_can_import_same_external_id_privately(self):
        self.enable()
        self.send()
        owner_record = HealthWorkoutRecord.objects.get()
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.other_token.key}")
        self.assertFalse(self.client.get(STATUS).data["enabled"])
        self.assertEqual(self.client.get(LIST).data["count"], 0)
        self.assertEqual(self.client.get(f"/api/v1/sessions/{owner_record.session_id}/").status_code, 404)
        self.enable()
        self.assertEqual(self.send().data["imported"], 1)
        self.assertEqual(HealthWorkoutRecord.objects.count(), 2)
        self.assertEqual(self.client.get(LIST).data["count"], 1)

    def test_disconnect_prevents_future_writes_but_retains_history(self):
        self.enable()
        value = entry()
        self.send(value)
        last = self.client.get(STATUS).data["last_sync_at"]
        self.assertEqual(self.client.patch(STATUS, {"enabled": False}, format="json").status_code, 200)
        self.assertEqual(self.send(value).status_code, 403)
        self.assertEqual(self.client.post("/api/v1/step-counts/record/", {"days": []}, format="json").status_code, 403)
        self.assertEqual(self.client.get(LIST).data["count"], 1)
        self.assertEqual(self.client.get(STATUS).data["last_sync_at"], last)
        self.enable()
        self.assertEqual(self.send(value).data["already_imported"], 1)

    def test_legacy_retry_cannot_erase_pause_aware_duration(self):
        self.enable()
        value = entry()
        self.send(value)
        value.pop("duration_seconds")
        self.assertEqual(self.send(value).data["already_imported"], 1)
        self.assertEqual(HealthWorkoutRecord.objects.get().duration_seconds, 3000)

    def test_reused_identity_cannot_change_activity_or_time(self):
        self.enable()
        value = entry()
        self.send(value)
        for change in ({"workout_activity": "yoga"}, {"started_at": (timezone.now() - timedelta(hours=4)).isoformat()}):
            self.assertEqual(self.send({**value, **change}).status_code, 400)
        self.assertEqual(HealthWorkoutRecord.objects.get().workout_activity, "weightlifting")

    def test_partial_heart_rate_update_checks_existing_values(self):
        self.enable()
        value = entry()
        self.send(value)
        value.pop("max_heart_rate_bpm")
        self.assertEqual(self.send({**value, "average_heart_rate_bpm": "180"}).status_code, 400)

    def test_late_batch_conflict_rolls_back_new_workout_and_sync_stamp(self):
        self.enable()
        value = entry()
        self.send(value)
        before = AppleHealthConnection.objects.get(owner=self.owner).last_sync_at
        invalid = {**value, "average_heart_rate_bpm": "180"}
        invalid.pop("max_heart_rate_bpm")
        response = self.client.post(IMPORT, {"workouts": [entry(external_id="new-id"), invalid]}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(HealthWorkoutRecord.objects.count(), 1)
        self.assertEqual(WorkoutSession.objects.count(), 1)
        self.assertEqual(AppleHealthConnection.objects.get(owner=self.owner).last_sync_at, before)

    def test_health_distance_correction_never_overwrites_existing_gps(self):
        self.enable()
        value = entry(activity="running", workout_activity="running", distance_km="1.000")
        self.send(value)
        session = HealthWorkoutRecord.objects.get().session
        session.recorded_distance_km = Decimal("3.000")
        session.route_summary = {"route_distance_km": 3.0}
        session.save()
        self.assertEqual(self.send({**value, "distance_km": "2.000"}).data["updated"], 1)
        session.refresh_from_db()
        self.assertEqual(session.recorded_distance_km, Decimal("3.000"))
        self.assertEqual(session.health_distance_km, Decimal("2.000"))

    def test_partial_updates_preserve_missing_metrics_and_zero_energy(self):
        self.enable()
        value = entry()
        self.send(value)
        changed = {**value, "active_energy_kcal": "0.00", "average_heart_rate_bpm": None}
        changed.pop("max_heart_rate_bpm")
        self.assertEqual(self.send(changed).data["updated"], 1)
        record = HealthWorkoutRecord.objects.get()
        self.assertEqual(record.active_energy_kcal, Decimal("0"))
        self.assertEqual(record.average_heart_rate_bpm, Decimal("120.25"))
        self.assertEqual(record.max_heart_rate_bpm, Decimal("160"))

    def test_historical_external_identity_is_upgraded_not_duplicated(self):
        self.enable()
        value = entry()
        workout = WorkoutTemplate.objects.create(owner=self.owner, name="Old", workout_type="lifting")
        old = WorkoutSession.objects.create(repbase_user=self.owner, workout=workout,
            health_external_id=value["external_id"], started_at=value["started_at"], ended_at=value["ended_at"], status="completed")
        self.assertEqual(self.send(value).data["updated"], 1)
        self.assertEqual(HealthWorkoutRecord.objects.get().session_id, old.pk)
        self.assertEqual(WorkoutSession.objects.count(), 1)

    def native(self, value, name="Legs", **changes):
        workout = WorkoutTemplate.objects.create(owner=self.owner, name=name, activity=value["workout_activity"], workout_type=value["activity"])
        return WorkoutSession.objects.create(repbase_user=self.owner, workout=workout, status="completed",
            started_at=value["started_at"], ended_at=value["ended_at"], **changes)

    def test_exact_native_match_enriches_without_replacing_logs_or_manual_data(self):
        self.enable()
        value = entry()
        native = self.native(value, manual_calories_kcal=123, notes="Private note", recorded_distance_km=Decimal("2"))
        exercise = Exercise.objects.create(name="Squat")
        line = SessionExercise.objects.create(session=native, exercise=exercise, order=1)
        logged = SetEntry.objects.create(session_exercise=line, set_number=1, reps=8, weight_kg=Decimal("70"))
        result = self.send(value)
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result.data["linked"], 1)
        self.assertEqual(result.data["imported"], 0)
        native.refresh_from_db()
        logged.refresh_from_db()
        self.assertIsNone(native.health_external_id)
        self.assertEqual(native.manual_calories_kcal, 123)
        self.assertEqual(native.notes, "Private note")
        self.assertEqual(native.recorded_distance_km, Decimal("2"))
        self.assertEqual(logged.weight_kg, Decimal("70"))
        self.assertEqual(WorkoutSession.objects.count(), 1)

    def test_ambiguous_native_matches_are_not_guessed(self):
        self.enable()
        value = entry()
        self.native(value, "First")
        self.native(value, "Second")
        result = self.send(value)
        self.assertEqual(result.data["skipped_overlapping"], 1)
        self.assertFalse(HealthWorkoutRecord.objects.exists())

    def test_partial_overlap_does_not_attach_to_wrong_workout(self):
        self.enable()
        value = entry()
        native = self.native(value)
        native.started_at = timezone.now() - timedelta(hours=4)
        native.save()
        self.assertEqual(self.send(value).data["skipped_overlapping"], 1)

    def test_nearby_short_nonoverlapping_sessions_are_not_linked(self):
        self.enable()
        start = timezone.now() - timedelta(hours=3)
        value = entry(started_at=start.isoformat(), ended_at=(start + timedelta(seconds=20)).isoformat(), duration_seconds=20)
        native = self.native(value)
        native.started_at = start + timedelta(seconds=30)
        native.ended_at = start + timedelta(seconds=50)
        native.save()
        response = self.send(value)
        self.assertEqual(response.data["linked"], 0)
        self.assertEqual(response.data["imported"], 1)
        self.assertNotEqual(HealthWorkoutRecord.objects.get().session_id, native.pk)

    def test_template_names_never_claim_user_created_workouts(self):
        self.enable()
        existing = WorkoutTemplate.objects.create(owner=self.owner, name="Apple Health · Weightlifting", activity="yoga")
        self.send()
        record = HealthWorkoutRecord.objects.get()
        self.assertNotEqual(record.session.workout_id, existing.pk)
        self.assertEqual(record.session.workout.activity, "weightlifting")
        self.assertEqual(record.session.workout.health_import_activity, "weightlifting")

    def test_specific_sport_and_source_are_retained(self):
        self.enable()
        self.send(entry(workout_activity="yoga", source_name="Garmin Connect", source_bundle_id="com.garmin.connect.mobile"))
        record = HealthWorkoutRecord.objects.get()
        self.assertEqual(record.workout_activity, "yoga")
        self.assertEqual(record.session.workout.activity, "yoga")
        response = self.client.get(LIST + "?source=garmin")
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["provider"], "apple_health")
        self.assertEqual(self.client.get(LIST + "?source=apple_health").data["count"], 0)

    def test_arbitrary_name_does_not_claim_garmin_source(self):
        self.enable()
        self.send(entry(source_name="Garmin Connect", source_bundle_id="example.other"))
        self.assertEqual(HealthWorkoutRecord.objects.get().source, "apple_health")

    def test_invalid_metrics_and_times_are_rejected_atomically(self):
        self.enable()
        for fields in ({"distance_km": "-1"}, {"active_energy_kcal": "-1"},
            {"average_heart_rate_bpm": "0"}, {"max_heart_rate_bpm": "301"},
            {"average_heart_rate_bpm": "170", "max_heart_rate_bpm": "160"},
            {"duration_seconds": 3602}, {"duration_seconds": 0}, {"duration_seconds": "NaN"},
            {"workout_activity": "cycling"}, {"device_name": "x\u0000y"},
            {"ended_at": (timezone.now() + timedelta(days=1)).isoformat()}):
            with self.subTest(fields=fields):
                response = self.send(entry(**fields))
                self.assertEqual(response.status_code, 400, response.data)
        value = entry()
        value["ended_at"] = value["started_at"]
        self.assertEqual(self.send(value).status_code, 400)
        self.assertFalse(HealthWorkoutRecord.objects.exists())

    def test_duplicate_batch_ids_and_big_batches_are_rejected(self):
        self.enable()
        value = entry()
        for values in ([value, value], [entry(external_id=str(i)) for i in range(501)]):
            self.assertEqual(self.client.post(IMPORT, {"workouts": values}, format="json").status_code, 400)

    def test_wearable_strength_training_counts_without_fake_sets(self):
        self.enable()
        self.send()
        record = HealthWorkoutRecord.objects.get()
        self.assertTrue(performed_sessions().filter(pk=record.session_id).exists())
        result = self.client.get("/api/v1/sessions/training-stats/")
        self.assertEqual(result.data["total_workouts"], 1)
        self.assertFalse(SetEntry.objects.exists())

    def test_session_patch_cannot_write_health_fields(self):
        self.enable()
        self.send()
        record = HealthWorkoutRecord.objects.get()
        response = self.client.patch(f"/api/v1/sessions/{record.session_id}/", {
            "health_summary": {"active_energy_kcal": "1"}}, format="json")
        self.assertEqual(response.status_code, 200)
        record.refresh_from_db()
        self.assertEqual(record.active_energy_kcal, Decimal("420.50"))

    def test_private_metrics_not_in_social_or_shared_calendar_contract(self):
        from .serializers import PostWorkoutSerializer
        from .calendar_sharing_serializers import SharedCalendarItemSerializer
        self.assertNotIn("health_summary", PostWorkoutSerializer().fields)
        self.assertNotIn("health_summary", SharedCalendarItemSerializer().fields)

    def test_list_validation_pagination_and_constant_query_count(self):
        self.enable()
        self.send()
        with CaptureQueriesContext(connection) as small:
            self.client.get(LIST)
        record = HealthWorkoutRecord.objects.get()
        for i in range(51):
            session = WorkoutSession.objects.create(repbase_user=self.owner, workout=record.session.workout,
                status="completed", started_at=record.started_at, ended_at=record.ended_at)
            HealthWorkoutRecord.objects.create(owner=self.owner, session=session, external_id=f"bulk-{i}",
                workout_activity="weightlifting", started_at=record.started_at,
                ended_at=record.ended_at, duration_seconds=60)
        with CaptureQueriesContext(connection) as large:
            response = self.client.get(LIST)
        self.assertEqual(len(small), len(large))
        self.assertEqual(len(response.data["results"]), 50)
        self.assertIsNotNone(response.data["next"])
        self.assertEqual(self.client.get(LIST + "?source=anything").status_code, 400)
        self.assertEqual(self.client.get(LIST + "?since=2026-02-30").status_code, 400)

    def test_schema_keeps_owner_session_addition_optional_and_read_only(self):
        from drf_spectacular.generators import SchemaGenerator
        schema = SchemaGenerator().get_schema(request=None, public=True)
        values = schema["components"]["schemas"]
        session = values["WorkoutSession"]
        self.assertNotIn("health_summary", session["required"])
        self.assertTrue(session["properties"]["health_summary"]["nullable"])
        self.assertNotIn("health_summary", values["WorkoutSessionRequest"]["properties"])
        self.assertNotIn("health_import_activity", values["WorkoutTemplate"]["properties"])
        self.assertEqual(values["HealthWorkoutRequest"]["properties"]["activity"]["$ref"],
                         "#/components/schemas/WorkoutTypeEnum")


@FAST_PASSWORDS
@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL account row locks")
class HealthIntegrationConcurrencyTests(APITransactionTestCase):
    def setUp(self):
        _, self.owner, self.token = make_account("concurrent-health")
        AppleHealthConnection.objects.create(owner=self.owner, enabled=True, consent_version="apple-health-v1")

    def worker(self, start, payload):
        close_old_connections()
        try:
            client = APIClient()
            client.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")
            start.wait(timeout=10)
            response = client.post(IMPORT, {"workouts": [payload]}, format="json")
            return response.status_code, response.data
        finally:
            close_old_connections()

    def test_concurrent_retries_create_exactly_one_workout(self):
        start = Barrier(2)
        payload = entry()
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(self.worker, start, payload) for _ in range(2)]
            results = [job.result(timeout=25) for job in jobs]
        self.assertEqual([value[0] for value in results], [200, 200])
        self.assertEqual(sum(value[1]["imported"] for value in results), 1)
        self.assertEqual(HealthWorkoutRecord.objects.count(), 1)
        self.assertEqual(WorkoutSession.objects.count(), 1)

    def test_disconnect_commits_before_waiting_import_and_prevents_write(self):
        from .models import RepbaseUser
        reached = Event()
        payload = entry()
        serializer = HealthWorkoutImportSerializer(data={"workouts": [payload]})
        serializer.is_valid(raise_exception=True)
        import core.health_imports as module
        original = module.lock_health_owner

        def waiting(owner):
            reached.set()
            return original(owner)

        def writer():
            close_old_connections()
            try:
                client = APIClient()
                client.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")
                return client.post(IMPORT, {"workouts": [payload]}, format="json").status_code
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                RepbaseUser.objects.select_for_update().get(pk=self.owner.pk)
                with patch.object(module, "lock_health_owner", waiting):
                    job = pool.submit(writer)
                    self.assertTrue(reached.wait(timeout=10))
                    AppleHealthConnection.objects.filter(owner=self.owner).update(enabled=False)
            self.assertEqual(job.result(timeout=20), 403)
        self.assertFalse(HealthWorkoutRecord.objects.exists())
