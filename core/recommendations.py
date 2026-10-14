"""The For You page: what to show somebody, in what order, and why.

Rules rather than a model, on purpose. There is not yet enough interaction
data here to train anything worth trusting, and a learned ranking nobody can
read is a ranking nobody can debug, explain to a user, or defend to a
moderator. Every number below has a name, a default and a sentence saying
what it does, and `FOR_YOU_WEIGHTS` in settings can move any of them.

## The pipeline

1. **What this person is into** (`build_profile`). Their likes, comments,
   saves, reposts, how long they stayed on what they saw, what they came
   back to, how much of a clip they watched, what they scrolled straight
   past and what they said they were not interested in -- each weighted,
   each fading with age -- plus who they follow and what they said they
   train when they signed up. Topics come out of that as a score between -1
   and 1 each, down to the exercises in a workout, and so do authors. What
   they did in the last few hours is kept apart as well, as momentum,
   because it says more about what they want now than last month does; and
   authors they keep being shown and never engage with are noted, so the
   page stops insisting on them.

2. **What they could be shown** (`candidates`). Only what passes every
   eligibility rule: posts this reader may see at all (`visible_posts_for`),
   from the last month, not their own, not reposts, not something they have
   already liked, commented on, saved or passed on, not from a suspended
   account, not something they reported or dismissed, not on a topic they
   hid, not carrying a clip still waiting for review, not something
   enough people have reported that a moderator should look first, and not
   -- unless they follow them -- by somebody a moderator acted against in
   the last month (`core.feed_integrity`). Drawn from up to six places at
   once -- the newest posts, people they follow, their best topics, the
   exercises they go for, what people with similar taste liked, and what the
   whole community has been liking this week -- each capped, so the work
   stays the same size however much is posted.

3. **How good a match each one is** (`score`). How well it suits the
   reader -- topic, exercises, momentum, author, following, similar taste --
   plus how well it does with everybody it has been shown to, counted only
   from engagement that can be trusted: saves and shares ahead of comments
   ahead of likes, per view, less how often people scrolled past it or said
   "not interested". Then freshness and a lift for new creators, less what
   they have already seen, an author shown to no effect, open reports and
   caption bait. Quality is drawn from what a post's record so far allows
   rather than pinned to its average, so a post that has had ten views gets
   a fair chance to show what it is, and one that has had ten thousand is
   judged on them. Every part is kept, so the reason a post ranked where it
   did can be shown.

4. **An order worth scrolling** (`rank`). Best first, except that the same
   author cannot appear twice in four posts, a repeated author or topic costs
   a little more each time, people the reader follows take no more than half
   of any ten slots while anybody else is there, and one slot in five goes to
   discovery: a good post from somebody they have never interacted with, on a
   topic outside their usual ones. That is what stops the page becoming one
   creator, and what gives it something to learn from next time.

5. **Keeping up** (`serve_page`). The first page is ranked from scratch.
   Every later page re-scores what is left against a fresh profile, so what
   the reader liked, dismissed or skipped on page one already shapes page
   two, while what was served stays served: paging never repeats or skips.

## New accounts

Somebody with no history gets the topics they named during onboarding and
the disciplines on their profile as a prior, and the ranking falls back on
quality, popularity and freshness with the same diversity rules. Somebody who
named nothing gets the best of what is new, mixed -- which is a reasonable
first page, and the first like starts making it theirs.

## The reader's say

`RecommendationPreferences` lets a reader hide topics, clear what the page
has learned, or turn personalisation off, in which case the page is ordered
by quality, popularity and freshness alone. See `RECOMMENDATIONS.md`.
"""

import hashlib
import logging
import math
import random
import secrets
import time
from collections import Counter
from dataclasses import dataclass, field, fields, replace
from datetime import timedelta

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.db.models import Count, Exists, F, OuterRef, Q, Subquery, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from .feed_integrity import (
    FULL_TRUST,
    account_trust,
    blend,
    bursting_accounts,
    caption_flags,
    inactive_accounts,
    new_since,
    recently_moderated_authors,
    trust_by_id,
)
from .models import (
    Block,
    Exercise,
    Follow,
    Personalization,
    PlannerCategory,
    Post,
    PostComment,
    PostFeedback,
    PostLike,
    PostReport,
    PostVideo,
    PostView,
    PostWorkoutExercise,
    RECOMMENDATION_HISTORY_DAYS,
    RecommendationPreferences,
    RepbaseUser,
    SavedFoodMeal,
    UserDiscipline,
    WorkoutTemplate,
)

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ settings

@dataclass(frozen=True)
class FeedWeights:
    """Every weight the ranking uses. Override any of them in
    `settings.FOR_YOU_WEIGHTS`; an unknown name is a configuration error
    rather than a silently ignored typo."""

    # --- how much each part of a post's score counts
    #: Match between the post's topics and the reader's.
    topic: float = 2.0
    #: Match between the post's topics and what the reader engaged with in
    #: the last MOMENTUM_HOURS.
    momentum: float = 1.5
    #: How the reader has treated this author before.
    author: float = 1.5
    #: Flat bonus for an author the reader follows.
    follows: float = 1.0
    #: Liked by people whose likes overlap the reader's.
    similar: float = 1.2
    #: Engagement for the views it has had, smoothed.
    quality: float = 1.0
    #: Raw engagement, on a log scale.
    popularity: float = 0.6
    #: Freshness, halving every RECENCY_HALF_LIFE_HOURS.
    recency: float = 1.2
    #: How often viewers stay, or watch a clip to the end.
    watch: float = 0.6
    #: A lift for somebody who joined lately and has few followers yet.
    newcomer: float = 0.4
    #: Penalty for something this reader has already seen lately. Grows with
    #: how often it was seen and fades over SEEN_HALF_LIFE_DAYS.
    seen: float = 3.0
    #: Penalty for open reports below the exclusion threshold.
    reported: float = 1.0
    #: A little noise so exact ties do not always break the same way.
    jitter: float = 0.02
    #: How far quality is sampled from what a post's record allows rather
    #: than fixed at its average: 0 ranks by the average alone, 1 samples in
    #: full (Thompson sampling). This is what gives a post with few views a
    #: chance. At 0.5 a post nobody has seen yet moves by about +-0.09 and
    #: one with a hundred views by about +-0.03 -- enough to try new things,
    #: not enough to outweigh what a reader said they like.
    explore: float = 0.5
    #: Penalty for how often other viewers scrolled straight past it,
    #: smoothed and measured against what a post with no record gets.
    skip_rate: float = 1.0
    #: Penalty for how often other viewers said "not interested", smoothed
    #: the same way. The strongest thing anybody can say about a post short
    #: of reporting it, so a little of it costs a lot.
    hide_rate: float = 12.0
    #: Penalty for a caption written to farm likes or to sell something
    #: (`core.feed_integrity.caption_flags`).
    bait: float = 1.5
    #: Match between the exercises in a workout and the ones the reader
    #: goes for -- the detail under the topic: deadlifts, not just lifting.
    #: Half the weight of the topic it refines: on the simulated community
    #: (`core.test_feed_evaluation`) a full weight narrowed pages onto the
    #: few creators of one lift without making them any better liked.
    detail: float = 0.5
    #: Penalty for an author the reader keeps being shown lately and has
    #: never once engaged with. Does not apply to people they follow. Kept
    #: modest: not liking something is weaker evidence than skipping it, and
    #: on the simulated community a heavier weight cost relevant authors.
    fatigue: float = 0.4
    #: How far engagement is counted only from accounts that can be trusted
    #: (`core.feed_integrity`): 1 counts each person once, never the author,
    #: never a suspended account, and less for brand-new, bursting or
    #: single-minded accounts; 0 counts every row as it stands.
    trust: float = 1.0

    # --- what one action on a post says about its value to strangers, per
    # view. Saves and shares lead: a save is a plan to do the thing, a share
    # is a recommendation to somebody else, and both are what Instagram says
    # matter most for reaching people who do not follow the author.
    like_value: float = 1.0
    comment_value: float = 1.5
    save_value: float = 3.0
    share_value: float = 3.0

    # --- what one action says about what someone likes
    like: float = 1.0
    comment: float = 2.0
    save: float = 3.0
    share: float = 2.5
    follow: float = 3.0
    long_view: float = 0.5
    #: Watched at least half of a clip without finishing it.
    half_watch: float = 0.5
    completion: float = 1.0
    skip: float = -0.3
    not_interested: float = -4.0
    #: Came back to a post and stayed on it again.
    rewatch: float = 1.0
    #: A topic named during onboarding, or a discipline on the profile.
    declared: float = 2.0

    # --- variety
    #: Cost of each earlier appearance of the same author on the page.
    author_repeat: float = 1.0
    #: Cost of each recent appearance of the same main topic.
    topic_repeat: float = 0.35
    #: One slot in this many goes to discovery. 0 turns discovery off.
    discovery_every: int = 5
    #: At most this share of any CONNECTED_WINDOW consecutive slots goes to
    #: people the reader follows, while anybody else is available. For You
    #: is where somebody finds what they do not follow yet; the Following
    #: feed is where they read who they do. 1 turns the limit off.
    connected_share: float = 0.5
    #: How much more the page varies topics while it knows little about the
    #: reader: up to (1 + this) times `topic_repeat` for somebody with no
    #: history, falling to 1x by CONFIDENT_SIGNALS signals. 0 turns it off.
    cold_variety: float = 1.0

    @classmethod
    def from_settings(cls, extra=None):
        overrides = dict(getattr(settings, "FOR_YOU_WEIGHTS", None) or {})
        overrides.update(extra or {})
        unknown = set(overrides) - {item.name for item in fields(cls)}
        if unknown:
            raise ImproperlyConfigured(
                "FOR_YOU_WEIGHTS names unknown weights: " + ", ".join(sorted(unknown))
            )
        return replace(cls(), **overrides)

    @classmethod
    def for_viewer(cls, viewer_id):
        """The weights this reader is ranked with, and the experiment
        variant that chose them (None when no experiment is running)."""
        variant, overrides = experiment_variant(viewer_id)
        return cls.from_settings(overrides), variant


# --------------------------------------------------------------- experiments
#
# One experiment at a time, configured in settings:
#
#     FOR_YOU_EXPERIMENT = {
#         "name": "recency-2026-11",
#         "variants": {"control": {}, "fresher": {"recency": 1.6}},
#     }
#
# Each reader lands in one variant for the life of the experiment, decided by
# a hash of the experiment's name and their id -- nothing is stored, so there
# is nothing to clean up and nothing about anyone to leak. `feed_report`
# splits every figure by variant. Renaming the experiment reshuffles everyone,
# which is what starting a new one should do.

