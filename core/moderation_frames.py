"""Bounded local FFmpeg sampling; exceeding coverage limits is never a pass."""
from contextlib import contextmanager
import math
from pathlib import Path
import tempfile

from django.conf import settings

from .video_transcode import probe, run, InvalidVideo


@contextmanager
def sampled_frames(source, *, interval=1.0, max_frames=300, scene_threshold=0.4):
    if (type(interval) not in (int, float) or not math.isfinite(interval) or not 0.1 <= interval <= 5
            or type(max_frames) is not int or not 1 <= max_frames <= 600
            or type(scene_threshold) not in (int, float) or not 0 < scene_threshold <= 1):
        raise ValueError('Invalid sampling limits')
    info = probe(source)
    if math.ceil(info['duration_ms'] / 1000 / interval) > max_frames:
        raise InvalidVideo('moderation_frame_limit')
    with tempfile.TemporaryDirectory(prefix='rytivo-review-') as directory:
        # First frame, intervals no greater than configured spacing, and scene
        # changes. Sampling can still miss brief material between sampled frames.
        select = f"select='eq(n,0)+gte(t-prev_selected_t,{interval})+gt(scene,{scene_threshold})'"
        scale = "scale=w='min(768,iw)':h='min(768,ih)':force_original_aspect_ratio=decrease"
        run([settings.VIDEO_FFMPEG, '-nostdin', '-v', 'error', '-y', '-threads', '1',
             '-protocol_whitelist', 'file', '-format_whitelist', 'mov', '-enable_drefs', '0',
             '-i', str(source), '-map', '0:v:0', '-an', '-sn', '-dn', '-map_metadata', '-1',
             '-vf', select + ',' + scale, '-filter_threads', '1', '-threads', '1',
             '-fps_mode', 'vfr', '-frames:v', str(max_frames + 1), '-q:v', '3',
             str(Path(directory) / '%06d.jpg')])
        frames = sorted(Path(directory).glob('*.jpg'))
        if not frames or len(frames) > max_frames:
            raise InvalidVideo('moderation_frame_limit')
        if any(not 0 < frame.stat().st_size <= 2 * 1024 * 1024 for frame in frames):
            raise InvalidVideo('moderation_frame_size')
        yield frames
