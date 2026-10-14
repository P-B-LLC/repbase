"""Bounded shadow queue. No public visibility is changed by these models."""
import uuid
from django.db import models


class LocalModerationJob(models.Model):
    # One mutable work item per post: repeated scheduling cannot flood the queue.
    post = models.OneToOneField('core.Post', on_delete=models.CASCADE, related_name='local_screening')
    revision = models.CharField(max_length=64)
    policy_version = models.CharField(max_length=64)
    generation = models.UUIDField(default=uuid.uuid4)
    state = models.CharField(max_length=16, default='queued')
    attempts = models.PositiveSmallIntegerField(default=0)
    available_at = models.DateTimeField()
    lease = models.UUIDField(null=True)
    lease_until = models.DateTimeField(null=True)
    outcome = models.CharField(max_length=16, blank=True)
    evidence = models.JSONField(default=list)
    error_code = models.CharField(max_length=40, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=['state', 'available_at'], name='local_mod_ready'),
                   models.Index(fields=['state', 'lease_until'], name='local_mod_expired')]


class LocalModerationCase(models.Model):
    post = models.ForeignKey('core.Post', on_delete=models.CASCADE, related_name='local_review_cases')
    generation = models.UUIDField(unique=True)
    revision = models.CharField(max_length=64)
    policy_version = models.CharField(max_length=64)
    outcome = models.CharField(max_length=16)
    evidence = models.JSONField(default=list)
    # Shadow findings are evaluation only, not claims that a live post was blocked.
    shadow = models.BooleanField(default=True)
    status = models.CharField(max_length=16, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=['status', 'created_at'], name='local_mod_review')]
