"""Small bounded imports under the same account lock as disconnect.

No provider HTTP requests, raw heart-rate samples, credentials or GPS routes are
stored here. Garmin provenance means a Garmin source inside Apple Health only.
"""
from datetime import timedelta
import uuid

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from .health_models import AppleHealthConnection, HealthWorkoutRecord
from .health_serializers import HEALTH_CONSENT_VERSION

METRICS = ("distance_km", "active_energy_kcal", "average_heart_rate_bpm", "max_heart_rate_bpm")
SOURCE_FIELDS = ("source_name", "source_bundle_id", "device_name")


def lock_health_owner(owner):
    from .models import RepbaseUser
    RepbaseUser.objects.select_for_update().get(pk=owner.pk)


def require_health_connection(owner):
    connection = AppleHealthConnection.objects.filter(owner=owner, enabled=True,
        consent_version=HEALTH_CONSENT_VERSION).first()
    if connection is None:
        raise PermissionDenied("Connect Apple Health and agree to import before syncing.")
    return connection


def _source(bundle):
    return "garmin" if bundle == "com.garmin.connect.mobile" else "apple_health"


def _template(owner, entry):
    from .models import WorkoutTemplate
    activity = entry["workout_activity"]
    found = WorkoutTemplate.objects.filter(owner=owner, health_import_activity=activity).first()
    if found and found.activity == activity and found.workout_type == entry["activity"]:
        return found
    if found:
        # An owner may edit their template. Preserve it but stop reusing it.
        found.health_import_activity = None
        found.save(update_fields=["health_import_activity"])
    base = f"Apple Health · {activity.replace('_', ' ').title()}"
    name = base
    if WorkoutTemplate.objects.filter(owner=owner, name=name).exists():
        # A maliciously crowded name space must not turn one import into an
        # unbounded query loop. The database still enforces uniqueness.
        name = f"{base} ({uuid.uuid4()})"
    return WorkoutTemplate.objects.create(owner=owner, name=name, workout_type=entry["activity"],
                                         activity=activity, health_import_activity=activity)


def _native_match(owner, entry):
    from .models import WorkoutSession
    # An overlap alone is not evidence that these are the same workout.
    # Only a single, completed, same-activity session with near-identical
    # boundaries is eligible, and one existing source can never be replaced.
    candidates = list(WorkoutSession.objects.filter(
        repbase_user=owner, status=WorkoutSession.Status.COMPLETED,
        health_external_id__isnull=True, health_summary__isnull=True,
        workout__activity=entry["workout_activity"],
        started_at__gte=entry["started_at"] - timedelta(seconds=90),
        started_at__lte=entry["started_at"] + timedelta(seconds=90),
        ended_at__gte=entry["ended_at"] - timedelta(seconds=90),
        ended_at__lte=entry["ended_at"] + timedelta(seconds=90),
    ).select_for_update(of=("self",)).order_by("pk")[:2])
    if len(candidates) != 1:
        return None
    candidate = candidates[0]
    overlap = (min(candidate.ended_at, entry["ended_at"]) -
               max(candidate.started_at, entry["started_at"])).total_seconds()
    native_elapsed = (candidate.ended_at - candidate.started_at).total_seconds()
    imported_elapsed = (entry["ended_at"] - entry["started_at"]).total_seconds()
    # Near boundaries on tiny workouts can otherwise match disjoint efforts.
    return candidate if overlap > 0 and overlap >= .9 * max(native_elapsed, imported_elapsed) else None


def _values(entry, previous=None):
    values = {name: entry[name] for name in ("workout_activity", "started_at", "ended_at")}
    if "duration_seconds" in entry:
        values["duration_seconds"] = entry["duration_seconds"]
    elif previous is None:
        values["duration_seconds"] = (entry["ended_at"] - entry["started_at"]).total_seconds()
    for field in (*METRICS, *SOURCE_FIELDS):
        # A denied/missing quantity never becomes zero or erases a reading.
        if entry.get(field) is not None:
            values[field] = entry[field]
    bundle = values.get("source_bundle_id", getattr(previous, "source_bundle_id", None))
    values["source"] = _source(bundle)
    average = values.get("average_heart_rate_bpm", getattr(previous, "average_heart_rate_bpm", None))
    maximum = values.get("max_heart_rate_bpm", getattr(previous, "max_heart_rate_bpm", None))
    if average is not None and maximum is not None and average > maximum:
        raise ValidationError({"workouts": "Maximum heart rate cannot be below the stored average heart rate."})
    return values


