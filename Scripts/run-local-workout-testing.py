#!/usr/bin/env python3
"""Run a real API backed by a private, durable local SQLite database.

No .env is read, no live configuration is inherited, and existing local work
is never reseeded or reset. Passwords are prompted without echo and only
Django's password hash is saved. Ctrl-C stops serving, not the database.
"""
import argparse
import getpass
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys

REPO = Path(__file__).resolve().parent.parent
STATE_NAME = ".local-workout-testing"
MARKER = "rytivo-local-workout-testing-v1\n"
USERNAME = "local_workout_tester"


def prepare_state(repo):
    state = Path(repo).resolve() / STATE_NAME
    if state.is_symlink():
        raise ValueError("Refusing a linked local state directory.")
    marker = state / ".rytivo-local-only"
    if state.exists():
        if not marker.is_file() or marker.is_symlink() or marker.read_text() != MARKER:
            raise ValueError("Existing local state is not marked as this testing workspace; nothing changed.")
    else:
        state.mkdir(mode=0o700)
        marker.write_text(MARKER)
    state.chmod(0o700)
    for name in ("db.sqlite3", "media", "private-videos"):
        if (state / name).is_symlink():
            raise ValueError("Refusing a linked local database/media path.")
    for name in ("media", "private-videos"):
        (state / name).mkdir(mode=0o700, exist_ok=True)
    return state


def local_environment(inherited):
    # An allowlist, not a denylist: a future provider key is excluded too.
    env = {key: inherited[key] for key in (
        "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL",
    ) if key in inherited}
    env.update({
        "DJANGO_SETTINGS_MODULE": "config.local_workout_testing",
        "DJANGO_SECRET_KEY": secrets.token_urlsafe(64),
        "DJANGO_DEBUG": "true",
        "RYTIVO_LOCAL_WORKOUT_TESTING": "1",
        "PYTHONUNBUFFERED": "1",
    })
    return env


def prepare_database():
    if os.environ.get("DJANGO_SETTINGS_MODULE") != "config.local_workout_testing":
        raise ValueError("Database setup requires the isolated local settings.")
    sys.path.insert(0, str(REPO))
    import django
    django.setup()
    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.contrib.auth.password_validation import validate_password
    from django.core.exceptions import ValidationError
    from django.core.management import call_command
    from django.db import transaction
    from core.models import RepbaseUser

    expected = (REPO / STATE_NAME / "db.sqlite3").resolve()
    if Path(settings.DATABASES["default"]["NAME"]).resolve() != expected:
        raise ValueError("Database is outside the isolated local workspace; refusing setup.")
    call_command("migrate", interactive=False)
    User = get_user_model()
    if not User.objects.filter(username=USERNAME).exists():
        if not sys.stdin.isatty():
            raise ValueError("First setup requires an interactive terminal to choose a local test password.")
        print(f"Create your separate LOCAL testing account: {USERNAME}")
        print("Choose a new password; do not reuse your Rytivo password.")
        candidate = User(username=USERNAME, email="local-workout@example.invalid", first_name="Local", last_name="Tester")
        while True:
            password = getpass.getpass("Local test password: ")
            confirmation = getpass.getpass("Confirm local test password: ")
            if password != confirmation:
                print("Passwords did not match. Try again.")
                continue
            try:
                validate_password(password, user=candidate)
            except ValidationError as error:
                print(" ".join(error.messages))
                continue
            break
        with transaction.atomic():
            candidate.set_password(password)
            candidate.save()
            RepbaseUser.objects.get_or_create(user=candidate)
        del password, confirmation
    print(f"Local account: {USERNAME}. Existing passwords, workouts and sessions are kept.")
    print("Review the agreements normally when you first sign in. No agreement is accepted by this script.")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--prepare-only", action="store_true", help="Migrate/create the local account without starting the server")
    parser.add_argument("--internal-prepare", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.internal_prepare:
        prepare_database()
        return
    if not 1024 <= args.port <= 65535:
        parser.error("Use an unprivileged local port from 1024 to 65535.")
    # Do not migrate anything before detecting a server already on this port.
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", args.port))
        except OSError:
            parser.error(f"Local port {args.port} is in use. Stop that server or choose --port; nothing migrated.")
    state = prepare_state(REPO)
    env = local_environment(os.environ)
    subprocess.run([sys.executable, str(Path(__file__).resolve()), "--internal-prepare"],
                   cwd=REPO, env=env, check=True)
    if args.prepare_only:
        return
    print(f"Real LOCAL API: http://127.0.0.1:{args.port}; leave this terminal open.", flush=True)
    print(f"Persistent local data: {state}. No live database or service credentials are used.", flush=True)
    try:
        subprocess.run([sys.executable, "manage.py", "runserver", f"127.0.0.1:{args.port}", "--noreload"],
                       cwd=REPO, env=env, check=True)
    except KeyboardInterrupt:
        print("\nLocal API stopped. Your local workouts are saved for next time.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from None
