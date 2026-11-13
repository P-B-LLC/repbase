"""Private, explicitly enabled wearable imports. Never used by social snapshots."""
from django.db import models


class AppleHealthConnection(models.Model):
    owner = models.OneToOneField("core.RepbaseUser", on_delete=models.CASCADE)
    enabled = models.BooleanField(default=False)
    consent_version = models.CharField(max_length=40, null=True, blank=True)
    consented_at = models.DateTimeField(null=True, blank=True)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    last_sync_imported_count = models.PositiveIntegerField(default=0)
    last_sync_updated_count = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)


class HealthWorkoutRecord(models.Model):
    """One source workout, optionally enriching an existing Rytivo session.

    The source record does not rewrite native sets, notes, routes or calories.
    There is no Garmin OAuth connection: Garmin-labelled records came through
    the person's Apple Health store, not a direct Garmin API.
    """
    owner = models.ForeignKey("core.RepbaseUser", on_delete=models.CASCADE)
    session = models.OneToOneField(
        "core.WorkoutSession", on_delete=models.CASCADE, related_name="health_summary"
    )
    external_id = models.CharField(max_length=64)
    source = models.CharField(max_length=20, default="apple_health")
    source_name = models.CharField(max_length=100, null=True, blank=True)
    source_bundle_id = models.CharField(max_length=255, null=True, blank=True)
    device_name = models.CharField(max_length=100, null=True, blank=True)
    workout_activity = models.CharField(max_length=40)
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField()
    duration_seconds = models.FloatField()
    distance_km = models.DecimalField(max_digits=7, decimal_places=3, null=True, blank=True)
    active_energy_kcal = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    average_heart_rate_bpm = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    max_heart_rate_bpm = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-started_at", "-id")
        constraints = [models.UniqueConstraint(
            fields=("owner", "external_id"), name="unique_health_record_per_owner"
        )]
        indexes = [models.Index(fields=("owner", "-started_at"), name="health_owner_started_idx")]
