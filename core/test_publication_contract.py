"""Milestone 2: what a post says about itself, and what it may be made from.

Everything here goes through the API the way a client does, and is checked
against two contracts at once: `openapi.yaml` as committed, which is what the
next app is generated from, and the contract as it stood before this
milestone (`test_fixtures/contract/posts_before_m2.json`), which is what the
apps already in people's hands were generated from. A response that satisfies
the first and not the second is a feed that stops decoding on somebody's
phone.

The four formats the design shows are four shapes of the same Post:

- a gym moment: a media post, a clip or photo and a caption;
- a workout: its frozen snapshot, and optionally the same short clip;
- a recipe: a meal snapshot with its method, and optionally a clip;
- a take: any of those, published as a response to somebody else's post.
"""

import json
from decimal import Decimal
from pathlib import Path
from unittest import skipUnless

import yaml
from django.apps import apps as global_apps
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APITestCase

from .contract_check import to_json_schema, violations
from .fixtures_for_tests import (
    TemporaryMediaMixin,
    as_base64,
    finished_session,
    meal_post,
    meal_with_foods,
    photo_bytes,
    planner_entry,
    video_fixture,
    workout_post,
)
from .models import (
    Block,
    Follow,
    FoodEntry,
    Post,
    PostHashtag,
    PostMeal,
    PostMealEntry,
    PostWorkout,
    PostWorkoutExercise,
    RepbaseUser,
    SavedFoodMeal,
    Sound,
    WorkoutTemplate,
)
from .publication import (
    MAX_HASHTAGS,
    declarable_topics,
    hashtags_in,
    normalised_language,
)
from .tests import RepbaseAPITestMixin

POSTS = "/api/v1/social/posts/"
FEED = "/api/v1/social/feed/"
FOR_YOU = "/api/v1/social/for-you/"

ROOT = Path(__file__).resolve().parent.parent
BEFORE_M2 = Path(__file__).resolve().parent / "test_fixtures" / "contract" / "posts_before_m2.json"

#: Every field this milestone added to a response the apps already decode.
ADDED_TO_POST = {
    "is_take", "source", "sound", "hashtags", "topics", "language",
    "attachment_version", "audience", "reuse_permission", "viewer_can_take",
}
ADDED_TO_REPOSTED_POST = ADDED_TO_POST - {"viewer_can_take"}
ADDED_TO_WORKOUT = {"details_included"}
ADDED_TO_MEAL = {"details_included", "entry_count"}

UNAVAILABLE = {
    "available": False, "post_id": None, "original_post_id": None, "kind": None,
    "author": None, "title": None, "attachment_version": None,
}


def as_sent(response):
    """The body as a client receives it: JSON, not Python objects."""
    return json.loads(response.content)


class Contracts:
    """The committed contract and the one from before this milestone."""

    _now = None
    _before = None

    @classmethod
    def now(cls):
        if cls._now is None:
            cls._now = to_json_schema(yaml.safe_load((ROOT / "openapi.yaml").read_text(encoding="utf-8")))
        return cls._now

    @classmethod
    def before(cls):
        if cls._before is None:
            cls._before = to_json_schema(json.loads(BEFORE_M2.read_text(encoding="utf-8")))
        return cls._before

    @staticmethod
    def component(spec, name):
        return {
            "$ref": f"#/components/schemas/{name}",
            "components": {"schemas": spec["components"]["schemas"]},
        }


class PublicationTestMixin(TemporaryMediaMixin, RepbaseAPITestMixin):
    """Three people: Mia posts, Jules answers, and a reader who knows neither."""

    def setUp(self):
        _, self.mia, self.mia_token = self.create_account("mia")
        _, self.jules, self.jules_token = self.create_account("jules")
        _, self.reader, self.reader_token = self.create_account("reader")

    # ------------------------------------------------------------ publishing

    def save_meal(self, post_id, owner=None):
        """Save a post's recipe into the saver's cookbook, as the app does.

        A recipe is always saved into a cookbook the reader picks; the reader
        gets one here the first time.
        """
        from .models import Cookbook

        book, _ = Cookbook.objects.get_or_create(owner=owner or self.reader, name="Saved from the feed")
        return self.client.post(f"{POSTS}{post_id}/save-meal/", {"cookbook": book.pk}, format="json")

    def publish(self, token, **body):
        self.authenticate(token)
        return self.client.post(POSTS, body, format="json")

    def published(self, token, **body):
        response = self.publish(token, **body)
        self.assertEqual(response.status_code, 201, response.data)
        return as_sent(response)

    def profile_of(self, token):
        return RepbaseUser.objects.get(user=token.user)

    def moment(self, token, **body):
        return self.published(
            token, kind="media", content_type="image/jpeg",
            image_base64=as_base64(photo_bytes()), **body,
        )

    def workout(self, token, **body):
        session = finished_session(self.profile_of(token))
        return self.published(token, kind="workout", source_id=session.pk, **body)

    def recipe(self, token, foods=(("Chicken", "300.00"), ("Rice", "200.00")), **body):
        meal = meal_with_foods(self.profile_of(token), foods=foods)
        body.setdefault("cooking_instructions", "Cook the rice. Sear the chicken.")
        return self.published(token, kind="meal", source_id=meal.pk, **body)

    def recipe_take(self, token, source_id, **body):
        return self.recipe(
            token, foods=(("Tofu", "180.00"), ("Rice", "200.00")),
            source_post_id=source_id, **body,
        )

    def take_attempt(self, token, source_id, **body):
        meal = meal_with_foods(self.profile_of(token))
        return self.publish(token, kind="meal", source_id=meal.pk, source_post_id=source_id, **body)

    def attach_clip(self, token, post_id):
        self.authenticate(token)
        response = self.client.generic(
            "POST", f"{POSTS}{post_id}/video/", video_fixture("h264_faststart.mp4"),
            content_type="video/mp4",
        )
        self.assertEqual(response.status_code, 200, response.data)
        return as_sent(response)

    # --------------------------------------------------------------- reading

    def read(self, token, path):
        self.authenticate(token)
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        return as_sent(response)

    def card(self, token, post_id):
        return self.read(token, f"{POSTS}{post_id}/")

    def page(self, token, path):
        return {card["id"]: card for card in self.read(token, path)["results"]}

    def everywhere(self, token, post_id):
        """The same post from each place it can be read: its own URL, the
        post list, the author's profile and the takes of its source."""
        post = Post.objects.get(pk=post_id)
        found = {
            "detail": self.card(token, post_id),
            "list": self.page(token, POSTS).get(post_id),
            "profile": self.page(token, f"{POSTS}?author={post.author_id}").get(post_id),
        }
        if post.source_post_id:
            self.authenticate(token)
            takes = self.client.get(f"{POSTS}{post.source_post_id}/takes/")
            if takes.status_code == 200:
                found["takes"] = {c["id"]: c for c in as_sent(takes)["results"]}.get(post_id)
        return found

    # ------------------------------------------------------------- contracts

    def assert_matches_both_contracts(self, body, component):
        for label, spec in (("committed", Contracts.now()), ("before M2", Contracts.before())):
            found = violations(Contracts.component(spec, component), body, limit=8)
            self.assertEqual(found, [], f"{component} against the {label} contract")


# ----------------------------------------------------------------- formats


