"""Private immutable local objects. Never serve this root as public media."""
import hashlib
import os
import shutil
from pathlib import Path
from django.conf import settings
from django.core.files.storage import FileSystemStorage


def path(key):
    root = Path(settings.VIDEO_LOCAL_ROOT).resolve()
    candidate = (root / key).resolve()
    if not key or candidate == root or not candidate.is_relative_to(root):
        raise ValueError('invalid_storage_key')
    return candidate


def storage():
    return FileSystemStorage(location=settings.VIDEO_LOCAL_ROOT)


def available():
    root = Path(settings.VIDEO_LOCAL_ROOT)
    root.mkdir(parents=True, exist_ok=True, mode=0o750)
    return shutil.disk_usage(root).free


def upload_policy(key, size, content_type):
    return {'url': '', 'fields': {}}


def write(key, source):
    target = path(key)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    digest = hashlib.sha256()
    size = 0
    with target.open('xb') as output:
        if os.name == 'posix':
            os.fchmod(output.fileno(), 0o640)
        while chunk := source.read(256 * 1024):
            size += len(chunk)
            digest.update(chunk)
            output.write(chunk)
    return size, digest.hexdigest()


def upload(key, source_path, content_type):
    with open(source_path, 'rb') as source:
        write(key, source)


def head(key):
    target = path(key)
    digest = hashlib.sha256()
    with target.open('rb') as source:
        while chunk := source.read(256 * 1024):
            digest.update(chunk)
    return {'ContentLength': target.stat().st_size, 'ETag': digest.hexdigest()}


def download(key, etag, destination, limit):
    digest = hashlib.sha256()
    size = 0
    with path(key).open('rb') as source, open(destination, 'wb') as output:
        while chunk := source.read(256 * 1024):
            size += len(chunk)
            if size > limit:
                raise ValueError('oversized_source')
            digest.update(chunk)
            output.write(chunk)
    if digest.hexdigest() != etag:
        raise ValueError('source_changed')
    return size


def delete(key):
    target = path(key)
    target.unlink(missing_ok=True)
    root = Path(settings.VIDEO_LOCAL_ROOT).resolve()
    parent = target.parent
    while parent != root:
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent
