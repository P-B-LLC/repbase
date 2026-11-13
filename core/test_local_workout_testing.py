"""Safety checks for the optional local developer launcher; no real DB writes."""
import importlib.util
import os
from pathlib import Path
import runpy
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch


_script = Path(__file__).resolve().parent.parent / "Scripts/run-local-workout-testing.py"
_spec = importlib.util.spec_from_file_location("local_workout_launcher", _script)
launcher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(launcher)


class LocalWorkoutLauncherTests(TestCase):
    def test_settings_refuse_implicit_activation_and_hosted_service_configuration(self):
        from django.core.exceptions import ImproperlyConfigured
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ImproperlyConfigured):
                runpy.run_module("config.local_workout_testing")
        with patch.dict(os.environ, {"RYTIVO_LOCAL_WORKOUT_TESTING": "1", "DATABASE_URL": "not-forwarded"}, clear=True):
            with self.assertRaises(ImproperlyConfigured):
                runpy.run_module("config.local_workout_testing")

    def test_isolated_settings_pin_database_cache_media_and_mail_to_local_state(self):
        source = _script.parent.parent / "config/local_workout_testing.py"
        original_resolve = Path.resolve
        with TemporaryDirectory() as temp:
            state = launcher.prepare_state(temp)
            env = launcher.local_environment({})
            def resolve(path, *args, **kwargs):
                if path == source:
                    return Path(temp) / "config/local_workout_testing.py"
                return original_resolve(path, *args, **kwargs)
            with patch.dict(os.environ, env, clear=True), patch.object(Path, "resolve", autospec=True, side_effect=resolve):
                settings = runpy.run_module("config.local_workout_testing")
            self.assertEqual(settings["DATABASES"]["default"]["ENGINE"], "django.db.backends.sqlite3")
            self.assertEqual(settings["DATABASES"]["default"]["NAME"], state / "db.sqlite3")
            self.assertEqual(settings["MEDIA_ROOT"], state / "media")
            self.assertEqual(settings["CACHES"]["default"]["BACKEND"], "django.core.cache.backends.locmem.LocMemCache")
            self.assertEqual(settings["MAILERS"]["default"]["BACKEND"], "django.core.mail.backends.locmem.EmailBackend")
            self.assertEqual(settings["ALLOWED_HOSTS"], ["127.0.0.1", "localhost", "[::1]"])
            self.assertFalse(settings["APNS_ENABLED"])
            self.assertFalse(settings["VIDEO_PIPELINE_ENABLED"])
            self.assertFalse(settings["MODERATION_ENABLED"])

    def test_environment_does_not_inherit_service_credentials_or_python_injection(self):
        env = launcher.local_environment({
            "PATH": "/safe/bin", "DATABASE_URL": "not-forwarded", "REDIS_URL": "not-forwarded",
            "OPENAI_API_KEY": "not-forwarded", "SMTP_PASSWORD": "not-forwarded",
            "PYTHONPATH": "/untrusted", "DJANGO_SETTINGS_MODULE": "config.production",
            "A_FUTURE_PROVIDER_SECRET": "not-forwarded", "HTTP_PROXY": "not-forwarded",
        })
        self.assertEqual(env["PATH"], "/safe/bin")
        self.assertEqual(env["DJANGO_SETTINGS_MODULE"], "config.local_workout_testing")
        self.assertEqual(env["RYTIVO_LOCAL_WORKOUT_TESTING"], "1")
        for key in ("DATABASE_URL", "REDIS_URL", "OPENAI_API_KEY", "SMTP_PASSWORD",
                    "PYTHONPATH", "A_FUTURE_PROVIDER_SECRET", "HTTP_PROXY"):
            self.assertNotIn(key, env)
        self.assertGreaterEqual(len(env["DJANGO_SECRET_KEY"]), 64)

    def test_prepare_creates_only_marked_private_state_and_preserves_work(self):
        with TemporaryDirectory() as temp:
            state = launcher.prepare_state(temp)
            database = state / "db.sqlite3"
            database.write_bytes(b"existing-local-work")
            self.assertEqual(launcher.prepare_state(temp), state)
            self.assertEqual(database.read_bytes(), b"existing-local-work")
            self.assertTrue((state / "media").is_dir())
            self.assertFalse((Path(temp) / "db.sqlite3").exists())

    def test_unmarked_existing_state_is_never_adopted(self):
        with TemporaryDirectory() as temp:
            state = Path(temp) / launcher.STATE_NAME
            state.mkdir()
            database = state / "db.sqlite3"
            database.write_bytes(b"do-not-touch")
            with self.assertRaises(ValueError):
                launcher.prepare_state(temp)
            self.assertEqual(database.read_bytes(), b"do-not-touch")

    def test_wrong_marker_is_refused(self):
        with TemporaryDirectory() as temp:
            state = Path(temp) / launcher.STATE_NAME
            state.mkdir()
            (state / ".rytivo-local-only").write_text("other-workspace")
            with self.assertRaises(ValueError):
                launcher.prepare_state(temp)

    def test_linked_database_is_refused(self):
        with TemporaryDirectory() as temp:
            state = launcher.prepare_state(temp)
            outside = Path(temp) / "outside.sqlite3"
            outside.write_bytes(b"unrelated")
            try:
                (state / "db.sqlite3").symlink_to(outside)
            except OSError:
                self.skipTest("Creating symbolic links requires OS permission")
            with self.assertRaises(ValueError):
                launcher.prepare_state(temp)
            self.assertEqual(outside.read_bytes(), b"unrelated")

    def test_link_guard_runs_before_touching_an_existing_database(self):
        with TemporaryDirectory() as temp:
            state = launcher.prepare_state(temp)
            database = state / "db.sqlite3"
            database.write_bytes(b"preserve-me")
            real_is_symlink = Path.is_symlink
            with patch.object(Path, "is_symlink", autospec=True,
                              side_effect=lambda path: path == database or real_is_symlink(path)):
                with self.assertRaises(ValueError):
                    launcher.prepare_state(temp)
            self.assertEqual(database.read_bytes(), b"preserve-me")
