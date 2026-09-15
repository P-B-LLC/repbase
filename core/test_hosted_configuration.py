from pathlib import Path
import os
import subprocess
import sys
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase, override_settings

from config.environment import database, production_values
from config.health import dependencies_available


class DatabaseConfigurationTests(SimpleTestCase):
    def test_sqlite_only_defaults_for_development(self):
        self.assertEqual(database({}, Path('/tmp'))['NAME'], Path('/tmp/db.sqlite3'))
        with self.assertRaises(ImproperlyConfigured):
            database({}, Path('/tmp'), production=True)

    def test_postgres_decodes_credentials_and_enforces_tls(self):
        value = database({'DATABASE_URL': 'postgresql://user:p%40ss%2Fword@db.test/app'}, Path('/tmp'), production=True)
        self.assertEqual(value['PASSWORD'], 'p@ss/word')
        self.assertEqual(value['OPTIONS']['sslmode'], 'verify-full')
        self.assertEqual(value['OPTIONS']['connect_timeout'], 5)
        self.assertIn('lock_timeout=5000', value['OPTIONS']['options'])
        self.assertTrue(value['CONN_HEALTH_CHECKS'])

    def test_transaction_pooler_disables_session_features(self):
        value = database({'DATABASE_URL': 'postgres://u:p@db.test/app', 'DATABASE_TRANSACTION_POOLING': 'true'}, Path('/tmp'), production=True)
        self.assertEqual(value['CONN_MAX_AGE'], 0)
        self.assertTrue(value['DISABLE_SERVER_SIDE_CURSORS'])
        self.assertIsNone(value['OPTIONS']['prepare_threshold'])

    def test_unsafe_or_malformed_urls_fail_without_echoing_credentials(self):
        for url in ['sqlite:///app', 'postgres://u:private-secret@db:bad/app',
                    'postgres://u:private-secret@db:0/app',
                    'postgres://u:private-secret@db/app?sslmode=disable',
                    'postgres://u:private-secret@db/app?sslmode=require',
                    'postgres://u:private-secret@db/app?options=unsafe',
                    'postgres://u:private-secret@db/app?sslmode=verify-full&sslmode=disable',
                    'postgres://u:private-secret@db/', 'postgres://u:private-secret@db/app#fragment']:
            with self.subTest(url=url):
                with self.assertRaises(ImproperlyConfigured) as caught:
                    database({'DATABASE_URL': url}, Path('/tmp'), production=True)
                self.assertNotIn('private-secret', str(caught.exception))

    def test_bounded_timeouts(self):
        for value in ['0', '-1', 'unlimited', '9999']:
            with self.assertRaises(ImproperlyConfigured):
                database({'DATABASE_URL': 'postgres://u:p@db/app', 'DATABASE_CONNECT_TIMEOUT': value}, Path('/tmp'))


