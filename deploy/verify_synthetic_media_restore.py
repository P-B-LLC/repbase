"""Restore only synthetic drill archives into a new isolated directory.

Not a production backup utility: no database snapshot or live writes are covered.
"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import zipfile


def restore(archive, destination):
    destination = Path(destination).resolve()
    if not str(destination).startswith('/tmp/rytivo-video-check.'):
        raise ValueError('Requires an isolated video-check destination')
    if destination.exists():
        raise ValueError('Refusing to overwrite an existing destination')
    with zipfile.ZipFile(archive) as backup:
        entries = backup.infolist()
        if len(entries) > 100 or sum(item.file_size for item in entries) > 100 * 1024 * 1024:
            raise ValueError('Synthetic archive exceeds drill limits')
        names = [item.filename for item in entries]
        if len(names) != len(set(names)):
            raise ValueError('Duplicate archive entries')
        manifest = json.loads(backup.read('manifest.json'))
        if not isinstance(manifest, dict) or set(names) != set(manifest) | {'manifest.json'}:
            raise ValueError('Archive does not match its manifest')
        # Verify every byte/path before writing any restored files.
        for name, digest in manifest.items():
            path = PurePosixPath(name)
            if not name or path.is_absolute() or '..' in path.parts or '\\' in name:
                raise ValueError('Unsafe archive path')
            target = (destination / name).resolve()
            if not target.is_relative_to(destination):
                raise ValueError('Unsafe destination')
            if hashlib.sha256(backup.read(name)).hexdigest() != digest:
                raise ValueError('Archive checksum mismatch')
        destination.mkdir(parents=True)
        for name in manifest:
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as output:
                output.write(backup.read(name))
        return len(manifest)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive')
    parser.add_argument('destination')
    args = parser.parse_args()
    count = restore(args.archive, args.destination)
    print(f'Verified and restored {count} synthetic media files; no production files changed.')
