"""Refuse to deploy with email that cannot deliver.

This product sends one email: a password reset code, to somebody who is
already locked out. There is no retry queue and no second channel, so if it
does not arrive there is nothing to fall back on.

What makes that worth a check rather than a note in a runbook is that broken
email is invisible from the outside. The reset endpoint answers 204 whether or
not the address belongs to an account -- deliberately, so it cannot be used to
find out who has one -- and it now keeps answering 204 when the send itself
fails, because a 500 only for real addresses would be the same account
enumerator by a slower route. So a provider that was never configured looks
exactly like one that works, and the first person to find out would be a beta
tester who never got their code and had no way to say so.

The deploy has to find out instead. ``manage.py check --deploy`` runs these,
and every default that would send mail nowhere is an Error rather than a
Warning: there is no useful half-configured state between "sends email" and
"loses accounts".
"""

from __future__ import annotations

from django.conf import settings
from django.core.checks import Error, Warning, register

# Suffixes that cannot receive mail: .local is reserved for mDNS, .localhost
# never leaves the machine, and .invalid/.example/.test are reserved by
# RFC 2606. Most providers refuse to relay a From address on any of them, so
# a deploy carrying one is rejected before the message goes anywhere.
UNDELIVERABLE_SUFFIXES = ('.local', '.localhost', '.invalid', '.example', '.test')

# What "no mail server was configured" looks like. localhost is Django's own
# default, so it is what a deploy that set nothing ends up with.
UNCONFIGURED_HOSTS = ('', 'localhost', '127.0.0.1', '::1')


@register(deploy=True)
def publication_storage_preserves_names(app_configs, **kwargs):
    from django.core.files.storage import default_storage
    if callable(getattr(default_storage, 'save_reserved', None)):
        return []
    return [Error(
        'Photo publication requires storage that preserves reserved upload names.',
        hint='Configure ReservedNameFileSystemStorage or an adapter implementing save_reserved without renaming.',
        id='core.E005',
    )]


def _default_mailer() -> dict:
    return (getattr(settings, 'MAILERS', None) or {}).get('default') or {}


@register(deploy=True)
def social_moderation_ready(app_configs, **kwargs):
    if settings.DEBUG:
        return []
    if not settings.MODERATION_ENABLED or not settings.MODERATION_API_KEY:
        return [Error('Production social publishing requires configured automated moderation.',
                      hint='Configure MODERATION_API_KEY and enable moderation. Do not bypass it to ship.',
                      id='core.E006')]
    if not settings.MODERATION_DISCLOSURE_CONFIRMED:
        return [Error('The third-party moderation disclosure/permission release gate is not confirmed.',
                      hint='Verify in-app permission and privacy disclosures before setting MODERATION_DISCLOSURE_CONFIRMED=true.',
                      id='core.E007')]
    return []


