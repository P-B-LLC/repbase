from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from core.analytics import ADMIN_EMAIL
from core.analytics_models import AnalyticsAccess


class Command(BaseCommand):
    help = 'Grant/revoke insights for an existing admin@rytivo.app account after operator ownership verification.'

    def add_arguments(self, parser):
        parser.add_argument('--user-id', required=True, type=int)
        parser.add_argument('--confirm-email-ownership', action='store_true')
        parser.add_argument('--revoke', action='store_true')

    @transaction.atomic
    def handle(self, *args, **options):
        user = get_user_model().objects.select_for_update().filter(pk=options['user_id']).first()
        if not user:
            raise CommandError('Account not found. This command never creates accounts.')
        if options['revoke']:
            AnalyticsAccess.objects.filter(user=user).update(enabled=False)
            self.stdout.write('Analytics access revoked.')
            return
        if not options['confirm_email_ownership']:
            raise CommandError('Verify mailbox ownership independently, then supply --confirm-email-ownership.')
        if not user.is_active or user.email.casefold() != ADMIN_EMAIL:
            raise CommandError('Only the active admin@rytivo.app account can receive access.')
        if get_user_model().objects.filter(email__iexact=ADMIN_EMAIL).exclude(pk=user.pk).exists():
            raise CommandError('Resolve duplicate admin email accounts before granting access.')
        user.is_staff = True
        user.save(update_fields=['is_staff'])
        AnalyticsAccess.objects.update_or_create(user=user, defaults={
            'verified_email': ADMIN_EMAIL, 'verified_at': timezone.now(), 'enabled': True})
        self.stdout.write('Analytics access granted. No superuser or model permissions were granted.')
