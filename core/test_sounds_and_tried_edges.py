"""Sounds and "I tried this": what an owner may change, and what a reader may say.

test_sounds_and_takes covers publishing, using and removing sounds;
test_social_insights covers how "I tried this" is counted. These pin the
requests either of them would refuse, and the ones that must quietly succeed.
"""

from rest_framework.test import APITestCase

from .models import Block, PostTried, Sound
from .test_sounds_and_takes import PROMPTS_URL, SOUNDS, SoundTestMixin


class EditingSoundTests(SoundTestMixin, APITestCase):
    def path(self, sound):
        return f"{SOUNDS}{sound['id']}/"

    def test_an_owner_renames_a_sound_and_the_name_is_trimmed(self):
        sound = self.sound(title="First")
        response = self.client.patch(self.path(sound), {"title": "  Second  "}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["title"], "Second")

    def test_a_blank_or_overlong_title_is_refused_and_the_old_one_kept(self):
        sound = self.sound(title="Keep me")
        for title in ("", "x" * 121):
            with self.subTest(length=len(title)):
                response = self.client.patch(self.path(sound), {"title": title}, format="json")
                self.assertEqual(response.status_code, 400)
                self.assertIn("title", response.data)
        self.assertEqual(Sound.objects.get(pk=sound["id"]).title, "Keep me")

    def test_a_title_of_exactly_one_hundred_and_twenty_characters_is_kept(self):
        sound = self.sound()
        response = self.client.patch(self.path(sound), {"title": "y" * 120}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data["title"]), 120)

    def test_an_unknown_reuse_permission_is_refused(self):
        sound = self.sound()
        response = self.client.patch(self.path(sound), {"reuse_permission": "strangers"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("reuse_permission", response.data)
        self.assertEqual(Sound.objects.get(pk=sound["id"]).reuse_permission, "everyone")

    def test_an_empty_change_answers_with_the_sound_as_it_is(self):
        sound = self.sound(title="Same")
        response = self.client.patch(self.path(sound), {}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["title"], "Same")

    def test_nobody_else_can_change_or_take_down_a_sound(self):
        sound = self.sound(title="Mine")
        self.authenticate(self.jules_token)
        self.assertEqual(self.client.patch(self.path(sound), {"title": "Theirs"}, format="json").status_code, 404)
        self.assertEqual(self.client.delete(self.path(sound)).status_code, 404)
        self.assertEqual(Sound.objects.get(pk=sound["id"]).title, "Mine")
        self.assertEqual(Sound.objects.get(pk=sound["id"]).status, Sound.Status.ACTIVE)

    def test_a_sound_that_does_not_exist_is_not_found(self):
        self.authenticate(self.reader_token)
        missing = (Sound.objects.order_by("-pk").values_list("pk", flat=True).first() or 0) + 1000
        self.assertEqual(self.client.get(f"{SOUNDS}{missing}/").status_code, 404)
        self.assertEqual(self.client.get(f"{SOUNDS}{missing}/posts/").status_code, 404)

    def test_restoring_a_sound_that_was_never_removed_changes_nothing(self):
        sound = self.sound()
        response = self.client.post(f"{self.path(sound)}restore/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(Sound.objects.get(pk=sound["id"]).status, Sound.Status.ACTIVE)

    def test_signed_out_readers_get_no_sounds_and_no_prompts(self):
        sound = self.sound()
        self.client.credentials()
        self.assertEqual(self.client.get(self.path(sound)).status_code, 401)
        self.assertEqual(self.client.get(PROMPTS_URL).status_code, 401)
        self.assertEqual(self.client.post(SOUNDS, {"post": self.origin}, format="json").status_code, 401)


class TriedEdgeTests(SoundTestMixin, APITestCase):
    def tried(self, token, method="post", post=None):
        self.authenticate(token)
        return getattr(self.client, method)(f"/api/v1/social/posts/{post or self.origin}/tried/")

    def test_taking_back_what_was_never_said_is_not_an_error(self):
        response = self.tried(self.reader_token, "delete")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data["viewer_has_tried"])
        self.assertEqual(response.data["tried_count"], 0)

    def test_an_author_cannot_say_they_tried_their_own_post(self):
        response = self.tried(self.mia_token)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(PostTried.objects.exists())

    def test_saying_it_twice_counts_once(self):
        self.tried(self.reader_token)
        response = self.tried(self.reader_token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["tried_count"], 1)
        self.assertEqual(PostTried.objects.count(), 1)

    def test_a_post_hidden_by_a_block_cannot_be_tried_either_way(self):
        Block.objects.create(blocker=self.mia, blocked=self.reader)
        self.assertEqual(self.tried(self.reader_token).status_code, 404)
        Block.objects.all().delete()
        Block.objects.create(blocker=self.reader, blocked=self.mia)
        self.assertEqual(self.tried(self.reader_token).status_code, 404)
        self.assertFalse(PostTried.objects.exists())

    def test_a_post_that_does_not_exist_cannot_be_tried(self):
        self.assertEqual(self.tried(self.reader_token, post=10 ** 9).status_code, 404)
        self.assertEqual(self.tried(self.reader_token, post="abc").status_code, 404)

    def test_signed_out_readers_cannot_say_it(self):
        self.client.credentials()
        response = self.client.post(f"/api/v1/social/posts/{self.origin}/tried/")
        self.assertEqual(response.status_code, 401)
        self.assertFalse(PostTried.objects.exists())
