"""The For You page measured on a simulated community, before and after.

Not a unit test of any one rule: a small world with readers whose tastes
are known, run through the real ranking, the real impression recording and
the real paging, so a change can be judged the way people would judge it --
were they shown what they like, were they shown it again and again, were
they shown things nobody should be shown.

The world (built once, identical for every arm):

- forty established creators across five topics, each post with a hidden
  "true" quality, and sixty community members whose likes, saves, views and
  scroll-pasts on those posts follow their own tastes and that quality;
- a promoter whose weak posts are liked by a ring of twenty accounts made
  this afternoon that like nothing else;
- a creator whose captions farm likes, and real people falling for it;
- a creator a moderator took a post down from this week, whose other posts
  are good and well liked;
- five creators who joined a few days ago, with good posts nobody has found.

Then twenty-four readers -- most with some history, some with nothing but
their onboarding answer, some with nothing at all -- each open For You,
page through three pages, act on what they see the way their taste says,
and come back for a second visit.

Two arms, on the same world and the same dice:

- **baseline**: every new part switched off -- raw counts, no rejection
  rates, no bait, exercise, fatigue, rewatch, followed-share or cold-start
  variety, the previous action values, and no moderation cooldown. Close to
  the ranking before `App-Algorithm`, not identical: the rules that are not
  weights (new accounts in trending and taste, rates from established viewers
  only, cleaned impressions, the separate exercise source) stay on;
- **current**: the defaults.

Run with `FEED_EVAL_REPORT=1` to print every figure for both arms, and with
`FEED_EVAL_ABLATION=1` to also run the current ranking with each new part
switched off in turn -- which is how to tell what a part is worth before
changing its weight:

    FEED_EVAL_REPORT=1 python manage.py test core.test_feed_evaluation
    FEED_EVAL_ABLATION=1 python manage.py test core.test_feed_evaluation
    FEED_EVAL_LEARNED=1 python manage.py test core.test_feed_evaluation

The last learns a ranking model from one round of the community using For
You, and compares a fresh round under the rules with one under the rules
plus that model.

The clock is frozen for each arm, so two runs of the same weights give the
same figures and any difference between arms is the weights'.
"""

import os
import random
import statistics
import time
import unittest
from contextlib import ExitStack
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from . import feed_integrity, recommendations
from .fixtures_for_tests import FAST_PASSWORDS, make_account, meal_post, workout_post
from .models import (
    Exercise,
    Follow,
    Personalization,
    Post,
    PostComment,
    PostFeedback,
    PostLike,
    PostView,
    PostWorkoutExercise,
    RepbaseUser,
    WorkoutTemplate,
)

TOPICS = ("running", "lifting", "biking", "swimming", "meal")
EXERCISES = ("Deadlift", "Back Squat", "Bench Press", "Overhead Press")
ONBOARDING = {"running": "Running", "lifting": "Strength", "biking": "Cycling", "swimming": "Swimming"}

#: The weights that reproduce the ranking before this branch.
BASELINE = {
    "trust": 0.0, "skip_rate": 0.0, "hide_rate": 0.0, "bait": 0.0, "detail": 0.0,
    "fatigue": 0.0, "rewatch": 0.0, "connected_share": 1.0, "cold_variety": 0.0,
    "like_value": 1.0, "comment_value": 2.0, "save_value": 3.0, "share_value": 2.0,
}

PAGES = 3
PAGE_SIZE = 10


def _days_ago(days, hours=0):
    return timezone.now() - timedelta(days=days, hours=hours)


def _age_account(profile, days):
    RepbaseUser.objects.filter(pk=profile.pk).update(created_at=_days_ago(days))


def _post(author, topic, rng, *, caption="", hours_ago=24, exercise=None):
    if topic == "meal":
        post = meal_post(author, caption=caption or "meal prep", hours_ago=hours_ago)
    else:
        post = workout_post(
            author, topic, caption=caption or f"{topic} today", hours_ago=hours_ago,
            exercise=exercise or ("Bench Press" if topic == "lifting" else "Easy Pace"),
        )
        if topic == "lifting" and exercise:
            PostWorkoutExercise.objects.filter(post_workout=post.workout).update(set_count=5)
    return post


