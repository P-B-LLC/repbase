"""A ranking model learned from what people did with what For You showed them.

The rules in `core.recommendations` stay the ranking. This learns, from the
page's own record, how much each of their parts actually predicts -- and adds
that as one more part, `learned`, once it has shown it predicts better than
the rules do. It is the step Instagram's own ranking took long ago, done the
smallest way that fits a small community and a team that has to be able to
read what it does:

- **What it learns from.** Every post For You puts on somebody's screen is
  recorded with what the ranking knew about it then -- each part of its score
  before weighting, whether it filled a discovery slot, and where on the page
  it was (`PostView.features`, written by the server, never the client). What
  the reader then did is the label: *engaged* (liked, commented, saved,
  shared, "more like this") or *turned down* (scrolled straight past, "not
  interested", "fewer like this").
- **What it is.** Two logistic regressions over those features -- the chance
  a reader engages with a post, and the chance they turn it down -- trained in
  plain Python (`fit`), with the features standardised. Nineteen numbers and a
  bias each, stored in `RankingModel`, readable by anybody who wants to know
  what it learned.
- **How it is used.** The `learned` part of a post's score is
  `weight x (P(engage) - REJECT_COST x P(turn down))`. Where the page slot is
  not known yet -- position, discovery -- the model sees the average, so it
  ranks on what the post is, not on where it would land.
- **When it is trusted.** `train_ranking_model` (daily, on the
  `repbase-ranking-model` timer) holds out a fifth of readers -- whole readers,
  so nobody's habits leak from training into testing -- and asks of them the
  question the page answers: does the score put what people took (engaged)
  above what they turned down? It compares the rules' own total with the
  rules plus this model, as the page would rank by them. A new model becomes
  the active one only with enough data behind it and a held-out AUC at least
  MIN_GAIN better than the rules alone. Otherwise the previous model (or
  none) stays, and the `learned` part is 0.
- **Position bias.** People engage more with what is near the top whatever it
  is. Position is a feature in training and held at its average when
  serving, so the model's view of a post is not just a record of where the
  rules put it.

Privacy: features live on the viewing-history row they belong to, so they are
pruned with it at 90 days and deleted at once by "start fresh". A model is
aggregate numbers only. Nothing is shown to anybody.
"""

import hashlib
import logging
import math
import random
import time
from datetime import timedelta

from django.core.cache import cache
from django.db import transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from .models import (
    Post,
    PostComment,
    PostFeedback,
    PostLike,
    PostView,
    RankingModel,
    SavedFoodMeal,
    WorkoutTemplate,
)

logger = logging.getLogger(__name__)

#: What the model sees, in order. The first seventeen are the score's parts
#: before weighting (`core.recommendations.score`); the last two are where
#: the post landed, known only once it was served.
SCORE_FEATURES = (
    "topic", "momentum", "detail", "author", "follows", "similar", "quality", "popularity",
    "watch", "skip_rate", "hide_rate", "recency", "newcomer", "seen", "fatigue", "reported", "bait",
)
SLOT_FEATURES = ("discovery", "position")
FEATURES = SCORE_FEATURES + SLOT_FEATURES
#: Recorded after the features: the rules' own total, which the model is
#: measured against and never learns from.
RULES = len(FEATURES)

#: How much a turned-down post costs against an engaged one, in the
#: `learned` part. Scrolling past is far more common than a like, so it
#: counts for half.
REJECT_COST = 0.5