class FourFormatsContractTests(PublicationTestMixin, APITestCase):
    """Each format, created through the API, in every place it is read, in
    both attachment modes, against both contracts."""

    def setUp(self):
        super().setUp()
        Follow.objects.create(follower=self.reader, following=self.mia)
        Follow.objects.create(follower=self.reader, following=self.jules)

    def make_all_four(self):
        moment = self.published(
            self.mia_token, kind="media", expects_video=True,
            caption="Finding the plan #gymlore", language="en",
        )
        self.attach_clip(self.mia_token, moment["id"])
        workout = self.workout(
            self.mia_token, caption="Dumbbell push day", topics=["type:lifting"],
        )
        self.attach_clip(self.mia_token, workout["id"])
        recipe = self.recipe(self.mia_token, caption="Weeknight chicken bowl #mealprep")
        self.attach_clip(self.mia_token, recipe["id"])
        take = self.recipe_take(
            self.jules_token, recipe["id"], caption="Made it with tofu, for two days",
        )
        self.attach_clip(self.jules_token, take["id"])
        return {"moment": moment["id"], "workout": workout["id"],
                "recipe": recipe["id"], "take": take["id"]}

    def test_a_created_post_of_each_format_matches_both_contracts(self):
        created = [
            self.moment(self.mia_token, caption="Rack taken"),
            self.workout(self.mia_token),
            self.recipe(self.mia_token),
        ]
        created.append(self.recipe_take(self.jules_token, created[2]["id"]))
        for body in created:
            with self.subTest(kind=body["kind"], take=body["is_take"]):
                self.assert_matches_both_contracts(body, "Post")
                self.assertEqual(ADDED_TO_POST - set(body), set())

    def test_every_format_is_what_the_design_says_it_is(self):
        ids = self.make_all_four()
        cards = {name: self.card(self.reader_token, pk) for name, pk in ids.items()}

        moment = cards["moment"]
        self.assertEqual(moment["kind"], "media")
        self.assertIsNotNone(moment["video"])
        self.assertIsNone(moment["workout"])
        self.assertIsNone(moment["meal"])
        self.assertEqual(moment["hashtags"], ["gymlore"])
        self.assertEqual(moment["language"], "en")

        # A workout and a recipe carry the same short clip a moment does,
        # beside the attachment they already had.
        self.assertEqual(cards["workout"]["kind"], "workout")
        self.assertIsNotNone(cards["workout"]["video"])
        self.assertEqual(cards["workout"]["workout"]["exercise_count"], 1)
        self.assertEqual(cards["workout"]["topics"], ["type:lifting"])
        self.assertEqual(cards["recipe"]["kind"], "meal")
        self.assertIsNotNone(cards["recipe"]["video"])
        self.assertEqual(cards["recipe"]["meal"]["entry_count"], 2)

        # A take is not a kind: it is a recipe post an older app can draw.
        take = cards["take"]
        self.assertEqual(take["kind"], "meal")
        self.assertTrue(take["is_take"])
        self.assertIsNotNone(take["video"])
        self.assertEqual(take["meal"]["entries"][0]["name"], "Tofu")
        self.assertEqual(take["source"]["post_id"], ids["recipe"])
        self.assertEqual(take["source"]["author"]["username"], "mia")
        self.assertEqual(take["source"]["title"], "Breakfast")
        self.assertEqual(take["source"]["kind"], "meal")
        for name in ("moment", "workout", "recipe"):
            self.assertFalse(cards[name]["is_take"])
            self.assertIsNone(cards[name]["source"])

    def test_every_reading_surface_matches_both_contracts_in_both_modes(self):
        ids = self.make_all_four()
        # A repost of the take, so the nested card is under test as well.
        self.authenticate(self.reader_token)
        self.assertEqual(self.client.post(f"{POSTS}{ids['take']}/repost/").status_code, 200)

        surfaces = {
            POSTS: "PaginatedPostList",
            FEED: "PaginatedPostList",
            FOR_YOU: "ForYouPage",
            f"{POSTS}{ids['recipe']}/takes/": "PaginatedPostList",
            f"{POSTS}?author={self.reader.pk}": "PaginatedPostList",
        }
        for path, component in surfaces.items():
            for mode in ("", "full", "summary"):
                joiner = "&" if "?" in path else "?"
                url = path + (f"{joiner}attachments={mode}" if mode else "")
                with self.subTest(url=url):
                    body = self.read(self.reader_token, url)
                    self.assertTrue(body["results"], "an empty page proves nothing")
                    self.assert_matches_both_contracts(body, component)
        for pk in ids.values():
            self.assert_matches_both_contracts(self.card(self.reader_token, pk), "Post")

        reposts = [
            card for card in self.read(self.reader_token, f"{POSTS}?author={self.reader.pk}")["results"]
            if card["kind"] == "repost"
        ]
        self.assertEqual(len(reposts), 1)
        nested = reposts[0]["repost_of"]
        self.assertTrue(nested["is_take"])
        self.assertEqual(nested["source"]["post_id"], ids["recipe"])
        self.assertEqual(ADDED_TO_REPOSTED_POST - set(nested), set())

    def test_for_you_carries_the_mode_to_its_next_page(self):
        for _ in range(3):
            self.recipe(self.mia_token)
        body = self.read(self.reader_token, f"{FOR_YOU}?page_size=1&attachments=summary")
        self.assertIn("attachments=summary", body["next"])
        self.assertFalse(body["results"][0]["meal"]["details_included"])

    def test_an_unknown_mode_is_refused_not_guessed(self):
        self.authenticate(self.reader_token)
        for path in (POSTS, FEED, FOR_YOU):
            response = self.client.get(f"{path}?attachments=brief")
            self.assertEqual(response.status_code, 400, path)
            self.assertIn("attachments", response.data)


class OlderAppsStillDecodeTests(TestCase):
    """The committed contract, read as a client generated before it would.

    A generated Swift client refuses a response missing a property it was
    told is required, and refuses a value outside an enum it was given. It
    ignores properties it has never heard of. So a contract may grow; it may
    not start requiring what the old server never sent, stop sending what the
    old contract required, or put a new enum in a response.
    """

    def schemas(self, spec):
        return spec["components"]["schemas"]

    def test_nothing_added_is_required(self):
        now = self.schemas(Contracts.now())
        added = {
            "Post": ADDED_TO_POST, "ForYouPost": ADDED_TO_POST,
            "RepostedPost": ADDED_TO_REPOSTED_POST,
            "PostWorkout": ADDED_TO_WORKOUT, "PostMeal": ADDED_TO_MEAL,
        }
        for name, fields in added.items():
            with self.subTest(component=name):
                self.assertEqual(fields - set(now[name]["properties"]), set(), "not in the contract")
                self.assertEqual(fields & set(now[name].get("required", [])), set(), "required")

    #: Changes the `work/` branches made to these components alongside
    #: Social, after the baseline was frozen. Neither stops an old app
    #: decoding: a new required field is one it ignores, and the generated
    #: client does not check a string's pattern.
    WORK_BRANCH_REQUIRED = {"Post": {"cookbook"}, "ForYouPost": {"cookbook"}, "RepostedPost": {"cookbook"}}
    WORK_BRANCH_PATTERNS = {("PostMealEntry", "servings")}

    def test_everything_that_was_required_still_is_and_nothing_else(self):
        now, before = self.schemas(Contracts.now()), self.schemas(Contracts.before())
        for name, old in before.items():
            if old.get("type") != "object" or name.endswith("Request"):
                continue
            with self.subTest(component=name):
                self.assertEqual(
                    sorted(set(now[name].get("required", [])) - self.WORK_BRANCH_REQUIRED.get(name, set())),
                    sorted(old.get("required", [])),
                )
                # A property an old client reads must still be there, with
                # the type it was promised.
                for prop, shape in old["properties"].items():
                    current = now[name]["properties"][prop]
                    if (name, prop) in self.WORK_BRANCH_PATTERNS:
                        current = {k: v for k, v in current.items() if k != "pattern"}
                        shape = {k: v for k, v in shape.items() if k != "pattern"}
                    self.assertEqual(current, shape, f"{name}.{prop}")

    def test_no_enum_was_put_in_a_response(self):
        """New values travel as plain strings, as Post.kind does."""
        now = self.schemas(Contracts.now())
        for name in ("Post", "RepostedPost", "ForYouPost", "PostSource", "PostSound",
                     "CreditedPerson", "PostWorkout", "PostMeal"):
            for prop, shape in now[name]["properties"].items():
                with self.subTest(field=f"{name}.{prop}"):
                    self.assertNotIn("enum", json.dumps(shape))
                    if "$ref" in json.dumps(shape):
                        for ref in _refs(shape):
                            self.assertNotIn("enum", now[ref], f"{name}.{prop} -> {ref}")

    def test_an_old_request_is_still_a_valid_request(self):
        """Nothing a client already sends became required or was renamed."""
        now, before = self.schemas(Contracts.now()), self.schemas(Contracts.before())
        for name in ("CreatePostRequest", "PatchedUpdatePostRequest"):
            with self.subTest(component=name):
                self.assertEqual(now[name].get("required", []), before[name].get("required", []))
                for prop, shape in before[name]["properties"].items():
                    self.assertEqual(now[name]["properties"][prop], shape)