def _topic_of(post):
    return "meal" if post.kind == Post.Kind.MEAL else post.workout.workout_type


@FAST_PASSWORDS
class SimulatedCommunityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        rng = random.Random(2026)
        #: Every arm runs at this moment, so freshness and decay are the same
        #: for both and only the weights differ.
        cls.now = timezone.now() + timedelta(minutes=5)
        for name in EXERCISES:
            Exercise.objects.create(name=name, created_by=None)

        cls.truth = {}          # post id -> (topic, exercise or None, quality, kind of post)
        cls.creators = []
        for index in range(40):
            creator = make_account(f"creator{index}")[1]
            _age_account(creator, 30 + index % 20)
            topic = TOPICS[index % len(TOPICS)]
            favourite = EXERCISES[index % len(EXERCISES)] if topic == "lifting" else None
            cls.creators.append((creator, topic))
            for number in range(5):
                post = _post(creator, topic, rng, hours_ago=6 + rng.randrange(240), exercise=favourite)
                cls.truth[post.pk] = (topic, favourite, round(rng.uniform(0.25, 1.0), 2), "organic")

        cls.promoter = make_account("promoter")[1]
        _age_account(cls.promoter, 60)
        cls.ring_posts = [
            _post(cls.promoter, "meal", rng, caption="My recipe", hours_ago=4 + n) for n in range(6)
        ]
        for post in cls.ring_posts:
            cls.truth[post.pk] = ("meal", None, 0.08, "ring")
        cls.puppets = [make_account(f"puppet{n}")[1] for n in range(20)]

        cls.baiter = make_account("baiter")[1]
        _age_account(cls.baiter, 45)
        cls.bait_posts = [
            _post(cls.baiter, topic, rng, caption="Like this if you trained today! Tag a friend", hours_ago=8 + n)
            for n, topic in enumerate(("running", "lifting", "meal"))
        ]
        for post in cls.bait_posts:
            cls.truth[post.pk] = (_topic_of(post), None, 0.3, "bait")

        cls.offender = make_account("offender")[1]
        _age_account(cls.offender, 90)
        taken_down = _post(cls.offender, "running", rng, hours_ago=30)
        Post.objects.filter(pk=taken_down.pk).update(is_hidden=True, hidden_at=_days_ago(3))
        cls.offender_posts = [_post(cls.offender, "running", rng, hours_ago=10 + n) for n in range(4)]
        for post in cls.offender_posts:
            cls.truth[post.pk] = ("running", None, 0.9, "moderated")

        cls.newcomers = []
        for index in range(5):
            person = make_account(f"newcomer{index}")[1]
            _age_account(person, 5)
            topic = TOPICS[index % len(TOPICS)]
            cls.newcomers.append(person)
            for number in range(2):
                post = _post(person, topic, rng, hours_ago=12 + number * 5)
                cls.truth[post.pk] = (topic, None, 0.85, "newcomer")

        # The community: sixty members with two favourite topics each,
        # whose engagement is what every post's standing is made of.
        members = []
        for index in range(60):
            member = make_account(f"member{index}")[1]
            _age_account(member, 20 + index % 30)
            members.append((member, set(rng.sample(TOPICS, 2))))
        cls.members = members
        likes, views, comments = [], [], []
        templates = []
        for post in Post.objects.exclude(kind=Post.Kind.REPOST).select_related("workout"):
            topic, _, quality, kind = cls.truth.get(post.pk, (None, None, 0, None))
            if kind is None or kind == "newcomer":
                continue
            audience = rng.sample(members, 14 if kind != "bait" else 20)
            for member, favourites in audience:
                match = 1.0 if topic in favourites else 0.2
                appeal = quality * match
                if kind == "bait":
                    appeal = 0.55 * match  # bait works on people, which is the problem
                liked = rng.random() < appeal * 0.7
                saved = kind == "organic" and post.kind == Post.Kind.WORKOUT and rng.random() < appeal * 0.15
                skipped = not liked and rng.random() > appeal
                views.append(PostView(
                    viewer=member, post=post, view_count=1,
                    total_dwell_ms=600 if skipped else int(2000 + appeal * 6000),
                    skipped=skipped, surface=PostView.Surface.FOR_YOU,
                    last_seen_at=_days_ago(1, rng.randrange(20)),
                ))
                if liked:
                    likes.append(PostLike(post=post, user=member))
                if saved:
                    templates.append(WorkoutTemplate(owner=member, name=f"saved {post.pk}", source_post=post))
        for post in cls.ring_posts:
            for puppet in cls.puppets:
                likes.append(PostLike(post=post, user=puppet))
                comments.append(PostComment(post=post, author=puppet, body="amazing!!"))
            for reply in range(10):
                comments.append(PostComment(post=post, author=cls.promoter, body="thank you!"))
        PostLike.objects.bulk_create(likes, ignore_conflicts=True)
        PostView.objects.bulk_create(views, ignore_conflicts=True)
        PostComment.objects.bulk_create(comments)
        WorkoutTemplate.objects.bulk_create(templates, ignore_conflicts=True)

        # The readers. Each has one or two favourite topics, a lifter a
        # favourite exercise, and one topic they would rather not see.
        cls.readers = []
        for index in range(24):
            profile, token = make_account(f"reader{index}")[1:]
            _age_account(profile, 15)
            favourites = set(rng.sample(TOPICS, 1 if index % 3 else 2))
            disliked = rng.choice([topic for topic in TOPICS if topic not in favourites])
            exercise = rng.choice(EXERCISES) if "lifting" in favourites else None
            if index < 16:
                kind = "warm"
                liked = [
                    pk for pk, (topic, ex, quality, origin) in sorted(cls.truth.items())
                    if origin == "organic" and topic in favourites and (exercise is None or ex in (None, exercise))
                ]
                for pk in rng.sample(liked, min(3, len(liked))):
                    PostLike.objects.create(post_id=pk, user=profile)
                favourite_creator = next(c for c, topic in cls.creators if topic in favourites)
                Follow.objects.create(follower=profile, following=favourite_creator)
            elif index < 20:
                kind = "onboarded"
                answers = [ONBOARDING[topic] for topic in sorted(favourites) if topic in ONBOARDING]
                Personalization.objects.create(
                    repbase_user=profile, training_types=answers,
                    emphasis="Nutrition" if "meal" in favourites else "",
                )
            else:
                kind = "blank"
            cls.readers.append({
                "profile": profile, "favourites": favourites, "disliked": disliked,
                "exercise": exercise, "kind": kind,
            })

    # ------------------------------------------------------------- one arm

    def utility(self, reader, post_id):
        """How much this reader actually wants this post, 0-1."""
        topic, exercise, quality, origin = self.truth.get(post_id, (None, None, 0.0, None))
        if topic is None:
            return 0.0
        match = 1.0 if topic in reader["favourites"] else (0.0 if topic == reader["disliked"] else 0.25)
        if reader["exercise"] and exercise == reader["exercise"]:
            match = min(1.0, match * 1.3)
        return round(quality * match, 4)

    def run_arm(self, overrides, moderation_days, new_account_hours):
        metrics = {
            "utility": [], "page1_utility_cold": [], "favourite_share": [], "integrity": 0,
            "ring": 0, "bait": 0, "moderated": 0, "newcomer": 0, "served": 0, "unconnected": 0,
            "distinct_authors": [], "max_author_share": [], "topics_on_page1_blank": [],
            "repeat_second_visit": [], "dismissed_topic": 0, "likes": 0, "integrity_top3": 0,
            "latency_first": [], "latency_next": [],
        }
        authors = dict(Post.objects.values_list("pk", "author_id"))
        with ExitStack() as stack:
            stack.enter_context(override_settings(FOR_YOU_WEIGHTS=overrides))
            stack.enter_context(mock.patch.object(feed_integrity, "MODERATION_COOLDOWN_DAYS", moderation_days))
            stack.enter_context(mock.patch.object(feed_integrity, "NEW_ACCOUNT_HOURS", new_account_hours))
            seeds = iter(range(10_000, 20_000))
            stack.enter_context(mock.patch.object(recommendations, "new_seed", side_effect=lambda: next(seeds)))
            stack.enter_context(mock.patch("django.utils.timezone.now", return_value=self.now))
            cache.clear()
            for number, reader in enumerate(self.readers):
                dice = random.Random(number)
                profile = reader["profile"]
                followed = set(Follow.objects.filter(follower=profile).values_list("following_id", flat=True))
                first_visit = []
                for visit in range(2):
                    served = []
                    session, offset = None, 0
                    for page_number in range(PAGES):
                        started = time.perf_counter()
                        page = recommendations.serve_page(profile, session, offset, PAGE_SIZE)
                        elapsed = (time.perf_counter() - started) * 1000
                        metrics["latency_first" if page_number == 0 else "latency_next"].append(elapsed)
                        ids = [item.pk for item in page.items]
                        served += ids
                        events = []
                        for pk in ids:
                            want = self.utility(reader, pk)
                            topic = self.truth.get(pk, (None,))[0]
                            if topic == reader["disliked"] and dice.random() < 0.5:
                                PostFeedback.objects.get_or_create(viewer=profile, post_id=pk)
                                events.append({"post": pk, "dwell_ms": 800, "surface": "for_you"})
                                continue
                            if dice.random() < want * 0.8:
                                PostLike.objects.get_or_create(post_id=pk, user=profile)
                                metrics["likes"] += 1
                                events.append({"post": pk, "dwell_ms": 6000, "surface": "for_you"})
                            elif want < 0.15:
                                events.append({"post": pk, "dwell_ms": 700, "surface": "for_you"})
                            else:
                                events.append({"post": pk, "dwell_ms": 2500, "surface": "for_you"})
                        if events:
                            recommendations.record_impressions(profile, events)
                        if page.next_offset is None:
                            break
                        session, offset = page.session, page.next_offset

                    if visit == 0:
                        first_visit = served
                        page_one = served[:PAGE_SIZE]
                        if reader["kind"] != "warm":
                            metrics["page1_utility_cold"] += [self.utility(reader, pk) for pk in page_one]
                        if reader["kind"] == "blank":
                            metrics["topics_on_page1_blank"].append(
                                len({self.truth[pk][0] for pk in page_one if pk in self.truth})
                            )
                        metrics["integrity_top3"] += sum(
                            self.truth.get(pk, (None, None, None, None))[3] in ("ring", "bait", "moderated")
                            for pk in page_one[:3]
                        )
                        metrics["favourite_share"].append(
                            sum(self.truth.get(pk, (None,))[0] in reader["favourites"] for pk in page_one)
                            / max(len(page_one), 1)
                        )
                        for pk in served:
                            metrics["utility"].append(self.utility(reader, pk))
                            origin = self.truth.get(pk, (None, None, None, None))[3]
                            metrics["served"] += 1
                            if origin in ("ring", "bait", "moderated"):
                                metrics["integrity"] += 1
                                metrics[origin] += 1
                            if origin == "newcomer":
                                metrics["newcomer"] += 1
                            if authors[pk] not in followed:
                                metrics["unconnected"] += 1
                            if topic_is(self.truth, pk, reader["disliked"]):
                                metrics["dismissed_topic"] += 1
                        by_author = {}
                        for pk in served:
                            by_author[authors[pk]] = by_author.get(authors[pk], 0) + 1
                        metrics["distinct_authors"].append(len(by_author))
                        metrics["max_author_share"].append(max(by_author.values()) / len(served))
                    else:
                        repeated = len(set(served[:PAGE_SIZE * 2]) & set(first_visit))
                        metrics["repeat_second_visit"].append(repeated / max(len(served[:PAGE_SIZE * 2]), 1))
        served = max(metrics["served"], 1)
        return {
            "mean_utility": round(statistics.mean(metrics["utility"]), 4),
            "favourite_topic_share_page1": round(statistics.mean(metrics["favourite_share"]), 4),
            "cold_start_utility_page1": round(statistics.mean(metrics["page1_utility_cold"]), 4),
            "blank_reader_topics_page1": round(statistics.mean(metrics["topics_on_page1_blank"]), 2),
            "integrity_exposure": round(metrics["integrity"] / served, 4),
            "integrity_in_top_three": metrics["integrity_top3"],
            "ring_exposure": round(metrics["ring"] / served, 4),
            "bait_exposure": round(metrics["bait"] / served, 4),
            "moderated_exposure": round(metrics["moderated"] / served, 4),
            "newcomer_share": round(metrics["newcomer"] / served, 4),
            "unconnected_share": round(metrics["unconnected"] / served, 4),
            "disliked_topic_share": round(metrics["dismissed_topic"] / served, 4),
            "distinct_authors_per_30": round(statistics.mean(metrics["distinct_authors"]), 2),
            "max_author_share": round(statistics.mean(metrics["max_author_share"]), 4),
            "second_visit_repeat_rate": round(statistics.mean(metrics["repeat_second_visit"]), 4),
            "simulated_likes": metrics["likes"],
            "first_page_ms_p50": round(statistics.median(metrics["latency_first"]), 1),
            "next_page_ms_p50": round(statistics.median(metrics["latency_next"]), 1),
        }

    def run_both(self):
        results = {}
        for name, overrides, moderation_days, new_account_hours in (
            ("baseline", BASELINE, 0, 0),
            ("current", {}, feed_integrity.MODERATION_COOLDOWN_DAYS, feed_integrity.NEW_ACCOUNT_HOURS),
        ):
            with transaction.atomic():
                results[name] = self.run_arm(overrides, moderation_days, new_account_hours)
                transaction.set_rollback(True)
        if os.environ.get("FEED_EVAL_REPORT"):
            width = max(len(key) for key in results["current"])
            print("\n" + "metric".ljust(width), "baseline".rjust(10), "current".rjust(10))
            for key in results["current"]:
                print(key.ljust(width), str(results["baseline"][key]).rjust(10), str(results["current"][key]).rjust(10))
        return results

    # ----------------------------------------------------------- the claims

    def test_the_new_ranking_is_better_where_it_should_be_and_no_worse_elsewhere(self):
        results = self.run_both()
        before, after = results["baseline"], results["current"]
        # The ring buys nothing: what is left of it is what any weak fresh
        # post gets, never the top of anybody's page. The moderated author is
        # for their followers only, and bait is mostly gone.
        self.assertLessEqual(after["ring_exposure"], before["ring_exposure"] / 2)
        self.assertLessEqual(after["bait_exposure"], before["bait_exposure"] / 4)
        self.assertEqual(after["moderated_exposure"], 0.0)
        self.assertLess(after["integrity_exposure"], before["integrity_exposure"] / 3)
        self.assertEqual(after["integrity_in_top_three"], 0)
        # And what people actually want is not given up for it.
        self.assertGreaterEqual(after["mean_utility"], before["mean_utility"] - 0.01)
        self.assertGreaterEqual(after["cold_start_utility_page1"], before["cold_start_utility_page1"] - 0.02)
        # For You is mostly people the reader does not follow yet.
        self.assertGreaterEqual(after["unconnected_share"], 0.6)
        # A page of thirty is spread across many creators.
        self.assertGreaterEqual(after["distinct_authors_per_30"], 20)
        self.assertLessEqual(after["max_author_share"], 0.12)
        # Somebody who said nothing yet sees at least as many topics.
        self.assertGreaterEqual(after["blank_reader_topics_page1"], before["blank_reader_topics_page1"])


    @unittest.skipUnless(os.environ.get("FEED_EVAL_ABLATION"), "set FEED_EVAL_ABLATION=1 to run")
    def test_what_each_part_is_worth(self):
        """Not a claim, a measurement: the current ranking with one new part
        switched off at a time."""
        keys = (
            "mean_utility", "cold_start_utility_page1", "integrity_exposure", "disliked_topic_share",
            "newcomer_share", "distinct_authors_per_30", "max_author_share",
        )
        arms = [("current", {})] + [
            (f"without {name}", overrides) for name, overrides in (
                ("trust", {"trust": 0.0}),
                ("rejection rates", {"skip_rate": 0.0, "hide_rate": 0.0}),
                ("bait", {"bait": 0.0}),
                ("detail", {"detail": 0.0}),
                ("fatigue", {"fatigue": 0.0}),
                ("rewatch", {"rewatch": 0.0}),
                ("followed share", {"connected_share": 1.0}),
                ("cold variety", {"cold_variety": 0.0}),
                ("new action values", {key: BASELINE[key] for key in BASELINE if key.endswith("_value")}),
            )
        ]
        print("\n" + "arm".ljust(26), *[key[:16].rjust(17) for key in keys])
        for name, overrides in arms:
            with transaction.atomic():
                result = self.run_arm(
                    overrides, feed_integrity.MODERATION_COOLDOWN_DAYS, feed_integrity.NEW_ACCOUNT_HOURS
                )
                transaction.set_rollback(True)
            print(name.ljust(26), *[str(result[key]).rjust(17) for key in keys])


    @unittest.skipUnless(os.environ.get("FEED_EVAL_LEARNED"), "set FEED_EVAL_LEARNED=1 to run")
    def test_a_learned_model_earns_its_place(self):
        """Learn from one round of the community using For You under the
        rules, then compare a fresh round under the rules with a fresh round
        under rules plus the model. Both rounds start from the same world."""
        from . import ranking_model
        from .models import RankingModel

        limits = {"MIN_TRAIN": 300, "MIN_POSITIVES": 30, "MIN_TEST": 50, "MIN_TEST_POSITIVES": 5}
        with ExitStack() as stack:
            for name, value in limits.items():
                stack.enter_context(mock.patch.object(ranking_model, name, value))
            stack.enter_context(mock.patch.object(ranking_model, "LABEL_DELAY", timedelta(0)))
            stack.enter_context(mock.patch("django.utils.timezone.now", return_value=self.now))
            with transaction.atomic():
                self.run_arm({"learned": 0}, feed_integrity.MODERATION_COOLDOWN_DAYS, feed_integrity.NEW_ACCOUNT_HOURS)
                model = ranking_model.train()
                learned = {
                    field: getattr(model, field) for field in (
                        "features", "means", "scales", "engage_weights", "engage_bias",
                        "reject_weights", "reject_bias", "metrics",
                    )
                }
                transaction.set_rollback(True)
        results = {}
        for name, overrides, with_model in (("rules", {"learned": 0}, False), ("rules + model", {}, True)):
            with transaction.atomic():
                if with_model:
                    RankingModel.objects.create(is_active=True, **learned)
                cache.clear()
                results[name] = self.run_arm(
                    overrides, feed_integrity.MODERATION_COOLDOWN_DAYS, feed_integrity.NEW_ACCOUNT_HOURS
                )
                transaction.set_rollback(True)
        keys = ("mean_utility", "cold_start_utility_page1", "integrity_exposure", "disliked_topic_share",
                "distinct_authors_per_30", "first_page_ms_p50")
        print("\nmodel:", {key: learned["metrics"].get(key) for key in (
            "train", "test", "auc_engage", "auc_turned_down", "rules_auc", "model_auc", "gate")})
        print("arm".ljust(16), *[key[:16].rjust(17) for key in keys])
        for name, result in results.items():
            print(name.ljust(16), *[str(result[key]).rjust(17) for key in keys])
        self.assertGreaterEqual(results["rules + model"]["mean_utility"], results["rules"]["mean_utility"] - 0.01)
        self.assertLessEqual(results["rules + model"]["integrity_exposure"], results["rules"]["integrity_exposure"] + 0.01)


def topic_is(truth, pk, topic):
    return truth.get(pk, (None,))[0] == topic
