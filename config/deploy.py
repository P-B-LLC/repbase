"""Provider-neutral commands: python -m config.deploy check|release|serve.

Migrations only run in the explicit release command, never in web workers.
"""
import os
import sys


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'release', 'serve', 'maintenance'))
    action = parser.parse_args().action
    os.environ['DJANGO_SETTINGS_MODULE'] = 'config.production'
    import django
    django.setup()
    from django.core.management import call_command
    from config.health import dependencies_available
    call_command('check', deploy=True, fail_level='ERROR')
    if not dependencies_available():
        raise SystemExit('Database/cache readiness failed. Check provider connectivity and credentials securely.')
    if action == 'release':
        call_command('migrate', interactive=False)
        call_command('collectstatic', interactive=False)
    elif action == 'serve':
        call_command('migrate', check_unapplied=True, interactive=False)
        os.execv(sys.executable, [sys.executable, '-m', 'gunicorn', '-c', 'gunicorn.conf.py', 'config.wsgi:application'])
    elif action == 'maintenance':
        # Everything that has to happen on a clock rather than on a request.
        # One command so the host needs one daily entry rather than a list it
        # can get half right: two tables that only grow without the first,
        # and storage that keeps files no row points at without the second.
        call_command('prune_expired_rows')
        call_command('retry_media_deletions')
        call_command('transfer_media', audit=True)


if __name__ == '__main__':
    main()
