"""Clips on posts: what is accepted, what is stored, who sees it.

The fixtures in test_fixtures/video are real encoder output (see the README
there), so acceptance is tested against files a phone or an editor writes.
The hostile cases are made from those same files by changing the one thing
under test, so each refusal is for the reason its name gives.
"""

import io
import os
import random
import shutil
import struct
import subprocess
import tempfile
from datetime import timedelta
from unittest import mock

from django.contrib.admin import AdminSite
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase
from rest_framework.throttling import SimpleRateThrottle

from .fixtures_for_tests import TemporaryMediaMixin, video_fixture, workout_post
from .models import PendingMediaDeletion, Post, PostVideo
from .tests import RepbaseAPITestMixin
from .uploads import VideoRejected, normalise_video

LIMITS = dict(max_seconds=60, max_dimension=4096, max_pixels=4096 * 2304)


def inspect(data, **overrides):
    info, clean = normalise_video(io.BytesIO(data), len(data), **{**LIMITS, **overrides})
    try:
        return info, clean.read()
    finally:
        clean.close()


def refusal(data, **overrides):
    try:
        inspect(data, **overrides)
    except VideoRejected as error:
        return str(error.detail["video"])
    raise AssertionError("expected the clip to be refused")


def patched_box(data, kind, mutate):
    """Rewrite the payload of the first box of `kind` with `mutate(bytearray, start)`."""
    out = bytearray(data)
    at = out.index(kind) - 4
    mutate(out, at + 8)
    return bytes(out)


class AcceptedClipsTests(SimpleTestCase):
    def test_the_common_shapes_are_accepted_and_measured(self):
        for name, container, codec, audio in [
            ("h264_faststart.mp4", "mp4", "avc1", True),
            ("moov_at_end.mp4", "mp4", "avc1", False),
            ("hevc.mov", "mov", "hvc1", True),
            ("with_location.mov", "mov", "avc1", True),
        ]:
            with self.subTest(name=name):
                info, _ = inspect(video_fixture(name))
                self.assertEqual((info.container, info.codec, info.has_audio), (container, codec, audio))
                self.assertEqual((info.width, info.height), (64, 48))
                self.assertGreaterEqual(info.duration_ms, 1000)
                self.assertLess(info.duration_ms, 1500)

    def test_location_and_device_metadata_do_not_survive(self):
        original = video_fixture("with_location.mov")
        for marker in (b"40.4406", b"ISO6709", b"My house"):
            self.assertIn(marker, original)
        _, clean = inspect(original)
        for marker in (b"40.4406", b"ISO6709", b"My house", b"Lavf"):
            self.assertNotIn(marker, clean)

    def test_the_media_itself_is_copied_untouched(self):
        for name in ("h264_faststart.mp4", "moov_at_end.mp4", "hevc.mov"):
            with self.subTest(name=name):
                original = video_fixture(name)
                _, clean = inspect(original)
                mdat = original.index(b"mdat") - 4
                size = struct.unpack(">I", original[mdat:mdat + 4])[0]
                self.assertIn(original[mdat:mdat + size], clean)

    def test_the_clean_copy_passes_its_own_inspection(self):
        for name in ("h264_faststart.mp4", "moov_at_end.mp4", "hevc.mov", "with_location.mov"):
            with self.subTest(name=name):
                info, clean = inspect(video_fixture(name))
                again, _ = inspect(clean)
                self.assertEqual(info, again)

    def test_a_real_decoder_plays_the_clean_copy(self):
        """Offsets moved inside a faststart file must still point at frames."""
        if shutil.which("ffmpeg") is None:
            self.skipTest("ffmpeg is not installed here")
        for name in ("h264_faststart.mp4", "with_location.mov", "moov_at_end.mp4"):
            with self.subTest(name=name):
                _, clean = inspect(video_fixture(name))
                with tempfile.NamedTemporaryFile(suffix=os.path.splitext(name)[1]) as handle:
                    handle.write(clean)
                    handle.flush()
                    result = subprocess.run(
                        ["ffmpeg", "-v", "error", "-i", handle.name, "-f", "null", "-"],
                        capture_output=True, text=True, timeout=30,
                    )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr.strip(), "")