@register(deploy=True)
def email_can_reach_a_real_person(app_configs, **kwargs):
    mailer = _default_mailer()
    backend = str(mailer.get('BACKEND', ''))
    if not backend.endswith('smtp.EmailBackend'):
        # The console, locmem and filebased backends deliver nowhere on
        # purpose. Complaining about their settings would only report a setup
        # for doing exactly what it was asked to do.
        return []

    options = mailer.get('OPTIONS') or {}
    problems = []

    sender = str(getattr(settings, 'DEFAULT_FROM_EMAIL', '') or '').strip()
    domain = sender.rpartition('@')[2].lower()
    if '@' not in sender:
        problems.append(
            Error(
                f'DEFAULT_FROM_EMAIL is not an email address: {sender!r}.',
                hint='Set DEFAULT_FROM_EMAIL to the address reset codes come from.',
                id='core.E001',
            )
        )
    elif domain.endswith(UNDELIVERABLE_SUFFIXES):
        problems.append(
            Error(
                f'DEFAULT_FROM_EMAIL is {sender!r}, on a domain that cannot '
                f'receive mail.',
                hint=(
                    'Set DEFAULT_FROM_EMAIL to an address on a domain that '
                    'exists and is authorised to send for this product. A '
                    'From address on a reserved suffix '
                    f'({", ".join(UNDELIVERABLE_SUFFIXES)}) is rejected by '
                    'most providers before the message leaves, and treated as '
                    'forged by the ones that accept it.'
                ),
                id='core.E002',
            )
        )

    host = str(options.get('host', '') or '').strip().lower()
    if host in UNCONFIGURED_HOSTS:
        problems.append(
            Error(
                f'No SMTP host is configured; the default mailer would try '
                f'{host or "an empty host"!r}.',
                hint=(
                    'Set SMTP_HOST, SMTP_PORT, SMTP_USERNAME and '
                    'SMTP_PASSWORD for a real provider. Left like this, every '
                    'password reset fails against a mail server that is not '
                    'running on the application host.'
                ),
                id='core.E003',
            )
        )
        return problems

    if not options.get('use_tls') and not options.get('use_ssl'):
        problems.append(
            Error(
                'SMTP is configured without TLS, so the account password '
                'would cross the network in the clear.',
                hint=(
                    'Leave SMTP_USE_TLS on for STARTTLS on port 587, or set '
                    'SMTP_USE_SSL for implicit TLS on port 465.'
                ),
                id='core.E004',
            )
        )

    if not options.get('username'):
        problems.append(
            Warning(
                'SMTP_USERNAME is empty, so the connection will not '
                'authenticate.',
                hint=(
                    'Most providers reject unauthenticated mail. This is a '
                    'warning rather than an error because a relay on a '
                    'private network may authorise by address instead.'
                ),
                id='core.W005',
            )
        )

    return problems


@register(deploy=True)
def scheduled_cleanup_is_running(app_configs, **kwargs):
    """Are there rows a daily maintenance run would already have removed?

    Three things have to happen on a clock rather than on a request, and
    nothing in the application can make them happen -- `config.deploy
    maintenance` does, and something has to call it. The failure mode is
    silence: tables grow, storage keeps files no row points at, and the first
    sign is a disk bill or a photo that outlived the account that uploaded it.

    So this asks about the outcome rather than the ceremony. A job that runs
    on a schedule and does nothing would satisfy "did it run"; it cannot
    satisfy "is there anything left that it should have taken". Warnings
    rather than errors, because a deployment that is a day old legitimately
    has none of this, and an overdue row is a thing to fix rather than a
    reason to refuse to start.
    """
    from datetime import timedelta

    from django.utils import timezone

    from .models import FoodSearchCache, PendingMediaDeletion, SaveReceipt

    now = timezone.now()
    overdue = []
    try:
        stale_terms = FoodSearchCache.objects.filter(
            fetched_at__lt=now - FoodSearchCache.RETENTION
        ).count()
        stale_receipts = SaveReceipt.objects.filter(
            created_at__lt=now - SaveReceipt.RETENTION, status_code__lt=300
        ).count()
        # A day, not a minute: these are retried after a storage failure and
        # a handful in flight is normal.
        stuck_deletions = PendingMediaDeletion.objects.filter(
            created_at__lt=now - timedelta(days=1)
        ).count()
    except Exception:
        # Before the first migrate there is nothing to ask. `check` runs ahead
        # of `migrate` in the deploy entry point, and a missing table is not a
        # configuration mistake.
        return []

    if stale_terms:
        overdue.append(f'{stale_terms} expired food-search cache entries')
    if stale_receipts:
        overdue.append(f'{stale_receipts} save receipts whose payload should have been compacted')
    if stuck_deletions:
        overdue.append(f'{stuck_deletions} media deletions older than a day still waiting')

    if not overdue:
        return []
    return [
        Warning(
            'Scheduled cleanup does not appear to be running: ' + ', '.join(overdue) + '.',
            hint=(
                'Run `python -m config.deploy maintenance` daily. Until something '
                'does, two tables only grow and storage keeps files no row points at.'
            ),
            id='core.W007',
        )
    ]
