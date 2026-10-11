import uuid
from django.db import models


class VideoJob(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    video = models.OneToOneField('core.PostVideo', on_delete=models.CASCADE, related_name='job')
    source_key = models.CharField(max_length=255, unique=True)
    declared_type = models.CharField(max_length=40)
    expected_bytes = models.PositiveBigIntegerField()
    etag = models.CharField(max_length=128, blank=True)
    state = models.CharField(max_length=16, default='uploading', db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    lease = models.UUIDField(null=True)
    lease_until = models.DateTimeField(null=True)
    next_attempt_at = models.DateTimeField(null=True)
    error_code = models.CharField(max_length=40, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)


class VideoStoredObject(models.Model):
    """Reserve before writing: failed attempts and deletions leave cleanup work."""
    key = models.CharField(max_length=255, unique=True)
    job = models.ForeignKey(VideoJob, on_delete=models.SET_NULL, null=True)
    retained = models.BooleanField(default=False)
    expires_at = models.DateTimeField(db_index=True)
    size_bytes = models.PositiveBigIntegerField(default=0)


class VideoMetric(models.Model):
    day = models.DateField()
    event = models.CharField(max_length=40)
    count = models.PositiveBigIntegerField(default=0)
    total_ms = models.PositiveBigIntegerField(default=0)
    total_bytes = models.PositiveBigIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['day', 'event'], name='unique_daily_video_metric')]


class VideoDailyUpload(models.Model):
    owner = models.ForeignKey('core.RepbaseUser', on_delete=models.CASCADE)
    day = models.DateField()
    count = models.PositiveSmallIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['owner', 'day'], name='unique_daily_video_upload')]