class ProductionConfigurationTests(SimpleTestCase):
    def valid(self):
        return {'DJANGO_DEBUG': 'false', 'DJANGO_SECRET_KEY': 'unit-test-only-not-a-real-secret-' * 3,
                'DJANGO_ALLOWED_HOSTS': 'api.example.com', 'DJANGO_MEDIA_ROOT': '/persistent/media',
                'REDIS_URL': 'rediss://:test-password@cache.example.com:6379/0'}

    def test_shared_tls_cache_and_explicit_storage(self):
        config = production_values(self.valid())
        self.assertEqual(config['CACHES']['default']['BACKEND'], 'django.core.cache.backends.redis.RedisCache')
        self.assertEqual(config['MEDIA_ROOT'], Path('/persistent/media'))

    def test_production_rejects_development_and_insecure_settings(self):
        for key, value in [('DJANGO_DEBUG', 'true'), ('DJANGO_SECRET_KEY', ''),
                           ('DJANGO_ALLOWED_HOSTS', '*'), ('DJANGO_ALLOWED_HOSTS', 'localhost'),
                           ('DJANGO_ALLOWED_HOSTS', ''), ('DJANGO_MEDIA_ROOT', 'media'),
                           ('REDIS_URL', 'redis://:secret@cache/0'),
                           ('REDIS_URL', 'rediss://cache/0'),
                           ('REDIS_URL', 'rediss://:secret@cache/0?ssl_cert_reqs=none')]:
            with self.subTest(key=key, value=value), self.assertRaises(ImproperlyConfigured):
                production_values({**self.valid(), key: value})

    def test_real_production_module_has_secure_mail_database_and_cookies(self):
        env = {**os.environ, **self.valid(), 'DJANGO_SETTINGS_MODULE': 'config.production',
               'DATABASE_URL': 'postgresql://test:test@db.example.com/rytivo',
               'DATABASE_SSLMODE': 'verify-full', 'DJANGO_TRUST_PROXY_HTTPS': 'false'}
        result = subprocess.run([sys.executable, '-c',
            "from django.conf import settings as s; "
            "assert not s.DEBUG; assert s.SESSION_COOKIE_SECURE; assert s.CSRF_COOKIE_SECURE; "
            "assert s.SECURE_SSL_REDIRECT; assert s.SECURE_PROXY_SSL_HEADER is None; "
            "assert s.DATABASES['default']['OPTIONS']['sslmode'] == 'verify-full'; "
            "assert s.MAILERS['default']['BACKEND'].endswith('smtp.EmailBackend')"],
            env=env, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_production_module_fails_without_explicit_debug_false(self):
        env = {**os.environ, **self.valid(), 'DJANGO_SETTINGS_MODULE': 'config.production',
               'DATABASE_URL': 'postgresql://test:test@db.example.com/rytivo',
               'DATABASE_SSLMODE': 'verify-full'}
        env.pop('DJANGO_DEBUG')
        result = subprocess.run([sys.executable, '-c', 'from django.conf import settings; print(settings.DEBUG)'],
                                env=env, capture_output=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'Set DJANGO_DEBUG=false explicitly', result.stderr)


class DeploymentCommandTests(SimpleTestCase):
    def run_action(self, action, healthy=True):
        from config.deploy import main
        with patch.dict('os.environ'), patch('sys.argv', ['deploy', action]), \
             patch('django.setup'), patch('django.core.management.call_command') as command, \
             patch('config.health.dependencies_available', return_value=healthy), \
             patch('os.execv') as execute:
            if not healthy:
                with self.assertRaises(SystemExit):
                    main()
            else:
                main()
            return command.call_args_list, execute.call_args_list

    def test_check_never_migrates(self):
        calls, execs = self.run_action('check')
        self.assertEqual([call.args[0] for call in calls], ['check'])
        self.assertFalse(execs)

    def test_release_is_the_only_migrating_command(self):
        calls, execs = self.run_action('release')
        self.assertEqual([call.args[0] for call in calls], ['check', 'migrate', 'collectstatic'])
        self.assertFalse(execs)

    def test_serve_checks_migrations_without_applying_them(self):
        calls, execs = self.run_action('serve')
        self.assertTrue(calls[1].kwargs['check_unapplied'])
        self.assertEqual(len(execs), 1)

    def test_failed_readiness_prevents_migration_and_server_start(self):
        for action in ['serve', 'release']:
            calls, execs = self.run_action(action, healthy=False)
            self.assertEqual([call.args[0] for call in calls], ['check'])
            self.assertFalse(execs)


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class InfrastructureHealthTests(TestCase):
    def test_liveness_does_not_touch_dependencies(self):
        with patch('config.health.connection') as db, patch('config.health.cache') as cache:
            response = self.client.get('/health/live/')
            self.assertEqual(response.status_code, 200)
            db.cursor.assert_not_called()
            cache.get.assert_not_called()

    def test_readiness_checks_real_test_database(self):
        response = self.client.get('/health/ready/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('no-store', response['Cache-Control'])

    def test_database_outage_returns_503_without_details(self):
        with patch('config.health.connection.cursor', side_effect=RuntimeError('secret connection details')):
            response = self.client.get('/health/ready/')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {'status': 'unavailable'})

    def test_cache_failure_and_silent_write_failure_are_not_healthy(self):
        with patch('config.health.cache') as cache:
            cache.get.return_value = None
            self.assertFalse(dependencies_available())
            cache.delete.assert_called_once()
        with patch('config.health.cache.set', side_effect=RuntimeError('cache secret')):
            self.assertFalse(dependencies_available())

    def test_post_is_not_allowed(self):
        self.assertEqual(self.client.post('/health/ready/').status_code, 405)