def experiment_config():
    """The configured experiment, checked, or None."""
    config = getattr(settings, "FOR_YOU_EXPERIMENT", None)
    if not config:
        return None
    if not isinstance(config, dict) or set(config) - {"name", "variants"}:
        raise ImproperlyConfigured('FOR_YOU_EXPERIMENT takes "name" and "variants" only.')
    name, variants = config.get("name"), config.get("variants")
    if not isinstance(name, str) or not name.strip():
        raise ImproperlyConfigured("FOR_YOU_EXPERIMENT needs a name.")
    if not isinstance(variants, dict) or len(variants) < 2:
        raise ImproperlyConfigured("FOR_YOU_EXPERIMENT needs at least two variants.")
    known = {item.name for item in fields(FeedWeights)}
    for variant, overrides in variants.items():
        if not isinstance(variant, str) or not isinstance(overrides, dict):
            raise ImproperlyConfigured("Each FOR_YOU_EXPERIMENT variant maps a name to weights.")
        unknown = set(overrides) - known
        if unknown:
            raise ImproperlyConfigured(
                f"FOR_YOU_EXPERIMENT variant {variant!r} names unknown weights: "
                + ", ".join(sorted(unknown))
            )
    return {"name": name, "variants": variants}


def experiment_variant(viewer_id, config=None):
    """(variant, weight overrides) for this reader; (None, {}) outside one."""
    config = config if config is not None else experiment_config()
    if not config:
        return None, {}
    names = sorted(config["variants"])
    digest = hashlib.sha256(f"{config['name']}:{viewer_id}".encode()).digest()
    bucket = int.from_bytes(digest[:8], "big") / 2 ** 64
    variant = names[min(int(bucket * len(names)), len(names) - 1)]
    return variant, config["variants"][variant]


#: How far back a post may be to be recommended at all.
WINDOW_DAYS = 30
#: The newest eligible posts considered on every request.
POOL_SIZE = 500
#: Cap on each extra source (following, interests, similar taste, trending).
SOURCE_LIMIT = 150
#: How many posts one session serves. Ten pages of twenty; after that a
#: fresh ranking is a pull to refresh away.
MAX_RANKED = 200
#: How many unserved candidates a session keeps for its later pages: twice
#: what it can still serve. Every later page re-checks all of them, so more
#: is slower for posts that would almost never be reached.
POOL_KEEP = 400
#: How far back interactions count, and how many of each kind are read.
#: Viewing history older than this is deleted (`prune_expired_rows`).
HISTORY_DAYS = RECOMMENDATION_HISTORY_DAYS
HISTORY_LIMIT = 300
#: How many of the reader's views are read to know what they have seen.
#: More than the history above: a heavy scroller sees hundreds of posts a
#: month, and every one of them is something not to show again.
VIEW_LIMIT = 1000
#: An interaction's weight halves over this many days.
SIGNAL_HALF_LIFE_DAYS = 21
#: What counts as lately, for momentum.
MOMENTUM_HOURS = 6
#: A post's freshness halves over this many hours.
RECENCY_HALF_LIFE_HOURS = 36
#: How quickly having seen something stops counting against it.
SEEN_HALF_LIFE_DAYS = 2
#: A viewing shorter than this, with no watching, is a scroll past.
SKIP_MS = 1500
#: A viewing at least this long is somebody stopping to look.
LONG_VIEW_MS = 5000
#: Open, unreviewed reports from this many different people take a post out
#: of recommendations until a moderator has looked. It stays in the feeds of
#: people who follow its author, who chose to see it.
REPORT_THRESHOLD = 3
#: How long one ranking is kept for paging through.
SESSION_TTL_SECONDS = 30 * 60
#: The same author may not appear twice within this many consecutive posts.
AUTHOR_GAP = 4
#: How far down the scored list each slot looks for its pick.
SCAN_DEPTH = 60
#: Quality starts from this engagement rate, worth this many views, until a
#: post has views of its own.
QUALITY_PRIOR_RATE = 0.1
QUALITY_PRIOR_VIEWS = 20

#: Per-post counts are the same for every reader, so they are worked out once
#: and shared for this long. Counting views is most of what a cold ranking
#: costs, and a post's standing does not change by the minute; the reader's
#: own likes and views are always read live, so nothing they just did waits
#: on this.
STATS_TTL_SECONDS = 10 * 60
#: Trending: the posts with the most likes over this many days, worked out
#: once for everybody every few minutes.
TRENDING_DAYS = 7
TRENDING_LIMIT = 300
TRENDING_TTL_SECONDS = 5 * 60

#: Similar taste. The reader's most recent positive actions (at most
#: SEED_LIMIT) are matched against everyone else's likes; people sharing at
#: least MIN_OVERLAP of them are candidates, the NEIGHBOUR_POOL with the most
#: in common are weighed, and the NEIGHBOURS most alike count. Their latest
#: NEIGHBOUR_LIKES likes are what gets recommended.
SEED_LIMIT = 100
MIN_OVERLAP = 2
NEIGHBOUR_POOL = 100
NEIGHBOURS = 50
NEIGHBOUR_LIKES = 3000
#: With fewer neighbours than this, similar taste counts proportionally less.
CONFIDENT_NEIGHBOURS = 5
#: "Liked by people with similar taste" is only said of a post at least this
#: many of them liked, so it never stands for one identifiable person.
MIN_SIMILAR_VOTERS = 2

#: A new creator: joined within NEWCOMER_DAYS, with fewer than
#: NEWCOMER_FOLLOWERS followers. The lift fades as followers arrive.
NEWCOMER_DAYS = 60
NEWCOMER_FOLLOWERS = 30
FOLLOWERS_TTL_SECONDS = 15 * 60

#: Which For You reason each recently served post went out with, kept so an
#: impression can be attributed to it. Never shown to anyone.
SERVED_MEMORY = 400
SERVED_TTL_SECONDS = 24 * 3600

#: What a post with no record is assumed to get: this share of viewers
#: scrolling past and this share saying "not interested", each worth this
#: many views. A post is only judged on its own rates once it has views
#: enough to outweigh these.
SKIP_PRIOR_RATE = 0.3
SKIP_PRIOR_VIEWS = 20
HIDE_PRIOR_RATE = 0.005
HIDE_PRIOR_VIEWS = 50
#: The most the hide rate can count against a post: at the default weight,
#: 1.8 -- a heavy demotion, but less than a strong topic match is worth, so
#: no crowd can bury a post for the people it suits. Reports, not
#: dismissals, are what take a post off the page.
HIDE_RATE_CEILING = 0.15
#: A skip or a "not interested" from an account younger than
#: YOUNG_REJECTION_DAYS counts YOUNG_REJECTION_SHARE less -- so a crowd made
#: last week has to be twice the size to move a post as far -- and nothing
#: at all from one in its first two days.
YOUNG_REJECTION_DAYS = 7
YOUNG_REJECTION_SHARE = 0.5

#: Fatigue: posts by one author on the reader's screen in the last
#: FATIGUE_HOURS, with no engagement with that author at all. The first is
#: free; FATIGUE_SCALE more count about three quarters of the weight.
FATIGUE_HOURS = 72
FATIGUE_SCALE = 3.0

#: The span `connected_share` is measured over.
CONNECTED_WINDOW = 10
#: How many signals make a profile confident enough that `cold_variety` no
#: longer applies.
CONFIDENT_SIGNALS = 30

#: Exercises: the most-performed few in a workout, by sets, count as what it
#: is about in detail -- but only names in `exercise_vocabulary`, so the
#: topics are a known vocabulary rather than whatever somebody typed.
DETAIL_LIMIT = 3
#: A detail topic's share of one interaction, beside a full share for the
#: post's kind and type: one of several exercises in one session.
DETAIL_SHARE = 0.5
#: An exercise name used by this many different people's posts is a topic
#: even when it is not in the shared catalogue.
VOCABULARY_AUTHORS = 3
VOCABULARY_TTL_SECONDS = 60 * 60
#: A posted workout's exercises are frozen when it is posted, so they are
#: kept for as long as a post is likely to be a candidate on any page.
EXERCISES_TTL_SECONDS = 6 * 60 * 60


# -------------------------------------------------------------------- topics

#: What each onboarding answer means in topics. The answers are the strings
#: the iOS onboarding stores, lowercased.
TRAINING_TYPE_TOPICS = {
    "strength": (
        "type:lifting", "discipline:powerlifting", "discipline:bodybuilding",
        "discipline:weightlifting", "discipline:crossfit", "discipline:calisthenics",
    ),
    "running": ("type:running", "discipline:running", "discipline:triathlon"),
    "cycling": ("type:biking", "discipline:cycling", "discipline:triathlon"),
    "swimming": ("type:swimming", "discipline:swimming", "discipline:triathlon"),
}
EMPHASIS_TOPICS = {
    "training": ("kind:workout",),
    "nutrition": ("kind:meal",),
    "movement": ("type:running", "type:biking", "type:swimming"),
}
#: The workout types each profile discipline implies, beyond itself.
DISCIPLINE_TOPICS = {
    "powerlifting": ("type:lifting",),
    "bodybuilding": ("type:lifting",),
    "weightlifting": ("type:lifting",),
    "crossfit": ("type:lifting",),
    "calisthenics": ("type:lifting",),
    "running": ("type:running",),
    "cycling": ("type:biking",),
    "swimming": ("type:swimming",),
    "triathlon": ("type:running", "type:biking", "type:swimming"),
    "general_fitness": ("kind:workout",),
}

#: The topics a reader may hide from their For You page. What a post is,
#: rather than who posted it: hiding "running" should hide runs, not every
#: meal a runner ever posted.
HIDEABLE_TOPICS = (
    "kind:workout",
    "type:lifting",
    "type:running",
    "type:biking",
    "type:swimming",
    "kind:meal",
    "kind:planner",
    "format:video",
)

_TOPIC_NAMES = {
    "kind:workout": "workouts",
    "kind:meal": "meals and recipes",
    "kind:planner": "planning",
    "type:lifting": "lifting",
    "type:running": "running",
    "type:biking": "cycling",
    "type:swimming": "swimming",
    "format:video": "videos",
    **{
        f"discipline:{value}": label.lower()
        for value, label in RepbaseUser.TrainingStyle.choices
    },
    **{f"category:{value}": label.lower() for value, label in PlannerCategory.choices},
}


def topic_name(topic):
    return _TOPIC_NAMES.get(topic, topic.split(":", 1)[-1].replace("_", " "))


def topic_label(topic):
    """A topic's name as a heading or a chip, capitalised."""
    name = topic_name(topic)
    return name[:1].upper() + name[1:]


def topics_for(kind, workout_type, planner_category, has_video, disciplines):
    """{topic: weight} for one post.

    What the post is about counts fully; who posted it -- their disciplines --
    counts half, because a powerlifter's lunch is still lunch.
    """
    topics = {f"kind:{kind}": 1.0}
    if kind == Post.Kind.WORKOUT:
        # A session with no template has no type, and the clients draw those
        # as lifting.
        topics[f"type:{workout_type or 'lifting'}"] = 1.0
    elif kind == Post.Kind.PLANNER and planner_category:
        topics[f"category:{planner_category}"] = 1.0
    if has_video:
        topics["format:video"] = 0.5
    for discipline in disciplines:
        topics.setdefault(f"discipline:{discipline}", 0.5)
    return topics


def primary_topic(topics):
    """The most specific thing a post is about, for keeping the page varied."""
    for prefix in ("type:", "category:"):
        for topic in topics:
            if topic.startswith(prefix):
                return topic
    return next(iter(topics))


# ---------------------------------------------------------------- exercises

def _exercise_key(name):
    return (name or "").strip().lower()


