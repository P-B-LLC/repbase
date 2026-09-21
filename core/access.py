"""Shared authorization for API, iOS, web and the private dashboard.

Always query grants rather than trusting client claims or a cached permission
set. Staff status alone and an admin-looking email never grant app roles.
"""
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError, APIException

from .access_models import AccountAccess, AccessAudit, AccessPolicyLock

ROLES = ('owner', 'analytics', 'moderator')


def roles_for(user):
    if not user.is_authenticated or not user.is_active:
        return []
    row = AccountAccess.objects.filter(user_id=user.pk).first()
    if row is None:
        # Existing explicit operator approval survives rollout. Once this
        # account has a versioned role record, that record replaces the legacy
        # grant so removing Analytics really revokes dashboard access.
        from .analytics_models import AnalyticsAccess
        if (user.is_staff and user.email.casefold() == 'admin@rytivo.app'
                and AnalyticsAccess.objects.filter(user_id=user.pk, enabled=True,
                    verified_email='admin@rytivo.app', verified_at__lte=timezone.now()).exists()):
            return ['analytics']
    return [role for role in ROLES if row and getattr(row, role)]


def capabilities(user):
    roles = roles_for(user)
    active = user.is_authenticated and user.is_active
    owner = bool(active and (user.is_superuser or 'owner' in roles))
    return dict(manage_roles=owner, view_analytics=owner or 'analytics' in roles,
                moderate=owner or 'moderator' in roles)


def require(user, capability):
    if not capabilities(user)[capability]:
        raise PermissionDenied('This account does not have access to this action.')


class AccessConflict(APIException):
    status_code = 409
    default_detail = 'Access changed since you opened this page. Reload and review before saving.'


def role_state(user):
    row = AccountAccess.objects.filter(user_id=user.pk).first()
    return dict(roles=roles_for(user), version=row.version if row else 0)


@transaction.atomic
def assign_roles(actor, target, roles, version, reason):
    if set(roles) - set(ROLES) or len(roles) != len(set(roles)):
        raise ValidationError('Choose only the documented roles, without duplicates.')
    AccessPolicyLock.objects.select_for_update().get(pk=1)
    # Re-read even the actor: a request authenticated before a revocation must
    # not retain authority while waiting for the policy lock.
    actor = get_user_model().objects.get(pk=actor.pk)
    require(actor, 'manage_roles')
    target = get_user_model().objects.select_for_update().get(pk=target.pk)
    if actor.pk == target.pk:
        raise ValidationError('Ask another Owner to change your access.')
    if target.is_superuser:
        raise ValidationError('Server administrator access is managed by the server operator.')
    if not target.is_active:
        raise ValidationError('Inactive accounts cannot receive role changes.')
    before = role_state(target)
    if before['version'] != version:
        raise AccessConflict()
    if set(before['roles']) == set(roles):
        return before
    row, _ = AccountAccess.objects.get_or_create(user=target)
    for role in ROLES:
        setattr(row, role, role in roles)
    row.version += 1
    row.save()
    after = role_state(target)
    AccessAudit.objects.create(actor=actor, target=target, action='roles_changed',
                               subject=f'user:{target.pk}', before=before, after=after, reason=reason)
    return after
