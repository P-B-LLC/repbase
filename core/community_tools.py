"""Owner-only cosmetic tools; all publication uses the normal safety review."""
import re
from django.contrib.auth import get_user_model
from django.db import transaction
from rest_framework.exceptions import PermissionDenied, ValidationError
from .access import AccessConflict, require
from .access_models import AccountAccess, AccessAudit, AccessPolicyLock, CommunitySpotlight
from .account_controls import can_control_account
from .moderation import check_public_content


def validate_flair(label, badge, color):
    if len(label) > 32 or len(badge) > 32 or not re.fullmatch(r'#[0-9a-fA-F]{6}', color):
        raise ValidationError('Use labels up to 32 characters and a six-digit hex color.')
    reserved = {'owner', 'superowner', 'admin', 'administrator', 'moderator', 'verified', 'official'}
    for text in (label, badge):
        normalized = re.sub(r'[^a-z]', '', text.casefold())
        if normalized and any(word in normalized for word in reserved):
            raise ValidationError('Cosmetic labels cannot impersonate administrative or verification badges.')


def set_flair(actor, target, label, badge, color, version, reason, request=None):
    require(actor, 'manage_roles')
    label, badge = label.strip(), badge.strip()
    validate_flair(label, badge, color)
    if not reason.strip() or len(reason) > 500:
        raise ValidationError('Provide a reason of up to 500 characters.')
    check_public_content({'title': label, 'description': badge}, request=request)
    with transaction.atomic():
        AccessPolicyLock.objects.select_for_update().get(pk=1)
        actor = get_user_model().objects.get(pk=actor.pk)
        require(actor, 'manage_roles')
        target = get_user_model().objects.select_for_update().get(pk=target.pk)
        if not target.is_active or (actor.pk != target.pk and not can_control_account(actor, target)):
            raise PermissionDenied('Your role cannot customize this account.')
        row, _ = AccountAccess.objects.get_or_create(user=target)
        if row.version != version:
            raise AccessConflict()
        before = dict(label=row.public_label, badge=row.public_badge, color=row.label_color)
        row.public_label, row.public_badge, row.label_color = label, badge, color.upper()
        row.version += 1
        row.save(update_fields=['public_label', 'public_badge', 'label_color', 'version'])
        AccessAudit.objects.create(actor=actor, target=target, action='community_flair_changed',
            subject=f'user:{target.pk}', before=before,
            after=dict(label=label, badge=badge, color=row.label_color), reason=reason.strip())


def set_spotlight(actor, announcement, featured_post_id, version, reason, request=None):
    require(actor, 'manage_roles')
    if len(announcement) > 500 or not reason.strip() or len(reason) > 500:
        raise ValidationError('Announcement and required reason must each be at most 500 characters.')
    check_public_content({'body': announcement}, request=request)
    with transaction.atomic():
        AccessPolicyLock.objects.select_for_update().get(pk=1)
        actor = get_user_model().objects.get(pk=actor.pk)
        require(actor, 'manage_roles')
        from .models import Post
        if featured_post_id is not None and not Post.objects.filter(
                pk=featured_post_id, visibility='public', is_hidden=False,
                author__is_profile_public=True, author__user__is_active=True,
                repost_of__isnull=True).exists():
            raise ValidationError('Choose an existing public original post from an active, public profile.')
        row, _ = CommunitySpotlight.objects.get_or_create(pk=1)
        if row.version != version:
            raise AccessConflict()
        before = dict(announcement=row.announcement, featured_post_id=row.featured_post_id)
        row.announcement, row.featured_post_id = announcement.strip(), featured_post_id
        row.version += 1
        row.save()
        AccessAudit.objects.create(actor=actor, action='community_spotlight_changed', subject='community:1',
            before=before, after=dict(announcement=row.announcement, featured_post_id=row.featured_post_id), reason=reason.strip())