def import_health_workouts(owner, incoming):
    from .models import WorkoutSession
    result = dict(imported=0, skipped_overlapping=0, already_imported=0, updated=0, linked=0)
    with transaction.atomic():
        lock_health_owner(owner)
        connection = require_health_connection(owner)
        records = {record.external_id: record for record in HealthWorkoutRecord.objects.select_related("session").select_for_update().filter(
            owner=owner, external_id__in=[entry["external_id"] for entry in incoming])}
        template_cache = {}
        for entry in incoming:
            record = records.get(entry["external_id"])
            if record is not None:
                # HealthKit sample identities are immutable. Corrections are
                # new samples; accepting a reused identity for another effort
                # could move private metrics onto an unrelated native workout.
                if any(getattr(record, field) != entry[field] for field in
                       ("workout_activity", "started_at", "ended_at")):
                    raise ValidationError({"workouts": "A known workout ID cannot identify a different activity or time."})
                values = _values(entry, record)
                changed = [key for key, value in values.items() if getattr(record, key) != value]
                for key in changed:
                    setattr(record, key, values[key])
                if changed:
                    record.save(update_fields=[*changed, "updated_at"])
                    # Only importer-owned legacy fields may change. A native
                    # session's timeline, manual values and GPS are untouched.
                    if record.session.health_external_id == record.external_id:
                        fields = {"started_at": record.started_at, "ended_at": record.ended_at}
                        if "distance_km" in changed:
                            fields["health_distance_km"] = record.distance_km
                            if not record.session.route_distance_km:
                                fields["recorded_distance_km"] = record.distance_km
                        WorkoutSession.objects.filter(pk=record.session_id).update(**fields)
                    result["updated"] += 1
                else:
                    result["already_imported"] += 1
                continue
            # Upgrade exact historical identities without a consent backfill.
            session = WorkoutSession.objects.select_for_update().filter(repbase_user=owner,
                health_external_id=entry["external_id"]).first()
            historical = session is not None
            if session is None:
                session = _native_match(owner, entry)
                if session is not None:
                    result["linked"] += 1
            if session is None:
                # Ambiguous overlaps are not automatically copied or linked:
                # counting both is worse than asking for a deliberate retry.
                if WorkoutSession.objects.filter(repbase_user=owner, health_external_id__isnull=True,
                    started_at__lt=entry["ended_at"], ended_at__gt=entry["started_at"]).exists():
                    result["skipped_overlapping"] += 1
                    continue
                activity = entry["workout_activity"]
                if activity not in template_cache:
                    template_cache[activity] = _template(owner, entry)
                session = WorkoutSession.objects.create(repbase_user=owner, workout=template_cache[activity],
                    status=WorkoutSession.Status.COMPLETED, started_at=entry["started_at"], ended_at=entry["ended_at"],
                    health_external_id=entry["external_id"], health_distance_km=entry.get("distance_km"),
                    recorded_distance_km=entry.get("distance_km"))
                result["imported"] += 1
            elif historical:
                result["updated"] += 1
                if entry.get("distance_km") is not None:
                    session.health_distance_km = entry["distance_km"]
                    # Respect a GPS route added after the old import.
                    fields = ["health_distance_km"]
                    if not session.route_distance_km:
                        session.recorded_distance_km = entry["distance_km"]
                        fields.append("recorded_distance_km")
                    session.save(update_fields=[*fields, "updated_at"])
            record = HealthWorkoutRecord.objects.create(owner=owner, session=session,
                external_id=entry["external_id"], **_values(entry))
            records[entry["external_id"]] = record
        connection.last_sync_at = timezone.now()
        connection.last_sync_imported_count = result["imported"]
        connection.last_sync_updated_count = result["updated"] + result["linked"]
        connection.save(update_fields=["last_sync_at", "last_sync_imported_count", "last_sync_updated_count", "updated_at"])
    return result