#: Training: at most MAX_SAMPLES of the newest labelled impressions from the
#: last TRAIN_DAYS, none younger than LABEL_DELAY (people need a moment to
#: act on what they saw).
TRAIN_DAYS = 30
MAX_SAMPLES = 200_000
LABEL_DELAY = timedelta(hours=1)
#: A fifth of readers held out to test on.
HOLDOUT_SHARE = 0.2
#: Stochastic gradient descent: passes over the data, starting step, how
#: fast it shrinks, and L2 regularisation.
EPOCHS = 4
LEARNING_RATE = 0.05
DECAY_STEPS = 20_000
L2 = 1e-4
#: A model is only made active with at least this much behind it ...
MIN_TRAIN = 2_000
MIN_POSITIVES = 100
MIN_TEST = 300
MIN_TEST_POSITIVES = 20
#: ... and a held-out AUC (engaged above turned down) at least this much
#: better with the model than the rules' own score achieves on the same
#: readers.
MIN_GAIN = 0.01
#: Inactive models older than the newest this many are deleted.
KEEP_MODELS = 10
#: Every worker reads the active model at most this often.
MODEL_TTL_SECONDS = 5 * 60
_CACHE_KEY = "for-you:ranking-model"


# ------------------------------------------------------------------- maths

def sigmoid(z):
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def standardise(rows):
    """(means, scales) of each column. A constant column scales by 1."""
    n = len(rows)
    width = len(rows[0])
    means = [sum(row[j] for row in rows) / n for j in range(width)]
    scales = []
    for j in range(width):
        variance = sum((row[j] - means[j]) ** 2 for row in rows) / n
        scales.append(math.sqrt(variance) if variance > 1e-12 else 1.0)
    return means, scales


def scaled(row, means, scales):
    return [(value - mean) / scale for value, mean, scale in zip(row, means, scales)]


def fit(rows, labels, epochs=EPOCHS, seed=0):
    """(weights, bias) of a logistic regression, by stochastic gradient
    descent with L2, averaged over the last pass (Polyak averaging, which
    takes out most of the noise single steps leave). `rows` are already
    standardised."""
    n = len(rows)
    width = len(rows[0])
    share = min(max(sum(labels) / n, 1e-4), 1 - 1e-4)
    bias = math.log(share / (1 - share))
    weights = [0.0] * width
    order = list(range(n))
    dice = random.Random(seed)
    step = 0
    total_weights, total_bias = [0.0] * width, 0.0
    for epoch in range(epochs):
        dice.shuffle(order)
        last = epoch == epochs - 1
        for index in order:
            step += 1
            rate = LEARNING_RATE / (1 + step / DECAY_STEPS)
            row = rows[index]
            error = sigmoid(bias + sum(w * x for w, x in zip(weights, row))) - labels[index]
            bias -= rate * error
            weights = [w - rate * (error * x + L2 * w) for w, x in zip(weights, row)]
            if last:
                total_bias += bias
                total_weights = [t + w for t, w in zip(total_weights, weights)]
    return [t / n for t in total_weights], total_bias / n


def predict(weights, bias, row):
    return sigmoid(bias + sum(w * x for w, x in zip(weights, row)))


def auc(scores, labels):
    """Area under the ROC curve: the chance a random positive outranks a
    random negative, ties counting half. None without both kinds."""
    positives = sum(labels)
    negatives = len(labels) - positives
    if not positives or not negatives:
        return None
    ranked = sorted(zip(scores, labels))
    rank_sum = 0.0
    index = 0
    while index < len(ranked):
        end = index
        while end + 1 < len(ranked) and ranked[end + 1][0] == ranked[index][0]:
            end += 1
        average_rank = (index + end) / 2 + 1
        rank_sum += average_rank * sum(label for _, label in ranked[index:end + 1])
        index = end + 1
    return (rank_sum - positives * (positives + 1) / 2) / (positives * negatives)


# ---------------------------------------------------------------- the data

def feature_row(raw, discovery=False, position=0):
    """The recorded list for one served post: its score's parts before
    weighting, its slot, and the rules' total last."""
    return [round(float(raw.get(name, 0.0)), 6) for name in SCORE_FEATURES] + [
        1.0 if discovery else 0.0, float(math.log1p(position)),
    ] + [round(float(raw.get("rules", 0.0)), 6)]