def exercise_vocabulary(now=None):
    """{lowercased exercise name: [the names as written]} for every exercise
    that can be a topic: the shared catalogue, and any name at least
    VOCABULARY_AUTHORS different people posted workouts with in the last
    WINDOW_DAYS -- the posts that can be recommended at all. Worked out once
    an hour for everybody.

    A known vocabulary rather than whatever anybody typed, because what the
    page learns is shown back to the reader as their interests: a name only
    one person uses is that person's, not a topic, and would never match
    anybody else's posts anyway. The names as written are kept so a
    candidate source can match them exactly, which an index can help with
    and a lowercased comparison cannot."""
    found = cache.get("for-you:exercises")
    if found is None:
        now = now or timezone.now()
        spellings, people = {}, {}
        # A row per spelling and author, so that one person writing a name
        # three ways is still one person.
        for name, author in (
            PostWorkoutExercise.objects.filter(
                post_workout__post__created_at__gte=now - timedelta(days=WINDOW_DAYS)
            )
            .order_by().values_list("name", "post_workout__post__author_id").distinct()
        ):
            key = _exercise_key(name)
            if key:
                spellings.setdefault(key, set()).add(name)
                people.setdefault(key, set()).add(author)
        found = {
            key: sorted(names) for key, names in spellings.items()
            if len(people[key]) >= VOCABULARY_AUTHORS
        }
        for name in Exercise.objects.filter(created_by__isnull=True).values_list("name", flat=True):
            key = _exercise_key(name)
            if key:
                found[key] = sorted(set(found.get(key, ())) | set(spellings.get(key, ())) | {name})
        cache.set("for-you:exercises", found, VOCABULARY_TTL_SECONDS)
    return found


def exercise_topic(name):
    return "exercise:" + _exercise_key(name)


def _count_exercises(post_ids):
    """{post id: [exercise names, most-performed first]}. What a posted
    workout contains never changes, so this is shared for a long time."""
    found = {pk: [] for pk in post_ids}
    for post_id, name in PostWorkoutExercise.objects.filter(
        post_workout__post_id__in=list(post_ids)
    ).order_by("post_workout__post_id", "-set_count", "order", "id").values_list(
        "post_workout__post_id", "name"
    ):
        found[post_id].append(name)
    return found


def details_by_post(post_ids):
    """{post id: {exercise topic: share}} for the workouts among these
    posts: their most-performed exercises from the vocabulary, at most
    DETAIL_LIMIT. At most one query for any number of posts, and none for
    posts looked at in the last EXERCISES_TTL_SECONDS."""
    if not post_ids:
        return {}
    vocabulary = exercise_vocabulary()
    rows = {}
    for post_id, names in _cached_many(
        "exercises", sorted(post_ids), _count_exercises, EXERCISES_TTL_SECONDS
    ).items():
        for name in names:
            if _exercise_key(name) not in vocabulary:
                continue
            found = rows.setdefault(post_id, {})
            topic = exercise_topic(name)
            if len(found) < DETAIL_LIMIT and topic not in found:
                found[topic] = 1.0
    return rows


def _topic_filter(topics):
    """A Q matching posts about any of these topics, or None."""
    match = Q()
    kinds = [topic.split(":", 1)[1] for topic in topics if topic.startswith("kind:")]
    types = [topic.split(":", 1)[1] for topic in topics if topic.startswith("type:")]
    categories = [topic.split(":", 1)[1] for topic in topics if topic.startswith("category:")]
    disciplines = [topic.split(":", 1)[1] for topic in topics if topic.startswith("discipline:")]
    if kinds:
        match |= Q(kind__in=kinds)
    if types:
        match |= Q(workout__workout_type__in=types)
        if "lifting" in types:
            # Untyped sessions are drawn, and so counted, as lifting.
            match |= Q(kind=Post.Kind.WORKOUT, workout__workout_type__isnull=True)
    if categories:
        match |= Q(planner__category__in=categories)
    if disciplines:
        # Exists rather than a join through the author's disciplines: an
        # author with two matching disciplines would otherwise come back
        # twice.
        match |= Exists(UserDiscipline.objects.filter(
            profile=OuterRef("author"), discipline__in=disciplines
        ))
    if "format:video" in topics:
        match |= Exists(PostVideo.objects.filter(post=OuterRef("pk")))
    exercises = [topic.split(":", 1)[1] for topic in topics if topic.startswith("exercise:")]
    if exercises:
        vocabulary = exercise_vocabulary()
        names = sorted({name for key in exercises for name in vocabulary.get(key, ())})
        if names:
            match |= Exists(PostWorkoutExercise.objects.filter(
                post_workout__post=OuterRef("pk"), name__in=names
            ))
    return match or None


# --------------------------------------------------------------- preferences

def preferences_for(viewer):
    """This reader's choices about their page, or the defaults if they never
    made any. Unsaved defaults rather than a row created on read, so looking
    at the page never writes."""
    return (
        RecommendationPreferences.objects.filter(viewer=viewer).first()
        or RecommendationPreferences(viewer=viewer)
    )


# ------------------------------------------------------------------- profile

def _squash(value, scale=4.0):
    """Map an unbounded tally onto (-1, 1). A few signals move it a lot; many
    more move it a little more, which is how interest actually saturates."""
    return math.tanh(value / scale)


@dataclass
class InterestProfile:
    topics: dict = field(default_factory=dict)
    authors: dict = field(default_factory=dict)
    #: Topics from the last MOMENTUM_HOURS alone, squashed more steeply: two
    #: likes this afternoon say more about tonight than twenty last month.
    momentum: dict = field(default_factory=dict)
    followed: frozenset = frozenset()
    #: Authors this reader has done anything positive with, followed or not.
    engaged_authors: frozenset = frozenset()
    #: Posts this reader liked, commented on, saved or shared, newest first.
    #: What similar taste is measured from.
    engaged_posts: tuple = ()
    #: post id -> how much having seen it counts against it, from 0 (long
    #: ago) up to 3 (just now, several times, scrolled past).
    seen: dict = field(default_factory=dict)
    declared: frozenset = frozenset()
    #: Of those, what the reader literally picked -- an onboarding answer's
    #: own topic, a discipline on their profile -- as opposed to what the
    #: ranking reads into it (Running implies triathlon; nobody said so).
    chosen: frozenset = frozenset()
    #: Topics the reader asked never to be shown.
    hidden: frozenset = frozenset()
    #: author id -> 0-1: how much the reader has been shown this author
    #: lately without ever engaging with them (FATIGUE_HOURS).
    fatigue: dict = field(default_factory=dict)
    signal_count: int = 0
    #: False when the reader turned personalisation off.
    personalized: bool = True

    @property
    def confidence(self):
        """0-1: how much the page knows about this reader yet, for how much
        extra variety to give them. Somebody who named their interests or
        follows people has said what they want, and gets it; somebody who has
        said nothing yet -- or turned personalisation off, so that nothing
        they say is used -- is shown a deliberately wider spread."""
        if self.declared or self.followed:
            return 1.0
        return min(1.0, self.signal_count / CONFIDENT_SIGNALS)

    @property
    def is_new(self):
        """No history, no follows, no declared interests: a cold start."""
        return not (self.signal_count or self.followed or self.declared)

    def affinity(self, topic):
        """Long-term interest and momentum together, kept within (-1, 1)."""
        return max(-1.0, min(1.0, self.topics.get(topic, 0.0) + self.momentum.get(topic, 0.0)))

    def top_topics(self, count=5, exercises=False):
        """The reader's strongest topics -- or, with `exercises`, their
        strongest exercises. Kept apart so that a lifter's favourite lifts
        never push meals or running out of what they are known to like."""
        names = {
            topic for topic in set(self.topics) | set(self.momentum)
            if topic.startswith("exercise:") == exercises
        }
        positive = [(self.affinity(topic), topic) for topic in names if self.affinity(topic) > 0]
        return [topic for _, topic in sorted(positive, reverse=True)[:count]]


def declared_topics(viewer):
    """(every topic somebody named for themselves, the ones they literally
    chose). Onboarding answers and profile disciplines both imply more than
    they say -- Strength means lifting and five disciplines -- and the
    ranking uses all of it; only the first part is the reader's own words."""
    topics, chosen = set(), set()
    choice = Personalization.objects.filter(repbase_user=viewer).first()
    if choice is not None:
        for answer in choice.training_types or []:
            if isinstance(answer, str):
                implied = TRAINING_TYPE_TOPICS.get(answer.strip().lower(), ())
                topics.update(implied)
                chosen.update(implied[:1])
        emphasis = EMPHASIS_TOPICS.get((choice.emphasis or "").strip().lower(), ())
        topics.update(emphasis)
        chosen.update(emphasis)
    for discipline in UserDiscipline.objects.filter(profile=viewer).values_list(
        "discipline", flat=True
    ):
        topics.add(f"discipline:{discipline}")
        chosen.add(f"discipline:{discipline}")
        topics.update(DISCIPLINE_TOPICS.get(discipline, ()))
    return frozenset(topics), frozenset(chosen)


def _post_topics_by_id(post_ids):
    """{post id: (author id, topics, clip length in ms or None, exercise
    topics)} for posts somebody interacted with or saw."""
    rows = list(
        Post.objects.filter(pk__in=post_ids).values(
            "pk", "author_id", "kind", "workout__workout_type", "planner__category",
            "video__status", "video__duration_ms",
        )
    )
    disciplines = _disciplines_by_author({row["author_id"] for row in rows})
    details = details_by_post([row["pk"] for row in rows if row["kind"] == Post.Kind.WORKOUT])
    return {
        row["pk"]: (
            row["author_id"],
            topics_for(
                row["kind"],
                row["workout__workout_type"],
                row["planner__category"],
                row["video__status"] == PostVideo.Status.APPROVED,
                disciplines.get(row["author_id"], ()),
            ),
            row["video__duration_ms"],
            details.get(row["pk"], {}),
        )
        for row in rows
    }


def _disciplines_by_author(author_ids):
    found = {}
    for author_id, discipline in UserDiscipline.objects.filter(
        profile_id__in=author_ids
    ).values_list("profile_id", "discipline"):
        found.setdefault(author_id, []).append(discipline)
    return found


def _seen_weight(count, age_days, skipped):
    """How much having seen a post counts against showing it again.

    Fades with time, so something seen last week can come back, and grows
    with repeats, so something seen four times does not."""
    repeats = min(1 + 0.5 * math.log2(count), 2.0) if count > 1 else 1.0
    return repeats * 0.5 ** (age_days / SEEN_HALF_LIFE_DAYS) * (1.5 if skipped else 1.0)


