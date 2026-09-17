"""Run as root on the droplet to save credentials without echoing them."""

import getpass
import os
from pathlib import Path
import secrets


def quote(value):
    if any(c in value for c in '\r\n\0'):
        raise ValueError('Values must be single lines')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def main():
    target = Path('/etc/repbase.env')
    if target.exists():
        raise SystemExit('Configuration already exists; refusing to overwrite secrets.')
    password = getpass.getpass('DigitalOcean PostgreSQL password (hidden): ')
    if not password:
        raise SystemExit('Password cannot be empty.')
    values = {
        'DJANGO_SETTINGS_MODULE': 'config.production',
        'DJANGO_DEBUG': 'false',
        'DJANGO_SECRET_KEY': secrets.token_urlsafe(64),
        'DJANGO_ALLOWED_HOSTS': '157.230.188.173,127.0.0.1,localhost',
        # Initial testing uses an encrypted SSH tunnel to the loopback listener.
        'DJANGO_SECURE_SSL_REDIRECT': 'false',
        'POSTGRES_HOST': 'dbaas-db-10569023-do-user-18918658-0.g.db.ondigitalocean.com',
        'POSTGRES_PORT': '25060',
        'POSTGRES_DB': 'defaultdb',
        'POSTGRES_USER': 'doadmin',
        'POSTGRES_PASSWORD': password,
        'POSTGRES_SSLMODE': 'require',
    }
    content = ''.join(f'{key}={quote(value)}\n' for key, value in values.items())
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(content)
    print('Configuration saved. Password was not displayed.')


if __name__ == '__main__':
    main()
