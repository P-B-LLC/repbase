from pathlib import Path
from unittest.mock import patch
from django.test import SimpleTestCase, override_settings
from .moderation_frames import sampled_frames
from .video_transcode import InvalidVideo


@override_settings(VIDEO_FFMPEG='ffmpeg')
class ModerationFrameTests(SimpleTestCase):
    @patch('core.moderation_frames.probe', return_value={'duration_ms': 3000})
    @patch('core.moderation_frames.run')
    def test_extraction_is_bounded_and_temporary(self, run, probe):
        def create(command):
            self.assertIn('file', command)
            self.assertIn('4', command)
            root = Path(command[-1]).parent
            (root / '000001.jpg').write_bytes(b'fixture')
            (root / '000002.jpg').write_bytes(b'fixture')
        run.side_effect = create
        with sampled_frames('local.mp4', max_frames=3) as frames:
            self.assertEqual(len(frames), 2)
            root = frames[0].parent
        self.assertFalse(root.exists())

    @patch('core.moderation_frames.probe', return_value={'duration_ms': 3000})
    @patch('core.moderation_frames.run')
    def test_overlong_content_is_not_partially_approved(self, run, probe):
        with self.assertRaises(InvalidVideo):
            with sampled_frames('local.mp4', max_frames=2):
                pass
        run.assert_not_called()

    @patch('core.moderation_frames.probe', return_value={'duration_ms': 1000})
    @patch('core.moderation_frames.run')
    def test_empty_output_never_passes(self, run, probe):
        with self.assertRaises(InvalidVideo):
            with sampled_frames('local.mp4'):
                pass
