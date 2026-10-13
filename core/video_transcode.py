"""Only worker code invokes encoders. Local files, bounded runtime and threads."""
import json
import math
import subprocess
from pathlib import Path
from django.conf import settings


class InvalidVideo(ValueError):
    pass


def run(command):
    # Shell interpretation is never involved. Sandbox the service as well.
    result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, timeout=settings.VIDEO_PROCESS_TIMEOUT, check=True)
    return result.stdout


def probe(path):
    try:
        data = json.loads(run([settings.VIDEO_FFPROBE, '-v', 'error', '-threads', '1', '-enable_drefs', '0', '-protocol_whitelist', 'file',
            '-format_whitelist', 'mov', '-show_entries',
            'format=duration:stream=codec_type,codec_name,width,height,avg_frame_rate', '-of', 'json', str(path)]))
    except subprocess.CalledProcessError as error:
        raise InvalidVideo('invalid_container') from error
    streams = data.get('streams', [])
    videos = [s for s in streams if s.get('codec_type') == 'video']
    if len(videos) != 1 or videos[0].get('codec_name') not in ('h264', 'hevc'):
        raise InvalidVideo('unsupported_video')
    video = videos[0]
    duration = float(data.get('format', {}).get('duration', 0))
    width, height = int(video.get('width', 0)), int(video.get('height', 0))
    if not math.isfinite(duration) or not 0 < duration <= settings.VIDEO_MAX_SECONDS:
        raise InvalidVideo('duration_limit')
    if min(width, height) < 2 or max(width, height) > settings.VIDEO_MAX_DIMENSION or width * height > settings.VIDEO_MAX_PIXELS:
        raise InvalidVideo('resolution_limit')
    numerator, denominator = video.get('avg_frame_rate', '0/1').split('/')
    fps = float(numerator) / max(float(denominator), 1)
    if not math.isfinite(fps) or fps > (60 if settings.VIDEO_STORAGE_BACKEND == 'local' else 120):
        raise InvalidVideo('frame_rate_limit')
    return {'duration_ms': round(duration * 1000), 'width': width, 'height': height,
        'has_audio': any(s.get('codec_type') == 'audio' for s in streams)}


def transcode(source, directory):
    info = probe(source)
    # Bounding boxes fit portrait and landscape; never upscale. Encoder output
    # is probed again to measure rotation and actual dimensions.
    sides = [854]
    if settings.VIDEO_STORAGE_BACKEND != 'local' and max(info['width'], info['height']) > 854:
        sides.append(1280)
    outputs = []
    for side in sides:
        path = Path(directory) / f'{side}.mp4'
        short = 480 if side == 854 else 720
        scale = f"scale=w='min(iw,if(gte(iw,ih),{side},{short}))':h='min(ih,if(gte(iw,ih),{short},{side}))':force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1,fps=30"
        run([settings.VIDEO_FFMPEG, '-nostdin', '-v', 'error', '-y', '-threads', '1',
            '-protocol_whitelist', 'file', '-format_whitelist', 'mov', '-enable_drefs', '0', '-i', str(source),
            '-map', '0:v:0', '-map', '0:a:0?', '-map_metadata', '-1', '-map_chapters', '-1',
            '-vf', scale, '-filter_threads', '1', '-c:v', 'libx264', '-threads', '1',
            '-preset', 'veryfast', '-crf', '24', '-maxrate', '2500k' if side > 854 else '1200k',
            '-bufsize', '5000k' if side > 854 else '2400k', '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', '96k', '-ac', '2', '-movflags', '+faststart',
            '-t', str(settings.VIDEO_MAX_SECONDS), str(path)])
        metadata = probe(path)
        outputs.append({'path': path, **metadata, 'size_bytes': path.stat().st_size})
    thumbnail = Path(directory) / 'thumbnail.jpg'
    run([settings.VIDEO_FFMPEG, '-nostdin', '-v', 'error', '-y', '-threads', '1',
        '-protocol_whitelist', 'file', '-i', str(outputs[0]['path']), '-map', '0:v:0',
        '-map_metadata', '-1', '-frames:v', '1', '-vf', 'scale=320:-2', '-filter_threads', '1', '-threads', '1', str(thumbnail)])
    return outputs, thumbnail