def _refs(node):
    if isinstance(node, dict):
        if "$ref" in node:
            yield node["$ref"].rsplit("/", 1)[1]
        for value in node.values():
            yield from _refs(value)
    elif isinstance(node, list):
        for value in node:
            yield from _refs(value)


# ------------------------------------------------------------------ lineage


class LineageTests(PublicationTestMixin, APITestCase):
    def test_a_take_records_its_direct_source_and_the_original(self):
        recipe = self.recipe(self.mia_token)
        take = self.recipe_take(self.jules_token, recipe["id"])
        row = Post.objects.get(pk=take["id"])
        self.assertTrue(row.is_take)
        self.assertEqual(row.source_post_id, recipe["id"])
        self.assertEqual(row.original_post_id, recipe["id"])
        self.assertEqual(row.source_attachment_version, 1)
        self.assertEqual(take["source"]["attachment_version"], 1)
        self.assertEqual(take["source"]["original_post_id"], recipe["id"])

    def test_a_take_of_a_take_credits_the_take_and_remembers_the_original(self):
        recipe = self.recipe(self.mia_token)
        first = self.recipe_take(self.jules_token, recipe["id"])
        second = self.recipe_take(self.reader_token, first["id"])
        self.assertEqual(second["source"]["post_id"], first["id"])
        self.assertEqual(second["source"]["author"]["username"], "jules")
        self.assertEqual(second["source"]["original_post_id"], recipe["id"])
        # The direct responses to the recipe are the first take alone.
        self.assertEqual(set(self.page(self.reader_token, f"{POSTS}{recipe['id']}/takes/")), {first["id"]})
        self.assertEqual(set(self.page(self.reader_token, f"{POSTS}{first['id']}/takes/")), {second["id"]})

    def test_a_take_of_a_repost_is_a_take_of_what_it_passes_on(self):
        recipe = self.recipe(self.mia_token)
        self.authenticate(self.reader_token)
        self.client.post(f"{POSTS}{recipe['id']}/repost/")
        repost = Post.objects.get(kind=Post.Kind.REPOST)
        take = self.recipe_take(self.jules_token, repost.pk)
        self.assertEqual(take["source"]["post_id"], recipe["id"])
        self.assertEqual(take["source"]["author"]["username"], "mia")
        self.assertEqual(
            set(self.page(self.jules_token, f"{POSTS}{repost.pk}/takes/")), {take["id"]}
        )

    def test_a_take_of_a_moment_has_no_attachment_version(self):
        moment = self.moment(self.mia_token)
        take = self.published(
            self.jules_token, kind="media", content_type="image/jpeg",
            image_base64=as_base64(photo_bytes()), source_post_id=moment["id"],
        )
        self.assertIsNone(take["source"]["attachment_version"])
        self.assertIsNone(take["source"]["title"])
        self.assertEqual(take["source"]["kind"], "media")

    def test_one_load_credits_a_page_of_takes(self):
        """The credit is read for the page, not for each card."""
        Follow.objects.create(follower=self.reader, following=self.jules)
        recipe = self.recipe(self.mia_token)
        self.recipe_take(self.jules_token, recipe["id"])
        self.authenticate(self.reader_token)
        self.client.get(FEED)  # Warm what is cached per process.
        with CaptureQueriesContext(connection) as one:
            self.client.get(FEED)
        for _ in range(4):
            self.recipe_take(self.jules_token, recipe["id"])
        with CaptureQueriesContext(connection) as five:
            body = as_sent(self.client.get(FEED))
        self.assertEqual(len(body["results"]), 5)
        self.assertTrue(all(card["source"]["available"] for card in body["results"]))
        self.assertEqual(len(five), len(one))

    def test_a_page_with_no_takes_and_no_sounds_pays_nothing_for_them(self):
        Follow.objects.create(follower=self.reader, following=self.mia)
        self.recipe(self.mia_token)
        self.authenticate(self.reader_token)
        self.client.get(FEED)
        with CaptureQueriesContext(connection) as queries:
            self.client.get(FEED)
        touched = [q["sql"] for q in queries if "core_sound" in q["sql"] or "core_posthashtag" in q["sql"]]
        self.assertEqual(touched, [])


# ------------------------------------------------- permissions, on creation


