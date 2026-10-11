"""Apply only the explicit local-video settings, preserving other deployment secrets."""
import argparse
import os
from pathlib import Path
import re
import tempfile
from datetime import datetime, timezone

VALUES = {
    'VIDEO_PIPELINE_ENABLED': 'true',
    'VIDEO_STORAGE_BACKEND': 'local',
    'VIDEO_LOCAL_ROOT': '/var/lib/rytivo/videos',
    'VIDEO_LOCAL_ACCEL_PREFIX': '/_private_video/',
    'VIDEO_MODERATION_BYPASS': 'true',
}


def updated_environment(original, enabled):
    values = dict(VALUES, VIDEO_PIPELINE_ENABLED='true' if enabled else 'false')
    kept = [line for line in original.splitlines()
            if not (match := re.match(r'^\s*(?:export\s+)?([A-Z_][A-Z_0-9]*)\s*=', line))
            or match.group(1) not in values]
    return '\n'.join(kept + [f'{key}={value}' for key, value in values.items()]) + '\n'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--enable', action='store_true', help='Activate only after validation.')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('Run as root.')
    target = Path('/etc/repbase.env')
    if target.is_symlink() or not target.is_file():
        raise SystemExit('Expected a regular /etc/repbase.env file.')
    original = target.read_text()
    backup = target.with_name('repbase.env.video-backup.' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    with backup.open('x', opener=lambda path, flags: os.open(path, flags, 0o600)) as handle:
        handle.write(original)
    descriptor, temporary = tempfile.mkstemp(prefix='.repbase-video-', dir='/etc')
    try:
        with os.fdopen(descriptor, 'w') as handle:
            handle.write(updated_environment(original, args.enable))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print('Video settings saved; pipeline ' + ('enabled' if args.enable else 'disabled') + '.')
    print('Recoverable environment backup: ' + str(backup))


if __name__ == '__main__':
    main()