def _held_out(viewer_id):
    digest = hashlib.sha256(f"ranking-holdout:{viewer_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2 ** 64 < HOLDOUT_SHARE


def labelled_impressions(now=None, days=TRAIN_DAYS, limit=MAX_SAMPLES):
    """[(viewer id, features, engaged, turned down)] for For You impressions
    that recorded features, newest first."""
    now = now or timezone.now()
    same = {"post": OuterRef("post")}

    def said(*kinds):
        return Exists(PostFeedback.objects.filter(viewer=OuterRef("viewer"), kind__in=kinds, **same))

    rows = (
        PostView.objects.filter(
            surface=PostView.Surface.FOR_YOU, features__isnull=False,
            first_seen_at__gte=now - timedelta(days=days), first_seen_at__lte=now - LABEL_DELAY,
            viewer__user__is_active=True,
        )
        .annotate(
            liked=Exists(PostLike.objects.filter(user=OuterRef("viewer"), **same)),
            commented=Exists(PostComment.objects.filter(author=OuterRef("viewer"), **same)),
            shared=Exists(Post.objects.filter(author=OuterRef("viewer"), repost_of=OuterRef("post"))),
            saved_workout=Exists(WorkoutTemplate.objects.filter(owner=OuterRef("viewer"), source_post=OuterRef("post"))),
            saved_meal=Exists(SavedFoodMeal.objects.filter(owner=OuterRef("viewer"), source_post=OuterRef("post"))),
            wanted_more=said(PostFeedback.Kind.MORE),
            turned_down=said(*PostFeedback.REJECTING),
        )
        .order_by("-first_seen_at", "-pk")
        .values_list(
            "viewer_id", "features", "skipped", "liked", "commented", "shared", "saved_workout",
            "saved_meal", "wanted_more", "turned_down",
        )[:limit]
    )
    found = []
    for viewer, features, skipped, *acted, wanted_more, turned_down in rows:
        if not isinstance(features, list) or len(features) != RULES + 1:
            continue
        engaged = any(acted) or wanted_more
        found.append((viewer, features, engaged, bool(skipped or turned_down) and not engaged))
    return found


# ---------------------------------------------------------------- training

def train(now=None, activate=True, seed=0):
    """Train on the newest labelled impressions, test on held-out readers,
    save the model, and make it active if it earned it. Returns the model,
    or None when there was nothing to learn from."""
    started = time.perf_counter()
    samples = labelled_impressions(now)
    training = [sample for sample in samples if not _held_out(sample[0])]
    testing = [sample for sample in samples if _held_out(sample[0])]
    metrics = {
        "samples": len(samples), "train": len(training), "test": len(testing),
        "train_engaged": sum(1 for sample in training if sample[2]),
        "train_turned_down": sum(1 for sample in training if sample[3]),
        "test_engaged": sum(1 for sample in testing if sample[2]),
    }
    if not training or not metrics["train_engaged"] or metrics["train_engaged"] == len(training):
        logger.info("ranking_model skipped: nothing to learn from %s", metrics)
        return None

    features = [sample[1][:RULES] for sample in training]
    means, scales = standardise(features)
    rows = [scaled(row, means, scales) for row in features]
    engage_weights, engage_bias = fit(rows, [1 if sample[2] else 0 for sample in training], seed=seed)
    reject_weights, reject_bias = fit(rows, [1 if sample[3] else 0 for sample in training], seed=seed + 1)

    if testing:
        from .recommendations import FeedWeights

        weight = FeedWeights.from_settings().learned
        test_rows = [scaled(sample[1][:RULES], means, scales) for sample in testing]
        engaged = [1 if sample[2] else 0 for sample in testing]
        turned_down = [1 if sample[3] else 0 for sample in testing]
        p_engage = [predict(engage_weights, engage_bias, row) for row in test_rows]
        p_reject = [predict(reject_weights, reject_bias, row) for row in test_rows]
        rules = [sample[1][RULES] for sample in testing]
        # The score the page would rank by with this model, against the
        # rules alone, judged on what matters: putting what readers took
        # above what they turned down. Impressions that were neither do not
        # say which order was right.
        decided = [index for index, sample in enumerate(testing) if sample[2] or sample[3]]
        with_model = [rules[i] + weight * (p_engage[i] - REJECT_COST * p_reject[i]) for i in decided]
        outcome = [engaged[i] for i in decided]
        metrics.update({
            "auc_engage": _round(auc(p_engage, engaged)),
            "auc_turned_down": _round(auc(p_reject, turned_down)),
            "rules_auc_engage": _round(auc(rules, engaged)),
            "test_decided": len(decided),
            # The bar a model has to clear: the rules' own total, ordering
            # engaged above turned down for the same held-out readers ...
            "rules_auc": _round(auc([rules[i] for i in decided], outcome)),
            # ... and what the rules plus this model achieve.
            "model_auc": _round(auc(with_model, outcome)),
        })
    passed, why = _gate(metrics)
    metrics["gate"] = why
    metrics["train_seconds"] = round(time.perf_counter() - started, 2)

    with transaction.atomic():
        model = RankingModel.objects.create(
            features=list(FEATURES), means=means, scales=scales,
            engage_weights=engage_weights, engage_bias=engage_bias,
            reject_weights=reject_weights, reject_bias=reject_bias,
            metrics=metrics,
        )
        if activate and passed:
            RankingModel.objects.filter(is_active=True).update(is_active=False)
            model.is_active = True
            model.save(update_fields=["is_active"])
        stale = list(
            RankingModel.objects.filter(is_active=False).order_by("-created_at", "-pk")
            .values_list("pk", flat=True)[KEEP_MODELS:]
        )
        if stale:
            RankingModel.objects.filter(pk__in=stale).delete()
    cache.delete(_CACHE_KEY)
    logger.info("ranking_model trained id=%s active=%s %s", model.pk, model.is_active, metrics)
    return model


def _round(value):
    return None if value is None else round(value, 4)


def _gate(metrics):
    """(whether a model may become active, and why or why not)."""
    if metrics["train"] < MIN_TRAIN:
        return False, f"only {metrics['train']} training impressions (needs {MIN_TRAIN})"
    if metrics["train_engaged"] < MIN_POSITIVES:
        return False, f"only {metrics['train_engaged']} engaged impressions (needs {MIN_POSITIVES})"
    if metrics["test"] < MIN_TEST or metrics["test_engaged"] < MIN_TEST_POSITIVES:
        return False, "not enough held-out readers to judge it"
    model, rules = metrics.get("model_auc"), metrics.get("rules_auc")
    if model is None or rules is None:
        return False, "held-out readers did not include both outcomes"
    if model < rules + MIN_GAIN:
        return False, f"held-out AUC {model} with the model did not beat the rules' {rules} by {MIN_GAIN}"
    return True, f"held-out AUC {model} with the model beat the rules' {rules}"


# ----------------------------------------------------------------- serving

def active_model():
    """The active model as plain numbers, or None. Shared through the cache
    for MODEL_TTL_SECONDS, so a ranking never waits on the database for it."""
    found = cache.get(_CACHE_KEY)
    if found is None:
        model = RankingModel.objects.filter(is_active=True).first()
        if model is None or list(model.features) != list(FEATURES):
            found = {"id": None}
        else:
            found = {
                "id": model.pk, "means": model.means, "scales": model.scales,
                "engage": (model.engage_weights, model.engage_bias),
                "reject": (model.reject_weights, model.reject_bias),
            }
        cache.set(_CACHE_KEY, found, MODEL_TTL_SECONDS)
    return found if found.get("id") is not None else None


def value(model, raw):
    """P(engage) - REJECT_COST x P(turn down) for a candidate, from its
    score's parts before weighting. The slot is not known yet, so the model
    sees the average one (a standardised 0)."""
    row = [float(raw.get(name, 0.0)) for name in SCORE_FEATURES]
    means, scales = model["means"], model["scales"]
    standard = scaled(row, means[:len(SCORE_FEATURES)], scales[:len(SCORE_FEATURES)]) + [0.0] * len(SLOT_FEATURES)
    engage = predict(*model["engage"], standard)
    reject = predict(*model["reject"], standard)
    return engage - REJECT_COST * reject
