"""Opt-in real nginx/encoder drill. Never uses the production database.

Run in an isolated checkout with RYTIVO_NGINX_DRILL=1 and a local nginx binary.
RYTIVO_DRILL_ARTIFACT_DIR optionally retains a synthetic media backup for SCP.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from unittest import skipUnless
import zipfile

from django.test import LiveServerTestCase, RequestFactory, override_settings
from rest_framework.test import APIClient
from .tests import RepbaseAPITestMixin
from .fixtures_for_tests import video_fixture, workout_post
from .models import PostVideo
from .video_pipeline import claim, process
from .video_pipeline_views import asset_url


@skipUnless(os.getenv('RYTIVO_NGINX_DRILL') == '1', 'explicit nginx drill required')
@override_settings(VIDEO_PIPELINE_ENABLED=True, VIDEO_STORAGE_BACKEND='local',
                   VIDEO_MODERATION_BYPASS=True, VIDEO_LOCAL_ACCEL_PREFIX='/_private_video/')
class VideoDeliveryDrill(RepbaseAPITestMixin, LiveServerTestCase):
    def test_real_upload_range_revocation_and_restored_media(self):
        self.client = APIClient()
        user, owner, token = self.create_account('delivery-drill')
        post = workout_post(owner)
        with tempfile.TemporaryDirectory(prefix='rytivo-delivery-') as directory:
            root = Path(directory)
            media = root / 'media'
            media.mkdir()
            with override_settings(VIDEO_LOCAL_ROOT=str(media)):
                self.authenticate(token)
                response = self.client.post(f'/api/v1/social/posts/{post.pk}/video/',
                    video_fixture('h264_faststart.mp4'), content_type='video/mp4')
                self.assertEqual(response.status_code, 200, response.data)
                self.assertIsNone(response.data['video']['url'])
                self.assertTrue(process(claim()))  # Actual FFmpeg, no encoder mock.
                video = PostVideo.objects.get(post=post)
                self.assertEqual(video.processing_state, 'ready')
                key = video.variants[0]['key']
                expected = (media / key).read_bytes()
                with socket.socket() as probe:
                    probe.bind(('127.0.0.1', 0))
                    port = probe.getsockname()[1]
                # Reuse installed production routes when available, changing only
                # upstream, media root and log destination for this disposable run.
                snippet = Path('/etc/nginx/snippets/rytivo-video-locations.conf')
                routes = snippet.read_text().replace('http://127.0.0.1:8001', self.live_server_url).replace(
                    '/var/lib/rytivo/videos/', str(media) + '/').replace(
                    '/var/log/nginx/rytivo-video-access.log', str(root / 'access.log'))
                config = root / 'nginx.conf'
                config.write_text(f'''daemon off;
pid {root}/nginx.pid;
error_log {root}/error.log;
events {{ worker_connections 32; }}
http {{
 include /etc/nginx/mime.types;
 log_format rytivo_video '$request_method $uri $status $body_bytes_sent';
 access_log {root}/access.log rytivo_video;
 limit_conn_zone $server_name zone=video_uploads:1m;
 client_body_temp_path {root}/body;
 proxy_temp_path {root}/proxy;
 server {{ listen 127.0.0.1:{port};
 {routes}
 location / {{ proxy_pass {self.live_server_url};
 proxy_set_header Host localhost; }}
 }}
}}
''')
                nginx = subprocess.Popen(['nginx', '-p', str(root), '-c', str(config)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                try:
                    for _ in range(100):
                        if nginx.poll() is not None:
                            self.fail('isolated nginx startup failed: ' + nginx.stderr.read().decode())
                        try:
                            with socket.create_connection(('127.0.0.1', port), timeout=.1):
                                break
                        except OSError:
                            time.sleep(.05)
                    # Actual HTTP upload through the installed ingress route,
                    # distinct from the in-process upload above.
                    second = workout_post(owner)
                    upload = Request(f'http://127.0.0.1:{port}/api/v1/social/posts/{second.pk}/video/',
                        data=video_fixture('h264_faststart.mp4'), method='POST',
                        headers={'Content-Type': 'video/mp4', 'Authorization': f'Token {token.key}'})
                    with urlopen(upload, timeout=15) as response:
                        uploaded = json.loads(response.read())
                        self.assertEqual(response.status, 200)
                        self.assertEqual(uploaded['video']['processing_state'], 'processing')
                        self.assertIsNone(uploaded['video']['url'])
                    self.assertTrue(process(claim()))
                    request = RequestFactory().get('/')
                    request.user = user
                    signed = urlsplit(asset_url(video, key, request))
                    url = f'http://127.0.0.1:{port}{signed.path}?{signed.query}'

                    def fetch(method='GET', range_value=None, target=None):
                        headers = {'Range': range_value} if range_value else {}
                        try:
                            result = urlopen(Request(target or url, headers=headers, method=method), timeout=10)
                        except HTTPError as error:
                            result = error
                        with result:
                            return result.status, result.headers, result.read()

                    status, headers, body = fetch()
                    self.assertEqual((status, body), (200, expected))
                    self.assertIn('video/mp4', headers['Content-Type'])
                    self.assertIn('no-store', headers['Cache-Control'])
                    status, headers, body = fetch('HEAD')
                    self.assertEqual((status, len(body), int(headers['Content-Length'])), (200, 0, len(expected)))
                    status, headers, body = fetch(range_value='bytes=0-31')
                    self.assertEqual((status, body), (206, expected[:32]))
                    self.assertEqual(headers['Content-Range'], f'bytes 0-31/{len(expected)}')
                    self.assertEqual(fetch(range_value='bytes=-16')[::2], (206, expected[-16:]))
                    self.assertEqual(fetch(range_value=f'bytes={len(expected)+10}-')[0], 416)
                    self.assertEqual(fetch(target=url + 'tampered')[0], 403)
                    self.assertEqual(fetch(target=f'http://127.0.0.1:{port}/_private_video/{key}')[0], 404)

                    # Quiescent media snapshot; production backups also need a matching DB snapshot.
                    manifest = {str(p.relative_to(media)): hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in media.rglob('*') if p.is_file()}
                    archive = root / 'synthetic-media.zip'
                    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as backup:
                        for name in manifest:
                            backup.write(media / name, name)
                        backup.writestr('manifest.json', json.dumps(manifest, sort_keys=True))
                    restored = root / 'restored'
                    restored.mkdir()
                    with zipfile.ZipFile(archive) as backup:
                        for name in manifest:
                            destination = (restored / name).resolve()
                            self.assertTrue(destination.is_relative_to(restored.resolve()))
                            data = backup.read(name)
                            self.assertEqual(hashlib.sha256(data).hexdigest(), manifest[name])
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            destination.write_bytes(data)
                    saved = root / 'original-media'
                    media.rename(saved)
                    restored.rename(media)
                    self.assertEqual(fetch(range_value='bytes=0-31')[::2], (206, expected[:32]))
                    user.is_active = False
                    user.save(update_fields=['is_active'])
                    self.assertEqual(fetch(range_value='bytes=0-31')[0], 403)
                    self.assertEqual(fetch('HEAD')[0], 403)
                    self.assertNotIn(signed.query, (root / 'access.log').read_text())
                    artifacts = os.getenv('RYTIVO_DRILL_ARTIFACT_DIR')
                    if artifacts:
                        destination = Path(artifacts).resolve()
                        if not str(destination).startswith('/tmp/rytivo-video-check.'):
                            self.fail('artifact destination must be an isolated video-check directory')
                        destination.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(archive, destination / archive.name)
                        (destination / 'result.json').write_text(json.dumps({
                            'upload': 'passed', 'real_encoding': 'passed', 'range': 'passed',
                            'revocation': 'passed', 'restored_range': 'passed',
                            'files': len(manifest), 'archive_sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}))
                finally:
                    nginx.terminate()
                    try:
                        nginx.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        nginx.kill()
                        nginx.wait(timeout=5)
                    nginx.stderr.close()