class TakePermissionTests(PublicationTestMixin, APITestCase):
    def refused(self, response, field="source_post_id"):
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn(field, response.data)
        return str(response.data[field])

    def test_a_post_the_taker_cannot_see_is_refused_like_one_that_does_not_exist(self):
        missing = self.refused(self.take_attempt(self.jules_token, 999_999))
        private = self.recipe(self.mia_token, visibility="private")["id"]
        followers = self.recipe(self.mia_token, visibility="followers")["id"]
        hidden = self.recipe(self.mia_token)["id"]
        Post.objects.filter(pk=hidden).update(is_hidden=True)
        blocked = self.recipe(self.reader_token)["id"]
        Block.objects.create(blocker=self.reader, blocked=self.jules)
        for label, pk in (("private", private), ("followers", followers),
                          ("hidden", hidden), ("blocked", blocked)):
            with self.subTest(source=label):
                # The same words, so the refusal is not an oracle.
                self.assertEqual(self.refused(self.take_attempt(self.jules_token, pk)), missing)
        self.assertFalse(Post.objects.filter(is_take=True).exists())

    def test_a_private_source_does_not_become_public_through_a_take(self):
        """A follower can see a followers-only recipe. They may not answer it
        in public -- nor at all, since their followers are not hers."""
        Follow.objects.create(follower=self.jules, following=self.mia)
        followers_only = self.recipe(self.mia_token, visibility="followers")["id"]
        self.assertEqual(self.card(self.jules_token, followers_only)["id"], followers_only)
        for visibility in ("public", "followers", "private"):
            with self.subTest(take_visibility=visibility):
                message = self.refused(
                    self.take_attempt(self.jules_token, followers_only, visibility=visibility)
                )
                self.assertIn("everybody can see", message)
        self.assertFalse(self.card(self.jules_token, followers_only)["viewer_can_take"])

    def test_a_public_post_on_a_closed_profile_is_not_open_to_takes(self):
        recipe = self.recipe(self.mia_token)["id"]
        Follow.objects.create(follower=self.jules, following=self.mia)
        RepbaseUser.objects.filter(pk=self.mia.pk).update(is_profile_public=False)
        self.assertIn("everybody can see", self.refused(self.take_attempt(self.jules_token, recipe)))
        self.assertEqual(self.card(self.jules_token, recipe)["audience"], "followers")

    def test_an_author_cannot_take_their_own_private_post_into_the_open(self):
        private = self.recipe(self.mia_token, visibility="private")["id"]
        self.assertIn("everybody can see", self.refused(self.take_attempt(self.mia_token, private)))

    def test_reuse_permission_is_enforced(self):
        nobody = self.recipe(self.mia_token, reuse_permission="nobody")["id"]
        followers = self.recipe(self.mia_token, reuse_permission="followers")["id"]
        everyone = self.recipe(self.mia_token)["id"]
        Follow.objects.create(follower=self.jules, following=self.mia)

        self.assertIn("not allowed", self.refused(self.take_attempt(self.jules_token, nobody)))
        self.assertIn("not allowed", self.refused(self.take_attempt(self.reader_token, followers)))
        self.assertEqual(self.take_attempt(self.jules_token, followers).status_code, 201)
        self.assertEqual(self.take_attempt(self.reader_token, everyone).status_code, 201)
        # Her own post is hers to answer whatever she told everybody else.
        self.assertEqual(self.take_attempt(self.mia_token, nobody).status_code, 201)

    def test_the_button_is_offered_exactly_where_the_take_would_be_accepted(self):
        """`viewer_can_take` is computed from the row; the create asks the
        database. They must not be able to disagree."""
        Follow.objects.create(follower=self.jules, following=self.mia)
        sources = {
            "open": self.recipe(self.mia_token)["id"],
            "nobody": self.recipe(self.mia_token, reuse_permission="nobody")["id"],
            "followers": self.recipe(self.mia_token, reuse_permission="followers")["id"],
            "followers_only_post": self.recipe(self.mia_token, visibility="followers")["id"],
            "moment": self.moment(self.mia_token)["id"],
            "workout": self.workout(self.mia_token)["id"],
        }
        planner = planner_entry(self.mia)
        sources["planner"] = self.published(self.mia_token, kind="planner", source_id=planner.pk)["id"]
        # A repost of a followers-reuse post: the reposter is somebody else,
        # so the follow that matters is of the original's author.
        self.authenticate(self.reader_token)
        self.client.post(f"{POSTS}{sources['followers']}/repost/")
        sources["repost"] = Post.objects.get(kind=Post.Kind.REPOST).pk

        for token, name in ((self.jules_token, "follower"), (self.reader_token, "stranger"),
                            (self.mia_token, "author")):
            for label, pk in sources.items():
                with self.subTest(viewer=name, source=label):
                    self.authenticate(token)
                    seen = self.client.get(f"{POSTS}{pk}/")
                    offered = seen.status_code == 200 and as_sent(seen)["viewer_can_take"]
                    accepted = self.take_attempt(token, pk).status_code == 201
                    self.assertEqual(offered, accepted)

    def test_a_post_read_by_its_own_url_knows_whether_its_author_is_followed(self):
        """It answered false for everybody: only list pages carried the
        annotation the field read."""
        recipe = self.recipe(self.mia_token)["id"]
        Follow.objects.create(follower=self.jules, following=self.mia)
        self.assertTrue(self.card(self.jules_token, recipe)["viewer_follows_author"])
        self.assertFalse(self.card(self.reader_token, recipe)["viewer_follows_author"])
        self.assertFalse(self.card(self.mia_token, recipe)["viewer_follows_author"])

    def test_what_cannot_be_a_take_or_be_taken(self):
        recipe = self.recipe(self.mia_token)["id"]
        entry = planner_entry(self.jules)
        as_planner = self.publish(
            self.jules_token, kind="planner", source_id=entry.pk, source_post_id=recipe,
        )
        self.assertIn("planner", self.refused(as_planner))
        planner_post_id = self.published(
            self.mia_token, kind="planner", source_id=planner_entry(self.mia).pk
        )["id"]
        self.assertIn("Only a photo", self.refused(self.take_attempt(self.jules_token, planner_post_id)))

    def test_closing_a_post_to_takes_stops_new_ones_and_keeps_the_old(self):
        recipe = self.recipe(self.mia_token)["id"]
        take = self.recipe_take(self.jules_token, recipe)["id"]
        self.authenticate(self.mia_token)
        closed = self.client.patch(f"{POSTS}{recipe}/", {"reuse_permission": "nobody"}, format="json")
        self.assertEqual(closed.status_code, 200, closed.data)
        self.assertEqual(closed.data["reuse_permission"], "nobody")

        self.assertIn("not allowed", self.refused(self.take_attempt(self.reader_token, recipe)))
        self.assertFalse(self.card(self.reader_token, recipe)["viewer_can_take"])
        # The take already made is its author's post. It stays, with its
        # credit, for as long as its readers may see what it credits.
        for where, card in self.everywhere(self.reader_token, take).items():
            with self.subTest(where=where):
                self.assertEqual(card["source"]["post_id"], recipe)


# ------------------------------------------------------ permissions, on reading


class SourcePropagationTests(PublicationTestMixin, APITestCase):
    """What happens to a take's credit when its source changes under it.

    In each case the take itself stays, and a reader who may no longer see
    the source learns only that the original is unavailable.
    """

    def setUp(self):
        super().setUp()
        self.recipe_id = self.recipe(self.mia_token, caption="Secret sauce")["id"]
        self.take_id = self.recipe_take(self.jules_token, self.recipe_id)["id"]

    def assert_withheld_everywhere(self, token):
        for where, card in self.everywhere(token, self.take_id).items():
            with self.subTest(where=where):
                self.assertIsNotNone(card, "the take itself must still be there")
                self.assertTrue(card["is_take"])
                self.assertEqual(card["source"], UNAVAILABLE)
                self.assertNotIn("mia", json.dumps(card["source"]))
                self.assert_matches_both_contracts(card, "Post")

    def assert_credited_everywhere(self, token):
        for where, card in self.everywhere(token, self.take_id).items():
            with self.subTest(where=where):
                self.assertEqual(card["source"]["post_id"], self.recipe_id)

    def test_before_anything_changes_the_source_is_credited(self):
        self.assert_credited_everywhere(self.reader_token)

    def test_the_source_is_deleted(self):
        self.authenticate(self.mia_token)
        self.assertEqual(self.client.delete(f"{POSTS}{self.recipe_id}/").status_code, 204)
        row = Post.objects.get(pk=self.take_id)
        self.assertTrue(row.is_take)
        self.assertIsNone(row.source_post_id)
        self.assertIsNone(row.original_post_id)
        self.assert_withheld_everywhere(self.reader_token)
        self.assert_withheld_everywhere(self.jules_token)

    def test_the_source_is_taken_down_by_a_moderator(self):
        Post.objects.filter(pk=self.recipe_id).update(is_hidden=True)
        self.assert_withheld_everywhere(self.reader_token)
        # Its own author included: a takedown is a takedown.
        self.assert_withheld_everywhere(self.mia_token)

    def test_the_source_is_waiting_for_review_again(self):
        Post.objects.filter(pk=self.recipe_id).update(moderation_status="pending", published_at=None)
        self.assert_withheld_everywhere(self.reader_token)

    def test_the_source_is_made_private(self):
        self.authenticate(self.mia_token)
        self.client.patch(f"{POSTS}{self.recipe_id}/", {"visibility": "private"}, format="json")
        self.assert_withheld_everywhere(self.reader_token)
        self.assert_withheld_everywhere(self.jules_token)
        # She can still see her own post, so she still sees it credited.
        self.assertEqual(self.card(self.mia_token, self.take_id)["source"]["post_id"], self.recipe_id)

    def test_the_source_is_narrowed_to_followers(self):
        Follow.objects.create(follower=self.jules, following=self.mia)
        self.authenticate(self.mia_token)
        self.client.patch(f"{POSTS}{self.recipe_id}/", {"visibility": "followers"}, format="json")
        self.assert_withheld_everywhere(self.reader_token)
        self.assert_credited_everywhere(self.jules_token)

    def test_the_source_authors_profile_is_closed(self):
        RepbaseUser.objects.filter(pk=self.mia.pk).update(is_profile_public=False)
        self.assert_withheld_everywhere(self.reader_token)

    def test_the_source_author_is_suspended(self):
        get_user_model().objects.filter(pk=self.mia.user_id).update(is_active=False)
        self.assert_withheld_everywhere(self.reader_token)

    def test_the_source_author_and_the_reader_have_a_block_between_them(self):
        Block.objects.create(blocker=self.mia, blocked=self.reader)
        self.assert_withheld_everywhere(self.reader_token)
        self.assert_credited_everywhere(self.jules_token)
        Block.objects.all().delete()
        Block.objects.create(blocker=self.reader, blocked=self.mia)
        self.assert_withheld_everywhere(self.reader_token)

    def test_a_repost_of_the_take_withholds_the_same_credit(self):
        self.authenticate(self.reader_token)
        self.client.post(f"{POSTS}{self.take_id}/repost/")
        mine = f"{POSTS}?author={self.reader.pk}"
        nested = self.read(self.reader_token, mine)["results"][0]["repost_of"]
        self.assertEqual(nested["source"]["post_id"], self.recipe_id)
        Post.objects.filter(pk=self.recipe_id).update(is_hidden=True)
        nested = self.read(self.reader_token, mine)["results"][0]["repost_of"]
        self.assertEqual(nested["source"], UNAVAILABLE)

    def test_the_original_goes_and_the_direct_source_stays(self):
        second = self.recipe_take(self.reader_token, self.take_id)["id"]
        self.authenticate(self.mia_token)
        self.client.delete(f"{POSTS}{self.recipe_id}/")
        card = self.card(self.mia_token, second)
        self.assertEqual(card["source"]["post_id"], self.take_id)
        self.assertIsNone(card["source"]["original_post_id"])
        # A take made now starts its family at the oldest link still there.
        third = self.recipe_take(self.mia_token, self.take_id)["id"]
        self.assertEqual(Post.objects.get(pk=third).original_post_id, self.take_id)

    def test_the_takes_of_a_post_the_reader_cannot_see_are_not_listed(self):
        Post.objects.filter(pk=self.recipe_id).update(is_hidden=True)
        self.authenticate(self.reader_token)
        self.assertEqual(self.client.get(f"{POSTS}{self.recipe_id}/takes/").status_code, 404)

    def test_a_take_is_listed_only_to_those_who_may_see_the_take(self):
        self.authenticate(self.jules_token)
        self.client.patch(f"{POSTS}{self.take_id}/", {"visibility": "private"}, format="json")
        self.assertEqual(self.page(self.reader_token, f"{POSTS}{self.recipe_id}/takes/"), {})
        self.assertEqual(set(self.page(self.jules_token, f"{POSTS}{self.recipe_id}/takes/")), {self.take_id})


