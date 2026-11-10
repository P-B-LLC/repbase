"""Milestone 6: original sounds and takes, with clear ownership.

The exit criterion is consistency: source deletion, blocked users, revoked
reuse, sound removal, private accounts and saved copies each behave the same
way wherever a post is read -- the feed, the post itself, and the lists of
responses. `ConsistencyTests` at the end is that sentence as a table.
"""

import shutil
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.core.management import call_command
from rest_framework.test import APITestCase

from .fixtures_for_tests import as_base64, photo_bytes
from .models import (
    Block, Follow, PendingMediaDeletion, Post, PostVideo, RepbaseUser, SavedFoodMeal, Sound,
)
from .publication import PROMPTS
from .sounds import MAX_PREPARE_ATTEMPTS, prepare_pending, set_removed
from .test_publication_contract import (
    FEED, POSTS, UNAVAILABLE, PublicationTestMixin, as_sent,
)

SOUNDS = "/api/v1/social/sounds/"
PROMPTS_URL = "/api/v1/social/prompts/"
HAS_FFMPEG = shutil.which("ffmpeg") is not None


class SoundTestMixin(PublicationTestMixin):
    def setUp(self):
        super().setUp()
        self.origin = self.published(self.mia_token, kind="media", expects_video=True, caption="Rack taken")["id"]
        self.attach_clip(self.mia_token, self.origin)

    def make_sound(self, token=None, post=None, **body):
        self.authenticate(token or self.mia_token)
        return self.client.post(SOUNDS, {"post": post or self.origin, **body}, format="json")

    def sound(self, **body):
        response = self.make_sound(**body)
        self.assertEqual(response.status_code, 201, response.data)
        return as_sent(response)

    def use(self, token, sound_id, **body):
        return self.publish(
            token, kind="media", content_type="image/jpeg",
            image_base64=as_base64(photo_bytes()), sound_id=sound_id, **body,
        )

    def status_of(self, token, path):
        self.authenticate(token)
        return self.client.get(path).status_code


class PublishingSoundTests(SoundTestMixin, APITestCase):
    def test_an_author_publishes_their_own_clips_audio_as_a_sound(self):
        sound = self.sound(title="  Plate math  ")
        self.assertEqual(sound["title"], "Plate math")
        self.assertEqual(sound["owner"]["username"], "mia")
        self.assertEqual(sound["origin_post_id"], self.origin)
        self.assertEqual(sound["reuse_permission"], "everyone")
        self.assertTrue(sound["viewer_is_owner"])
        self.assertIsNone(sound["audio_url"], "prepared by a worker, not in the request")
        card = self.card(self.reader_token, self.origin)
        self.assertEqual(card["sound"]["id"], sound["id"])
        self.assertTrue(card["sound"]["is_original"])

    def test_a_sound_is_named_after_its_owner_when_no_name_is_given(self):
        self.assertEqual(self.sound()["title"], "Original sound - @mia")

    def test_asking_twice_makes_one_sound(self):
        first = self.sound()
        again = self.make_sound(title="Another name")
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.data["id"], first["id"])
        self.assertEqual(Sound.objects.count(), 1)

    def test_what_cannot_become_a_sound(self):
        # Somebody else's post, in the words used for one that is not there.
        theirs = self.make_sound(token=self.jules_token)
        self.assertEqual(theirs.status_code, 400)
        missing = self.make_sound(post=999_999)
        self.assertEqual(str(theirs.data["post"]), str(missing.data["post"]))
        # No clip.
        photo = self.moment(self.mia_token)["id"]
        self.assertIn("no approved video", str(self.make_sound(post=photo).data["post"]))
        # A clip still waiting for review.
        PostVideo.objects.filter(post_id=self.origin).update(status=PostVideo.Status.PENDING)
        self.assertEqual(self.make_sound().status_code, 400)
        # A clip with no audio in it.
        PostVideo.objects.filter(post_id=self.origin).update(status=PostVideo.Status.APPROVED, has_audio=False)
        self.assertIn("no audio", str(self.make_sound().data["post"]))
        self.assertFalse(Sound.objects.exists())

    def test_a_clip_set_to_somebody_elses_sound_is_not_the_authors_to_publish(self):
        sound = self.sound()
        theirs = as_sent(self.use(self.jules_token, sound["id"], expects_video=True))["id"]
        self.attach_clip(self.jules_token, theirs)
        response = self.make_sound(token=self.jules_token, post=theirs)
        self.assertEqual(response.status_code, 400)
        self.assertIn("somebody else's sound", str(response.data["post"]))

    def test_the_owner_renames_it_and_changes_who_may_use_it(self):
        sound = self.sound()
        self.authenticate(self.mia_token)
        changed = self.client.patch(
            f"{SOUNDS}{sound['id']}/", {"title": "Deload week", "reuse_permission": "followers"}, format="json"
        )
        self.assertEqual(changed.status_code, 200, changed.data)
        self.assertEqual((changed.data["title"], changed.data["reuse_permission"]), ("Deload week", "followers"))
        self.authenticate(self.jules_token)
        for method in ("patch", "delete"):
            self.assertEqual(getattr(self.client, method)(f"{SOUNDS}{sound['id']}/", {}, format="json").status_code, 404)
        self.assertEqual(self.client.post(f"{SOUNDS}{sound['id']}/restore/").status_code, 404)
        self.assertFalse(as_sent(self.client.get(f"{SOUNDS}{sound['id']}/"))["viewer_can_use"])


