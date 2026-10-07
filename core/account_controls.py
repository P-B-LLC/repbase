"""Audited account lifecycle actions. Cosmetic roles never confer authority."""
from django.contrib.auth import get_user_model, logout
from django.contrib.auth.signals import user_logged_in
from django.db import transaction
from django.dispatch import receiver
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import PermissionDenied, ValidationError

from .access import AccessConflict, capabilities, require, roles_for
from .access_models import AccountAccess, AccessAudit, AccessPolicyLock


def can_control_account(actor, target):
    if not capabilities(actor)['moderate'] or actor.pk == target.pk or target.is_superuser or target.is_staff:
        return False
    # Never use roles_for here: disabled accounts still retain their rank.
    row = AccountAccess.objects.filter(user=target).first()
    if row and row.superowner:
        return False
    if row and row.owner:
        return 'superowner' in roles_for(actor)
    if row and (row.moderator or row.analytics):
        return capabilities(actor)['manage_roles']
    return True


def account_status(user):
    row = AccountAccess.objects.filter(user=user).first()
    return dict(active=user.is_active, disabled_by_admin=bool(row and row.disabled_by_admin))


@transaction.atomic
def change_account_status(actor, target, active, version, reason, confirm_username):
    AccessPolicyLock.objects.select_for_update().get(pk=1)
    actor = get_user_model().objects.get(pk=actor.pk)
    require(actor, 'moderate')
    target = get_user_model().objects.select_for_update().get(pk=target.pk)
    if not can_control_account(actor, target):
        raise PermissionDenied('Your role cannot disable or restore this account.')
    if not reason.strip() or len(reason) > 500 or confirm_username != target.username:
        raise ValidationError('Enter the exact username and a reason of up to 500 characters.')
    row, _ = AccountAccess.objects.get_or_create(user=target)
    if row.version != version:
        raise AccessConflict()
    if active and not target.is_active and not row.disabled_by_admin:
        raise PermissionDenied('This account was disabled outside this dashboard; ask the server operator.')
    if target.is_active == active:
        return
    before = account_status(target)
    target.is_active = active
    target.save(update_fields=['is_active'])
    row.disabled_by_admin = not active
    row.version += 1
    # Invalidate old browser cookies permanently, even after restoration.
    row.session_epoch += 1
    row.save(update_fields=['disabled_by_admin', 'version', 'session_epoch'])
    list(Token.objects.select_for_update().filter(user=target))
    Token.objects.filter(user=target).delete()
    AccessAudit.objects.create(actor=actor, target=target,
        action='account_restored' if active else 'account_disabled', subject=f'user:{target.pk}',
        before=before, after=account_status(target), reason=reason.strip())


@receiver(user_logged_in)
def stamp_admin_session_epoch(sender, request, user, **kwargs):
    with transaction.atomic():
        fresh = get_user_model().objects.select_for_update().get(pk=user.pk)
        if not fresh.is_active:
            logout(request)
            return
        request.session['account_epoch'] = AccountAccess.objects.filter(user=user).values_list('session_epoch', flat=True).first() or 0


class AccountSessionMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            epoch = AccountAccess.objects.filter(user_id=request.user.pk).values_list('session_epoch', flat=True).first() or 0
            if request.session.get('account_epoch', 0) != epoch:
                logout(request)
        return self.get_response(request)
