"""Explicit, one-time operator bootstrap. Never binds authority to an email alone."""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from core.access import role_state
from core.access_models import AccountAccess, AccessAudit, AccessPolicyLock


class Command(BaseCommand):
    help = 'Grant the sole app Superowner to an existing verified account. Does not grant Django superuser.'

    def add_arguments(self, parser):
        parser.add_argument('--username', required=True)
        parser.add_argument('--email', required=True)
        parser.add_argument('--user-id', required=True, type=int, help='Verified auth-user ID, not the public profile ID.')
        parser.add_argument('--confirm-account-ownership', action='store_true')
        parser.add_argument('--dry-run', action='store_true')

    @transaction.atomic
    def handle(self, *args, **options):
        if not options['confirm_account_ownership'] and not options['dry_run']:
            raise CommandError('Verify the exact account before supplying --confirm-account-ownership.')
        AccessPolicyLock.objects.select_for_update().get(pk=1)
        candidates = list(get_user_model().objects.select_for_update().filter(
            Q(username__iexact=options['username']) | Q(email__iexact=options['email'])).order_by('pk'))
        if len(candidates) != 1:
            raise CommandError('Identity is missing or ambiguous. No account or role was changed.')
        user = candidates[0]
        if (user.pk != options['user_id'] or user.username != options['username']
                or user.email.casefold() != options['email'].casefold() or not user.is_active
                or not hasattr(user, 'repbase_profile')):
            raise CommandError('ID, exact username, email, active status, and app profile must all match.')
        existing = AccountAccess.objects.filter(superowner=True).first()
        if existing and existing.user_id != user.pk:
            raise CommandError('A different Superowner already exists. This command cannot replace or transfer it.')
        if options['dry_run']:
            self.stdout.write(f'Verified account ID {user.pk}: {user.username}. Dry run only; no permissions changed.')
            return
        if existing:
            self.stdout.write('This account is already the Superowner; no changes needed.')
            return
        before = role_state(user)
        row, _ = AccountAccess.objects.get_or_create(user=user)
        row.superowner = True
        row.analytics = row.analytics or 'analytics' in before['roles']
        row.version += 1
        row.save()
        AccessAudit.objects.create(target=user, action='superowner_bootstrap', subject=f'user:{user.pk}',
                                   before=before, after=role_state(user),
                                   reason='Server operator verified exact ID, username and email for initial Superowner.')
        self.stdout.write(f'Superowner granted to {user.username} (ID {user.pk}). Django staff/superuser flags unchanged.')
