"""Run as root on the droplet to save credentials without echoing them.

Writes /etc/repbase.env for `config.production`, once, and refuses to
overwrite an existing file. Secrets are read with a hidden prompt and never
printed.

The earlier version of this file wrote settings `config.production` refuses
(localhost in DJANGO_ALLOWED_HOSTS, POSTGRES_* variables nothing reads,
`sslmode=require`) and connected the application as `doadmin`, the managed
cluster's administrator. The application needs to read and write its own
tables and nothing more; see "Database role" in deploy/README.md.
"""

import getpass
import os
import secrets
from pathlib import Path
from urllib.parse import quote as url_quote

#: Roles that administer a cluster rather than use one. An application
#: connected as one of these can drop every database on it.
ADMINISTRATIVE_ROLES = {"doadmin", "postgres", "root", "admin"}


def quote(value):
    if any(c in value for c in '\r\n\0'):
        raise ValueError('Values must be single lines')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def ask(prompt, *, secret=False, default=None):
    label = f'{prompt} [{default}]: ' if default else f'{prompt}: '
    value = (getpass.getpass(label) if secret else input(label)).strip()
    value = value or (default or '')
    if not value:
        raise SystemExit(f'{prompt} cannot be empty.')
    return value


def main():
    target = Path('/etc/repbase.env')
    if target.exists():
        raise SystemExit('Configuration already exists; refusing to overwrite secrets.')

    hosts = ask('Public hostnames, comma separated', default='rytivo.app')
    if any(host.strip() in {'localhost', '127.0.0.1', '::1'} for host in hosts.split(',')):
        raise SystemExit('Production refuses localhost hostnames; list the public ones only.')

    db_host = ask('PostgreSQL host')
    db_port = ask('PostgreSQL port', default='25060')
    db_name = ask('PostgreSQL database', default='rytivo')
    db_user = ask('PostgreSQL application role (not the cluster administrator)')
    if db_user.casefold() in ADMINISTRATIVE_ROLES:
        raise SystemExit(
            f'{db_user} administers the cluster. Create a role that owns only this '
            'database (deploy/README.md, "Database role") and use that.'
        )
    db_password = ask('PostgreSQL password (hidden)', secret=True)
    db_ca = ask('Path to the provider CA certificate', default='/etc/repbase/database-ca.pem')
    redis_url = ask('Redis URL, rediss://:password@host:port/0 (hidden)', secret=True)
    if not redis_url.startswith('rediss://'):
        raise SystemExit('Production requires authenticated TLS to Redis (rediss://).')

    database_url = 'postgresql://{}:{}@{}:{}/{}?sslmode=verify-full&sslrootcert={}'.format(
        url_quote(db_user, safe=''), url_quote(db_password, safe=''), db_host, db_port,
        url_quote(db_name, safe=''), url_quote(db_ca, safe='/'),
    )
    values = {
        'DJANGO_SETTINGS_MODULE': 'config.production',
        'DJANGO_DEBUG': 'false',
        'DJANGO_SECRET_KEY': secrets.token_urlsafe(64),
        'DJANGO_ALLOWED_HOSTS': hosts,
        'DJANGO_MEDIA_ROOT': '/home/django/app/media',
        'DATABASE_URL': database_url,
        'REDIS_URL': redis_url,
        # nginx on this machine replaces X-Forwarded-Proto and appends the
        # client's address to X-Forwarded-For; gunicorn listens on loopback
        # only, so nothing else can reach it to claim otherwise.
        'DJANGO_TRUST_PROXY_HTTPS': 'true',
        'REPBASE_NUM_PROXIES': '1',
        'GUNICORN_BIND_HOST': '127.0.0.1',
        'PORT': '8001',
    }
    content = ''.join(f'{key}={quote(value)}\n' for key, value in values.items())
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(content)
    print('Configuration saved. Secrets were not displayed.')
    print('Still to add by hand: SMTP_*, DEFAULT_FROM_EMAIL, MODERATION_* (see .env.example).')


if __name__ == '__main__':
    main()
