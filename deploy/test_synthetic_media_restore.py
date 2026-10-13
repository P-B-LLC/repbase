import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
import zipfile
from .verify_synthetic_media_restore import restore


@unittest.skipUnless(os.name == 'posix' and Path('/tmp').is_dir(), 'Linux-only isolated restore drill')
class SyntheticRestoreTests(unittest.TestCase):
    def test_valid_restore_and_existing_destination_refused(self):
        with tempfile.TemporaryDirectory(prefix='rytivo-video-check.', dir='/tmp') as directory:
            root = Path(directory)
            archive = root / 'backup.zip'
            data = b'synthetic-media'
            with zipfile.ZipFile(archive, 'w') as output:
                output.writestr('ready/example.mp4', data)
                output.writestr('manifest.json', json.dumps({'ready/example.mp4': hashlib.sha256(data).hexdigest()}))
            destination = root / 'restored'
            self.assertEqual(restore(archive, destination), 1)
            self.assertEqual((destination / 'ready/example.mp4').read_bytes(), data)
            with self.assertRaises(ValueError):
                restore(archive, destination)

    def test_corruption_traversal_and_extra_files_fail_before_writing(self):
        for name, digest, extra in [
            ('ready/example.mp4', '0' * 64, False),
            ('../escape.mp4', hashlib.sha256(b'clip').hexdigest(), False),
            ('ready/example.mp4', hashlib.sha256(b'clip').hexdigest(), True),
        ]:
            with self.subTest(name=name, extra=extra), tempfile.TemporaryDirectory(prefix='rytivo-video-check.', dir='/tmp') as directory:
                root = Path(directory)
                archive = root / 'backup.zip'
                with zipfile.ZipFile(archive, 'w') as output:
                    output.writestr(name, b'clip')
                    output.writestr('manifest.json', json.dumps({name: digest}))
                    if extra:
                        output.writestr('untracked.mp4', b'extra')
                with self.assertRaises(ValueError):
                    restore(archive, root / 'restored')
                self.assertFalse((root / 'restored').exists())