class RefusedClipsTests(SimpleTestCase):
    def test_things_that_are_not_mp4_or_quicktime(self):
        self.assertIn("MP4 or MOV", refusal(video_fixture("vp9.webm")))
        self.assertIn("MP4 or MOV", refusal(b""))
        self.assertIn("MP4 or MOV", refusal(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64))
        self.assertIn("MP4 or MOV", refusal(b"GIF89a" + b"\x00" * 64))

    def test_a_photo_in_a_video_container(self):
        data = patched_box(video_fixture("h264_faststart.mp4"), b"ftyp",
                           lambda out, at: out.__setitem__(slice(at, at + 4), b"heic"))
        self.assertIn("MP4 or MOV", refusal(data))

    def test_sound_with_no_picture(self):
        self.assertIn("no picture", refusal(video_fixture("audio_only.m4a")))

    def test_a_codec_outside_the_allowlist(self):
        self.assertIn("H.264 or HEVC", refusal(video_fixture("mpeg4part2.mp4")))

    def test_fragmented_files(self):
        self.assertIn("streaming format", refusal(video_fixture("fragmented.mp4")))

    def test_too_long(self):
        self.assertIn("at most 2 seconds", refusal(video_fixture("three_seconds.mp4"), max_seconds=2))
        info, _ = inspect(video_fixture("three_seconds.mp4"), max_seconds=5)
        self.assertEqual(info.duration_ms, 3000)

    def test_too_large_a_picture(self):
        self.assertIn("32 pixels", refusal(video_fixture("h264_faststart.mp4"), max_dimension=32))

    def test_a_duration_forged_in_the_movie_header_is_not_believed(self):
        """Shrinking mvhd's duration does not shrink the sample table."""
        def stretch_samples(out, at):
            count = struct.unpack(">I", out[at + 4:at + 8])[0]
            for index in range(count):
                entry = at + 8 + index * 8
                samples, delta = struct.unpack(">II", out[entry:entry + 8])
                out[entry + 4:entry + 8] = struct.pack(">I", delta * 600)

        data = patched_box(video_fixture("h264_faststart.mp4"), b"stts", stretch_samples)
        self.assertIn("at most 60 seconds", refusal(data))

    def test_an_edit_list_that_loops_the_clip_is_counted(self):
        def long_edit(out, at):
            out[at + 8:at + 12] = struct.pack(">I", 8000 * 3600)

        data = patched_box(video_fixture("h264_faststart.mp4"), b"elst", long_edit)
        self.assertIn("at most 60 seconds", refusal(data))

    def test_a_track_header_claiming_a_huge_picture(self):
        def huge(out, at):
            size = struct.unpack(">I", out[at - 8:at - 4])[0]
            end = at - 8 + size
            out[end - 8:end] = struct.pack(">II", 9000 << 16, 9000 << 16)

        data = patched_box(video_fixture("h264_faststart.mp4"), b"tkhd", huge)
        self.assertIn("pixels on a side", refusal(data))

    def test_truncated_files(self):
        data = video_fixture("h264_faststart.mp4")
        for cut in (8, 40, len(data) // 2, len(data) - 1):
            with self.subTest(cut=cut):
                refusal(data[:cut])

    def test_structures_built_to_exhaust_the_parser(self):
        ftyp = video_fixture("h264_faststart.mp4")[:32]
        many = b"".join(struct.pack(">I4s", 8, b"free") for _ in range(50_000))
        bomb = ftyp + struct.pack(">I4s", 8 + len(many), b"moov") + many + struct.pack(">I4s", 8, b"mdat")
        refusal(bomb)
        nested = struct.pack(">I4s", 8, b"stts")
        for _ in range(200):
            nested = struct.pack(">I4s", 8 + len(nested), b"edts") + nested
        deep = ftyp + struct.pack(">I4s", 8 + len(nested), b"moov") + nested + struct.pack(">I4s", 8, b"mdat")
        refusal(deep)
        huge_claim = ftyp + struct.pack(">I4sQ", 1, b"moov", 2 ** 40) + b"\x00" * 64
        refusal(huge_claim)
        refusal(ftyp + struct.pack(">I4s", 4, b"moov") + b"\x00" * 32)

    def test_random_damage_is_always_a_clean_refusal_or_a_clean_accept(self):
        """Never a 500: whatever the bytes, the answer is a VideoRejected."""
        base = video_fixture("h264_faststart.mp4")
        generator = random.Random(20261010)
        for attempt in range(400):
            damaged = bytearray(base)
            if attempt % 2:
                damaged = damaged[:generator.randrange(1, len(damaged))]
            else:
                for _ in range(generator.randint(1, 8)):
                    damaged[generator.randrange(len(damaged))] = generator.randrange(256)
            try:
                inspect(bytes(damaged))
            except VideoRejected:
                pass


@override_settings(VIDEO_REVIEW_REQUIRED=False)
class VideoUploadTests(TemporaryMediaMixin, RepbaseAPITestMixin, APITestCase):
    def setUp(self):
        _, self.author, self.author_token = self.create_account("filmmaker")
        _, self.viewer, self.viewer_token = self.create_account("watcher")
        self.post = workout_post(self.author)
        self.authenticate(self.author_token)

    def url(self, post=None):
        return f"/api/v1/social/posts/{(post or self.post).pk}/video/"

    def upload(self, data, content_type="video/mp4", post=None):
        return self.client.generic("POST", self.url(post), data, content_type=content_type)

    def test_the_author_attaches_a_clip_and_gets_the_card_back(self):
        response = self.upload(video_fixture("h264_faststart.mp4"))
        self.assertEqual(response.status_code, 200, response.data)
        clip = response.data["video"]
        self.assertEqual(clip["status"], "approved")
        self.assertEqual((clip["width"], clip["height"]), (64, 48))
        self.assertTrue(clip["url"].startswith("http"))
        self.assertTrue(PostVideo.objects.get(post=self.post).file.name.startswith("post-videos/"))

    def test_the_declared_type_is_only_advisory(self):
        response = self.upload(video_fixture("hevc.mov"), content_type="video/mp4")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["video"]["content_type"], "video/quicktime")
        self.assertTrue(PostVideo.objects.get(post=self.post).file.name.endswith(".mov"))

    def test_the_stored_file_has_no_location(self):
        self.upload(video_fixture("with_location.mov"))
        with default_storage.open(PostVideo.objects.get(post=self.post).file.name, "rb") as handle:
            self.assertNotIn(b"40.4406", handle.read())

    def test_the_wrong_kind_of_request_is_refused(self):
        self.assertEqual(self.upload(b"{}", content_type="application/json").status_code, 415)
        self.assertEqual(self.upload(b"hello", content_type="text/plain").status_code, 415)
        self.assertEqual(self.upload(video_fixture("vp9.webm"), content_type="video/webm").status_code, 415)
        empty = self.client.generic("POST", self.url(), b"", content_type="video/mp4")
        self.assertEqual(empty.status_code, 400)
        self.assertFalse(PostVideo.objects.exists())

    def test_content_that_is_not_an_acceptable_clip_is_refused_with_a_reason(self):
        for name, words in [("vp9.webm", "MP4 or MOV"), ("audio_only.m4a", "no picture"),
                            ("mpeg4part2.mp4", "H.264")]:
            with self.subTest(name=name):
                response = self.upload(video_fixture(name))
                self.assertEqual(response.status_code, 400)
                self.assertIn(words, str(response.data["video"]))
        self.assertFalse(PostVideo.objects.exists())

    def test_too_large_is_refused_on_the_header_and_on_the_bytes(self):
        data = video_fixture("h264_faststart.mp4")
        with override_settings(VIDEO_MAX_BYTES=1024):
            response = self.upload(data)
            self.assertEqual(response.status_code, 413)
            # A client that understates its length is caught while reading.
            lying = self.client.generic(
                "POST", self.url(), data, content_type="video/mp4", CONTENT_LENGTH="10"
            )
            self.assertIn(lying.status_code, (400, 413))
        self.assertFalse(PostVideo.objects.exists())

    def test_too_long_is_refused(self):
        with override_settings(VIDEO_MAX_SECONDS=2):
            response = self.upload(video_fixture("three_seconds.mp4"))
        self.assertEqual(response.status_code, 400)
        self.assertIn("2 seconds", str(response.data["video"]))

    def test_only_the_author_can_attach_or_remove(self):
        self.authenticate(self.viewer_token)
        self.assertEqual(self.upload(video_fixture("h264_faststart.mp4")).status_code, 404)
        self.assertEqual(self.client.delete(self.url()).status_code, 404)
        self.client.credentials()
        self.assertEqual(self.upload(video_fixture("h264_faststart.mp4")).status_code, 401)

    def test_a_repost_cannot_carry_a_clip(self):
        repost = Post.objects.create(author=self.author, kind=Post.Kind.REPOST, repost_of=workout_post(self.viewer))
        response = self.upload(video_fixture("h264_faststart.mp4"), post=repost)
        self.assertEqual(response.status_code, 400)

    def test_replacing_a_clip_deletes_the_old_file(self):
        self.upload(video_fixture("h264_faststart.mp4"))
        first = PostVideo.objects.get(post=self.post).file.name
        with self.captureOnCommitCallbacks(execute=True):
            self.upload(video_fixture("hevc.mov"))
        second = PostVideo.objects.get(post=self.post).file.name
        self.assertNotEqual(first, second)
        self.assertFalse(default_storage.exists(first))
        self.assertTrue(default_storage.exists(second))
        self.assertEqual(PostVideo.objects.count(), 1)

    def test_removing_the_clip_or_the_post_deletes_the_file(self):
        self.upload(video_fixture("h264_faststart.mp4"))
        name = PostVideo.objects.get(post=self.post).file.name
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.delete(self.url())
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data["video"])
        self.assertFalse(default_storage.exists(name))

        self.upload(video_fixture("h264_faststart.mp4"))
        name = PostVideo.objects.get(post=self.post).file.name
        with self.captureOnCommitCallbacks(execute=True):
            self.client.delete(f"/api/v1/social/posts/{self.post.pk}/")
        self.assertFalse(default_storage.exists(name))

    def test_a_storage_failure_leaves_nothing_behind(self):
        with mock.patch.object(default_storage, "save", side_effect=OSError("disk gone")):
            response = self.upload(video_fixture("h264_faststart.mp4"))
        self.assertEqual(response.status_code, 503)
        self.assertFalse(PostVideo.objects.exists())
        self.assertFalse(PendingMediaDeletion.objects.exists())

    def test_uploads_are_rate_limited(self):
        with mock.patch.dict(SimpleRateThrottle.THROTTLE_RATES, {"upload": "2/hour"}):
            codes = [self.upload(video_fixture("h264_faststart.mp4")).status_code for _ in range(3)]
        self.assertEqual(codes, [200, 200, 429])

    def serve(self, url, **headers):
        return self.client.get(url.replace("http://testserver", ""), **headers)

    def test_players_can_ask_for_byte_ranges(self):
        url = self.upload(video_fixture("h264_faststart.mp4")).data["video"]["url"]
        size = PostVideo.objects.get(post=self.post).size_bytes

        first = self.serve(url, HTTP_RANGE="bytes=0-1")
        self.assertEqual(first.status_code, 206)
        self.assertEqual(first["Content-Range"], f"bytes 0-1/{size}")
        self.assertEqual(b"".join(first.streaming_content), b"\x00\x00")

        tail = self.serve(url, HTTP_RANGE="bytes=-8")
        self.assertEqual(tail.status_code, 206)
        self.assertEqual(len(b"".join(tail.streaming_content)), 8)

        past = self.serve(url, HTTP_RANGE=f"bytes={size + 10}-")
        self.assertEqual(past.status_code, 416)
        self.assertEqual(past["Content-Range"], f"bytes */{size}")

        whole = self.serve(url)
        self.assertEqual(whole.status_code, 200)
        self.assertEqual(whole["Accept-Ranges"], "bytes")
        self.assertEqual(len(b"".join(whole.streaming_content)), size)

    def test_a_hidden_post_takes_its_clip_down(self):
        url = self.upload(video_fixture("h264_faststart.mp4")).data["video"]["url"]
        Post.objects.filter(pk=self.post.pk).update(is_hidden=True)
        self.assertEqual(self.serve(url).status_code, 403)

    def test_an_unsigned_or_tampered_link_is_refused(self):
        url = self.upload(video_fixture("h264_faststart.mp4")).data["video"]["url"]
        self.assertEqual(self.serve(url.split("?")[0]).status_code, 403)
        self.assertEqual(self.serve(url[:-3] + "000").status_code, 403)