def build_profile(viewer, weights=None, now=None, preferences=None):
    """What this reader is into, from what they have done and said."""
    weights = weights or FeedWeights.from_settings()
    now = now or timezone.now()
    preferences = preferences or preferences_for(viewer)
    hidden = frozenset(topic for topic in preferences.hidden_topics or () if topic in HIDEABLE_TOPICS)
    window_start = now - timedelta(days=WINDOW_DAYS)
    since = now - timedelta(days=HISTORY_DAYS)
    # Clearing the history means what came before stops counting as taste.
    # It still counts as seen: a cleared page that opens on everything
    # already scrolled through would be a worse page, not a fresh one.
    if preferences.history_cleared_at and preferences.history_cleared_at > since:
        since = preferences.history_cleared_at
    momentum_start = now - timedelta(hours=MOMENTUM_HOURS)

    views = list(
        PostView.objects.filter(viewer=viewer, last_seen_at__gte=now - timedelta(days=HISTORY_DAYS))
        .order_by("-last_seen_at")
        .values_list(
            "post_id", "last_seen_at", "total_dwell_ms", "view_count", "completed",
            "skipped", "max_watch_ms", "surface",
        )[:VIEW_LIMIT]
    )
    seen = {}
    for post_id, when, _, count, _, skipped, _, _ in views:
        if when >= window_start:
            age_days = max((now - when).total_seconds(), 0) / 86400
            seen[post_id] = _seen_weight(count, age_days, skipped)

    if not preferences.personalized:
        # Nothing the reader did shapes the page, except that it does not
        # repeat itself and that hidden topics stay hidden.
        return InterestProfile(seen=seen, hidden=hidden, personalized=False)

    # (post id, weight, when, explicit). Every list is bounded twice: by age
    # and count. Explicit positive actions are what similar taste is
    # measured from.
    events = []

    def take(rows, weight, explicit=False):
        events.extend((post_id, weight, when, explicit) for post_id, when in rows if post_id)

    take(
        PostLike.objects.filter(user=viewer, created_at__gte=since)
        .exclude(post__author=viewer).order_by("-created_at")
        .values_list("post_id", "created_at")[:HISTORY_LIMIT],
        weights.like, True,
    )
    take(
        PostComment.objects.filter(author=viewer, created_at__gte=since)
        .exclude(post__author=viewer).order_by("-created_at")
        .values_list("post_id", "created_at")[:HISTORY_LIMIT],
        weights.comment, True,
    )
    for model in (WorkoutTemplate, SavedFoodMeal):
        take(
            model.objects.filter(owner=viewer, source_post__isnull=False, created_at__gte=since)
            .order_by("-created_at").values_list("source_post_id", "created_at")[:HISTORY_LIMIT],
            weights.save, True,
        )
    take(
        Post.objects.filter(author=viewer, kind=Post.Kind.REPOST, created_at__gte=since)
        .order_by("-created_at").values_list("repost_of_id", "created_at")[:HISTORY_LIMIT],
        weights.share, True,
    )

    watched = []
    for post_id, when, dwell, count, completed, skipped, max_watch, _ in views[:HISTORY_LIMIT]:
        if when < since:
            continue
        if completed:
            events.append((post_id, weights.completion, when, False))
        elif max_watch:
            # Half a clip is a judgement that needs the clip's length, which
            # is read with the topics below.
            watched.append((post_id, max_watch, when))
        if count and dwell / count >= LONG_VIEW_MS:
            events.append((post_id, weights.long_view, when, False))
            if count >= 2:
                # Came back to it and stayed again: the clearest thing a
                # view alone can say.
                events.append((post_id, weights.rewatch, when, False))
        if skipped:
            events.append((post_id, weights.skip, when, False))

    # What For You put on screen lately, for fatigue: whose posts keep being
    # shown to somebody who never does anything with them. Only For You:
    # scrolling through somebody's own profile is seeking them out.
    fatigue_start = now - timedelta(hours=FATIGUE_HOURS)
    shown_lately = [
        post_id for post_id, when, *_, surface in views
        if when >= fatigue_start and when >= since and surface == PostView.Surface.FOR_YOU
    ]

    take(
        PostFeedback.objects.filter(viewer=viewer, created_at__gte=since)
        .order_by("-created_at").values_list("post_id", "created_at")[:HISTORY_LIMIT],
        weights.not_interested,
    )

    topics_by_post = _post_topics_by_id(
        {post_id for post_id, _, _, _ in events}
        | {post_id for post_id, _, _ in watched}
        | set(shown_lately)
    )
    for post_id, max_watch, when in watched:
        found = topics_by_post.get(post_id)
        if found and found[2] and max_watch * 2 >= found[2]:
            events.append((post_id, weights.half_watch, when, False))

    topic_tally = Counter()
    momentum_tally = Counter()
    author_tally = Counter()
    engaged = set()
    explicit = []
    for post_id, weight, when, is_explicit in sorted(events, key=lambda event: event[2], reverse=True):
        found = topics_by_post.get(post_id)
        if found is None:
            continue
        author_id, topics, _, details = found
        age_days = max((now - when).total_seconds(), 0) / 86400
        decayed = weight * 0.5 ** (age_days / SIGNAL_HALF_LIFE_DAYS)
        for topic, share in topics.items():
            topic_tally[topic] += decayed * share
            if when >= momentum_start:
                momentum_tally[topic] += weight * share
        for topic, share in details.items():
            topic_tally[topic] += decayed * share * DETAIL_SHARE
            if when >= momentum_start:
                momentum_tally[topic] += weight * share * DETAIL_SHARE
        # Dismissing one post is mostly about the post; it says less about
        # everything else its author might make.
        author_tally[author_id] += decayed * (0.75 if weight < 0 else 1.0)
        if weight > 0:
            engaged.add(author_id)
        if is_explicit and weight > 0 and post_id not in explicit:
            explicit.append(post_id)

    declared, chosen = declared_topics(viewer)
    for topic in declared:
        topic_tally[topic] += weights.declared

    followed = frozenset(
        Follow.objects.filter(follower=viewer).values_list("following_id", flat=True)
    )
    for author_id in followed:
        author_tally[author_id] += weights.follow

    engaged_authors = frozenset(engaged) | followed
    exposures = Counter()
    for post_id in shown_lately:
        found = topics_by_post.get(post_id)
        if found is not None and found[0] not in engaged_authors:
            exposures[found[0]] += 1

    return InterestProfile(
        topics={topic: _squash(value) for topic, value in topic_tally.items()},
        authors={author: _squash(value) for author, value in author_tally.items()},
        momentum={topic: _squash(value, 2.0) for topic, value in momentum_tally.items() if value},
        followed=followed,
        engaged_authors=engaged_authors,
        engaged_posts=tuple(explicit[:SEED_LIMIT]),
        seen=seen,
        declared=declared,
        chosen=chosen,
        hidden=hidden,
        # The first look is free: one post on screen and scrolled on from
        # is no verdict on its author. Every one after it counts.
        fatigue={
            author: _squash(n - 1, FATIGUE_SCALE) for author, n in exposures.items() if n > 1
        },
        signal_count=len(events),
    )


def interests(profile, limit=8):
    """What the page has picked up about a reader, strongest first, for
    showing back to them. Topics only -- never authors or posts, which would
    turn "what Rytivo thinks you like" into a list of who you look at.

    One line per name a person would recognise: running as a workout type and
    running as somebody's discipline are the same interest to the reader, so
    they are shown once, under the topic that can be hidden, at the strength
    of the stronger. `declared` is true only for what the reader picked
    themselves, never for what the ranking inferred from it."""
    if not profile.personalized:
        return []
    by_label = {}
    for topic in set(profile.topics) | set(profile.momentum):
        strength = profile.affinity(topic)
        if strength <= 0.05 or topic in profile.hidden:
            continue
        label = topic_label(topic)
        current = by_label.get(label)
        if current is None:
            by_label[label] = {
                "topic": topic, "label": label, "strength": strength,
                "declared": topic in profile.chosen,
            }
            continue
        current["declared"] = current["declared"] or topic in profile.chosen
        current["strength"] = max(current["strength"], strength)
        if topic in HIDEABLE_TOPICS and current["topic"] not in HIDEABLE_TOPICS:
            current["topic"] = topic
    ranked = sorted(by_label.values(), key=lambda item: (-item["strength"], item["label"]))[:limit]
    for item in ranked:
        item["strength"] = round(item["strength"], 3)
    return ranked


# ----------------------------------------------------------- shared counts
#
# A post's likes, comments and views are the same whoever is asking, so they
# are counted once and kept for a couple of minutes where every worker can
# read them. Counting them per reader was most of the cost of a ranking, and
# a ranking does not need them to the second.

@dataclass(frozen=True)
class Engagement:
    """One post's standing with everybody, the same whoever is asking.

    Each action twice: as the rows stand, and counted only from accounts
    that can be trusted -- once per person, never the author, never a
    suspended account, and less for brand-new, bursting or single-minded
    ones (`core.feed_integrity`). `FeedWeights.trust` chooses between them,
    so the difference can be run as an experiment."""

    likes: int = 0
    comments: int = 0
    shares: int = 0
    saves: int = 0
    impressions: int = 0
    long_views: int = 0
    completions: int = 0
    trusted_likes: float = 0.0
    trusted_comments: float = 0.0
    trusted_shares: float = 0.0
    trusted_saves: float = 0.0
    #: Viewers whose latest viewing was a scroll past, the young ones
    #: counted at half.
    skips: float = 0.0
    #: Viewers who said "not interested", the young ones counted at half.
    dismissals: float = 0.0


def _cached_many(prefix, ids, compute, ttl):
    """{id: value} for these ids, from the cache where it has them and from
    `compute(missing ids)` where it does not. A bounded number of queries
    however many ids there are."""
    keys = {pk: f"for-you:{prefix}:{pk}" for pk in ids}
    hits = cache.get_many(list(keys.values())) if keys else {}
    found = {pk: hits[key] for pk, key in keys.items() if key in hits}
    missing = [pk for pk in keys if pk not in found]
    if missing:
        computed = compute(missing)
        cache.set_many({keys[pk]: value for pk, value in computed.items()}, ttl)
        found.update(computed)
    return found


def _grouped(queryset, field_name, **counts):
    """{value of field_name: {count name: n}} in one query."""
    rows = queryset.order_by().values(field_name).annotate(**counts)
    return {row[field_name]: row for row in rows}


def _people(queryset, post_field, person, inactive, also=None):
    """{post id: {rows, people}} in one query: every row, and the distinct
    people behind them who are neither the post's author nor suspended (and
    meet `also`). What an action counts for before trust: once per person,
    however many times they did it."""
    counted = ~Q(**{f"{person}_id": F(f"{post_field}__author_id")})
    if inactive:
        counted &= ~Q(**{f"{person}_id__in": inactive})
    if also is not None:
        counted &= also
    return _grouped(
        queryset, f"{post_field}_id",
        rows=Count("pk"), people=Count(f"{person}_id", distinct=True, filter=counted),
    )


def _flagged(queryset, post_field, person, inactive, bursting, young, also=None):
    """[(post id, person id, author id, when they joined)], once per person
    and post, for the people among these rows who count for less than one:
    the young (`core.feed_integrity.young_since`) and the bursting. They are
    a small share of anybody's audience, so this is a short list however
    much engagement there is."""
    who = Q(**{f"{person}__created_at__gt": young})
    if bursting:
        who |= Q(**{f"{person}_id__in": bursting})
    rows = queryset.filter(who).exclude(**{f"{person}_id": F(f"{post_field}__author_id")})
    if inactive:
        rows = rows.exclude(**{f"{person}_id__in": inactive})
    if also is not None:
        rows = rows.filter(also)
    return list(
        rows.order_by().values_list(
            f"{post_field}_id", f"{person}_id", f"{post_field}__author_id", f"{person}__created_at"
        ).distinct()
    )