# ------------------------------------------------------------------- sounds


class SoundTests(PublicationTestMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.origin = self.moment(self.mia_token)["id"]
        self.sound = Sound.objects.create(
            owner=self.mia, origin_post_id=self.origin, title="Original sound - mia",
            duration_ms=9000, audio_key="sounds/abc.m4a",
        )

    def use(self, token, **body):
        return self.publish(
            token, kind="media", content_type="image/jpeg",
            image_base64=as_base64(photo_bytes()), sound_id=self.sound.pk, **body,
        )

    def test_a_post_references_the_sound_and_credits_its_owner(self):
        response = self.use(self.jules_token)
        self.assertEqual(response.status_code, 201, response.data)
        body = as_sent(response)
        self.assert_matches_both_contracts(body, "Post")
        self.assertEqual(body["sound"]["id"], self.sound.pk)
        self.assertEqual(body["sound"]["owner"]["username"], "mia")
        self.assertFalse(body["sound"]["is_original"])
        # A reference: one sound, however many posts use it.
        self.use(self.reader_token)
        self.assertEqual(Sound.objects.count(), 1)
        self.assertEqual(Post.objects.filter(sound=self.sound).count(), 2)

    def test_the_post_it_came_from_says_so(self):
        Post.objects.filter(pk=self.origin).update(sound=self.sound)
        self.assertTrue(self.card(self.reader_token, self.origin)["sound"]["is_original"])

    def test_who_may_use_a_sound(self):
        cases = (
            ("removed", lambda: Sound.objects.update(status=Sound.Status.REMOVED), "No sound"),
            ("blocked", lambda: Block.objects.create(blocker=self.mia, blocked=self.jules), "No sound"),
            ("suspended", lambda: get_user_model().objects.filter(pk=self.mia.user_id).update(is_active=False), "No sound"),
            ("closed", lambda: Sound.objects.update(reuse_permission="nobody"), "not allowed"),
            ("followers", lambda: Sound.objects.update(reuse_permission="followers"), "not allowed"),
        )
        for label, change, words in cases:
            with self.subTest(sound=label):
                Sound.objects.update(status=Sound.Status.ACTIVE, reuse_permission="everyone")
                Block.objects.all().delete()
                get_user_model().objects.update(is_active=True)
                change()
                response = self.use(self.jules_token)
                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn(words, str(response.data["sound_id"]))
        self.assertEqual(self.publish(
            self.jules_token, kind="media", content_type="image/jpeg",
            image_base64=as_base64(photo_bytes()), sound_id=999_999,
        ).status_code, 400)
        # Its owner always may.
        Sound.objects.update(status=Sound.Status.ACTIVE, reuse_permission="nobody")
        get_user_model().objects.update(is_active=True)
        self.assertEqual(self.use(self.mia_token).status_code, 201)

    def test_a_removed_sound_leaves_the_post_and_takes_its_credit(self):
        post = as_sent(self.use(self.jules_token))["id"]
        self.assertIsNotNone(self.card(self.reader_token, post)["sound"])
        Sound.objects.update(status=Sound.Status.REMOVED)
        card = self.card(self.reader_token, post)
        self.assertIsNone(card["sound"])
        self.assert_matches_both_contracts(card, "Post")
        # Reversible: the reference was never dropped.
        Sound.objects.update(status=Sound.Status.ACTIVE)
        self.assertIsNotNone(self.card(self.reader_token, post)["sound"])

    def test_a_block_takes_the_credit_from_that_reader_alone(self):
        post = as_sent(self.use(self.jules_token))["id"]
        Block.objects.create(blocker=self.mia, blocked=self.reader)
        self.assertIsNone(self.card(self.reader_token, post)["sound"])
        self.assertIsNotNone(self.card(self.jules_token, post)["sound"])

    def test_deleting_the_owners_account_leaves_the_posts_without_a_sound(self):
        post = as_sent(self.use(self.jules_token))["id"]
        self.mia.user.delete()
        self.assertFalse(Sound.objects.exists())
        card = self.card(self.reader_token, post)
        self.assertIsNone(card["sound"])


# ------------------------------------------- hashtags, topics and language


class HashtagTests(PublicationTestMixin, APITestCase):
    def test_tags_are_read_from_the_caption_and_normalised(self):
        self.assertEqual(
            hashtags_in("Leg day #LegDay #legday ＃ＬＥＧＤＡＹ #Größe #1 #run_club x#no a&#39;b http://x.y/#frag"),
            ["legday", "grösse", "run_club"],
        )
        self.assertEqual(hashtags_in(""), [])
        self.assertEqual(hashtags_in(None), [])
        self.assertEqual(hashtags_in("#" + "a" * 51), [])

    def test_a_caption_stuffed_with_tags_reaches_no_further_than_the_cap(self):
        caption = " ".join(f"#t{n}" for n in range(40))
        post = self.moment(self.mia_token, caption=caption)
        self.assertEqual(len(post["hashtags"]), MAX_HASHTAGS)
        self.assertEqual(post["caption"], caption)
        self.assertEqual(PostHashtag.objects.filter(post_id=post["id"]).count(), MAX_HASHTAGS)

    def test_the_list_on_the_post_and_the_rows_agree_through_edits(self):
        post = self.moment(self.mia_token, caption="#one #two")
        self.assert_tags(post["id"], ["one", "two"])
        self.authenticate(self.mia_token)
        edited = self.client.patch(f"{POSTS}{post['id']}/", {"caption": "#two #three"}, format="json")
        self.assertEqual(edited.data["hashtags"], ["two", "three"])
        self.assert_tags(post["id"], ["two", "three"])
        self.client.patch(f"{POSTS}{post['id']}/", {"visibility": "followers"}, format="json")
        self.assert_tags(post["id"], ["two", "three"])
        self.client.patch(f"{POSTS}{post['id']}/", {"caption": "none now"}, format="json")
        self.assert_tags(post["id"], [])

    def assert_tags(self, post_id, tags):
        self.assertEqual(Post.objects.get(pk=post_id).hashtags, tags)
        self.assertEqual(
            sorted(PostHashtag.objects.filter(post_id=post_id).values_list("tag", flat=True)),
            sorted(tags),
        )

    def test_deleting_a_post_deletes_its_tag_rows(self):
        post = self.moment(self.mia_token, caption="#gone")
        self.authenticate(self.mia_token)
        self.client.delete(f"{POSTS}{post['id']}/")
        self.assertFalse(PostHashtag.objects.exists())


class TopicsAndLanguageTests(PublicationTestMixin, APITestCase):
    def test_topics_come_from_the_catalogue_once_each_in_its_order(self):
        post = self.moment(
            self.mia_token,
            topics=["discipline:powerlifting", "type:running", "discipline:powerlifting"],
        )
        self.assertEqual(post["topics"], ["type:running", "discipline:powerlifting"])
        self.assertTrue(set(post["topics"]) <= set(declarable_topics()))

    def test_free_text_and_derived_topics_are_refused(self):
        for topics in (["leg day"], ["kind:meal"], ["format:video"], ["discipline:other"]):
            with self.subTest(topics=topics):
                response = self.publish(
                    self.mia_token, kind="media", content_type="image/jpeg",
                    image_base64=as_base64(photo_bytes()), topics=topics,
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn("topics", response.data)

    def test_too_many_topics_are_refused(self):
        response = self.publish(
            self.mia_token, kind="media", content_type="image/jpeg",
            image_base64=as_base64(photo_bytes()), topics=declarable_topics()[:6],
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("at most", str(response.data["topics"]))

    def test_language_is_a_tag_in_canonical_case_or_nothing(self):
        self.assertEqual(normalised_language("EN"), "en")
        self.assertEqual(normalised_language("pt_br"), "pt-BR")
        self.assertEqual(normalised_language("zh-hans-cn"), "zh-Hans-CN")
        self.assertEqual(normalised_language("  "), "")
        for bad in ("english", "e", "en-", "12", "en us"):
            with self.subTest(language=bad), self.assertRaises(ValueError):
                normalised_language(bad)
        self.assertIsNone(self.moment(self.mia_token)["language"])
        self.assertEqual(self.moment(self.mia_token, language="es-mx")["language"], "es-MX")
        refused = self.publish(
            self.mia_token, kind="media", content_type="image/jpeg",
            image_base64=as_base64(photo_bytes()), language="spanish",
        )
        self.assertEqual(refused.status_code, 400)
        self.assertIn("language", refused.data)

    def test_they_can_be_changed_afterwards(self):
        post = self.moment(self.mia_token, language="en", topics=["type:lifting"])
        self.authenticate(self.mia_token)
        response = self.client.patch(
            f"{POSTS}{post['id']}/", {"language": "", "topics": ["type:running"]}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNone(response.data["language"])
        self.assertEqual(response.data["topics"], ["type:running"])

    def test_audience_says_who_is_actually_reading(self):
        public = self.moment(self.mia_token)["id"]
        followers = self.moment(self.mia_token, visibility="followers")["id"]
        private = self.moment(self.mia_token, visibility="private")["id"]
        read = lambda pk: self.card(self.mia_token, pk)["audience"]  # noqa: E731
        self.assertEqual([read(public), read(followers), read(private)],
                         ["public", "followers", "private"])
        RepbaseUser.objects.filter(pk=self.mia.pk).update(is_profile_public=False)
        self.assertEqual([read(public), read(followers), read(private)],
                         ["followers", "followers", "private"])
        # What the author chose is still what `visibility` says.
        self.assertEqual(self.card(self.mia_token, public)["visibility"], "public")


# ----------------------------------------------------------- summary first


class SummaryFirstTests(PublicationTestMixin, APITestCase):
    def setUp(self):
        super().setUp()
        Follow.objects.create(follower=self.reader, following=self.mia)
        self.workout_id = self.workout(self.mia_token)["id"]
        # One and a half servings of the first food, so the totals are sums
        # of products and not just of the figures typed in.
        meal = meal_with_foods(
            self.mia, foods=(("Chicken", "310.50"), ("Rice", "199.99"), ("Oil", "0.33")),
        )
        FoodEntry.objects.filter(meal=meal, name="Chicken").update(servings=Decimal("1.50"))
        self.recipe_id = self.published(
            self.mia_token, kind="meal", source_id=meal.pk,
            cooking_instructions="Cook the rice. Sear the chicken.",
        )["id"]

    def test_a_summary_has_the_same_figures_and_none_of_the_rows(self):
        full = self.page(self.reader_token, FEED)
        brief = self.page(self.reader_token, f"{FEED}?attachments=summary")

        workout, short = full[self.workout_id]["workout"], brief[self.workout_id]["workout"]
        self.assertTrue(workout["details_included"])
        self.assertFalse(short["details_included"])
        self.assertEqual(len(workout["exercises"]), 1)
        self.assertEqual(short["exercises"], [])
        for figure in ("title", "exercise_count", "total_set_count", "total_volume_kg",
                       "duration_seconds", "workout_type"):
            self.assertEqual(short[figure], workout[figure], figure)
        self.assertEqual(short["total_volume_kg"], "1500.00")

        meal, short = full[self.recipe_id]["meal"], brief[self.recipe_id]["meal"]
        self.assertEqual(len(meal["entries"]), 3)
        self.assertTrue(meal["cooking_instructions"])
        self.assertEqual(short["entries"], [])
        self.assertEqual(short["cooking_instructions"], "")
        for figure in ("name", "entry_count", "total_calories", "total_protein_grams",
                       "total_carbohydrate_grams", "total_fat_grams"):
            self.assertEqual(short[figure], meal[figure], figure)
        self.assertEqual(short["total_calories"], "666.07")

    def test_the_post_itself_always_has_the_details(self):
        for suffix in ("", "?attachments=summary"):
            card = self.read(self.reader_token, f"{POSTS}{self.recipe_id}/{suffix}")
            self.assertTrue(card["meal"]["details_included"])
            self.assertEqual(len(card["meal"]["entries"]), 3)

    def test_a_summary_page_reads_no_exercise_or_food_rows(self):
        self.authenticate(self.reader_token)
        self.client.post(f"{POSTS}{self.recipe_id}/repost/")
        for path in (FEED, POSTS, f"{POSTS}?author={self.reader.pk}", FOR_YOU):
            with self.subTest(path=path):
                joiner = "&" if "?" in path else "?"
                with CaptureQueriesContext(connection) as full:
                    self.client.get(path)
                with CaptureQueriesContext(connection) as brief:
                    self.client.get(f"{path}{joiner}attachments=summary")
                rows = lambda queries: [  # noqa: E731
                    q["sql"] for q in queries
                    if 'FROM "core_postworkoutexercise"' in q["sql"] or 'FROM "core_postmealentry"' in q["sql"]
                ]
                self.assertTrue(rows(full))
                self.assertEqual(rows(brief), [])
                self.assertLess(len(brief), len(full))

    def test_a_snapshot_never_added_up_is_summarised_from_its_rows(self):
        """What the previous release writes between a migration and the
        restart: the rows, and none of the stored figures."""
        full = self.page(self.reader_token, FEED)
        PostWorkout.objects.update(exercise_count=None, total_set_count=None, total_volume_kg=None)
        PostMeal.objects.update(
            entry_count=None, total_calories=0, total_protein_grams=0,
            total_carbohydrate_grams=0, total_fat_grams=0,
        )
        brief = self.page(self.reader_token, f"{FEED}?attachments=summary")
        for figure in ("exercise_count", "total_set_count", "total_volume_kg"):
            self.assertEqual(
                brief[self.workout_id]["workout"][figure], full[self.workout_id]["workout"][figure]
            )
        for figure in ("entry_count", "total_calories", "total_protein_grams"):
            self.assertEqual(
                brief[self.recipe_id]["meal"][figure], full[self.recipe_id]["meal"][figure]
            )
        self.assertEqual(brief[self.workout_id]["workout"]["exercises"], [])
        self.assertFalse(brief[self.recipe_id]["meal"]["details_included"])

    def test_a_published_snapshot_stores_what_its_rows_add_up_to(self):
        workout = PostWorkout.objects.get(post_id=self.workout_id)
        rows = list(workout.exercises.all())
        self.assertEqual(workout.exercise_count, len(rows))
        self.assertEqual(workout.total_set_count, sum(row.set_count for row in rows))
        self.assertEqual(workout.total_volume_kg, sum(row.volume_kg for row in rows))
        # No load logged is null, never zero.
        bodyweight = finished_session(self.mia, exercise_name="Pull Up", weight="0", reps=8)
        bodyweight.session_exercises.first().sets.update(weight_kg=None)
        post = self.published(self.mia_token, kind="workout", source_id=bodyweight.pk)
        self.assertIsNone(PostWorkout.objects.get(post_id=post["id"]).total_volume_kg)
        brief = self.page(self.mia_token, f"{POSTS}?author={self.mia.pk}&attachments=summary")
        self.assertIsNone(brief[post["id"]]["workout"]["total_volume_kg"])


# ------------------------------------------------------------ personal saves


class PersonalSaveTests(PublicationTestMixin, APITestCase):
    """Saving is a private, editable copy of the attachment, and never of the
    media."""

    def test_saving_a_take_saves_the_take_not_the_recipe_it_answers(self):
        recipe = self.recipe(self.mia_token)["id"]
        take = self.recipe_take(
            self.jules_token, recipe, cooking_instructions="Press the tofu first.",
        )["id"]
        self.authenticate(self.reader_token)
        response = self.save_meal(take)
        self.assertEqual(response.status_code, 201, response.data)
        self.assert_matches_both_contracts(as_sent(response), "SavedMealResult")
        saved = SavedFoodMeal.objects.get(owner=self.reader)
        self.assertEqual(saved.source_post_id, take)
        self.assertEqual(saved.cooking_instructions, "Press the tofu first.")
        self.assertEqual(
            list(saved.ingredients.values_list("name", flat=True)), ["Tofu", "Rice"]
        )
        # And the original is still there to be saved as itself.
        self.assertEqual(self.save_meal(recipe).status_code, 201)
        self.assertEqual(SavedFoodMeal.objects.filter(owner=self.reader).count(), 2)
        self.assertTrue(self.card(self.reader_token, take)["viewer_saved"])

    def test_a_saved_recipe_is_the_savers_to_edit_and_the_post_does_not_move(self):
        recipe = self.recipe(self.mia_token)["id"]
        before = self.card(self.reader_token, recipe)["meal"]
        self.authenticate(self.reader_token)
        saved_id = self.save_meal(recipe).data["meal"]
        edited = self.client.patch(
            f"/api/v1/food/saved-meals/{saved_id}/",
            {
                "name": "My chicken bowl",
                "cooking_instructions": "Mine now.",
                "ingredients": [{"name": "Turkey", "servings": "1.00", "calories": "250.00",
                                 "protein_grams": "30.00", "carbohydrate_grams": "0.00",
                                 "fat_grams": "9.00", "position": 1}],
            },
            format="json",
        )
        self.assertEqual(edited.status_code, 200, edited.data)
        self.assertEqual(edited.data["name"], "My chicken bowl")
        self.assertEqual([row["name"] for row in edited.data["ingredients"]], ["Turkey"])
        self.assertEqual(self.card(self.reader_token, recipe)["meal"], before)
        # Still the copy of that post: saving again makes no second one.
        again = self.save_meal(recipe)
        self.assertEqual(again.status_code, 200)
        self.assertTrue(again.data["already_saved"])
        self.assertEqual(again.data["meal"], saved_id)

    def test_a_saved_workout_is_the_savers_to_edit_and_the_post_does_not_move(self):
        workout = self.workout(self.mia_token)["id"]
        before = self.card(self.reader_token, workout)["workout"]
        self.authenticate(self.reader_token)
        response = self.client.post(f"{POSTS}{workout}/save-workout/")
        self.assertEqual(response.status_code, 201, response.data)
        self.assert_matches_both_contracts(as_sent(response), "SavedWorkoutResult")
        template = WorkoutTemplate.objects.get(pk=response.data["workout"])
        self.assertEqual(template.owner, self.reader)
        renamed = self.client.patch(
            f"/api/v1/workouts/{template.pk}/", {"name": "My push day"}, format="json"
        )
        self.assertEqual(renamed.status_code, 200, renamed.data)
        self.assertEqual(self.card(self.reader_token, workout)["workout"], before)

    def test_saving_copies_no_media(self):
        recipe = self.recipe(
            self.mia_token, content_type="image/jpeg", image_base64=as_base64(photo_bytes()),
        )["id"]
        self.attach_clip(self.mia_token, recipe)
        stored = sorted(str(p.relative_to(self._media.name)) for p in Path(self._media.name).rglob("*") if p.is_file())
        self.authenticate(self.reader_token)
        self.assertEqual(self.save_meal(recipe).status_code, 201)
        after = sorted(str(p.relative_to(self._media.name)) for p in Path(self._media.name).rglob("*") if p.is_file())
        self.assertEqual(after, stored)
        self.assertTrue(stored, "the post's own media should be on disk")

    def test_a_save_outlives_the_post_it_came_from(self):
        recipe = self.recipe(self.mia_token)["id"]
        self.authenticate(self.reader_token)
        saved_id = self.save_meal(recipe).data["meal"]
        self.authenticate(self.mia_token)
        self.client.delete(f"{POSTS}{recipe}/")
        saved = SavedFoodMeal.objects.get(pk=saved_id)
        self.assertIsNone(saved.source_post_id)
        self.assertEqual(saved.ingredients.count(), 2)


# ---------------------------------------------------------------- migration


class MigrationCoverageTests(TestCase):
    def test_the_models_and_the_migrations_agree(self):
        call_command("makemigrations", "--check", "--dry-run", verbosity=0)

    def test_posts_from_before_are_given_what_new_posts_store(self):
        """Run the backfill over rows shaped like the ones 0089 leaves
        behind: every new column at its default."""
        from importlib import import_module

        backfill = import_module("core.migrations.0097_backfill_publication_summaries")
        user = get_user_model().objects.create_user(username="old", password="StrongPass!234")
        author = RepbaseUser.objects.create(user=user)
        lifting = workout_post(author, caption="Old #PR day #pr #2024")
        PostWorkoutExercise.objects.create(
            post_workout=lifting.workout, name="Squat", order=2, set_count=5,
            volume_kg=Decimal("2500.00"),
        )
        lunch = meal_post(author, caption="no tags here")
        PostMealEntry.objects.create(
            post_meal=lunch.meal, name="Beans", servings=Decimal("2.50"),
            calories=Decimal("120.33"), protein_grams=Decimal("7.10"),
        )
        empty = Post.objects.create(author=author, kind=Post.Kind.MEAL, caption="#empty")
        PostMeal.objects.create(post=empty, name="Nothing", date=lunch.meal.date)
        PostWorkout.objects.update(exercise_count=None, total_set_count=None, total_volume_kg=None)
        PostMeal.objects.update(
            entry_count=None, total_calories=0, total_protein_grams=0,
            total_carbohydrate_grams=0, total_fat_grams=0,
        )
        Post.objects.update(hashtags=[])
        PostHashtag.objects.all().delete()

        backfill.forwards(global_apps, None)
        backfill.forwards(global_apps, None)  # Safe to run twice.

        workout = PostWorkout.objects.get(post=lifting)
        self.assertEqual(
            (workout.exercise_count, workout.total_set_count, workout.total_volume_kg),
            (2, 8, Decimal("2500.00")),
        )
        meal = PostMeal.objects.get(post=lunch)
        self.assertEqual(meal.entry_count, 2)
        # 200 + 120.33 * 2.5 = 500.825, rounded half to even as the rendered
        # total is.
        self.assertEqual(meal.total_calories, Decimal("500.82"))
        self.assertEqual(meal.total_protein_grams, Decimal("17.75"))
        self.assertEqual(PostMeal.objects.get(post=empty).entry_count, 0)
        self.assertEqual(Post.objects.get(pk=lifting.pk).hashtags, ["pr"])
        self.assertEqual(Post.objects.get(pk=lunch.pk).hashtags, [])
        self.assertEqual(
            sorted(PostHashtag.objects.values_list("post_id", "tag")),
            sorted([(lifting.pk, "pr"), (empty.pk, "empty")]),
        )

    def test_the_backfill_and_the_live_code_read_hashtags_the_same_way(self):
        from importlib import import_module

        backfill = import_module("core.migrations.0097_backfill_publication_summaries")
        for caption in ("#A #a ＃Ａ", "x#y #z/#w", "#" + "q" * 60, " ".join(f"#n{i}" for i in range(30))):
            self.assertEqual(backfill.hashtags_in(caption), hashtags_in(caption))

    def test_the_previous_release_can_still_write_after_the_migration(self):
        """`deploy.sh` migrates, then restarts. In between -- or for good, if
        a check stops the deploy -- the code serving requests does not know
        the new columns and leaves them out of its INSERTs. Each one that is
        NOT NULL therefore needs its default in the database itself."""
        from django.db.models.sql import InsertQuery

        def insert_without(row, added):
            fields = [
                field for field in row._meta.concrete_fields
                if field.name not in added and not field.primary_key
            ]
            query = InsertQuery(type(row))
            query.insert_values(fields, [row])
            query.get_compiler(connection.alias).execute_sql()
            return type(row).objects.order_by("-pk").first()

        user = get_user_model().objects.create_user(username="older", password="StrongPass!234")
        author = RepbaseUser.objects.create(user=user)
        post = insert_without(
            Post(author=author, kind=Post.Kind.WORKOUT, caption="from the old code"),
            {"is_take", "source_post", "original_post", "attachment_version",
             "source_attachment_version", "sound", "hashtags", "topics", "language",
             "reuse_permission"},
        )
        self.assertEqual(post.caption, "from the old code")
        self.assertEqual(
            (post.is_take, post.attachment_version, post.hashtags, post.topics,
             post.language, post.reuse_permission),
            (False, 1, [], [], "", "everyone"),
        )
        workout = insert_without(
            PostWorkout(post=post, title="Old", performed_at=post.created_at),
            {"exercise_count", "total_set_count", "total_volume_kg"},
        )
        # Not added up, which is not the same as nothing to add.
        self.assertIsNone(workout.exercise_count)
        other = Post.objects.create(author=author, kind=Post.Kind.MEAL)
        meal = insert_without(
            PostMeal(post=other, name="Old", date=post.created_at.date()),
            {"entry_count", "total_calories", "total_protein_grams",
             "total_carbohydrate_grams", "total_fat_grams"},
        )
        self.assertIsNone(meal.entry_count)
        self.assertEqual(meal.total_calories, Decimal("0"))

    def test_a_post_from_before_reads_as_an_ordinary_post(self):
        """Every new column has a default that means "nothing special"."""
        user = get_user_model().objects.create_user(username="plain", password="StrongPass!234")
        post = Post.objects.create(author=RepbaseUser.objects.create(user=user), kind=Post.Kind.MEDIA)
        self.assertFalse(post.is_take)
        self.assertIsNone(post.source_post_id)
        self.assertIsNone(post.sound_id)
        self.assertEqual((post.hashtags, post.topics, post.language), ([], [], ""))
        self.assertEqual(post.attachment_version, 1)
        self.assertEqual(post.reuse_permission, "everyone")


@skipUnless(connection.vendor == "postgresql", "query plans are PostgreSQL's")
class IndexPlanTests(TestCase):
    """Each new index serves the query it was added for, in the order asked,
    without a sort. Sequential scans are switched off for the check: on a
    test table of a dozen rows the planner would rightly prefer one, and the
    question here is whether the index *can* answer the query."""

    @classmethod
    def setUpTestData(cls):
        user = get_user_model().objects.create_user(username="planner", password="StrongPass!234")
        cls.author = RepbaseUser.objects.create(user=user)
        cls.source = Post.objects.create(author=cls.author, kind=Post.Kind.MEDIA)
        cls.sound = Sound.objects.create(owner=cls.author, title="s")
        for n in range(30):
            post = Post.objects.create(
                author=cls.author, kind=Post.Kind.MEDIA, is_take=True,
                source_post=cls.source, original_post=cls.source, sound=cls.sound,
            )
            PostHashtag.objects.create(post=post, tag="legday")
        # Rows the queries must skip. With only matching rows, a planner with
        # fresh statistics rightly walks any index and filters, and which one
        # it picked depended on what the rest of the suite left behind.
        for n in range(300):
            other = Post.objects.create(author=cls.author, kind=Post.Kind.MEDIA)
            PostHashtag.objects.create(post=other, tag=f"other{n % 10}")

    def plan(self, queryset):
        with connection.cursor() as cursor:
            # Fresh statistics, as a long-lived table would have.
            cursor.execute("ANALYZE core_post, core_posthashtag")
            cursor.execute("SET LOCAL enable_seqscan = off")
            cursor.execute("SET LOCAL enable_bitmapscan = off")
        return queryset.explain()

    def assert_walks(self, plan, index):
        self.assertIn(index, plan)
        self.assertNotIn("Sort", plan)

    def test_takes_of_a_post_newest_first(self):
        takes = Post.objects.filter(source_post=self.source).order_by("-published_at", "-id")[:21]
        self.assert_walks(self.plan(takes), "post_takes_newest")

    def test_posts_using_a_sound_newest_first(self):
        uses = Post.objects.filter(sound=self.sound).order_by("-published_at", "-id")[:21]
        self.assert_walks(self.plan(uses), "post_sound_uses_newest")

    def test_posts_carrying_a_tag_newest_first(self):
        tagged = PostHashtag.objects.filter(tag="legday").order_by("-post_id")[:21]
        self.assert_walks(self.plan(tagged), "one_row_per_tag_and_post")

    def test_the_family_of_an_original(self):
        family = Post.objects.filter(original_post=self.source)
        self.assertIn("post_take_family", self.plan(family))