class UsingSoundTests(SoundTestMixin, APITestCase):
    def test_posts_reference_one_sound_and_are_listed_under_it(self):
        sound = self.sound()
        first = as_sent(self.use(self.jules_token, sound["id"]))["id"]
        private = as_sent(self.use(self.reader_token, sound["id"], visibility="private"))["id"]
        self.assertEqual(Sound.objects.count(), 1)
        listed = self.page(self.jules_token, f"{SOUNDS}{sound['id']}/posts/")
        self.assertEqual(set(listed), {self.origin, first})
        self.assert_matches_both_contracts(
            self.read(self.jules_token, f"{SOUNDS}{sound['id']}/posts/?attachments=summary"), "PaginatedPostList"
        )
        # Each is listed to whoever may see it, and counted only if anybody may.
        self.assertIn(private, self.page(self.reader_token, f"{SOUNDS}{sound['id']}/posts/"))
        self.assertEqual(self.read(self.jules_token, f"{SOUNDS}{sound['id']}/")["use_count"], 2)

    def test_closing_a_sound_stops_new_uses_and_keeps_the_old(self):
        sound = self.sound()
        kept = as_sent(self.use(self.jules_token, sound["id"]))["id"]
        self.authenticate(self.mia_token)
        self.client.patch(f"{SOUNDS}{sound['id']}/", {"reuse_permission": "nobody"}, format="json")
        self.assertEqual(self.use(self.reader_token, sound["id"]).status_code, 400)
        self.assertEqual(self.card(self.reader_token, kept)["sound"]["id"], sound["id"])