@override_settings(VIDEO_REVIEW_REQUIRED=True)
class VideoReviewTests(TemporaryMediaMixin, RepbaseAPITestMixin, APITestCase):
    """Where moderation is on, a person sees a clip before anyone else does."""

    def setUp(self):
        _, self.author, self.author_token = self.create_account("filmmaker")
        _, self.viewer, self.viewer_token = self.create_account("watcher")
        self.post = workout_post(self.author)
        self.authenticate(self.author_token)
        self.client.generic(
            "POST", f"/api/v1/social/posts/{self.post.pk}/video/",
            video_fixture("h264_faststart.mp4"), content_type="video/mp4",
        )
        self.clip = PostVideo.objects.get(post=self.post)

    def card_for(self, token):
        self.authenticate(token)
        return self.client.get(f"/api/v1/social/posts/{self.post.pk}/").data

    def test_a_new_clip_waits_and_only_its_author_sees_it(self):
        self.assertEqual(self.clip.status, PostVideo.Status.PENDING)
        self.assertEqual(self.card_for(self.author_token)["video"]["status"], "pending")
        self.assertIsNone(self.card_for(self.viewer_token)["video"])

    def admin(self, user):
        from .admin import PostVideoAdmin

        request = RequestFactory().post("/admin/")
        request.user = user
        request._messages = mock.MagicMock()
        return PostVideoAdmin(PostVideo, AdminSite()), request

    def test_a_moderator_approval_shows_it_to_everyone(self):
        moderator = self.create_account("moderator_one")[0]
        moderator.is_staff = moderator.is_superuser = True
        moderator.save()
        admin, request = self.admin(moderator)
        admin.approve_clips(request, PostVideo.objects.filter(pk=self.clip.pk))
        card = self.card_for(self.viewer_token)
        self.assertEqual(card["video"]["status"], "approved")
        self.clip.refresh_from_db()
        self.assertEqual(self.clip.reviewed_by, moderator)

    def test_a_rejected_clip_stays_with_its_author_and_is_not_served(self):
        moderator = self.create_account("moderator_two")[0]
        moderator.is_staff = moderator.is_superuser = True
        moderator.save()
        url = self.card_for(self.author_token)["video"]["url"]
        admin, request = self.admin(moderator)
        admin.reject_clips(request, PostVideo.objects.filter(pk=self.clip.pk))
        self.assertIsNone(self.card_for(self.viewer_token)["video"])
        self.assertEqual(self.card_for(self.author_token)["video"]["status"], "rejected")
        self.assertEqual(self.client.get(url.replace("http://testserver", "")).status_code, 403)

    def test_a_read_only_moderator_is_not_offered_the_decision(self):
        from .admin import PostAdmin, PostVideoAdmin

        request = RequestFactory().get("/admin/")
        request.user = mock.MagicMock()
        request.user.has_perm.return_value = False
        request.user.has_perms.return_value = False
        actions = PostVideoAdmin(PostVideo, AdminSite()).get_actions(request)
        self.assertNotIn("approve_clips", actions)
        self.assertNotIn("reject_clips", actions)
        # The same gap on posts, closed alongside: hiding needs change permission.
        post_actions = PostAdmin(Post, AdminSite()).get_actions(request)
        self.assertNotIn("hide_posts", post_actions)
        self.assertNotIn("unhide_posts", post_actions)

    def test_waiting_clips_raise_the_hourly_alert_once_overdue(self):
        output = io.StringIO()
        call_command("check_moderation_queue", stdout=output)
        self.assertIn("clips awaiting review: 1", output.getvalue())
        PostVideo.objects.filter(pk=self.clip.pk).update(created_at=timezone.now() - timedelta(hours=30))
        with self.assertRaises(CommandError):
            call_command("check_moderation_queue", stdout=io.StringIO())

    def test_a_waiting_clip_keeps_its_post_out_of_recommendations(self):
        from .recommendations import eligible_posts

        self.assertFalse(eligible_posts(self.viewer).filter(pk=self.post.pk).exists())
        PostVideo.objects.filter(pk=self.clip.pk).update(status=PostVideo.Status.APPROVED)
        self.assertTrue(eligible_posts(self.viewer).filter(pk=self.post.pk).exists())
