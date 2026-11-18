"""Milestone 9: the learned ranker reads measured outcomes, is judged on
readers and a period it has not seen, and has more to answer for than a
better prediction score.

What it cannot test is the thing the milestone is for: whether a model
trained on a real beta's measurements improves the page. These hold the
machinery to its rules so that question can be asked honestly later.
"""

import io
import json
import random
import uuid
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from . import experiment_stats, ranking_model
from .fixtures_for_tests import make_account
from .models import (
    PostComment, PostFeedback, PostLike, PostVideo, PostView, RankingModel, RepbaseUser,
)
from .ranking_model import FEATURE_VERSION, FEATURES, Exposure, feature_row, judge, serving_context
from .test_recommendations import IMPRESSIONS, ForYouTestCase

NOW = timezone.now()


def exposure(**changes):
    values = dict(
        viewer=1, author=1, features=[0.0] * (len(FEATURES) + 1), when=NOW, format="workout:still",
        clip_ms=None, position=0, exploration=False, satisfied=False, saved=False, conversed=False,
        approved=False, rejected=False, skipped=False,
    )
    values.update(changes)
    return Exposure(**values)


class OutcomeTests(SimpleTestCase):
    def test_each_good_outcome_is_its_own_and_any_makes_it_positive(self):
        for name in ("satisfied", "saved", "conversed", "approved"):
            one = exposure(**{name: True})
            self.assertTrue(one.positive, name)
            self.assertFalse(one.negative, name)

    def test_explicit_rejection_and_a_scroll_past_are_kept_apart(self):
        said_no, scrolled = exposure(rejected=True), exposure(skipped=True)

        self.assertTrue(said_no.negative and scrolled.negative)
        self.assertTrue(said_no.rejected and not scrolled.rejected)

    def test_doing_nothing_is_neither(self):
        nothing = exposure()

        self.assertFalse(nothing.positive or nothing.negative)

    def test_a_good_outcome_outweighs_a_scroll_past(self):
        self.assertFalse(exposure(skipped=True, saved=True).negative)