def _count_engagement(post_ids, now=None):
    """Every post's counts, as the rows stand and from trusted accounts.

    Each action counts once per person and as much as that person can be
    trusted (`core.feed_integrity`): never the author's own, nothing from a
    suspended account, less from young, bursting or single-minded ones. The
    counting is done by the database; only the engagement of the few people
    who count for less than one is read row by row, to take off what they
    are not worth. Views, scroll-pasts and "not interested" count only from
    accounts past their first two days, and "not interested" only from
    somebody who was shown the post: a crowd of fresh accounts must not be
    able to bury a post any more than to lift one.

    A bounded number of queries for any number of posts."""
    from .feed_integrity import new_since, young_since

    now = now or timezone.now()
    inactive = inactive_accounts()
    bursting = sorted(bursting_accounts(now))
    young = young_since(now)
    # A comment from somebody the author has blocked is not engagement to
    # reward, though it is still a row. A comment a moderator hid is not
    # counted at all.
    unblocked = ~Exists(Block.objects.filter(blocker=OuterRef("post__author"), blocked=OuterRef("author")))
    sources = {
        "likes": [(PostLike.objects.filter(post_id__in=post_ids), "post", "user", None)],
        "comments": [(
            PostComment.objects.filter(post_id__in=post_ids, is_hidden=False), "post", "author", unblocked,
        )],
        "shares": [(Post.objects.filter(repost_of_id__in=post_ids), "repost_of", "author", None)],
        "saves": [
            (WorkoutTemplate.objects.filter(source_post_id__in=post_ids), "source_post", "owner", None),
            (SavedFoodMeal.objects.filter(source_post_id__in=post_ids), "source_post", "owner", None),
        ],
    }
    raw = {name: Counter() for name in sources}
    trusted = {name: Counter() for name in sources}
    flagged = {name: [] for name in sources}
    for name, parts in sources.items():
        for queryset, post_field, person, also in parts:
            for post_id, row in _people(queryset, post_field, person, inactive, also).items():
                raw[name][post_id] += row["rows"]
                trusted[name][post_id] += row["people"]
            flagged[name] += _flagged(queryset, post_field, person, inactive, bursting, young, also)
    trust = account_trust({
        person: joined for rows in flagged.values() for _, person, _, joined in rows
    }, now)
    for name, rows in flagged.items():
        for post_id, person, author, _ in rows:
            trusted[name][post_id] -= 1 - trust.get(person, FULL_TRUST).for_author(author)

    # Turning a post down counts for YOUNG_REJECTION_SHARE less from an
    # account younger than YOUNG_REJECTION_DAYS: burying a post with a crowd
    # should cost as much as lifting one.
    established = Q(viewer__created_at__lte=new_since(now))
    youngish = Q(viewer__created_at__gt=now - timedelta(days=YOUNG_REJECTION_DAYS))
    views = _grouped(
        PostView.objects.filter(established, post_id__in=post_ids).exclude(viewer_id__in=inactive),
        "post_id",
        impressions=Count("pk"),
        long_views=Count("pk", filter=Q(total_dwell_ms__gte=LONG_VIEW_MS)),
        completions=Count("pk", filter=Q(completed=True)),
        skips=Count("pk", filter=Q(skipped=True)),
        young_skips=Count("pk", filter=Q(skipped=True) & youngish),
    )
    dismissals = _grouped(
        PostFeedback.objects.filter(established, post_id__in=post_ids)
        .filter(Exists(PostView.objects.filter(viewer=OuterRef("viewer"), post=OuterRef("post"))))
        .exclude(viewer_id__in=inactive),
        "post_id", n=Count("pk"), young=Count("pk", filter=youngish),
    )

    def n(table, pk, name="n"):
        return table.get(pk, {}).get(name, 0)

    return {
        pk: Engagement(
            likes=raw["likes"][pk],
            comments=raw["comments"][pk],
            shares=raw["shares"][pk],
            saves=raw["saves"][pk],
            impressions=n(views, pk, "impressions"),
            long_views=n(views, pk, "long_views"),
            completions=n(views, pk, "completions"),
            trusted_likes=round(max(trusted["likes"][pk], 0.0), 4),
            trusted_comments=round(max(trusted["comments"][pk], 0.0), 4),
            trusted_shares=round(max(trusted["shares"][pk], 0.0), 4),
            trusted_saves=round(max(trusted["saves"][pk], 0.0), 4),
            skips=n(views, pk, "skips") - YOUNG_REJECTION_SHARE * n(views, pk, "young_skips"),
            dismissals=n(dismissals, pk) - YOUNG_REJECTION_SHARE * n(dismissals, pk, "young"),
        )
        for pk in post_ids
    }


def engagement_for(post_ids, now=None):
    """{post id: Engagement}, shared across readers for STATS_TTL_SECONDS.
    The key carries a version, so counts cached by an earlier shape of
    Engagement are never read back as this one."""
    return _cached_many(
        "stats5", post_ids, lambda ids: _count_engagement(ids, now), STATS_TTL_SECONDS
    )


def _count_followers(author_ids):
    counts = dict(
        Follow.objects.filter(following_id__in=author_ids).order_by()
        .values("following_id").annotate(n=Count("pk")).values_list("following_id", "n")
    )
    return {pk: counts.get(pk, 0) for pk in author_ids}


def follower_counts(author_ids):
    return _cached_many("followers", author_ids, _count_followers, FOLLOWERS_TTL_SECONDS)


def trending_post_ids(now=None):
    """The most liked recent posts this week, for everybody. Eligibility is
    applied per reader afterwards; this is only where to look."""
    found = cache.get("for-you:trending")
    if found is None:
        now = now or timezone.now()
        # Counted only from active accounts older than a couple of days,
        # and never the author's own: a ring of fresh accounts liking one
        # post is the cheapest way there is to manufacture a trend.
        found = list(
            PostLike.objects.filter(
                created_at__gte=now - timedelta(days=TRENDING_DAYS),
                post__created_at__gte=now - timedelta(days=WINDOW_DAYS),
            )
            .filter(user__created_at__lte=new_since(now))
            .exclude(user_id__in=inactive_accounts())
            .exclude(user_id=F("post__author_id"))
            .order_by().values("post_id").annotate(n=Count("pk"))
            .order_by("-n", "-post_id").values_list("post_id", flat=True)[:TRENDING_LIMIT]
        )
        cache.set("for-you:trending", found, TRENDING_TTL_SECONDS)
    return found


# ------------------------------------------------------------ similar taste

@dataclass(frozen=True)
class Similar:
    #: 0-1: how strongly people like this reader went for it, relative to the
    #: best such post, scaled down when there are few such people.
    score: float
    #: How many of them liked it.
    voters: int


def similar_tastes(viewer, profile, now=None):
    """{post id: Similar}: recent posts liked by people whose likes overlap
    this reader's.

    Similarity is cosine: shared likes over the geometric mean of both
    histories, so somebody who likes everything is not everybody's twin.
    Neighbours are never named or shown -- they only move posts -- and blocked
    or suspended accounts are not anyone's neighbour.
    """
    from .views import without_blocked_authors

    now = now or timezone.now()
    seeds = list(profile.engaged_posts[:SEED_LIMIT])
    if not profile.personalized or len(seeds) < MIN_OVERLAP:
        return {}
    likers = without_blocked_authors(
        viewer,
        PostLike.objects.filter(post_id__in=seeds, user__user__is_active=True).exclude(user=viewer),
        "user",
    )
    overlap = list(
        likers.order_by().values("user_id").annotate(shared=Count("pk"))
        .filter(shared__gte=MIN_OVERLAP).order_by("-shared", "user_id")
        .values_list("user_id", "shared")[:NEIGHBOUR_POOL]
    )
    if not overlap:
        return {}
    totals = dict(
        PostLike.objects.filter(
            user_id__in=[user for user, _ in overlap],
            created_at__gte=now - timedelta(days=HISTORY_DAYS),
        ).order_by().values("user_id").annotate(n=Count("pk")).values_list("user_id", "n")
    )
    # A suspended or brand-new account is nobody's taste twin, and one that
    # bursts or exists to like one person's posts counts for less
    # (`Trust.as_neighbour`): otherwise a crowd of fresh accounts liking the
    # same things would be the cheapest way there is to fake a taste.
    trust = trust_by_id([user for user, _ in overlap], now)
    similarity = {
        user: shared / math.sqrt(len(seeds) * max(totals.get(user, shared), shared))
        * trust.get(user, FULL_TRUST).as_neighbour()
        for user, shared in overlap
    }
    similarity = {user: value for user, value in similarity.items() if value > 0}
    neighbours = sorted(similarity, key=lambda user: (-similarity[user], user))[:NEIGHBOURS]
    if not neighbours:
        return {}
    pairs = (
        PostLike.objects.filter(
            user_id__in=neighbours, created_at__gte=now - timedelta(days=WINDOW_DAYS),
            post__created_at__gte=now - timedelta(days=WINDOW_DAYS),
        )
        .exclude(post_id__in=seeds).exclude(post__author=viewer)
        .order_by("-created_at").values_list("user_id", "post_id")[:NEIGHBOUR_LIKES]
    )
    tally, voters = Counter(), Counter()
    for user, post_id in pairs:
        tally[post_id] += similarity[user]
        voters[post_id] += 1
    if not tally:
        return {}
    best = max(tally.values())
    confidence = min(1.0, len(neighbours) / CONFIDENT_NEIGHBOURS)
    top = sorted(tally, key=lambda post_id: (-tally[post_id], -post_id))[:SOURCE_LIMIT]
    return {post_id: Similar(tally[post_id] / best * confidence, voters[post_id]) for post_id in top}


# ---------------------------------------------------------------- candidates

def eligible_posts(viewer, now=None, hidden=()):
    """Every post that may be recommended to this reader, before ranking.

    Built on `visible_posts_for`, so nothing here can be shown that the
    reader could not open anyway: hidden posts, blocked authors in either
    direction, private posts and closed profiles are already gone. On top of
    that, what does not belong on a page of recommendations.
    """
    from .views import visible_posts_for

    now = now or timezone.now()
    open_reports = (
        PostReport.objects.filter(post=OuterRef("pk"), reviewed_at__isnull=True)
        .order_by().values("post").annotate(n=Count("pk")).values("n")[:1]
    )
    queryset = (
        visible_posts_for(viewer, Post.objects.all(), include_reposts=False)
        .exclude(kind=Post.Kind.REPOST)
        .exclude(author=viewer)
        .filter(created_at__gte=now - timedelta(days=WINDOW_DAYS))
        .exclude(Exists(PostFeedback.objects.filter(viewer=viewer, post=OuterRef("pk"))))
        .exclude(Exists(PostReport.objects.filter(reporter=viewer, post=OuterRef("pk"))))
        # Something the reader already liked, commented on, saved or passed
        # on is something they have already found. Showing it again is the
        # page repeating itself.
        .exclude(Exists(PostLike.objects.filter(user=viewer, post=OuterRef("pk"))))
        .exclude(Exists(PostComment.objects.filter(author=viewer, post=OuterRef("pk"))))
        .exclude(Exists(Post.objects.filter(author=viewer, repost_of=OuterRef("pk"))))
        .exclude(Exists(WorkoutTemplate.objects.filter(owner=viewer, source_post=OuterRef("pk"))))
        .exclude(Exists(SavedFoodMeal.objects.filter(owner=viewer, source_post=OuterRef("pk"))))
        # A clip waiting for review is its author's alone; a post whose clip
        # nobody else may watch is not one to put in front of strangers.
        .exclude(
            Exists(
                PostVideo.objects.filter(post=OuterRef("pk")).exclude(
                    status=PostVideo.Status.APPROVED
                )
            )
        )
        .annotate(open_reports=Coalesce(Subquery(open_reports), Value(0)))
        .filter(open_reports__lt=REPORT_THRESHOLD)
    )
    # An author a moderator acted against lately is not put in front of
    # strangers (core.feed_integrity.MODERATION_COOLDOWN_DAYS). Their
    # followers chose them, and still get them.
    moderated = recently_moderated_authors(now)
    if moderated:
        queryset = queryset.exclude(author_id__in=moderated, viewer_follows_author=False)
    hiding = _topic_filter([topic for topic in hidden if topic in HIDEABLE_TOPICS])
    if hiding is not None:
        queryset = queryset.exclude(hiding)
    return queryset


