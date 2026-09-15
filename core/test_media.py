"""Photos are served with DEBUG off, and only to a live signature.

The two failures these cover both shipped and neither was visible from a test
that only ever read JSON: media was routed under `if settings.DEBUG`, so every
photo 404'd the moment DEBUG was off, and any URL worked forever for anyone
once it had been handed out.

The caching assertions matter as much as the security ones. A signed URL that
changes on every read would be safe and unusable -- the app keys its decoded
image cache on the URL, so a URL that churns re-downloads and re-decodes every
photo on screen on every refresh.
"""

import time

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APITestCase

from . import media
from .models import Post
from .tests import RepbaseAPITestMixin


@override_settings(
    STORAGES={'default': {'BACKEND': 'core.media_storage.ReservedNameInMemoryStorage'}}
)
class SignedMediaTests(RepbaseAPITestMixin, APITestCase):
    def setUp(self):
        self.user, self.profile, self.token = self.create_account('shutter')
        self.authenticate(self.token)
        self.post = Post.objects.create(author=self.profile, kind=Post.Kind.MEAL)
        self.post.image.save('photo.jpg', ContentFile(b'original-bytes'))
        self.name = self.post.image.name

    def url_for(self, name=None):
        return media.signed_media_path(name or self.name)

    # -- it is served at all ---------------------------------------------

    def test_a_signed_photo_is_served(self):
        response = self.client.get(self.url_for())
        self.assertEqual(response.status_code, 200)
        # A FileResponse streams, and has no `content` to read at all.
        body = b''.join(response.streaming_content) if response.streaming else response.content
        self.assertEqual(body, b'original-bytes')

    @override_settings(DEBUG=False)
    def test_it_is_still_served_with_debug_off(self):
        """The whole bug. This used to 404 and production requires DEBUG off."""
        self.assertEqual(self.client.get(self.url_for()).status_code, 200)

    @override_settings(DEBUG=False)
    def test_the_api_and_the_photo_agree_with_debug_off(self):
        """A URL the API hands out is one the server will actually serve."""
        response = self.client.get(f'/api/v1/social/posts/{self.post.id}/')
        self.assertEqual(response.status_code, 200, response.data)
        served = response.data['image_url']
        self.assertIn('?', served, 'the API handed out an unsigned URL')
        path = served.split('testserver', 1)[-1]
        self.assertEqual(self.client.get(path).status_code, 200)

    # -- and only when it should be ---------------------------------------

    def test_an_unsigned_url_is_refused(self):
        self.assertEqual(self.client.get(f'/media/{self.name}').status_code, 403)

    def test_a_tampered_signature_is_refused(self):
        url = self.url_for()
        # Change the last character to one it is not. Flipping it to a fixed
        # letter looks equivalent and is not: a signature that already ends in
        # that letter is not tampered with at all, and the test then asserts
        # that a valid URL is refused -- which it is not, about one run in
        # sixteen. It failed exactly that way once before this was noticed.
        tampered = url[:-1] + ('0' if url[-1] != '0' else '1')
        self.assertEqual(self.client.get(tampered).status_code, 403)

    def test_a_signature_for_one_file_does_not_open_another(self):
        """The part an attacker actually tries."""
        other = Post.objects.create(author=self.profile, kind=Post.Kind.MEAL)
        other.image.save('secret.jpg', ContentFile(b'someone-elses'))
        stolen = self.url_for().split('?', 1)[1]
        self.assertEqual(
            self.client.get(f'/media/{other.image.name}?{stolen}').status_code, 403
        )

    def test_an_expired_link_stops_working(self):
        long_ago = time.time() - (media.MEDIA_URL_TTL + media.MEDIA_URL_WINDOW + 10)
        self.assertEqual(self.client.get(self.url_for()).status_code, 200)
        self.assertFalse(
            media.verify(
                self.name,
                media._expiry_for(long_ago),
                media._signature(self.name, media._expiry_for(long_ago)),
            )
        )

    def test_a_missing_file_is_not_reported_before_the_signature(self):
        """Otherwise this endpoint answers which names exist."""
        self.assertEqual(self.client.get('/media/nothing/here.jpg').status_code, 403)
        signed = media.signed_media_path('nothing/here.jpg')
        self.assertEqual(self.client.get(signed).status_code, 404)

    def test_a_traversing_path_is_refused(self):
        expires = media._expiry_for(time.time())
        name = '../config/settings.py'
        signature = media._signature(name, expires)
        response = self.client.get(f'/media/{name}?e={expires}&s={signature}')
        self.assertIn(response.status_code, (403, 404))

    # -- and it stays cacheable -------------------------------------------

    def test_the_same_photo_gives_the_same_url_twice(self):
        """A URL that churns defeats every cache between here and the screen."""
        self.assertEqual(media.signed_media_path(self.name), media.signed_media_path(self.name))

    def test_the_url_is_stable_while_the_window_lasts(self):
        """Every read inside one window must agree, or nothing can cache.

        Anchored to a boundary rather than to "now", because two samples
        taken either side of one are *supposed* to differ -- that is the
        window doing its job, and asserting otherwise tests the wrong thing.
        """
        base = (int(time.time()) // media.MEDIA_URL_WINDOW) * media.MEDIA_URL_WINDOW
        urls = {
            media.signed_media_path(self.name, now=base + offset)
            for offset in (0, 1, media.MEDIA_URL_WINDOW // 2, media.MEDIA_URL_WINDOW - 1)
        }
        self.assertEqual(len(urls), 1, urls)

    def test_the_url_changes_once_a_window_and_no_more_often(self):
        """The other half: it does roll over, so a leaked link does expire."""
        base = (int(time.time()) // media.MEDIA_URL_WINDOW) * media.MEDIA_URL_WINDOW
        windows = 5
        urls = {
            media.signed_media_path(self.name, now=base + n * media.MEDIA_URL_WINDOW)
            for n in range(windows)
        }
        self.assertEqual(len(urls), windows)

    def test_the_url_is_live_for_at_least_the_ttl(self):
        now = time.time()
        expires = media._expiry_for(now)
        self.assertGreaterEqual(expires - now, media.MEDIA_URL_TTL)
        self.assertLessEqual(expires - now, media.MEDIA_URL_TTL + media.MEDIA_URL_WINDOW)

    def test_the_response_tells_the_client_to_keep_it(self):
        """Without this the app revalidates every photo on every launch."""
        cache_control = self.client.get(self.url_for())['Cache-Control']
        self.assertIn('immutable', cache_control)
        self.assertIn('private', cache_control)
        max_age = int(cache_control.split('max-age=')[1].split(',')[0])
        self.assertGreater(max_age, media.MEDIA_URL_TTL - 60)

    # -- and a proxy can take over -----------------------------------------

    @override_settings(MEDIA_ACCEL_REDIRECT_ROOT='/protected-media/')
    def test_a_proxy_is_handed_the_file_instead(self):
        """With nginx in front, no image bytes pass through Python."""
        response = self.client.get(self.url_for())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['X-Accel-Redirect'], f'/protected-media/{self.name}')
        self.assertEqual(response.content, b'')

    @override_settings(MEDIA_ACCEL_REDIRECT_ROOT='/protected-media/')
    def test_the_proxy_route_still_checks_the_signature(self):
        """Otherwise the fast path is also the open one."""
        response = self.client.get(f'/media/{self.name}')
        self.assertEqual(response.status_code, 403)
        self.assertNotIn('X-Accel-Redirect', response)