class ServingContextTests(ForYouTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()

    def summary(self, post_id, **figures):
        return {
            "post": post_id, "surface": "for_you", "sequence": 1,
            "observed_at": timezone.now().isoformat(), "dwell_ms": 5000, "active_dwell_ms": 5000,
            **figures,
        }

    def send(self, events):
        return self.client.post(IMPRESSIONS, {
            "schema_version": 2, "batch_id": str(uuid.uuid4()), "session_id": str(uuid.uuid4()),
            "events": events,
        }, format="json")

    def test_what_was_served_is_recorded_with_how_it_came_to_be(self):
        profile, token = self.reader("recorded", ["Running"])
        page = self.ids(self.feed(token, page_size=5))

        self.send([self.summary(page[2])])

        served = PostView.objects.get(viewer=profile, post_id=page[2]).served
        self.assertEqual(served["v"], FEATURE_VERSION)
        self.assertEqual(served["algo"], "control")
        self.assertEqual(served["pos"], 2)
        self.assertIsNone(served["model"])
        self.assertIsInstance(served["explore"], bool)
        self.assertTrue(served["src"])
        self.assertTrue(served["format"])

    @override_settings(FOR_YOU_EXPERIMENT={"name": "x", "variants": {"control": {}, "fresher": {"recency": 1.6}}})
    def test_the_arm_that_served_it_is_named(self):
        profile, token = self.reader("armed", ["Running"])
        page = self.ids(self.feed(token, page_size=3))

        self.send([self.summary(page[0])])

        served = PostView.objects.get(viewer=profile, post_id=page[0]).served
        self.assertIn(served["algo"], ("control", "fresher"))
        self.assertEqual(served["algo"], ranking_model_variant(profile))

    def test_it_is_written_before_outcomes_and_not_rewritten_by_them(self):
        profile, token = self.reader("ordered", ["Running"])
        first = self.ids(self.feed(token, page_size=5))
        self.send([self.summary(first[0])])
        before = PostView.objects.get(viewer=profile, post_id=first[0]).served
        # They like it, the page is ranked again, and it is seen again lower.
        PostLike.objects.create(post_id=first[0], user=profile)
        self.feed(token, page_size=5)

        self.send([self.summary(first[0])])

        self.assertEqual(PostView.objects.get(viewer=profile, post_id=first[0]).served, before)

    def test_the_old_events_record_it_too(self):
        profile, token = self.reader("old-app", ["Running"])
        page = self.ids(self.feed(token, page_size=3))

        self.client.post(IMPRESSIONS, {"events": [
            {"post": page[1], "dwell_ms": 4000, "surface": "for_you"},
        ]}, format="json")

        self.assertEqual(PostView.objects.get(viewer=profile, post_id=page[1]).served["pos"], 1)

    def test_other_surfaces_and_unsampled_exposures_record_none(self):
        profile, token = self.reader("elsewhere", ["Running"])
        page = self.ids(self.feed(token, page_size=3))
        self.send([self.summary(page[0], surface="following")])
        with override_settings(FOR_YOU_TRAINING_SAMPLE_RATE=0):
            self.send([self.summary(page[1])])

        for post_id in page[:2]:
            row = PostView.objects.get(viewer=profile, post_id=post_id)
            self.assertIsNone(row.features)
            self.assertIsNone(row.served)

    def test_it_is_cleared_with_the_sample_and_names_nothing_private(self):
        profile, token = self.reader("cleared", ["Running"])
        page = self.ids(self.feed(token, page_size=3))
        self.send([self.summary(page[0])])
        row = PostView.objects.get(viewer=profile, post_id=page[0])
        self.assertEqual(
            set(row.served), {"v", "algo", "model", "pos", "src", "explore", "format"}
        )
        PostView.objects.filter(pk=row.pk).update(first_seen_at=timezone.now() - timedelta(days=15))

        call_command("prune_expired_rows", stdout=io.StringIO())

        row.refresh_from_db()
        self.assertIsNone(row.features)
        self.assertIsNone(row.served)


def ranking_model_variant(profile):
    from .recommendations import FeedWeights

    return FeedWeights.for_viewer(profile.pk)[1]


class MeasuredWorld(ForYouTestCase):
    """Forty readers who are satisfied by `quality`, measured by the device,
    over ten days. The rules' own total says nothing about it."""

    #: Overridden to build a world where the model's top is worse somehow.
    seed = 9

    @classmethod
    def row(cls, viewer, post, position, raw, when, **figures):
        measured = {"qualified_views": 1, "active_dwell_ms": 2000, **figures}
        return PostView(
            viewer=viewer, post=post, view_count=1, surface=PostView.Surface.FOR_YOU,
            features=feature_row(raw, position=position % 10),
            served=serving_context(position=position % 10, sources={"interest"}, post_format=cls.format_of(post)),
            last_observed_at=when, last_seen_at=when, **measured,
        )

    @classmethod
    def format_of(cls, post):
        return "meal:still" if post in cls.meals else "workout:still"

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.viewers = [make_account(f"measured{n}")[1] for n in range(40)]
        RepbaseUser.objects.filter(pk__in=[v.pk for v in cls.viewers]).update(
            created_at=timezone.now() - timedelta(days=30)
        )
        dice = random.Random(cls.seed)
        posts = cls.running + cls.lifting + cls.meals
        views, moments = [], {}
        for viewer in cls.viewers:
            for position, post in enumerate(posts):
                raw = {name: dice.random() for name in ranking_model.SCORE_FEATURES}
                raw["rules"] = dice.gauss(0, 1)
                satisfied = raw["quality"] + dice.gauss(0, 0.15) > 0.7
                skipped = not satisfied and raw["seen"] > 0.4
                when = timezone.now() - timedelta(hours=3 + dice.random() * 240)
                views.append(cls.row(
                    viewer, post, position, raw, when,
                    active_dwell_ms=8000 if satisfied else 1500, skipped=skipped,
                ))
                moments[(viewer.pk, post.pk)] = when
        PostView.objects.bulk_create(views)
        for row in PostView.objects.filter(viewer__in=cls.viewers):
            PostView.objects.filter(pk=row.pk).update(first_seen_at=moments[(row.viewer_id, row.post_id)])

    def setUp(self):
        super().setUp()
        cache.clear()
        for name, value in (
            ("MIN_TRAIN", 200), ("MIN_POSITIVES", 30), ("MIN_TEST", 30), ("MIN_TEST_POSITIVES", 5),
            ("MIN_FORMAT_DECIDED", 10),
        ):
            patcher = mock.patch.object(ranking_model, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class MeasuredLabelTests(MeasuredWorld):
    def one(self, **figures):
        """A fresh measured exposure for a new reader, as `measured_impressions` reads it."""
        _, viewer, _ = make_account(f"one{uuid.uuid4().hex[:8]}")
        post = figures.pop("post", self.running[0])
        raw = {name: 0.5 for name in ranking_model.SCORE_FEATURES} | {"rules": 0.0}
        when = timezone.now() - timedelta(hours=2)
        row = self.row(viewer, post, 0, raw, when, **figures)
        row.save()
        PostView.objects.filter(pk=row.pk).update(first_seen_at=when)
        found = [e for e in ranking_model.measured_impressions() if e.viewer == viewer.pk]
        return viewer, (found[0] if found else None)

    def test_reading_long_enough_is_satisfied_and_a_glance_is_not(self):
        self.assertTrue(self.one(active_dwell_ms=6000)[1].satisfied)
        self.assertFalse(self.one(active_dwell_ms=2000)[1].satisfied)

    def test_a_clip_is_judged_against_its_own_length(self):
        PostVideo.objects.create(
            post=self.lifting[0], file="post-videos/long.mp4", content_type="video/mp4", codec="avc1",
            duration_ms=60_000, width=64, height=48, size_bytes=10, status=PostVideo.Status.APPROVED,
        )
        # Twenty seconds on screen is a long read of a photo and a third of this clip.
        _, part = self.one(post=self.lifting[0], active_dwell_ms=20_000, playback_ms=20_000, coverage_ms=20_000)
        _, whole = self.one(
            post=self.lifting[0], active_dwell_ms=58_000, playback_ms=58_000, coverage_ms=58_000,
            watched_through=True,
        )

        self.assertFalse(part.satisfied)
        self.assertTrue(whole.satisfied)
        self.assertEqual(whole.clip_ms, 60_000)

    def test_reading_the_workout_or_recipe_is_satisfied(self):
        self.assertTrue(self.one(attachment_opens=1, attachment_read_ms=4000)[1].satisfied)

    def test_a_post_never_properly_seen_is_not_a_rejection(self):
        self.assertIsNone(self.one(qualified_views=0, skipped=True)[1])

    def test_but_saying_no_to_it_still_counts(self):
        _, viewer, _ = make_account("said-no")
        raw = {name: 0.5 for name in ranking_model.SCORE_FEATURES} | {"rules": 0.0}
        when = timezone.now() - timedelta(hours=2)
        row = self.row(viewer, self.running[0], 0, raw, when, qualified_views=0)
        row.save()
        PostView.objects.filter(pk=row.pk).update(first_seen_at=when)
        PostFeedback.objects.create(viewer=viewer, post=self.running[0])

        found = [e for e in ranking_model.measured_impressions() if e.viewer == viewer.pk]

        self.assertTrue(found[0].rejected and found[0].negative)

    def test_a_clip_that_would_not_play_is_not_a_verdict(self):
        self.assertIsNone(self.one(playback_failures=1, skipped=True)[1])

    def test_a_hidden_comment_is_not_conversation(self):
        viewer, _ = self.one()
        comment = PostComment.objects.create(post=self.running[0], author=viewer, body="nice")

        def conversed():
            return [e for e in ranking_model.measured_impressions() if e.viewer == viewer.pk][0].conversed

        self.assertTrue(conversed())
        PostComment.objects.filter(pk=comment.pk).update(is_hidden=True)
        self.assertFalse(conversed())

    def test_legacy_rows_and_other_feature_versions_are_never_mixed_in(self):
        _, legacy, _ = make_account("legacy-app")
        _, other, _ = make_account("other-version")
        raw = {name: 0.5 for name in ranking_model.SCORE_FEATURES} | {"rules": 0.0}
        when = timezone.now() - timedelta(hours=2)
        # An app that does not measure: position only, no observation.
        old = PostView.objects.create(
            viewer=legacy, post=self.running[0], view_count=1, surface=PostView.Surface.FOR_YOU,
            features=feature_row(raw), served=serving_context(), max_watch_ms=9000, completed=True,
        )
        newer = self.row(other, self.running[0], 0, raw, when)
        newer.served = {**newer.served, "v": FEATURE_VERSION + 1}
        newer.save()
        PostView.objects.filter(pk__in=[old.pk, newer.pk]).update(first_seen_at=when)

        viewers = {e.viewer for e in ranking_model.measured_impressions()}

        self.assertFalse({legacy.pk, other.pk} & viewers)
        # And the legacy path still reads its own.
        self.assertIn(legacy.pk, {sample[0] for sample in ranking_model.labelled_impressions()})


class MeasuredTrainingTests(MeasuredWorld):
    def test_it_learns_from_the_earlier_part_and_is_tested_on_unseen_readers_later(self):
        exposures = ranking_model.measured_impressions()
        cut = ranking_model._time_cut(exposures)

        model = ranking_model.train(labels="measured")

        metrics = model.metrics
        earlier = [e for e in exposures if not ranking_model._held_out(e.viewer) and e.when < cut]
        later = [e for e in exposures if ranking_model._held_out(e.viewer) and e.when >= cut]
        self.assertEqual((metrics["train"], metrics["test"]), (len(earlier), len(later)))
        self.assertLess(metrics["train"] + metrics["test"], metrics["samples"])
        self.assertEqual(
            (metrics["labels"], metrics["label_version"], metrics["feature_version"]),
            ("measured", 2, FEATURE_VERSION),
        )

    def test_a_model_that_beats_the_rules_without_cost_becomes_active(self):
        model = ranking_model.train(labels="measured")

        self.assertTrue(model.is_active, model.metrics["gate"])
        self.assertGreater(model.metrics["model_auc"], model.metrics["rules_auc"] + 0.1)
        quality = model.engage_weights[FEATURES.index("quality")]
        self.assertEqual(max(model.engage_weights, key=abs), quality)
        judged = model.metrics["measured"]
        self.assertEqual(set(judged["formats"]), {"meal:still", "workout:still"})
        self.assertGreaterEqual(judged["top_model"]["satisfied_rate"], judged["top_rules"]["satisfied_rate"])

    def test_the_default_is_still_the_legacy_labels(self):
        self.assertEqual(ranking_model.label_source(), "legacy")
        with override_settings(FOR_YOU_RANKER_LABELS="everything"):
            self.assertEqual(ranking_model.label_source(), "legacy")
        # Read the legacy way, this world has no outcome to learn from: the
        # measured figures are not borrowed to make one.
        self.assertIsNone(ranking_model.train())

    @override_settings(FOR_YOU_RANKER_LABELS="measured")
    def test_the_setting_and_the_command_choose(self):
        out = io.StringIO()

        call_command("train_ranking_model", "--no-activate", stdout=out)

        report = json.loads(out.getvalue())
        self.assertEqual(report["labels"], "measured")
        self.assertFalse(report["active"])
        self.assertNotIn("measured0", out.getvalue())

    def test_too_few_of_a_format_and_it_is_not_promoted(self):
        with mock.patch.object(ranking_model, "MIN_FORMAT_DECIDED", 10_000):
            model = ranking_model.train(labels="measured")

        self.assertFalse(model.is_active)
        self.assertIn("not enough to say what the model does to that format", model.metrics["gate"])

    def test_a_better_score_that_costs_creators_is_not_promoted(self):
        narrow = {"exposures": 50, "creators": 3, "satisfied_rate": 0.9, "save_rate": 0.0,
                  "conversation_rate": 0.0, "rejection_rate": 0.0}
        real = ranking_model.judge

        def narrowed(*args):
            return {**real(*args), "top_model": narrow}

        with mock.patch.object(ranking_model, "judge", narrowed):
            model = ranking_model.train(labels="measured")

        self.assertGreater(model.metrics["model_auc"], model.metrics["rules_auc"] + 0.1)
        self.assertFalse(model.is_active)
        self.assertIn("creators", model.metrics["gate"])

    def test_the_previous_model_stays_when_the_new_one_fails(self):
        first = ranking_model.train(labels="measured")
        with mock.patch.object(ranking_model, "MIN_FORMAT_DECIDED", 10_000):
            ranking_model.train(labels="measured")

        self.assertEqual(RankingModel.objects.get(is_active=True).pk, first.pk)


class JudgementTests(SimpleTestCase):
    def world(self):
        """Ten creators. The rules spread the top across them; a model that
        scores by `feature` piles onto whoever it likes."""
        return [
            exposure(viewer=index, author=index % 10, satisfied=index % 2 == 0, format="workout:still")
            for index in range(100)
        ]

    def test_the_top_is_counted_for_what_it_is_made_of(self):
        exposures = self.world()
        spread = [-(index // 10) for index in range(100)]          # ten of each creator on top
        piled = [1.0 if e.author < 2 else 0.0 for e in exposures]   # two creators on top

        judged = judge(exposures, spread, piled)

        self.assertEqual(judged["top_rules"]["creators"], 10)
        self.assertEqual(judged["top_model"]["creators"], 2)
        self.assertEqual(judged["top_rules"]["exposures"], 20)

    def gate(self, **model):
        top = {"exposures": 20, "creators": 10, "satisfied_rate": 0.5, "save_rate": 0.1,
               "conversation_rate": 0.0, "rejection_rate": 0.02}
        formats = {"workout:still": {
            "exposures": 100, "share": 1.0, "decided": 80, "rules_auc": 0.6, "model_auc": 0.7,
        }}
        formats["workout:still"].update(model.pop("format", {}))
        return ranking_model._measured_gate(
            {"formats": formats, "top_rules": top, "top_model": {**top, **model}}
        )

    def test_each_cost_fails_the_gate_by_name(self):
        self.assertTrue(self.gate()[0])
        for change, word in (
            ({"creators": 9}, "creators"),
            ({"rejection_rate": 0.03}, "turned down"),
            ({"save_rate": 0.05}, "saves"),
            ({"satisfied_rate": 0.4}, "satisfied"),
            ({"format": {"decided": 10}}, "not enough"),
            ({"format": {"model_auc": 0.5}}, "worse than the rules"),
        ):
            passed, why = self.gate(**change)
            self.assertFalse(passed, change)
            self.assertIn(word, why)

    def test_a_format_too_small_to_matter_does_not_block(self):
        self.assertTrue(self.gate(format={"share": 0.05, "decided": 2})[0])

    def test_clip_lengths_are_reported_apart(self):
        exposures = [
            exposure(clip_ms=8000, satisfied=True), exposure(clip_ms=40_000, satisfied=False),
            exposure(clip_ms=None, satisfied=True),
        ]

        lengths = judge(exposures, [0, 0, 0], [0, 0, 0])["clip_length"]

        self.assertEqual(lengths["short_clip"], {"exposures": 1, "satisfied_rate": 1.0})
        self.assertEqual(lengths["long_clip"], {"exposures": 1, "satisfied_rate": 0.0})
        self.assertEqual(lengths["no_clip"]["exposures"], 1)


class ExperimentClaimTests(SimpleTestCase):
    def test_a_small_beta_is_directional_however_large_the_gap(self):
        result = experiment_stats.compare(4, 20, 10, 20)

        self.assertEqual(result["claim"], "directional")
        self.assertFalse(result["powered"])
        self.assertIsNone(result["p_value"], "no p-value to quote when it cannot be claimed")
        self.assertGreater(result["readers_needed_per_arm"], 20)

    def test_with_enough_readers_a_real_difference_is_significant(self):
        result = experiment_stats.compare(4000, 20000, 4500, 20000)

        self.assertEqual(result["claim"], "significant")
        self.assertLess(result["p_value"], 0.05)
        self.assertEqual(result["relative_lift"], 0.125)

    def test_with_enough_readers_no_difference_is_said_too(self):
        self.assertEqual(experiment_stats.compare(4000, 20000, 4010, 20000)["claim"], "no difference found")

    def test_the_sample_needed_is_the_textbook_one(self):
        # 20% against 22%, two-sided 5%, 80% power: about 6,500 an arm.
        needed = experiment_stats.needed_per_arm(0.2, 0.1)

        self.assertTrue(6300 < needed < 6700, needed)
        self.assertGreater(experiment_stats.needed_per_arm(0.2, 0.05), needed * 3)

    def test_nothing_to_compare_is_not_an_error(self):
        self.assertEqual(experiment_stats.compare(0, 0, 0, 0)["claim"], "directional")
        self.assertIsNone(experiment_stats.needed_per_arm(0))
        self.assertIsNone(experiment_stats.p_value(0, 10, 0, 10))


class ExperimentReportTests(ForYouTestCase):
    @override_settings(FOR_YOU_EXPERIMENT={"name": "m9", "variants": {"control": {}, "measured": {"measured": 1.0}}})
    def test_the_report_says_what_can_be_claimed_per_reader(self):
        from .management.commands.feed_report import build_report

        for index in range(12):
            profile, _ = self.reader(f"arm{index}")
            PostView.objects.create(viewer=profile, post=self.running[0], view_count=1,
                                    surface=PostView.Surface.FOR_YOU)
            if index % 2:
                PostLike.objects.create(post=self.running[0], user=profile)

        arms = build_report()["by_variant"]

        self.assertEqual(set(arms), {"control", "measured"})
        self.assertNotIn("engaged_readers_vs_control", arms["control"])
        verdict = arms["measured"]["engaged_readers_vs_control"]
        self.assertEqual(verdict["claim"], "directional")
        self.assertEqual(arms["control"]["viewers"] + arms["measured"]["viewers"], 12)
