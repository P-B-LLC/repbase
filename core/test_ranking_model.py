"""The learned ranking model: what it learns from, how it is trained and
judged, and how little it is trusted until it has earned it.

- the maths: AUC, a logistic fit that finds the signal, standardisation;
- what the page records: each For You impression carries what the ranking
  knew when it served the post -- never for other surfaces, the first
  ranking only, deleted with the viewing history;
- the gate: too little data, or no better than the rules on held-out
  readers, and the model stays inactive and adds nothing;
- earned: a model that beats the rules becomes the one active model, moves
  the page, and can be switched off by weight;
- a model is numbers only.
"""

import io
import json
import random
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from . import ranking_model, recommendations
from .fixtures_for_tests import make_account
from .models import PostLike, PostView, RankingModel, RepbaseUser
from .ranking_model import FEATURES, RULES, auc, feature_row, fit, scaled, standardise
from .test_recommendations import IMPRESSIONS, ForYouTestCase


class MathsTests(SimpleTestCase):
    def test_auc(self):
        self.assertEqual(auc([0.1, 0.2, 0.8, 0.9], [0, 0, 1, 1]), 1.0)
        self.assertEqual(auc([0.9, 0.8, 0.2, 0.1], [0, 0, 1, 1]), 0.0)
        self.assertEqual(auc([0.5, 0.5, 0.5, 0.5], [0, 1, 0, 1]), 0.5)
        self.assertIsNone(auc([0.1, 0.2], [1, 1]))

    def test_a_fit_finds_the_signal_and_ignores_the_noise(self):
        dice = random.Random(4)
        rows = [[dice.gauss(0, 1), dice.gauss(0, 1)] for _ in range(3000)]
        labels = [1 if row[0] + dice.gauss(0, 0.3) > 0.8 else 0 for row in rows]
        means, scales = standardise(rows)
        standard = [scaled(row, means, scales) for row in rows]
        weights, bias = fit(standard, labels)
        self.assertGreater(weights[0], 1.0)
        self.assertLess(abs(weights[1]), 0.3)
        scores = [ranking_model.predict(weights, bias, row) for row in standard]
        self.assertGreater(auc(scores, labels), 0.95)

    def test_a_constant_column_does_not_divide_by_zero(self):
        means, scales = standardise([[1.0, 3.0], [1.0, 5.0]])
        self.assertEqual(scales[0], 1.0)

    def test_what_is_recorded_has_the_rules_last(self):
        raw = {name: 0.5 for name in ranking_model.SCORE_FEATURES} | {"rules": 2.25}
        row = feature_row(raw, discovery=True, position=3)
        self.assertEqual(len(row), len(FEATURES) + 1)
        self.assertEqual(row[RULES], 2.25)
        self.assertEqual(row[FEATURES.index("discovery")], 1.0)