_CANDIDATE_FIELDS = (
    "pk", "author_id", "author__created_at", "kind", "created_at", "workout__workout_type",
    "planner__category", "video__status", "open_reports", "caption",
)


@dataclass
class Candidate:
    pk: int
    author_id: int
    created_at: object
    topics: dict
    has_video: bool
    open_reports: int
    likes: int
    comments: int
    shares: int
    saves: int
    impressions: int
    long_views: int
    completions: int
    #: The same counts from trusted accounts only (`Engagement`). Default to
    #: the raw ones when not given.
    trusted_likes: float = None
    trusted_comments: float = None
    trusted_shares: float = None
    trusted_saves: float = None
    skips: int = 0
    dismissals: int = 0
    #: 0-1, from `similar_tastes`.
    similar: float = 0.0
    similar_voters: int = 0
    #: 0-1: how new and how small the author is.
    newcomer: float = 0.0
    #: 0-1: how much the caption reads as bait or a sales pitch.
    bait: float = 0.0
    #: {exercise topic: share}: what a workout is about in detail.
    details: dict = field(default_factory=dict)
    sources: set = field(default_factory=set)
    score: float = 0.0
    parts: dict = field(default_factory=dict)

    def __post_init__(self):
        for name in ("likes", "comments", "shares", "saves"):
            if getattr(self, f"trusted_{name}") is None:
                setattr(self, f"trusted_{name}", getattr(self, name))

    @property
    def main_topic(self):
        return primary_topic(self.topics)


def _newcomer_scores(rows, now):
    """{author id: 0-1} for authors who joined within NEWCOMER_DAYS."""
    joined_after = now - timedelta(days=NEWCOMER_DAYS)
    young = sorted({
        row["author_id"] for row in rows
        if row["author__created_at"] and row["author__created_at"] >= joined_after
    })
    if not young:
        return {}
    followers = follower_counts(young)
    scale = math.log1p(NEWCOMER_FOLLOWERS)
    return {
        author_id: max(0.0, 1 - math.log1p(followers.get(author_id, 0)) / scale)
        for author_id in young
    }


def _as_candidates(rows, similar, now):
    """Turn candidate rows into Candidates: topics, shared counts, similar
    taste and newcomer lift. A fixed number of queries for any number."""
    if not rows:
        return []
    disciplines = _disciplines_by_author({row["author_id"] for row, _ in rows.values()})
    stats = engagement_for(sorted(rows), now)
    newcomers = _newcomer_scores([row for row, _ in rows.values()], now)
    details = details_by_post(sorted(pk for pk, (row, _) in rows.items() if row["kind"] == Post.Kind.WORKOUT))
    found = []
    for pk, (row, names) in rows.items():
        has_video = row["video__status"] == PostVideo.Status.APPROVED
        counts = stats.get(pk) or Engagement()
        taste = similar.get(pk)
        found.append(Candidate(
            pk=pk,
            author_id=row["author_id"],
            created_at=row["created_at"],
            topics=topics_for(
                row["kind"], row["workout__workout_type"], row["planner__category"],
                has_video, disciplines.get(row["author_id"], ()),
            ),
            has_video=has_video,
            open_reports=row["open_reports"],
            likes=counts.likes,
            comments=counts.comments,
            shares=counts.shares,
            saves=counts.saves,
            impressions=counts.impressions,
            long_views=counts.long_views,
            completions=counts.completions,
            trusted_likes=counts.trusted_likes,
            trusted_comments=counts.trusted_comments,
            trusted_shares=counts.trusted_shares,
            trusted_saves=counts.trusted_saves,
            skips=counts.skips,
            dismissals=counts.dismissals,
            similar=taste.score if taste else 0.0,
            similar_voters=taste.voters if taste else 0,
            newcomer=newcomers.get(row["author_id"], 0.0),
            bait=caption_flags(row.get("caption")),
            details=details.get(pk, {}),
            sources=names,
        ))
    return found


def candidates(viewer, profile, now=None, similar=None):
    """Recent eligible posts from up to six sources, merged, each capped."""
    now = now or timezone.now()
    eligible = eligible_posts(viewer, now, profile.hidden)
    newest = ("-created_at", "-id")
    sources = [("fresh", eligible.order_by(*newest)[:POOL_SIZE])]

    if profile.followed:
        sources.append((
            "following",
            eligible.filter(author_id__in=profile.followed).order_by(*newest)[:SOURCE_LIMIT],
        ))

    match = _topic_filter(profile.top_topics(5))
    if match is not None:
        sources.append(("interest", eligible.filter(match).order_by(*newest)[:SOURCE_LIMIT]))
    # The reader's best exercises, as a source of their own: OR'd into the
    # one above they would cost the database a much worse plan.
    match = _topic_filter(profile.top_topics(3, exercises=True))
    if match is not None:
        sources.append(("detail", eligible.filter(match).order_by(*newest)[:SOURCE_LIMIT]))

    similar = similar if similar is not None else similar_tastes(viewer, profile, now)
    if similar:
        sources.append((
            "similar", eligible.filter(pk__in=list(similar)).order_by(*newest)[:SOURCE_LIMIT],
        ))

    trending = trending_post_ids(now)
    if trending:
        sources.append(("trending", eligible.filter(pk__in=trending).order_by(*newest)[:SOURCE_LIMIT]))

    rows = {}
    for name, queryset in sources:
        for row in queryset.values(*_CANDIDATE_FIELDS):
            rows.setdefault(row["pk"], (row, set()))[1].add(name)
    return _as_candidates(rows, similar, now)


def candidates_by_id(viewer, profile, post_ids, similar=None, now=None):
    """These posts as candidates again, minus any that stopped being
    eligible -- deleted, hidden, dismissed, liked, blocked -- since they were
    first found. What a later page of a session ranks from."""
    if not post_ids:
        return []
    now = now or timezone.now()
    rows = {
        row["pk"]: (row, set())
        for row in eligible_posts(viewer, now, profile.hidden)
        .filter(pk__in=list(post_ids)).values(*_CANDIDATE_FIELDS)
    }
    return _as_candidates(rows, similar or {}, now)


# ------------------------------------------------------------------- scoring

def _explored(mean, draw, explore):
    """Somewhere between a post's average and a draw from what its record
    allows. With few observations the draw ranges widely, and that is the
    point: a new post is sometimes ranked as though it were very good, and
    finds out whether it is."""
    return mean + explore * (draw - mean)


def _match(topics, affinity):
    """Weighted mean of the reader's affinity for these topics."""
    total = sum(topics.values())
    if not total:
        return 0.0
    return sum(affinity(topic) * share for topic, share in topics.items()) / total


def score(candidate, profile, weights, now, rng=None):
    """A candidate's score, and the parts it is made of.

    Three kinds of part, added up:

    - **how well it suits this reader**: topic, momentum, detail (the
      exercises in it), author, follows, similar taste;
    - **how well it does with everybody**, counted from trusted engagement
      per view so a post that was shown a lot is not mistaken for a good one:
      quality (saves and shares first), popularity, watch, and against it
      how often others scrolled past or said "not interested";
    - **what keeps the page honest and fresh**: recency, newcomer, and
      against it having been seen, an author shown again and again to no
      effect, open reports and bait.
    """
    topic_match = _match(candidate.topics, lambda topic: profile.topics.get(topic, 0.0))
    momentum = _match(candidate.topics, lambda topic: profile.momentum.get(topic, 0.0))
    detail = _match(candidate.details, profile.affinity) if candidate.details else 0.0

    trust = weights.trust
    likes = blend(candidate.likes, candidate.trusted_likes, trust)
    comments = blend(candidate.comments, candidate.trusted_comments, trust)
    saves = blend(candidate.saves, candidate.trusted_saves, trust)
    shares = blend(candidate.shares, candidate.trusted_shares, trust)
    engagement = max(0.0, (
        weights.like_value * likes
        + weights.comment_value * comments
        + weights.save_value * saves
        + weights.share_value * shares
    ))
    # Engagement per view, pulled toward a modest prior until there are
    # enough views to say otherwise: one like on one view is not the best post
    # on the server. The posterior is a gamma -- engagement is weighted, so a
    # count rather than a yes-or-no per view -- whose mean is that smoothed
    # rate; exploring draws from it instead.
    alpha = QUALITY_PRIOR_RATE * QUALITY_PRIOR_VIEWS + engagement
    beta = QUALITY_PRIOR_VIEWS + candidate.impressions
    rate = alpha / beta
    stayed = min(candidate.completions if candidate.has_video else candidate.long_views,
                 candidate.impressions)
    watch = (stayed + 1) / (candidate.impressions + 2)
    if rng is not None and weights.explore:
        rate = _explored(rate, rng.gammavariate(alpha, 1 / beta), weights.explore)
    quality = 1 - math.exp(-max(rate, 0.0) / 0.3)
    popularity = min(1.0, math.log1p(engagement) / math.log1p(50))
    age_hours = max((now - candidate.created_at).total_seconds(), 0) / 3600
    recency = 0.5 ** (age_hours / RECENCY_HALF_LIFE_HOURS)
    reported = min(candidate.open_reports, REPORT_THRESHOLD) / REPORT_THRESHOLD
    # The negative half of the value of a post: how often the people it was
    # shown to turned it down. Measured against what a post with no record
    # is assumed to get, so a post nobody has seen is neither helped nor
    # hurt, and one people stop on is helped a little.
    skip_rate = (
        (candidate.skips + SKIP_PRIOR_RATE * SKIP_PRIOR_VIEWS)
        / (candidate.impressions + SKIP_PRIOR_VIEWS)
    ) - SKIP_PRIOR_RATE
    # Never more dismissals than views, and never more than a ceiling's
    # worth of penalty: past it, a crowd could bury a post for the very
    # people it suits.
    hide_rate = min((
        (min(candidate.dismissals, candidate.impressions) + HIDE_PRIOR_RATE * HIDE_PRIOR_VIEWS)
        / (candidate.impressions + HIDE_PRIOR_VIEWS)
    ) - HIDE_PRIOR_RATE, HIDE_RATE_CEILING)

    parts = {
        "topic": weights.topic * topic_match,
        "momentum": weights.momentum * momentum,
        "detail": weights.detail * detail,
        "author": weights.author * profile.authors.get(candidate.author_id, 0.0),
        "follows": weights.follows * (candidate.author_id in profile.followed),
        "similar": weights.similar * candidate.similar,
        "quality": weights.quality * quality,
        "popularity": weights.popularity * popularity,
        "recency": weights.recency * recency,
        "watch": weights.watch * watch,
        "newcomer": weights.newcomer * candidate.newcomer,
        "skip_rate": -weights.skip_rate * skip_rate,
        "hide_rate": -weights.hide_rate * hide_rate,
        "seen": -weights.seen * profile.seen.get(candidate.pk, 0.0),
        "fatigue": -weights.fatigue * profile.fatigue.get(candidate.author_id, 0.0),
        "reported": -weights.reported * reported,
        "bait": -weights.bait * candidate.bait,
    }
    if rng is not None and weights.jitter:
        parts["jitter"] = weights.jitter * rng.random()
    return sum(parts.values()), parts