class RemovingSoundTests(SoundTestMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.sound_id = self.sound()["id"]
        self.used = as_sent(self.use(self.jules_token, self.sound_id))["id"]

    def remove(self):
        self.authenticate(self.mia_token)
        self.assertEqual(self.client.delete(f"{SOUNDS}{self.sound_id}/").status_code, 204)

    def test_removal_is_one_change_felt_everywhere(self):
        Follow.objects.create(follower=self.reader, following=self.jules)
        self.remove()
        # The credit, on the post itself, in the feed and in the post list.
        self.assertIsNone(self.card(self.reader_token, self.used)["sound"])
        self.assertIsNone(self.page(self.reader_token, FEED)[self.used]["sound"])
        self.assertIsNone(self.page(self.reader_token, POSTS)[self.used]["sound"])
        # The sound, and what was set to it.
        self.assertEqual(self.status_of(self.reader_token, f"{SOUNDS}{self.sound_id}/"), 404)
        self.assertEqual(self.status_of(self.reader_token, f"{SOUNDS}{self.sound_id}/posts/"), 404)
        self.assertEqual(self.status_of(self.mia_token, f"{SOUNDS}{self.sound_id}/posts/"), 404)
        # New uses.
        self.assertEqual(self.use(self.reader_token, self.sound_id).status_code, 400)
        # The post is still its author's, and still there.
        self.assertEqual(self.card(self.reader_token, self.used)["id"], self.used)
        self.assertEqual(Post.objects.get(pk=self.used).sound_id, self.sound_id)

    def test_the_owner_can_put_back_what_they_took_down(self):
        self.remove()
        self.assertEqual(self.status_of(self.mia_token, f"{SOUNDS}{self.sound_id}/"), 200)
        self.authenticate(self.mia_token)
        self.assertEqual(self.client.post(f"{SOUNDS}{self.sound_id}/restore/").status_code, 200)
        self.assertEqual(self.card(self.reader_token, self.used)["sound"]["id"], self.sound_id)
        self.assertIn(self.used, self.page(self.reader_token, f"{SOUNDS}{self.sound_id}/posts/"))

    def test_only_a_moderator_puts_back_what_a_moderator_took_down(self):
        sound = Sound.objects.get(pk=self.sound_id)
        self.assertTrue(set_removed(sound, True, by_moderator=True))
        self.authenticate(self.mia_token)
        refused = self.client.post(f"{SOUNDS}{self.sound_id}/restore/")
        self.assertEqual(refused.status_code, 400)
        self.assertIn("moderator", str(refused.data["sound"]))
        self.assertIsNone(self.card(self.reader_token, self.used)["sound"])
        # A moderator's removal also outranks the owner's own.
        self.assertTrue(set_removed(sound, False, by_moderator=True))
        self.remove()
        self.assertTrue(set_removed(sound, True, by_moderator=True))
        self.assertEqual(self.client.post(f"{SOUNDS}{self.sound_id}/restore/").status_code, 400)

    def test_removing_twice_and_restoring_twice_change_nothing(self):
        sound = Sound.objects.get(pk=self.sound_id)
        self.assertTrue(set_removed(sound, True, by_moderator=False))
        self.assertFalse(set_removed(sound, True, by_moderator=False))
        self.assertTrue(set_removed(sound, False, by_moderator=False))
        self.assertFalse(set_removed(sound, False, by_moderator=False))

    def test_the_admin_actions_remove_and_restore(self):
        from django.contrib import admin
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.test import RequestFactory

        moderator = get_user_model().objects.create_superuser("mod", "mod@example.com", "StrongPass!234")
        request = RequestFactory().post("/")
        request.user, request.session = moderator, {}
        request._messages = FallbackStorage(request)
        sound_admin = admin.site._registry[Sound]
        sound_admin.remove_sounds(request, Sound.objects.all())
        self.assertTrue(Sound.objects.get().removed_by_moderator)
        sound_admin.restore_sounds(request, Sound.objects.all())
        self.assertEqual(Sound.objects.get().status, Sound.Status.ACTIVE)
        self.assertFalse(sound_admin.has_add_permission(request))


@skipUnless(HAS_FFMPEG, "needs ffmpeg")
class PreparingSoundTests(SoundTestMixin, APITestCase):
    def test_the_audio_is_extracted_once_outside_the_request_and_served_signed(self):
        sound_id = self.sound()["id"]
        self.assertEqual(prepare_pending(), (1, 0))
        row = Sound.objects.get(pk=sound_id)
        self.assertTrue(row.audio_key.startswith("sounds/") and row.audio_key.endswith(".m4a"))
        self.assertEqual(prepare_pending(), (0, 0), "nothing left to prepare")
        self.assertFalse(PendingMediaDeletion.objects.exists(), "the staged file was kept, not queued for deletion")

        url = self.read(self.jules_token, f"{SOUNDS}{sound_id}/")["audio_url"]
        self.assertIn(row.audio_key, url)
        self.client.credentials()
        served = self.client.get(url)
        self.assertEqual(served.status_code, 200)
        body = b"".join(served.streaming_content)
        self.assertIn(b"ftyp", body[:32])
        # Audio only: the picture did not come with it.
        self.assertLess(len(body), row.origin_post.video.size_bytes)

        # One file, however many posts are set to it.
        self.use(self.jules_token, sound_id)
        self.use(self.reader_token, sound_id)
        self.assertEqual(Sound.objects.count(), 1)

        # Taken down, the link in somebody's hands stops working.
        set_removed(row, True, by_moderator=False)
        self.assertEqual(self.client.get(url).status_code, 403)
        set_removed(row, False, by_moderator=False)
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_a_clip_that_cannot_be_read_is_given_up_on(self):
        sound_id = self.sound()["id"]
        video = PostVideo.objects.get(post_id=self.origin)
        with open(video.file.path, "wb") as handle:
            handle.write(b"not a video")
        for _ in range(MAX_PREPARE_ATTEMPTS):
            self.assertEqual(prepare_pending(), (0, 1))
        self.assertEqual(prepare_pending(), (0, 0), "not tried for ever")
        self.assertEqual(Sound.objects.get(pk=sound_id).audio_key, "")
        self.assertFalse(PendingMediaDeletion.objects.exists())

    def test_a_removed_sound_is_not_prepared_and_the_command_runs(self):
        sound_id = self.sound()["id"]
        set_removed(Sound.objects.get(pk=sound_id), True, by_moderator=False)
        call_command("prepare_sounds", verbosity=0)
        self.assertEqual(Sound.objects.get(pk=sound_id).audio_key, "")
        set_removed(Sound.objects.get(pk=sound_id), False, by_moderator=False)
        call_command("prepare_sounds", "--limit", "1", verbosity=0)
        self.assertTrue(Sound.objects.get(pk=sound_id).audio_key)

    def test_deleting_the_owner_deletes_the_audio_file(self):
        sound_id = self.sound()["id"]
        prepare_pending()
        key = Sound.objects.get(pk=sound_id).audio_key
        from django.core.files.storage import default_storage

        self.assertTrue(default_storage.exists(key))
        with self.captureOnCommitCallbacks(execute=True):
            self.mia.user.delete()
        self.assertFalse(default_storage.exists(key))


class TakeCountTests(PublicationTestMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.recipe_id = self.recipe(self.mia_token)["id"]

    def count(self, token=None):
        return self.card(token or self.reader_token, self.recipe_id)["take_count"]

    def test_only_responses_anybody_could_see_are_counted(self):
        self.assertEqual(self.count(), 0)
        public = self.recipe_take(self.jules_token, self.recipe_id)["id"]
        self.recipe_take(self.reader_token, self.recipe_id, visibility="private")
        self.recipe_take(self.reader_token, self.recipe_id, visibility="followers")
        self.assertEqual(self.count(), 1)
        cases = (
            ("hidden", lambda: Post.objects.filter(pk=public).update(is_hidden=True)),
            ("pending", lambda: Post.objects.filter(pk=public).update(moderation_status="pending")),
            ("suspended", lambda: get_user_model().objects.filter(pk=self.jules.user_id).update(is_active=False)),
            ("closed profile", lambda: RepbaseUser.objects.filter(pk=self.jules.pk).update(is_profile_public=False)),
        )
        for label, change in cases:
            with self.subTest(take=label):
                Post.objects.filter(pk=public).update(is_hidden=False, moderation_status="approved")
                get_user_model().objects.update(is_active=True)
                RepbaseUser.objects.update(is_profile_public=True)
                self.assertEqual(self.count(), 1)
                change()
                self.assertEqual(self.count(self.mia_token), 0)

    def test_the_count_is_the_same_for_every_reader(self):
        """A block changes what a reader is shown, never the public total --
        or the total would tell them somebody had blocked them."""
        take = self.recipe_take(self.jules_token, self.recipe_id)["id"]
        Block.objects.create(blocker=self.jules, blocked=self.reader)
        self.assertEqual(self.count(self.reader_token), self.count(self.mia_token))
        self.assertNotIn(take, self.page(self.reader_token, f"{POSTS}{self.recipe_id}/takes/"))

    def test_the_count_is_on_every_card_and_costs_no_query_per_card(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        Follow.objects.create(follower=self.reader, following=self.mia)
        self.recipe_take(self.jules_token, self.recipe_id)
        self.assertEqual(self.page(self.reader_token, FEED)[self.recipe_id]["take_count"], 1)
        self.authenticate(self.reader_token)
        with CaptureQueriesContext(connection) as one:
            self.client.get(FEED)
        for _ in range(4):
            self.recipe(self.mia_token)
        with CaptureQueriesContext(connection) as five:
            self.client.get(FEED)
        self.assertEqual(len(five), len(one))


class PromptTests(PublicationTestMixin, APITestCase):
    def test_prompts_are_a_fixed_list_to_post_under_and_browse_by(self):
        offered = self.read(self.mia_token, PROMPTS_URL)
        self.assertEqual([row["code"] for row in offered], [code for code, _ in PROMPTS])
        lore = self.moment(self.mia_token, prompt="gym_lore")
        plain = self.moment(self.mia_token)
        self.assertEqual((lore["prompt"], plain["prompt"]), ("gym_lore", None))
        self.assert_matches_both_contracts(lore, "Post")
        self.assertEqual(set(self.page(self.reader_token, f"{POSTS}?prompt=gym_lore")), {lore["id"]})
        self.assertEqual(self.page(self.reader_token, f"{POSTS}?prompt=rack_taken"), {})

    def test_a_prompt_is_never_free_text(self):
        refused = self.publish(
            self.mia_token, kind="media", content_type="image/jpeg",
            image_base64=as_base64(photo_bytes()), prompt="anything I like",
        )
        self.assertEqual(refused.status_code, 400)
        self.assertIn("prompt", refused.data)
        self.authenticate(self.reader_token)
        self.assertEqual(self.client.get(f"{POSTS}?prompt=nope").status_code, 400)

    def test_browsing_a_prompt_shows_only_what_the_reader_may_see(self):
        hidden = self.moment(self.mia_token, prompt="gym_lore", visibility="private")["id"]
        self.assertNotIn(hidden, self.page(self.reader_token, f"{POSTS}?prompt=gym_lore"))
        self.authenticate(self.mia_token)
        cleared = self.client.patch(f"{POSTS}{hidden}/", {"prompt": ""}, format="json")
        self.assertIsNone(cleared.data["prompt"])


class ConsistencyTests(SoundTestMixin, APITestCase):
    """The exit criterion, as a table: each condition, read from every place
    a post is read, gives the same answer."""

    def setUp(self):
        super().setUp()
        Follow.objects.create(follower=self.reader, following=self.jules)
        self.sound_id = self.sound()["id"]
        self.recipe_id = self.recipe(self.mia_token)["id"]
        take = self.recipe(
            self.jules_token, foods=(("Tofu", "180.00"),), source_post_id=self.recipe_id,
            sound_id=self.sound_id, cooking_instructions="Press the tofu.",
        )
        self.take_id = take["id"]

    def surfaces(self, token=None):
        """The take, from the feed, its own URL, the post list, the takes of
        its source and the posts set to its sound. A surface that does not
        answer is left out; one that answers without the take holds None."""
        token = token or self.reader_token
        found = {"detail": None, "feed": None, "list": None}
        self.authenticate(token)
        detail = self.client.get(f"{POSTS}{self.take_id}/")
        if detail.status_code == 200:
            found["detail"] = as_sent(detail)
        found["feed"] = self.page(token, FEED).get(self.take_id)
        found["list"] = self.page(token, POSTS).get(self.take_id)
        for name, path in (("takes", f"{POSTS}{self.recipe_id}/takes/"),
                           ("sound", f"{SOUNDS}{self.sound_id}/posts/")):
            response = self.client.get(path)
            if response.status_code == 200:
                found[name] = {c["id"]: c for c in as_sent(response)["results"]}.get(self.take_id)
        return found

    def assert_everywhere(self, check, token=None, expect=None):
        found = self.surfaces(token)
        if expect is not None:
            self.assertEqual(set(found), expect)
        for where, card in found.items():
            with self.subTest(where=where):
                self.assertIsNotNone(card, "the take itself is still there")
                check(card)

    def test_untouched_everything_agrees(self):
        def check(card):
            self.assertEqual(card["source"]["post_id"], self.recipe_id)
            self.assertEqual(card["sound"]["id"], self.sound_id)
        self.assert_everywhere(check, expect={"detail", "feed", "list", "takes", "sound"})

    def test_source_deletion(self):
        self.authenticate(self.mia_token)
        self.client.delete(f"{POSTS}{self.recipe_id}/")
        self.assert_everywhere(
            lambda card: self.assertEqual(card["source"], UNAVAILABLE),
            expect={"detail", "feed", "list", "sound"},
        )

    def test_a_block_between_the_reader_and_the_source_author(self):
        Block.objects.create(blocker=self.mia, blocked=self.reader)
        def check(card):
            self.assertEqual(card["source"], UNAVAILABLE)
            self.assertIsNone(card["sound"], "her sound's credit goes with her")
        # Her post and her sound are both 404 to them, so neither list answers.
        self.assert_everywhere(check, expect={"detail", "feed", "list"})

    def test_a_block_between_the_reader_and_the_takes_author(self):
        Block.objects.create(blocker=self.reader, blocked=self.jules)
        found = self.surfaces()
        self.assertEqual(found, {"detail": None, "feed": None, "list": None, "takes": None, "sound": None})

    def test_revoked_reuse(self):
        self.authenticate(self.mia_token)
        self.client.patch(f"{POSTS}{self.recipe_id}/", {"reuse_permission": "nobody"}, format="json")
        self.client.patch(f"{SOUNDS}{self.sound_id}/", {"reuse_permission": "nobody"}, format="json")
        def check(card):
            self.assertEqual(card["source"]["post_id"], self.recipe_id)
            self.assertEqual(card["sound"]["id"], self.sound_id)
        self.assert_everywhere(check, expect={"detail", "feed", "list", "takes", "sound"})
        self.assertEqual(self.take_attempt(self.reader_token, self.recipe_id).status_code, 400)
        self.assertEqual(self.use(self.reader_token, self.sound_id).status_code, 400)

    def test_sound_removal(self):
        set_removed(Sound.objects.get(pk=self.sound_id), True, by_moderator=True)
        def check(card):
            self.assertIsNone(card["sound"])
            self.assertEqual(card["source"]["post_id"], self.recipe_id)
        self.assert_everywhere(check, expect={"detail", "feed", "list", "takes"})

    def test_the_source_authors_account_goes_private(self):
        RepbaseUser.objects.filter(pk=self.mia.pk).update(is_profile_public=False)
        def check(card):
            self.assertEqual(card["source"], UNAVAILABLE)
            # Closing a profile is about her posts. Her sound is still hers
            # to have shared, and is credited.
            self.assertEqual(card["sound"]["id"], self.sound_id)
        self.assert_everywhere(check, expect={"detail", "feed", "list", "sound"})
        # Somebody who follows her still sees the credit, everywhere.
        Follow.objects.create(follower=self.reader, following=self.mia)
        self.assert_everywhere(
            lambda card: self.assertEqual(card["source"]["post_id"], self.recipe_id),
            expect={"detail", "feed", "list", "takes", "sound"},
        )

    def test_a_saved_copy_survives_all_of_it_and_is_the_version_that_was_chosen(self):
        self.authenticate(self.reader_token)
        saved_id = self.client.post(f"{POSTS}{self.take_id}/save-meal/").data["meal"]
        for card in self.surfaces().values():
            self.assertTrue(card["viewer_saved"])
        self.assertFalse(self.card(self.reader_token, self.recipe_id)["viewer_saved"],
                         "saving the take did not save the recipe it answers")
        set_removed(Sound.objects.get(pk=self.sound_id), True, by_moderator=True)
        self.authenticate(self.mia_token)
        self.client.delete(f"{POSTS}{self.recipe_id}/")
        self.authenticate(self.jules_token)
        self.client.delete(f"{POSTS}{self.take_id}/")
        saved = SavedFoodMeal.objects.get(pk=saved_id)
        self.assertEqual(saved.cooking_instructions, "Press the tofu.")
        self.assertEqual(list(saved.ingredients.values_list("name", flat=True)), ["Tofu"])
