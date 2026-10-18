"""Real decoder checks using generated, non-user media; no external API calls."""
from pathlib import Path
import shutil
import subprocess
import tempfile
from unittest import skipUnless
from PIL import Image
from django.test import SimpleTestCase, override_settings
from .moderation_frames import sampled_frames
from .video_transcode import InvalidVideo


@skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Requires FFmpeg/ffprobe')
@override_settings(VIDEO_FFMPEG='ffmpeg', VIDEO_FFPROBE='ffprobe')
class RealModerationFrameTests(SimpleTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.source = Path(self.directory.name) / 'synthetic.mp4'
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi',
            '-i', 'testsrc2=size=96x64:rate=10', '-t', '3', '-c:v', 'libx264',
            '-threads', '1', '-pix_fmt', 'yuv420p', str(self.source)],
            check=True, timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    def test_real_sampling_and_cleanup(self):
        with sampled_frames(self.source, scene_threshold=1) as frames:
            self.assertEqual(len(frames), 3)
            directory = frames[0].parent
            for frame in frames:
                with Image.open(frame) as image:
                    self.assertEqual(image.format, 'JPEG')
                    self.assertLessEqual(max(image.size), 768)
                    self.assertFalse(image.getexif())
        self.assertFalse(directory.exists())

    def test_real_overlong_sampling_is_rejected(self):
        with self.assertRaises(InvalidVideo):
            with sampled_frames(self.source, max_frames=2):
                self.fail('Partial coverage must never be accepted')

    def test_cleanup_when_consumer_fails(self):
        with self.assertRaisesRegex(RuntimeError, 'consumer'):
            with sampled_frames(self.source) as frames:
                directory = frames[0].parent
                raise RuntimeError('consumer')
        self.assertFalse(directory.exists())
