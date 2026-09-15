"""Provider-neutral commands: python -m config.deploy check|release|serve.

Migrations only run in the explicit release command, never in web workers.
"""
import os
import sys


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'release', 'serve'))
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


if __name__ == '__main__':
    main()
