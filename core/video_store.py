"""The beta filesystem can later be replaced by private object storage."""
from django.conf import settings
from . import video_local_store, video_object_store


def backend():
    if settings.VIDEO_STORAGE_BACKEND == 'local':
        return video_local_store
    if settings.VIDEO_STORAGE_BACKEND == 's3':
        return video_object_store
    raise ValueError('Unknown video storage backend.')


def __getattr__(name):
    return getattr(backend(), name)
