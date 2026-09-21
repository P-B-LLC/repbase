from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from core.access import role_state
from core.access_models import AccountAccess, AccessAudit, AccessPolicyLock


class Command(BaseCommand):
    help = 'Bootstrap an existing, operator-verified app Owner. Does not grant Django staff/superuser.'

    def add_arguments(self, parser):
        parser.add_argument('--username', required=True)
        parser.add_argument('--confirm-account-ownership', action='store_true')

    @transaction.atomic
    def handle(self, *args, **options):
        if not options['confirm_account_ownership']:
            raise CommandError('Verify the account independently before supplying --confirm-account-ownership.')
        AccessPolicyLock.objects.select_for_update().get(pk=1)
        user = get_user_model().objects.filter(username=options['username'], is_active=True).first()
        if not user:
            raise CommandError('Active account not found; no account was created.')
        before = role_state(user)
        row, _ = AccountAccess.objects.get_or_create(user=user)
        if not row.owner:
            row.owner = True
            row.analytics = row.analytics or 'analytics' in before['roles']
            row.version += 1
            row.save()
            AccessAudit.objects.create(target=user, action='owner_bootstrap', subject=f'user:{user.pk}',
                                       before=before, after=role_state(user), reason='Server operator verified account ownership.')
        self.stdout.write('Owner access granted. No Django staff or superuser permissions changed.')
