from datetime import timedelta
import math

from django.utils import timezone
from rest_framework import serializers

from .activities import ACTIVITIES, LEGACY_DEFAULT_ACTIVITY
from .health_models import HealthWorkoutRecord

HEALTH_CONSENT_VERSION = "apple-health-v1"


class AppleHealthConnectionSerializer(serializers.Serializer):
    enabled = serializers.BooleanField()
    consent_version = serializers.CharField(allow_null=True)
    last_sync_at = serializers.DateTimeField(allow_null=True)
    last_sync_imported_count = serializers.IntegerField()
    last_sync_updated_count = serializers.IntegerField()
    schema_version = serializers.IntegerField(default=1)
    garmin_direct_available = serializers.BooleanField(default=False)


class AppleHealthConnectionUpdateSerializer(serializers.Serializer):
    enabled = serializers.BooleanField()
    consent_version = serializers.CharField(required=False, max_length=40)

    def validate(self, attrs):
        if attrs["enabled"] and attrs.get("consent_version") != HEALTH_CONSENT_VERSION:
            raise serializers.ValidationError({"consent_version": "Review the current Apple Health import agreement."})
        return attrs


class HealthWorkoutSummarySerializer(serializers.ModelSerializer):
    provider = serializers.CharField(read_only=True, default="apple_health")

    class Meta:
        model = HealthWorkoutRecord
        fields = ("id", "session", "external_id", "provider", "source", "source_name",
                  "source_bundle_id", "device_name", "workout_activity", "started_at", "ended_at",
                  "duration_seconds", "distance_km", "active_energy_kcal", "average_heart_rate_bpm",
                  "max_heart_rate_bpm", "updated_at")
        read_only_fields = fields


class HealthWorkoutSerializer(serializers.Serializer):
    external_id = serializers.CharField(max_length=64)
    activity = serializers.ChoiceField(choices=(("lifting", "Lifting"), ("running", "Running"),
                                               ("biking", "Biking"), ("swimming", "Swimming")))
    workout_activity = serializers.CharField(required=False, max_length=40)
    started_at = serializers.DateTimeField()
    ended_at = serializers.DateTimeField()
    duration_seconds = serializers.FloatField(required=False, min_value=0.001, max_value=604800)
    distance_km = serializers.DecimalField(max_digits=7, decimal_places=3, min_value=0,
                                           required=False, allow_null=True)
    active_energy_kcal = serializers.DecimalField(max_digits=8, decimal_places=2,
        min_value=0, max_value=100000, required=False, allow_null=True)
    average_heart_rate_bpm = serializers.DecimalField(max_digits=5, decimal_places=2,
        min_value=20, max_value=300, required=False, allow_null=True)
    max_heart_rate_bpm = serializers.DecimalField(max_digits=5, decimal_places=2,
        min_value=20, max_value=300, required=False, allow_null=True)
    source_name = serializers.CharField(max_length=100, required=False, allow_null=True)
    source_bundle_id = serializers.CharField(max_length=255, required=False, allow_null=True)
    device_name = serializers.CharField(max_length=100, required=False, allow_null=True)

    def validate(self, attrs):
        elapsed = (attrs["ended_at"] - attrs["started_at"]).total_seconds()
        if not 0 < elapsed <= 604800:
            raise serializers.ValidationError({"ended_at": "Use a finished workout lasting at most seven days."})
        if attrs["ended_at"] > timezone.now() + timedelta(minutes=5):
            raise serializers.ValidationError({"ended_at": "A completed workout cannot end in the future."})
        duration = attrs.get("duration_seconds", elapsed)
        if not math.isfinite(duration) or duration > elapsed + 1:
            raise serializers.ValidationError({"duration_seconds": "Active duration cannot exceed elapsed time."})
        activity = attrs.get("workout_activity", LEGACY_DEFAULT_ACTIVITY[attrs["activity"]])
        if activity not in ACTIVITIES or ACTIVITIES[activity].family != attrs["activity"]:
            raise serializers.ValidationError({"workout_activity": "Use an activity matching the workout family."})
        attrs["workout_activity"] = activity
        average, maximum = attrs.get("average_heart_rate_bpm"), attrs.get("max_heart_rate_bpm")
        if average is not None and maximum is not None and average > maximum:
            raise serializers.ValidationError({"max_heart_rate_bpm": "Maximum heart rate cannot be below average heart rate."})
        for name in ("external_id", "source_name", "source_bundle_id", "device_name"):
            value = attrs.get(name)
            if value is not None and any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise serializers.ValidationError({name: "Control characters are not accepted."})
        return attrs


class HealthWorkoutImportSerializer(serializers.Serializer):
    workouts = HealthWorkoutSerializer(many=True, allow_empty=True, max_length=500)

    def validate_workouts(self, value):
        ids = [item["external_id"] for item in value]
        if len(set(ids)) != len(ids):
            raise serializers.ValidationError("A batch must contain each workout only once.")
        return value


class HealthImportResultSerializer(serializers.Serializer):
    imported = serializers.IntegerField()
    skipped_overlapping = serializers.IntegerField()
    already_imported = serializers.IntegerField()
    updated = serializers.IntegerField()
    linked = serializers.IntegerField()
    optional_response_fields = frozenset({"updated", "linked"})