class RecordingTests(ForYouTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()

    def test_a_for_you_impression_carries_what_the_ranking_knew(self):
        profile, token = self.reader("recorded", ["Running"])
        page = self.ids(self.feed(token, page_size=5))
        self.client.post(IMPRESSIONS, {"events": [
            {"post": page[0], "dwell_ms": 4000, "surface": "for_you"},
            {"post": page[1], "dwell_ms": 4000, "surface": "following"},
        ]}, format="json")
        recorded = PostView.objects.get(viewer=profile, post_id=page[0])
        self.assertEqual(len(recorded.features), len(FEATURES) + 1)
        self.assertEqual(recorded.features[FEATURES.index("position")], 0.0, "the first slot")
        self.assertIsNone(PostView.objects.get(viewer=profile, post_id=page[1]).features,
                          "only For You impressions are learned from")

    def test_the_first_ranking_is_the_one_kept(self):
        profile, token = self.reader("recorded_twice", [])
        page = self.ids(self.feed(token, page_size=5))
        send = {"events": [{"post": page[2], "dwell_ms": 3000, "surface": "for_you"}]}
        self.client.post(IMPRESSIONS, send, format="json")
        first = PostView.objects.get(viewer=profile, post_id=page[2]).features
        recommendations.remember_features(profile, {page[2]: [9.0] * (len(FEATURES) + 1)})
        self.client.post(IMPRESSIONS, send, format="json")
        self.assertEqual(PostView.objects.get(viewer=profile, post_id=page[2]).features, first)

    def test_starting_fresh_deletes_it_with_the_history(self):
        profile, token = self.reader("forgets", [])
        page = self.ids(self.feed(token, page_size=3))
        self.client.post(IMPRESSIONS, {"events": [{"post": page[0], "dwell_ms": 3000, "surface": "for_you"}]},
                         format="json")
        self.client.delete("/api/v1/social/for-you/history/")
        self.assertFalse(PostView.objects.filter(viewer=profile).exists())
        self.assertEqual(recommendations.served_features(profile), {})


@override_settings(FOR_YOU_WEIGHTS={"discovery_every": 0})
class TrainingTests(ForYouTestCase):
    """A world where readers act on `quality` and the rules' total says
    nothing: a model should find that, and beat the rules."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.viewers = [make_account(f"labeller{n}")[1] for n in range(40)]
        RepbaseUser.objects.filter(pk__in=[v.pk for v in cls.viewers]).update(
            created_at=timezone.now() - timedelta(days=30)
        )
        dice = random.Random(9)
        posts = cls.running + cls.lifting + cls.meals
        views, likes = [], []
        for viewer in cls.viewers:
            for position, post in enumerate(posts):
                raw = {name: dice.random() for name in ranking_model.SCORE_FEATURES}
                raw["rules"] = dice.gauss(0, 1)
                engaged = raw["quality"] + dice.gauss(0, 0.15) > 0.75
                skipped = not engaged and raw["seen"] > 0.5
                views.append(PostView(
                    viewer=viewer, post=post, view_count=1, total_dwell_ms=500 if skipped else 4000,
                    skipped=skipped, surface=PostView.Surface.FOR_YOU,
                    features=feature_row(raw, position=position % 10),
                ))
                if engaged:
                    likes.append(PostLike(post=post, user=viewer))
        PostView.objects.bulk_create(views)
        PostLike.objects.bulk_create(likes)
        PostView.objects.filter(viewer__in=cls.viewers).update(first_seen_at=timezone.now() - timedelta(hours=3))

    def setUp(self):
        super().setUp()
        cache.clear()
        for name, value in (("MIN_TRAIN", 300), ("MIN_POSITIVES", 30), ("MIN_TEST", 50), ("MIN_TEST_POSITIVES", 5)):
            patcher = mock.patch.object(ranking_model, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_model_that_beats_the_rules_becomes_the_active_one(self):
        model = ranking_model.train()
        self.assertTrue(model.is_active, model.metrics)
        self.assertGreater(model.metrics["model_auc"], model.metrics["rules_auc"] + 0.1)
        self.assertGreater(model.metrics["auc_engage"], model.metrics["rules_auc_engage"] + 0.1)
        quality = model.engage_weights[FEATURES.index("quality")]
        self.assertEqual(max(model.engage_weights, key=abs), quality, "it found what readers act on")
        self.assertIsNotNone(ranking_model.active_model())

    def test_the_active_model_moves_the_page_and_a_weight_of_zero_stops_it(self):
        ranking_model.train()
        reader, _ = self.reader("ranked_by_a_model", ["Running"])
        page = recommendations.build_ranking(reader, seed=3)
        self.assertTrue(page.items)
        profile = recommendations.build_profile(reader)
        found = recommendations.candidates(reader, profile)
        model = ranking_model.active_model()
        weights = recommendations.FeedWeights()
        _, parts = recommendations.score(found[0], profile, weights, timezone.now(), model=model)
        self.assertNotEqual(parts["learned"], 0.0)
        _, off = recommendations.score(found[0], profile, recommendations.FeedWeights(learned=0), timezone.now(), model=model)
        self.assertEqual(off["learned"], 0.0)

    def test_too_little_data_and_the_model_stays_inactive(self):
        with mock.patch.object(ranking_model, "MIN_TRAIN", 10_000):
            model = ranking_model.train()
        self.assertFalse(model.is_active)
        self.assertIn("training impressions", model.metrics["gate"])
        self.assertIsNone(ranking_model.active_model())
        reader, _ = self.reader("ruled_only", [])
        profile = recommendations.build_profile(reader)
        found = recommendations.candidates(reader, profile)
        _, parts = recommendations.score(found[0], profile, recommendations.FeedWeights(), timezone.now(),
                                         model=ranking_model.active_model())
        self.assertEqual(parts["learned"], 0.0)

    def test_no_better_than_the_rules_and_it_stays_inactive(self):
        with mock.patch.object(ranking_model, "MIN_GAIN", 1.0):
            model = ranking_model.train()
        self.assertFalse(model.is_active)
        self.assertIn("did not beat", model.metrics["gate"])

    def test_one_active_model_and_old_ones_go(self):
        first = ranking_model.train()
        second = ranking_model.train()
        self.assertEqual(list(RankingModel.objects.filter(is_active=True)), [second])
        first.refresh_from_db()
        self.assertFalse(first.is_active)
        with self.assertRaises(IntegrityError), transaction.atomic():
            RankingModel.objects.filter(pk=first.pk).update(is_active=True)
        with mock.patch.object(ranking_model, "MIN_GAIN", 1.0):
            for _ in range(ranking_model.KEEP_MODELS + 2):
                ranking_model.train()
        self.assertLessEqual(RankingModel.objects.filter(is_active=False).count(), ranking_model.KEEP_MODELS)

    def test_a_model_is_numbers_and_names_nobody(self):
        model = ranking_model.train()
        stored = json.dumps({
            "features": model.features, "means": model.means, "scales": model.scales,
            "engage": model.engage_weights, "reject": model.reject_weights, "metrics": model.metrics,
        })
        for viewer in self.viewers[:5]:
            self.assertNotIn(viewer.user.username, stored)
        self.assertEqual(model.features, list(FEATURES))

    def test_the_command_reports_counts(self):
        output = io.StringIO()
        call_command("train_ranking_model", "--no-activate", stdout=output)
        report = json.loads(output.getvalue())
        self.assertTrue(report["trained"])
        self.assertFalse(report["active"])
        self.assertGreater(report["train"], 0)


class ReportTests(ForYouTestCase):
    def test_the_report_says_which_model_is_in_use(self):
        output = io.StringIO()
        call_command("feed_report", "--json", stdout=output)
        self.assertEqual(json.loads(output.getvalue())["model"], {"active": None, "newest": None})
        RankingModel.objects.create(features=list(FEATURES), metrics={"train": 5, "gate": "only 5"})
        output = io.StringIO()
        call_command("feed_report", "--json", stdout=output)
        model = json.loads(output.getvalue())["model"]
        self.assertIsNone(model["active"])
        self.assertEqual(model["newest"]["train"], 5)
