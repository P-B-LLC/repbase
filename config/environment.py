"""Small, testable environment parsers. Never include secret values in errors."""
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

from django.core.exceptions import ImproperlyConfigured


def integer(env, name, default, minimum=0, maximum=3600):
    try:
        value = int(env.get(name, str(default)))
    except (TypeError, ValueError):
        raise ImproperlyConfigured(f'{name} must be an integer.') from None
    if not minimum <= value <= maximum:
        raise ImproperlyConfigured(f'{name} must be between {minimum} and {maximum}.')
    return value


def boolean(env, name, default=False):
    value = env.get(name, 'true' if default else 'false').lower()
    if value not in {'true', 'false', '1', '0'}:
        raise ImproperlyConfigured(f'{name} must be true or false.')
    return value in {'true', '1'}


def database(env, base_dir, *, production=False):
    raw = env.get('DATABASE_URL', '')
    if not raw:
        if production:
            raise ImproperlyConfigured('Production requires DATABASE_URL for PostgreSQL.')
        return {'ENGINE': 'django.db.backends.sqlite3', 'NAME': base_dir / 'db.sqlite3',
                'CONN_MAX_AGE': integer(env, 'DJANGO_CONN_MAX_AGE', 60), 'CONN_HEALTH_CHECKS': True}
    try:
        url = urlsplit(raw)
        port = 5432 if url.port is None else url.port
        pairs = parse_qsl(url.query, strict_parsing=True)
        if (url.scheme not in {'postgres', 'postgresql'} or not url.hostname
                or not url.username or not url.password or not url.path.strip('/')
                or url.fragment or not 1 <= port <= 65535):
            raise ValueError()
        if any(key not in {'sslmode', 'sslrootcert'} for key, _ in pairs) or len(dict(pairs)) != len(pairs):
            raise ValueError()
    except ValueError:
        raise ImproperlyConfigured('DATABASE_URL must be a PostgreSQL URL with host, database, user and password; only sslmode/sslrootcert query options are supported.') from None
    query = dict(pairs)
    mode = query.get('sslmode', env.get('DATABASE_SSLMODE', 'verify-full' if production else 'disable'))
    allowed = {'verify-full'} if production else {'disable', 'require', 'verify-ca', 'verify-full'}
    if mode not in allowed:
        raise ImproperlyConfigured('DATABASE_SSLMODE must verify the server hostname in production (verify-full).')
    options = {
        'sslmode': mode,
        'connect_timeout': integer(env, 'DATABASE_CONNECT_TIMEOUT', 5, 1, 60),
        'options': '-c statement_timeout={} -c lock_timeout={} -c idle_in_transaction_session_timeout={}'.format(
            integer(env, 'DATABASE_STATEMENT_TIMEOUT_MS', 30000, 1000, 3600000),
            integer(env, 'DATABASE_LOCK_TIMEOUT_MS', 5000, 100, 3600000),
            integer(env, 'DATABASE_IDLE_TRANSACTION_TIMEOUT_MS', 60000, 1000, 3600000)),
    }
    root = query.get('sslrootcert', env.get('DATABASE_SSLROOTCERT', ''))
    if root:
        options['sslrootcert'] = root
    pooled = boolean(env, 'DATABASE_TRANSACTION_POOLING')
    if pooled:
        # Works with poolers that do not support protocol-level prepared statements.
        options['prepare_threshold'] = None
    return {
        'ENGINE': 'django.db.backends.postgresql', 'NAME': unquote(url.path[1:]),
        'USER': unquote(url.username), 'PASSWORD': unquote(url.password),
        'HOST': url.hostname, 'PORT': port, 'OPTIONS': options,
        'CONN_MAX_AGE': 0 if pooled else integer(env, 'DJANGO_CONN_MAX_AGE', 60),
        'CONN_HEALTH_CHECKS': True, 'DISABLE_SERVER_SIDE_CURSORS': pooled,
    }


def production_values(env):
    if boolean(env, 'DJANGO_DEBUG'):
        raise ImproperlyConfigured('DJANGO_DEBUG must be false in production.')
    secret = env.get('DJANGO_SECRET_KEY', '')
    if len(secret) < 50 or len(set(secret)) < 5 or secret.startswith('django-insecure-'):
        raise ImproperlyConfigured('Production requires a strong DJANGO_SECRET_KEY of at least 50 characters.')
    hosts = [host.strip() for host in env.get('DJANGO_ALLOWED_HOSTS', '').split(',') if host.strip()]
    if not hosts or any(host in {'localhost', '127.0.0.1', '::1'} or '*' in host or host.startswith('.') or '://' in host or '/' in host for host in hosts):
        raise ImproperlyConfigured('DJANGO_ALLOWED_HOSTS must explicitly list the deployed hostnames.')
    media = env.get('DJANGO_MEDIA_ROOT', '')
    if not media or not Path(media).is_absolute():
        raise ImproperlyConfigured('DJANGO_MEDIA_ROOT must be an absolute persistent-volume path.')
    cache_url = env.get('REDIS_URL', '')
    try:
        cache = urlsplit(cache_url)
        if cache.scheme != 'rediss' or not cache.hostname or cache.query or cache.fragment or not cache.password:
            raise ValueError()
        if cache.port is not None and not 1 <= cache.port <= 65535:
            raise ValueError()
    except ValueError:
        raise ImproperlyConfigured('REDIS_URL must use authenticated TLS (rediss://), without query options.') from None
    return {'SECRET_KEY': secret, 'ALLOWED_HOSTS': hosts, 'MEDIA_ROOT': Path(media),
            'CACHES': {'default': {
                'BACKEND': 'django.core.cache.backends.redis.RedisCache', 'LOCATION': cache_url,
                'KEY_PREFIX': env.get('CACHE_KEY_PREFIX', 'rytivo'),
                'OPTIONS': {'socket_connect_timeout': 3, 'socket_timeout': 3},
            }}}