# ------------------------------------------------------------------- ranking

@dataclass(frozen=True)
class Ranked:
    pk: int
    reason: str
    label: str


#: Every reason a post can be given, as sent in `recommendation.reason`.
#: Plain strings on the wire, so a client that meets one it does not know
#: shows the label rather than failing to decode.
REASONS = ("following", "interest", "similar", "discovery", "new_creator", "popular", "fresh")


def _reason(candidate, profile, discovery):
    if discovery:
        return "discovery", "Something new for you"
    if candidate.author_id in profile.followed:
        return "following", "From someone you follow"
    strength, topic = max(
        (profile.affinity(topic) * share, topic) for topic, share in candidate.topics.items()
    )
    # An exercise names it more exactly when it matched about as well:
    # "because you like deadlift" says more than "because you like lifting".
    if candidate.details:
        detail_strength, detail = max(
            (profile.affinity(topic) * share, topic) for topic, share in candidate.details.items()
        )
        if detail_strength > 0.15 and detail_strength >= 0.5 * strength:
            strength, topic = detail_strength, detail
    if strength > 0.15:
        return "interest", f"Because you like {topic_name(topic)}"
    if candidate.similar_voters >= MIN_SIMILAR_VOTERS and candidate.similar >= 0.3:
        return "similar", "Liked by people with similar taste"
    if candidate.newcomer >= 0.5:
        return "new_creator", "From a new creator"
    parts = candidate.parts
    if parts.get("quality", 0) + parts.get("popularity", 0) > parts.get("recency", 0):
        return "popular", "Popular in the community"
    return "fresh", "New in the community"


@dataclass
class RankState:
    """Where a page left off: what the next slot must stay varied against.

    Carried from one page of a session to the next, so the author gap and
    the discovery slots hold across the boundary rather than restarting."""

    position: int = 0
    authors_used: Counter = field(default_factory=Counter)
    recent_authors: list = field(default_factory=list)
    recent_topics: list = field(default_factory=list)
    #: Whether each recent slot went to somebody the reader follows.
    recent_followed: list = field(default_factory=list)

    def dump(self):
        return [
            self.position, dict(self.authors_used), self.recent_authors[-8:], self.recent_topics[-8:],
            [bool(flag) for flag in self.recent_followed[-CONNECTED_WINDOW:]],
        ]

    @classmethod
    def load(cls, data):
        # A state saved before `recent_followed` existed has four parts.
        position, used, authors, topics, *rest = data
        followed = list(rest[0]) if rest else []
        # A cache that went through JSON has turned author ids into strings.
        return cls(position, Counter({int(key): value for key, value in dict(used).items()}),
                   list(authors), list(topics), followed)


def rank(found, profile, weights, rng, limit=MAX_RANKED, state=None):
    """Order the scored candidates into a page worth scrolling."""
    state = state if state is not None else RankState()
    remaining = sorted(found, key=lambda item: (-item.score, -item.pk))
    usual = set(profile.top_topics(3))
    ordered = []
    # Somebody the page knows little about yet is shown more of a spread,
    # which is also how it finds out what they like.
    topic_repeat = weights.topic_repeat * (1 + weights.cold_variety * (1 - profile.confidence))
    connected_slots = int(weights.connected_share * CONNECTED_WINDOW + 1e-9)

    def adjusted(candidate):
        return (
            candidate.score
            - weights.author_repeat * state.authors_used[candidate.author_id]
            - topic_repeat * state.recent_topics[-5:].count(candidate.main_topic)
        )

    while remaining and len(ordered) < limit:
        window = remaining[:SCAN_DEPTH]
        spaced = [
            item for item in window
            if item.author_id not in state.recent_authors[-(AUTHOR_GAP - 1):]
        ]
        pool = spaced or window
        if connected_slots < CONNECTED_WINDOW and profile.followed:
            # No more than connected_share of any CONNECTED_WINDOW slots go
            # to people the reader follows, while anybody else is there to
            # take the slot.
            lately = sum(state.recent_followed[-(CONNECTED_WINDOW - 1):])
            if lately + 1 > connected_slots:
                unconnected = [item for item in pool if item.author_id not in profile.followed]
                pool = unconnected or pool
        pick = None
        discovery = False
        every = weights.discovery_every
        if every and state.position % every == every - 1:
            # Unknown means no history either way. Somebody whose posts this
            # reader dismissed is not unknown to them, and neither is a topic
            # they have scrolled past; offering those as "something new"
            # would be undoing what the reader just told the ranking.
            unknown = [
                item for item in pool
                if item.author_id not in profile.engaged_authors
                and profile.authors.get(item.author_id, 0.0) >= 0
                and profile.affinity(item.main_topic) >= 0
            ]
            new_ground = [item for item in unknown if item.main_topic not in usual]
            options = new_ground or unknown
            if options:
                # Among the best few, so discovery is a good unknown rather
                # than the single top one every time.
                pick = rng.choice(sorted(options, key=adjusted, reverse=True)[:5])
                discovery = True
        if pick is None:
            pick = max(pool, key=adjusted)
        remaining.remove(pick)
        state.position += 1
        state.authors_used[pick.author_id] += 1
        state.recent_authors = (state.recent_authors + [pick.author_id])[-8:]
        state.recent_topics = (state.recent_topics + [pick.main_topic])[-8:]
        state.recent_followed = (
            state.recent_followed + [pick.author_id in profile.followed]
        )[-CONNECTED_WINDOW:]
        reason, label = _reason(pick, profile, discovery)
        ordered.append(Ranked(pick.pk, reason, label))
    return ordered


def new_seed():
    """Where a ranking's randomness starts. Its own function so a test can
    fix it and get the same page twice."""
    return secrets.randbits(32)


@dataclass
class Ranking:
    items: list
    candidate_count: int
    is_new: bool
    duration_ms: float
    variant: object = None


def build_ranking(viewer, now=None, seed=None, limit=MAX_RANKED):
    """Everything above, in order, for one reader, all at once.

    The page itself goes through `serve_page`, which ranks a page at a time;
    this is the same work for when the whole order is wanted -- a report, a
    test, a look at what somebody would be shown.
    """
    started = time.perf_counter()
    now = now or timezone.now()
    weights, variant = FeedWeights.for_viewer(viewer.pk)
    rng = random.Random(seed if seed is not None else new_seed())
    profile = build_profile(viewer, weights, now)
    found = candidates(viewer, profile, now)
    for candidate in found:
        candidate.score, candidate.parts = score(candidate, profile, weights, now, rng)
    items = rank(found, profile, weights, rng, limit=limit)
    duration_ms = (time.perf_counter() - started) * 1000
    return Ranking(items, len(found), profile.is_new, duration_ms, variant)


# ------------------------------------------------------------------ sessions
#
# A ranking is not an ORDER BY a database can page through, so it is kept in
# the cache for half an hour and paged by position. What a session holds:
#
# - `served`: every post handed out so far, in order, with its reason. Only
#   ever appended to, so a position means the same post on every request and
#   a retried page comes back the same.
# - `pool`: the candidates not served yet, by id. Each later page re-checks
#   them against the eligibility rules, re-scores them against a fresh
#   profile and ranks the next page from them -- which is how the page keeps
#   up with what the reader does while they scroll.
#
# The cursor names the session and the position; it is signed so a client
# cannot make one up, and the cache key carries the reader's id, so a cursor
# taken from somebody else opens nothing.

_CURSOR_SALT = "core.recommendations.cursor"


class BadCursor(ValueError):
    pass


@dataclass
class FeedSession:
    seed: int
    served: list = field(default_factory=list)
    pool: list = field(default_factory=list)
    #: post id -> (similar score, voters), for the pool. Similar taste is
    #: worked out once per session, not per page.
    similar: dict = field(default_factory=dict)
    state: RankState = field(default_factory=RankState)

    def dump(self):
        return {
            "v": 2, "seed": self.seed, "served": self.served, "pool": self.pool,
            "similar": self.similar, "state": self.state.dump(),
        }

    @classmethod
    def load(cls, data):
        if isinstance(data, list):
            # A session cached by the version before this one: the order it
            # served, and nothing left to re-rank.
            return cls(seed=0, served=[tuple(row) for row in data])
        return cls(
            seed=data["seed"],
            served=[tuple(row) for row in data["served"]],
            pool=list(data["pool"]),
            similar=dict(data["similar"]),
            state=RankState.load(data["state"]),
        )


def _session_key(viewer_id, session):
    # "v3": a session saved by this version holds a five-part rank state an
    # older version cannot read, and the other way round. Different keys mean
    # that during a rolling deploy, or after a rollback, a cursor from the
    # other version finds no session and ranks afresh from the top -- what an
    # expired session does -- rather than failing.
    return f"for-you:v3:{viewer_id}:{session}"


def _save_session(viewer, session_id, session):
    cache.set(_session_key(viewer.pk, session_id), session.dump(), SESSION_TTL_SECONDS)


def load_session(viewer, session_id):
    stored = cache.get(_session_key(viewer.pk, session_id))
    return None if stored is None else FeedSession.load(stored)


def make_cursor(session, offset):
    return signing.dumps({"s": session, "o": offset}, salt=_CURSOR_SALT, compress=False)


def read_cursor(value):
    try:
        data = signing.loads(value, salt=_CURSOR_SALT)
        session, offset = data["s"], int(data["o"])
    except (signing.BadSignature, KeyError, TypeError, ValueError):
        raise BadCursor() from None
    if not isinstance(session, str) or offset < 0:
        raise BadCursor()
    return session, offset


def _extend(session, found, profile, weights, now, need):
    """Score what is left, rank the next `need` posts, and keep the rest."""
    rng = random.Random(session.seed * 1_000_003 + len(session.served))
    for candidate in found:
        candidate.score, candidate.parts = score(candidate, profile, weights, now, rng)
    chosen = rank(found, profile, weights, rng, limit=need, state=session.state)
    taken = {item.pk for item in chosen}
    session.served.extend((item.pk, item.reason, item.label) for item in chosen)
    rest = sorted(
        (item for item in found if item.pk not in taken), key=lambda item: (-item.score, -item.pk)
    )
    session.pool = [item.pk for item in rest[:POOL_KEEP]]
    keep = set(session.pool)
    session.similar = {pk: value for pk, value in session.similar.items() if pk in keep}
    return chosen


