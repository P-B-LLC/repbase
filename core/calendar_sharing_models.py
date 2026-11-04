"""Explicit, revocable read-only calendar invitations, never copied tasks."""

from django.db import models, transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from django.utils import timezone


class CalendarShare(models.Model):
    class Scope(models.TextChoices):
        OCCURRENCE = "occurrence", "Only this occurrence"
        SERIES = "series", "This occurrence and future occurrences"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        DECLINED = "declined", "Declined"
        REVOKED = "revoked", "Revoked"

    owner = models.ForeignKey("core.RepbaseUser", on_delete=models.CASCADE, related_name="calendar_shares_sent")
    recipient = models.ForeignKey("core.RepbaseUser", on_delete=models.CASCADE, related_name="calendar_shares_received")
    scope = models.CharField(max_length=12, choices=Scope.choices)
    # Only occurrence grants point at a concrete row. Deleting that row revokes
    # access by deleting the grant; a series grant must outlive its first row.
    source_entry = models.ForeignKey("core.PlannerEntry", on_delete=models.CASCADE, null=True, blank=True, related_name="calendar_shares")
    # A series invitation may preview the exact row that was selected, but its
    # permission does not depend on that row continuing to exist.
    invited_entry = models.ForeignKey("core.PlannerEntry", on_delete=models.SET_NULL, null=True, blank=True, related_name="calendar_invitations")
    series_id = models.UUIDField(null=True, blank=True)
    # Immutable occurrence identity, not a mutable displayed/scheduled date.
    starts_on = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    responded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(fields=("recipient", "status"), name="calendar_recipient_state"),
            models.Index(fields=("owner", "series_id"), name="calendar_owner_series"),
        ]
        constraints = [
            models.CheckConstraint(condition=~models.Q(owner=models.F("recipient")), name="calendar_share_not_self"),
            models.CheckConstraint(condition=(
                models.Q(scope="occurrence", source_entry__isnull=False, series_id__isnull=True, starts_on__isnull=True)
                | models.Q(scope="series", source_entry__isnull=True, series_id__isnull=False, starts_on__isnull=False)
            ), name="calendar_share_scope_target"),
            models.CheckConstraint(condition=models.Q(status__in=("pending", "accepted", "declined", "revoked")), name="calendar_share_valid_status"),
            models.UniqueConstraint(fields=("recipient", "source_entry"), condition=models.Q(scope="occurrence"), name="calendar_occurrence_recipient"),
            models.UniqueConstraint(fields=("owner", "recipient", "series_id"), condition=models.Q(scope="series"), name="calendar_series_recipient"),
        ]


class CalendarShareExclusion(models.Model):
    """An occurrence made private must stay private after edits/date moves."""

    owner = models.ForeignKey("core.RepbaseUser", on_delete=models.CASCADE, related_name="calendar_share_exclusions")
    series_id = models.UUIDField()
    occurrence_date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("owner", "series_id", "occurrence_date"), name="calendar_private_occurrence")]


@receiver(post_delete, sender="core.Follow", dispatch_uid="calendar_revoke_on_unfollow")
def revoke_calendar_follow(sender, instance, **kwargs):
    # This also covers admin/bulk deletion. Following again must not resurrect
    # an accepted invitation or a question that was previously withdrawn.
    CalendarShare.objects.filter(owner_id=instance.following_id, recipient_id=instance.follower_id,
                                 status__in=("pending", "accepted")).update(status="revoked", responded_at=timezone.now())


@receiver(post_save, sender="core.Block", dispatch_uid="calendar_revoke_on_block")
@transaction.atomic
def revoke_calendar_block(sender, instance, created, **kwargs):
    if created:
        # Serializes even raw/admin block creation against invitation creation.
        # The profile locks precede the grant query, so a not-yet-committed
        # invitation cannot be missed and later resurrected by unblocking.
        from .models import Follow, RepbaseUser
        list(RepbaseUser.objects.select_for_update(no_key=True).filter(
            pk__in=(instance.blocker_id, instance.blocked_id),
        ).order_by("pk"))
        # Unfollow locks its Follow before its revoke signal touches grants.
        # Use that same order, otherwise API blocking (which deletes follows
        # after save) could hold a grant while waiting on an unfollow's row.
        list(Follow.objects.select_for_update().filter(
            models.Q(follower_id=instance.blocker_id, following_id=instance.blocked_id)
            | models.Q(follower_id=instance.blocked_id, following_id=instance.blocker_id),
        ).order_by("pk"))
        CalendarShare.objects.filter(
            models.Q(owner_id=instance.blocker_id, recipient_id=instance.blocked_id)
            | models.Q(owner_id=instance.blocked_id, recipient_id=instance.blocker_id),
            status__in=("pending", "accepted"),
        ).update(status="revoked", responded_at=timezone.now())
