"""Add or change the mail settings in /etc/repbase.env, run as root.

The password is read with a hidden prompt and never echoed, never passed as an
argument, and never written anywhere but the root-only env file. Every other
key in that file is preserved exactly as it was.

    sudo python3 /home/django/app/deploy/configure-email.py
"""

import getpass
import json
import os
import re
import shutil
import sys
from pathlib import Path

TARGET = Path('/etc/repbase.env')

# The address mail is sent from has to be on a domain that exists and is
# authorised to send, which is what verifying the domain at the provider does.
UNDELIVERABLE = ('.local', '.localhost', '.invalid', '.example', '.test')

DEFAULTS = {
    'SMTP_HOST': 'smtp.resend.com',
    'SMTP_PORT': '587',
    'SMTP_USERNAME': 'resend',
    'SMTP_USE_TLS': 'true',
}


def quote(value):
    """A double-quoted value systemd's EnvironmentFile will read back intact.

    json.dumps rather than hand-rolled escaping: it already produces the
    double-quoted form with backslashes and quotes escaped, which is exactly
    what systemd expects, and one fewer thing to get subtly wrong in a file
    that holds a password.
    """
    if any(c in value for c in '\r\n\0'):
        raise SystemExit('Values must be single lines.')
    return json.dumps(value)


def ask(name, prompt, default=None, secret=False):
    suffix = f' [{default}]' if default else ''
    if secret:
        value = getpass.getpass(f'{prompt}: ').strip()
    else:
        value = input(f'{prompt}{suffix}: ').strip()
    if not value and default:
        return default
    if not value:
        raise SystemExit(f'{name} is required.')
    return value


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run this as root: sudo python3 ' + __file__)
    if not TARGET.exists():
        raise SystemExit(f'{TARGET} does not exist. Run configure.py first.')

    print('Mail settings for password resets. Enter blank to keep a default.\n')

    sender = ask('DEFAULT_FROM_EMAIL', 'From address (e.g. Rytivo <noreply@rytivo.app>)')
    address = sender.split('<')[-1].rstrip('>').strip().lower()
    if '@' not in address:
        raise SystemExit('That is not an email address.')
    if address.rsplit('.', 1)[-1] and address.endswith(UNDELIVERABLE):
        raise SystemExit(
            'That domain cannot receive mail, so providers will reject or '
            'distrust it. Use the domain you verified with your provider.'
        )

    values = {
        'DEFAULT_FROM_EMAIL': sender,
        'SMTP_HOST': ask('SMTP_HOST', 'SMTP host', DEFAULTS['SMTP_HOST']),
        'SMTP_PORT': ask('SMTP_PORT', 'SMTP port', DEFAULTS['SMTP_PORT']),
        'SMTP_USERNAME': ask('SMTP_USERNAME', 'SMTP username', DEFAULTS['SMTP_USERNAME']),
        'SMTP_PASSWORD': ask('SMTP_PASSWORD', 'SMTP password or API key (hidden)', secret=True),
        'SMTP_USE_TLS': DEFAULTS['SMTP_USE_TLS'],
    }

    original = TARGET.read_text()
    shutil.copy2(TARGET, str(TARGET) + '.bak')

    lines = original.splitlines()
    for key, value in values.items():
        line = f'{key}={quote(value)}'
        pattern = re.compile(rf'^{re.escape(key)}=')
        for at, existing in enumerate(lines):
            if pattern.match(existing):
                lines[at] = line
                break
        else:
            lines.append(line)

    TARGET.write_text('\n'.join(lines) + '\n')
    os.chmod(TARGET, 0o600)

    print('\nWritten. The previous file is at /etc/repbase.env.bak')
    print('Keys now set:', ', '.join(sorted(values)))
    print('\nNothing is sent yet — the service still has to be restarted.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