@dataclass
class Page:
    session: str
    items: list
    #: Where the next page starts, or None at the end.
    next_offset: object
    #: Milliseconds spent ranking for this page, or None if it was served
    #: from what an earlier page had already ranked.
    duration_ms: object = None


def serve_page(viewer, session_id=None, offset=0, size=20, now=None, seed=None):
    """One page of For You: from a new ranking, or from a session's next
    stretch, re-ranked for what the reader has done since."""
    started = time.perf_counter()
    now = now or timezone.now()
    session = load_session(viewer, session_id) if session_id else None
    ranked = None
    if session is None:
        # No cursor, or the session expired: rank afresh from the top. An
        # expired session is not resumed at its old position -- position 40
        # of a different ranking is no continuation of anything, and what the
        # reader already saw is pushed down by having been seen.
        weights, variant = FeedWeights.for_viewer(viewer.pk)
        session_id = secrets.token_urlsafe(12)
        session = FeedSession(seed=seed if seed is not None else new_seed())
        offset = 0
        profile = build_profile(viewer, weights, now)
        found = candidates(viewer, profile, now)
        session.similar = {
            item.pk: (item.similar, item.similar_voters) for item in found if item.similar
        }
        ranked = _extend(session, found, profile, weights, now, size)
        _log_ranking("first", started, viewer, ranked, len(found), profile, variant)
    else:
        offset = min(offset, len(session.served))
        need = min(offset + size, MAX_RANKED) - len(session.served)
        if need > 0 and session.pool:
            weights, variant = FeedWeights.for_viewer(viewer.pk)
            profile = build_profile(viewer, weights, now)
            similar = {pk: Similar(*value) for pk, value in session.similar.items()}
            found = candidates_by_id(viewer, profile, session.pool, similar, now)
            ranked = _extend(session, found, profile, weights, now, need)
            _log_ranking("next", started, viewer, ranked, len(found), profile, variant)
    if ranked is not None:
        _save_session(viewer, session_id, session)

    items = [Ranked(*row) for row in session.served[offset:offset + size]]
    following = offset + size
    more = following < len(session.served) or (
        bool(session.pool) and len(session.served) < MAX_RANKED
    )
    remember_reasons(viewer, items)
    duration = (time.perf_counter() - started) * 1000 if ranked is not None else None
    return Page(session_id, items, following if more else None, duration)


def _log_ranking(kind, started, viewer, items, candidate_count, profile, variant):
    duration_ms = (time.perf_counter() - started) * 1000
    record_latency(duration_ms, kind=kind)
    logger.log(
        logging.INFO if kind == "first" else logging.DEBUG,
        "for_you %s ranked=%d candidates=%d new_account=%s personalized=%s variant=%s "
        "duration_ms=%.1f reasons=%s",
        kind, len(items), candidate_count, profile.is_new, profile.personalized, variant,
        duration_ms, dict(Counter(item.reason for item in items)),
    )


# ------------------------------------------------------------- attribution
#
# Which reason a post went out with, so `feed_report` can say whether
# discovery slots, similar taste or new creators are working. Kept server
# side for a day rather than echoed back by the client: a client cannot get
# it wrong, and an old client that knows nothing of it is still measured.

def _served_key(viewer_id):
    return f"for-you:served:{viewer_id}"


def remember_reasons(viewer, items):
    if not items:
        return
    known = cache.get(_served_key(viewer.pk)) or {}
    for item in items:
        known.pop(item.pk, None)
        known[item.pk] = item.reason
    if len(known) > SERVED_MEMORY:
        known = dict(list(known.items())[-SERVED_MEMORY:])
    cache.set(_served_key(viewer.pk), known, SERVED_TTL_SECONDS)


def served_reasons(viewer):
    return cache.get(_served_key(viewer.pk)) or {}


def forget_reasons(viewer):
    cache.delete(_served_key(viewer.pk))


# ---------------------------------------------------------------- monitoring
#
# Latency, kept where every worker can add to it: one cache counter per hour,
# kind and bucket. `feed_report` turns them into a distribution. Counters
# rather than a list of timings so recording one costs a single increment.
# "first" is a fresh ranking (a first page, or a pull to refresh); "next" is
# a later page re-ranked from its session.

LATENCY_BUCKETS_MS = (25, 50, 100, 250, 500, 1000, 2500)
LATENCY_KINDS = ("first", "next")


def _latency_key(hour, label, kind="first"):
    # The first kind keeps the key it always had, so counts recorded before
    # there were two kinds still read as what they were.
    prefix = "for-you:latency" if kind == "first" else f"for-you:latency:{kind}"
    return f"{prefix}:{hour}:{label}"


def _bucket_label(duration_ms):
    for limit in LATENCY_BUCKETS_MS:
        if duration_ms <= limit:
            return f"le{limit}"
    return "over"


def record_latency(duration_ms, now=None, kind="first"):
    hour = int((now or time.time()) // 3600)
    for label in (_bucket_label(duration_ms), "count"):
        key = _latency_key(hour, label, kind)
        # add() then incr(): incr alone raises on a key that does not exist,
        # and the first ranking of every hour would be the one that raised.
        cache.add(key, 0, 8 * 24 * 3600)
        try:
            cache.incr(key)
        except ValueError:
            cache.set(key, 1, 8 * 24 * 3600)


def latency_summary(hours=24, now=None, kind="first"):
    """{bucket: count} over the last `hours`, plus an estimated p50 and p95."""
    current = int((now or time.time()) // 3600)
    labels = [f"le{limit}" for limit in LATENCY_BUCKETS_MS] + ["over"]
    totals = Counter()
    for hour in range(current - hours + 1, current + 1):
        keys = [_latency_key(hour, label, kind) for label in labels + ["count"]]
        values = cache.get_many(keys)
        for label, key in zip(labels + ["count"], keys):
            totals[label] += values.get(key, 0) or 0

    def percentile(fraction):
        target = totals["count"] * fraction
        running = 0
        for limit, label in zip(list(LATENCY_BUCKETS_MS) + [None], labels):
            running += totals[label]
            if totals["count"] and running >= target:
                return limit
        return None

    return {
        "count": totals["count"],
        "buckets": {label: totals[label] for label in labels},
        "p50_ms_at_most": percentile(0.5),
        "p95_ms_at_most": percentile(0.95),
    }


# ----------------------------------------------------------------- impressions

def record_impressions(viewer, events, now=None):
    """Fold a batch of view events into PostView rows. Returns how many posts.

    Only posts the reader may see are recorded, and never their own: a view
    of something you cannot see did not happen, and looking at your own post
    says nothing about what you like or what others think of it.
    """
    from .views import visible_posts_for

    now = now or timezone.now()
    merged = {}
    for event in events:
        entry = merged.setdefault(event["post"], {
            "count": 0, "dwell": 0, "watch": None, "completed": False,
            "surface": event.get("surface") or PostView.Surface.OTHER,
        })
        entry["count"] += 1
        entry["dwell"] += event.get("dwell_ms") or 0
        watched = event.get("watch_ms")
        if watched is not None:
            entry["watch"] = max(entry["watch"] or 0, watched)
        entry["completed"] = entry["completed"] or bool(event.get("completed"))

    clips = {
        pk: (status == PostVideo.Status.APPROVED, duration)
        for pk, status, duration in visible_posts_for(viewer, Post.objects.filter(pk__in=merged))
        .exclude(author=viewer).values_list("pk", "video__status", "video__duration_ms")
    }
    visible = sorted(clips)
    for post_id in visible:
        # Watching is only something that happens to a clip, and never past
        # its end. A client that reports a finished clip on a photo, or an
        # hour into a ten-second one, is describing something that did not
        # happen, and it would count toward everybody's ranking.
        has_clip, duration = clips[post_id]
        entry = merged[post_id]
        if not has_clip:
            entry["watch"], entry["completed"] = None, False
        elif entry["watch"] is not None and duration:
            entry["watch"] = min(entry["watch"], duration)
    if visible:
        reasons = {}
        if any(merged[post_id]["surface"] == PostView.Surface.FOR_YOU for post_id in visible):
            reasons = served_reasons(viewer)
        for post_id in visible:
            entry = merged[post_id]
            entry["reason"] = (
                reasons.get(post_id, "") if entry["surface"] == PostView.Surface.FOR_YOU else ""
            )
        _upsert_views(viewer, [(post_id, merged[post_id]) for post_id in visible], now)
    return len(visible)


def _upsert_views(viewer, entries, now):
    """Add a batch to its PostView rows in one statement.

    `INSERT ... ON CONFLICT DO UPDATE`, which SQLite and PostgreSQL both
    speak. It was a bulk insert and then an UPDATE per post -- 50 posts, 51
    statements -- because Django's own upsert can only overwrite a column,
    and every counter here is added to. One statement is also one row lock
    per pair taken in post order, rather than fifty separate round trips.

    The new row and the update say the same thing: the counters are summed,
    the furthest watch and "ever completed" are kept, `skipped` is the latest
    viewing's verdict, and `surface`, `reason` and `first_seen_at` keep the
    first.
    """
    ops = connection.ops
    table = ops.quote_name(PostView._meta.db_table)
    columns = [
        "viewer_id", "post_id", "view_count", "total_dwell_ms", "max_watch_ms",
        "completed", "skipped", "surface", "reason", "first_seen_at", "last_seen_at",
    ]
    first_seen = ops.adapt_datetimefield_value(timezone.now())
    last_seen = ops.adapt_datetimefield_value(now)
    params = []
    for post_id, entry in entries:
        params += [
            viewer.pk,
            post_id,
            entry["count"],
            entry["dwell"],
            entry["watch"],
            entry["completed"],
            # The latest viewing decides. Coming back and staying is not a
            # skip, however the first pass went.
            (
                entry["dwell"] < SKIP_MS * entry["count"]
                and not entry["completed"]
                and (entry["watch"] or 0) < SKIP_MS
            ),
            # Plain strings: raw parameters skip the field's own conversion,
            # and psycopg would send an enum member by its name.
            str(entry["surface"]),
            str(entry.get("reason") or ""),
            first_seen,
            last_seen,
        ]
    row = "(" + ", ".join(["%s"] * len(columns)) + ")"
    current = f"{table}.{ops.quote_name('max_watch_ms')}"
    sql = (
        f"INSERT INTO {table} ({', '.join(ops.quote_name(c) for c in columns)}) "
        f"VALUES {', '.join([row] * len(entries))} "
        f"ON CONFLICT ({ops.quote_name('viewer_id')}, {ops.quote_name('post_id')}) DO UPDATE SET "
        f"view_count = {table}.view_count + excluded.view_count, "
        f"total_dwell_ms = {table}.total_dwell_ms + excluded.total_dwell_ms, "
        # CASE rather than GREATEST, which SQLite does not have.
        f"max_watch_ms = CASE "
        f"WHEN excluded.max_watch_ms IS NULL THEN {current} "
        f"WHEN {current} IS NULL OR excluded.max_watch_ms > {current} THEN excluded.max_watch_ms "
        f"ELSE {current} END, "
        f"completed = ({table}.completed OR excluded.completed), "
        f"skipped = excluded.skipped, "
        f"last_seen_at = excluded.last_seen_at"
    )
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
