"""The For You page: what to show somebody, in what order, and why.

Rules rather than a model, on purpose. There is not yet enough interaction
data here to train anything worth trusting, and a learned ranking nobody can
read is a ranking nobody can debug, explain to a user, or defend to a
moderator. Every number below has a name, a default and a sentence saying
what it does, and `FOR_YOU_WEIGHTS` in settings can move any of them.

## The pipeline

1. **What this person is into** (`build_profile`). Their likes, comments,
   saves, reposts, how long they stayed on what they saw, what they scrolled
   straight past and what they said they were not interested in -- each
   weighted, each fading with age -- plus who they follow and what they said
   they train when they signed up. Topics come out of that as a score
   between -1 and 1 each, and so do authors.

2. **What they could be shown** (`candidates`). Only what passes every
   eligibility rule: posts this reader may see at all (`visible_posts_for`),
   from the last month, not their own, not reposts, not from a suspended
   account, not something they reported or dismissed, not carrying a clip
   still waiting for review, and not something enough people have reported
   that a moderator should look first. Drawn from three places at once --
   the newest posts, posts from people they follow, and posts on their best
   topics -- each capped, so the work stays the same size however much is
   posted.

3. **How good a match each one is** (`score`). Topic match, author
   affinity, following, quality (engagement for its views, smoothed so one
   like on one view is not a perfect score), popularity, freshness and watch
   quality, less a penalty for what they have already seen. Every part is
   kept, so the reason a post ranked where it did can be shown.

4. **An order worth scrolling** (`rank`). Best first, except that the same
   author cannot appear twice in four posts, a repeated author or topic costs
   a little more each time, and one slot in five goes to discovery: a good
   post from somebody they have never interacted with, on a topic outside
   their usual ones. That is what stops the page becoming one creator, and
   what gives it something to learn from next time.

## New accounts

Somebody with no history gets the topics they named during onboarding and
the disciplines on their profile as a prior, and the ranking falls back on
quality, popularity and freshness with the same diversity rules. Somebody who
named nothing gets the best of what is new, mixed -- which is a reasonable
first page, and the first like starts making it theirs.
"""

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
from django.db.models import Count, Exists, F, OuterRef, Q, Subquery, Value
from django.db.models.functions import Coalesce, Greatest
from django.utils import timezone

from .models import (
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
    #: How the reader has treated this author before.
    author: float = 1.5
    #: Flat bonus for an author the reader follows.
    follows: float = 1.0
    #: Engagement for the views it has had, smoothed.
    quality: float = 1.0
    #: Raw engagement, on a log scale.
    popularity: float = 0.6
    #: Freshness, halving every RECENCY_HALF_LIFE_HOURS.
    recency: float = 1.2
    #: How often viewers stay, or watch a clip to the end.
    watch: float = 0.6
    #: Penalty for something this reader has already seen lately.
    seen: float = 2.5
    #: Penalty for open reports below the exclusion threshold.
    reported: float = 1.0
    #: A little noise so exact ties do not always break the same way.
    jitter: float = 0.02

    # --- what one action says about what someone likes
    like: float = 1.0
    comment: float = 2.0
    save: float = 3.0
    share: float = 2.5
    follow: float = 3.0
    long_view: float = 0.5
    completion: float = 1.0
    skip: float = -0.3
    not_interested: float = -4.0
    #: A topic named during onboarding, or a discipline on the profile.
    declared: float = 2.0

    # --- variety
    #: Cost of each earlier appearance of the same author on the page.
    author_repeat: float = 1.0
    #: Cost of each recent appearance of the same main topic.
    topic_repeat: float = 0.35
    #: One slot in this many goes to discovery. 0 turns discovery off.
    discovery_every: int = 5

    @classmethod
    def from_settings(cls):
        overrides = getattr(settings, "FOR_YOU_WEIGHTS", None) or {}
        unknown = set(overrides) - {item.name for item in fields(cls)}
        if unknown:
            raise ImproperlyConfigured(
                "FOR_YOU_WEIGHTS names unknown weights: " + ", ".join(sorted(unknown))
            )
        return replace(cls(), **overrides)


#: How far back a post may be to be recommended at all.
WINDOW_DAYS = 30
#: The newest eligible posts considered on every request.
POOL_SIZE = 500
#: Cap on each extra source (following, interests).
SOURCE_LIMIT = 150
#: How many posts one ranking orders. Ten pages of twenty.
MAX_RANKED = 200
#: How far back interactions count, and how many of each kind are read.
HISTORY_DAYS = 90
HISTORY_LIMIT = 300
#: An interaction's weight halves over this many days.
SIGNAL_HALF_LIFE_DAYS = 21
#: A post's freshness halves over this many hours.
RECENCY_HALF_LIFE_HOURS = 36
#: Something seen within this long ago counts as already seen.
SEEN_WINDOW_DAYS = 3
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


# ------------------------------------------------------------------- profile

def _squash(value, scale=4.0):
    """Map an unbounded tally onto (-1, 1). A few signals move it a lot; many
    more move it a little more, which is how interest actually saturates."""
    return math.tanh(value / scale)


@dataclass
class InterestProfile:
    topics: dict = field(default_factory=dict)
    authors: dict = field(default_factory=dict)
    followed: frozenset = frozenset()
    #: Authors this reader has done anything positive with, followed or not.
    engaged_authors: frozenset = frozenset()
    #: post id -> skipped, for posts seen within SEEN_WINDOW_DAYS.
    seen: dict = field(default_factory=dict)
    declared: frozenset = frozenset()
    signal_count: int = 0

    @property
    def is_new(self):
        """No history, no follows, no declared interests: a cold start."""
        return not (self.signal_count or self.followed or self.declared)

    def top_topics(self, count=5):
        positive = [(score, topic) for topic, score in self.topics.items() if score > 0]
        return [topic for _, topic in sorted(positive, reverse=True)[:count]]


def declared_topics(viewer):
    """The topics somebody named for themselves: onboarding and profile."""
    topics = set()
    choice = Personalization.objects.filter(repbase_user=viewer).first()
    if choice is not None:
        for answer in choice.training_types or []:
            if isinstance(answer, str):
                topics.update(TRAINING_TYPE_TOPICS.get(answer.strip().lower(), ()))
        topics.update(EMPHASIS_TOPICS.get((choice.emphasis or "").strip().lower(), ()))
    for discipline in UserDiscipline.objects.filter(profile=viewer).values_list(
        "discipline", flat=True
    ):
        topics.add(f"discipline:{discipline}")
        topics.update(DISCIPLINE_TOPICS.get(discipline, ()))
    return frozenset(topics)


def _post_topics_by_id(post_ids):
    """{post id: (author id, topics)} for posts somebody interacted with."""
    rows = list(
        Post.objects.filter(pk__in=post_ids).values(
            "pk", "author_id", "kind", "workout__workout_type", "planner__category",
            "video__status",
        )
    )
    disciplines = _disciplines_by_author({row["author_id"] for row in rows})
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


def build_profile(viewer, weights=None, now=None):
    """What this reader is into, from what they have done and said."""
    weights = weights or FeedWeights.from_settings()
    now = now or timezone.now()
    since = now - timedelta(days=HISTORY_DAYS)
    seen_since = now - timedelta(days=SEEN_WINDOW_DAYS)

    # (post id, weight, when). Every list is bounded twice: by age and count.
    events = []

    def take(rows, weight):
        events.extend((post_id, weight, when) for post_id, when in rows if post_id)

    take(
        PostLike.objects.filter(user=viewer, created_at__gte=since)
        .exclude(post__author=viewer).order_by("-created_at")
        .values_list("post_id", "created_at")[:HISTORY_LIMIT],
        weights.like,
    )
    take(
        PostComment.objects.filter(author=viewer, created_at__gte=since)
        .exclude(post__author=viewer).order_by("-created_at")
        .values_list("post_id", "created_at")[:HISTORY_LIMIT],
        weights.comment,
    )
    for model in (WorkoutTemplate, SavedFoodMeal):
        take(
            model.objects.filter(owner=viewer, source_post__isnull=False, created_at__gte=since)
            .order_by("-created_at").values_list("source_post_id", "created_at")[:HISTORY_LIMIT],
            weights.save,
        )
    take(
        Post.objects.filter(author=viewer, kind=Post.Kind.REPOST, created_at__gte=since)
        .order_by("-created_at").values_list("repost_of_id", "created_at")[:HISTORY_LIMIT],
        weights.share,
    )

    seen = {}
    for post_id, when, dwell, count, completed, skipped in (
        PostView.objects.filter(viewer=viewer, last_seen_at__gte=since)
        .order_by("-last_seen_at")
        .values_list(
            "post_id", "last_seen_at", "total_dwell_ms", "view_count", "completed", "skipped"
        )[:HISTORY_LIMIT]
    ):
        if when >= seen_since:
            seen[post_id] = skipped
        if completed:
            events.append((post_id, weights.completion, when))
        if count and dwell / count >= LONG_VIEW_MS:
            events.append((post_id, weights.long_view, when))
        if skipped:
            events.append((post_id, weights.skip, when))

    dismissed = list(
        PostFeedback.objects.filter(viewer=viewer, created_at__gte=since)
        .order_by("-created_at").values_list("post_id", "created_at")[:HISTORY_LIMIT]
    )
    take(dismissed, weights.not_interested)

    topics_by_post = _post_topics_by_id({post_id for post_id, _, _ in events})
    topic_tally = Counter()
    author_tally = Counter()
    engaged = set()
    for post_id, weight, when in events:
        found = topics_by_post.get(post_id)
        if found is None:
            continue
        author_id, topics = found
        age_days = max((now - when).total_seconds(), 0) / 86400
        decayed = weight * 0.5 ** (age_days / SIGNAL_HALF_LIFE_DAYS)
        for topic, share in topics.items():
            topic_tally[topic] += decayed * share
        # Dismissing one post is mostly about the post; it says less about
        # everything else its author might make.
        author_tally[author_id] += decayed * (0.75 if weight < 0 else 1.0)
        if weight > 0:
            engaged.add(author_id)

    declared = declared_topics(viewer)
    for topic in declared:
        topic_tally[topic] += weights.declared

    followed = frozenset(
        Follow.objects.filter(follower=viewer).values_list("following_id", flat=True)
    )
    for author_id in followed:
        author_tally[author_id] += weights.follow

    return InterestProfile(
        topics={topic: _squash(value) for topic, value in topic_tally.items()},
        authors={author: _squash(value) for author, value in author_tally.items()},
        followed=followed,
        engaged_authors=frozenset(engaged) | followed,
        seen=seen,
        declared=declared,
        signal_count=len(events),
    )


# ---------------------------------------------------------------- candidates

def _count(model, field_name, **filters):
    """A correlated count, which adds a column and never a row."""
    return Coalesce(
        Subquery(
            model.objects.filter(**{field_name: OuterRef("pk")}, **filters)
            .order_by().values(field_name).annotate(n=Count("pk")).values("n")[:1]
        ),
        Value(0),
    )


def eligible_posts(viewer, now=None):
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
    return (
        visible_posts_for(viewer, Post.objects.all(), include_reposts=False)
        .exclude(kind=Post.Kind.REPOST)
        .exclude(author=viewer)
        .filter(created_at__gte=now - timedelta(days=WINDOW_DAYS))
        .exclude(Exists(PostFeedback.objects.filter(viewer=viewer, post=OuterRef("pk"))))
        .exclude(Exists(PostReport.objects.filter(reporter=viewer, post=OuterRef("pk"))))
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


#: Counts carry an `n_` prefix because the plain names are taken: `likes`,
#: `comments` and `views` are already Post's reverse relations, and Django
#: refuses an annotation that shadows one.
_CANDIDATE_FIELDS = (
    "pk", "author_id", "kind", "created_at", "workout__workout_type",
    "planner__category", "video__status", "open_reports", "n_likes",
    "n_comments", "n_shares", "n_workout_saves", "n_meal_saves",
    "n_impressions", "n_long_views", "n_completions",
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
    sources: set = field(default_factory=set)
    score: float = 0.0
    parts: dict = field(default_factory=dict)

    @property
    def main_topic(self):
        return primary_topic(self.topics)


def _with_counts(queryset):
    return queryset.annotate(
        n_likes=_count(PostLike, "post"),
        n_comments=_count(PostComment, "post", is_hidden=False),
        n_shares=_count(Post, "repost_of"),
        n_workout_saves=_count(WorkoutTemplate, "source_post"),
        n_meal_saves=_count(SavedFoodMeal, "source_post"),
        n_impressions=_count(PostView, "post"),
        n_long_views=_count(PostView, "post", total_dwell_ms__gte=LONG_VIEW_MS),
        n_completions=_count(PostView, "post", completed=True),
    )


def candidates(viewer, profile, now=None):
    """Recent eligible posts from three sources, merged, each capped."""
    now = now or timezone.now()
    eligible = eligible_posts(viewer, now)
    newest = ("-created_at", "-id")
    sources = [("fresh", _with_counts(eligible).order_by(*newest)[:POOL_SIZE])]

    if profile.followed:
        sources.append((
            "following",
            _with_counts(eligible.filter(author_id__in=profile.followed)).order_by(*newest)[:SOURCE_LIMIT],
        ))

    best = profile.top_topics(5)
    if best:
        match = Q()
        kinds = [topic.split(":", 1)[1] for topic in best if topic.startswith("kind:")]
        types = [topic.split(":", 1)[1] for topic in best if topic.startswith("type:")]
        categories = [topic.split(":", 1)[1] for topic in best if topic.startswith("category:")]
        disciplines = [topic.split(":", 1)[1] for topic in best if topic.startswith("discipline:")]
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
        if match:
            sources.append((
                "interest",
                _with_counts(eligible.filter(match)).order_by(*newest)[:SOURCE_LIMIT],
            ))

    rows = {}
    for name, queryset in sources:
        for row in queryset.values(*_CANDIDATE_FIELDS):
            rows.setdefault(row["pk"], (row, set()))[1].add(name)

    disciplines = _disciplines_by_author({row["author_id"] for row, _ in rows.values()})
    found = []
    for row, names in rows.values():
        has_video = row["video__status"] == PostVideo.Status.APPROVED
        found.append(Candidate(
            pk=row["pk"],
            author_id=row["author_id"],
            created_at=row["created_at"],
            topics=topics_for(
                row["kind"], row["workout__workout_type"], row["planner__category"],
                has_video, disciplines.get(row["author_id"], ()),
            ),
            has_video=has_video,
            open_reports=row["open_reports"],
            likes=row["n_likes"],
            comments=row["n_comments"],
            shares=row["n_shares"],
            saves=row["n_workout_saves"] + row["n_meal_saves"],
            impressions=row["n_impressions"],
            long_views=row["n_long_views"],
            completions=row["n_completions"],
            sources=names,
        ))
    return found


# ------------------------------------------------------------------- scoring

def score(candidate, profile, weights, now, rng=None):
    """A candidate's score, and the parts it is made of."""
    topic_weight = sum(candidate.topics.values()) or 1.0
    topic_match = sum(
        profile.topics.get(topic, 0.0) * share for topic, share in candidate.topics.items()
    ) / topic_weight

    engagement = (
        candidate.likes + 2 * candidate.comments + 3 * candidate.saves + 2 * candidate.shares
    )
    # Engagement per view, pulled toward a modest prior until there are
    # enough views to say otherwise: one like on one view is not the best post
    # on the server.
    rate = (engagement + 0.1 * 20) / (candidate.impressions + 20)
    quality = 1 - math.exp(-rate / 0.3)
    popularity = min(1.0, math.log1p(engagement) / math.log1p(50))
    age_hours = max((now - candidate.created_at).total_seconds(), 0) / 3600
    recency = 0.5 ** (age_hours / RECENCY_HALF_LIFE_HOURS)
    stayed = candidate.completions if candidate.has_video else candidate.long_views
    watch = (stayed + 1) / (candidate.impressions + 2)

    seen = 0.0
    if candidate.pk in profile.seen:
        seen = 1.5 if profile.seen[candidate.pk] else 1.0
    reported = min(candidate.open_reports, REPORT_THRESHOLD) / REPORT_THRESHOLD

    parts = {
        "topic": weights.topic * topic_match,
        "author": weights.author * profile.authors.get(candidate.author_id, 0.0),
        "follows": weights.follows * (candidate.author_id in profile.followed),
        "quality": weights.quality * quality,
        "popularity": weights.popularity * popularity,
        "recency": weights.recency * recency,
        "watch": weights.watch * watch,
        "seen": -weights.seen * seen,
        "reported": -weights.reported * reported,
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


def _reason(candidate, profile, discovery):
    if discovery:
        return "discovery", "Something new for you"
    if candidate.author_id in profile.followed:
        return "following", "From someone you follow"
    matching = [
        (profile.topics.get(topic, 0.0) * share, topic)
        for topic, share in candidate.topics.items()
    ]
    strength, topic = max(matching)
    if strength > 0.15:
        return "interest", f"Because you like {topic_name(topic)}"
    parts = candidate.parts
    if parts.get("quality", 0) + parts.get("popularity", 0) > parts.get("recency", 0):
        return "popular", "Popular in the community"
    return "fresh", "New in the community"


def rank(found, profile, weights, rng, limit=MAX_RANKED):
    """Order the scored candidates into a page worth scrolling."""
    remaining = sorted(found, key=lambda item: (-item.score, -item.pk))
    usual = set(profile.top_topics(3))
    authors_used = Counter()
    recent_authors = []
    recent_topics = []
    ordered = []

    def adjusted(candidate):
        return (
            candidate.score
            - weights.author_repeat * authors_used[candidate.author_id]
            - weights.topic_repeat * recent_topics[-5:].count(candidate.main_topic)
        )

    while remaining and len(ordered) < limit:
        window = remaining[:SCAN_DEPTH]
        spaced = [item for item in window if item.author_id not in recent_authors[-(AUTHOR_GAP - 1):]]
        pool = spaced or window
        pick = None
        discovery = False
        every = weights.discovery_every
        if every and len(ordered) % every == every - 1:
            # Unknown means no history either way. Somebody whose posts this
            # reader dismissed is not unknown to them, and neither is a topic
            # they have scrolled past; offering those as "something new"
            # would be undoing what the reader just told the ranking.
            unknown = [
                item for item in pool
                if item.author_id not in profile.engaged_authors
                and profile.authors.get(item.author_id, 0.0) >= 0
                and profile.topics.get(item.main_topic, 0.0) >= 0
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
        authors_used[pick.author_id] += 1
        recent_authors.append(pick.author_id)
        recent_topics.append(pick.main_topic)
        reason, label = _reason(pick, profile, discovery)
        ordered.append(Ranked(pick.pk, reason, label))
    return ordered


@dataclass
class Ranking:
    items: list
    candidate_count: int
    is_new: bool
    duration_ms: float


def build_ranking(viewer, now=None, seed=None):
    """Everything above, in order, for one reader."""
    started = time.perf_counter()
    weights = FeedWeights.from_settings()
    now = now or timezone.now()
    rng = random.Random(seed if seed is not None else secrets.randbits(32))
    profile = build_profile(viewer, weights, now)
    found = candidates(viewer, profile, now)
    for candidate in found:
        candidate.score, candidate.parts = score(candidate, profile, weights, now, rng)
    items = rank(found, profile, weights, rng)
    duration_ms = (time.perf_counter() - started) * 1000
    record_latency(duration_ms)
    logger.info(
        "for_you ranked=%d candidates=%d new_account=%s duration_ms=%.1f reasons=%s",
        len(items), len(found), profile.is_new, duration_ms,
        dict(Counter(item.reason for item in items)),
    )
    return Ranking(items, len(found), profile.is_new, duration_ms)


# ------------------------------------------------------------------ sessions
#
# A ranking is not an ORDER BY a database can page through, so it is worked
# out once, kept for half an hour, and paged by position. Without that, page
# two would be ranked again from scratch and could repeat page one or skip
# what page one pushed down. The cursor names the ranking and the position;
# it is signed so a client cannot make one up, and the cache key carries the
# reader's id, so a cursor taken from somebody else opens nothing.

_CURSOR_SALT = "core.recommendations.cursor"


class BadCursor(ValueError):
    pass


def _session_key(viewer_id, session):
    return f"for-you:{viewer_id}:{session}"


def start_session(viewer, ranking):
    session = secrets.token_urlsafe(12)
    cache.set(
        _session_key(viewer.pk, session),
        [(item.pk, item.reason, item.label) for item in ranking.items],
        SESSION_TTL_SECONDS,
    )
    return session


def load_session(viewer, session):
    stored = cache.get(_session_key(viewer.pk, session))
    if stored is None:
        return None
    return [Ranked(*row) for row in stored]


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


# ---------------------------------------------------------------- monitoring
#
# Latency, kept where every worker can add to it: one cache counter per hour
# and bucket. `feed_report` turns them into a distribution. Counters rather
# than a list of timings so recording one costs a single increment.

LATENCY_BUCKETS_MS = (25, 50, 100, 250, 500, 1000, 2500)


def _latency_key(hour, label):
    return f"for-you:latency:{hour}:{label}"


def _bucket_label(duration_ms):
    for limit in LATENCY_BUCKETS_MS:
        if duration_ms <= limit:
            return f"le{limit}"
    return "over"


def record_latency(duration_ms, now=None):
    hour = int((now or time.time()) // 3600)
    for label in (_bucket_label(duration_ms), "count"):
        key = _latency_key(hour, label)
        # add() then incr(): incr alone raises on a key that does not exist,
        # and the first ranking of every hour would be the one that raised.
        cache.add(key, 0, 8 * 24 * 3600)
        try:
            cache.incr(key)
        except ValueError:
            cache.set(key, 1, 8 * 24 * 3600)


def latency_summary(hours=24, now=None):
    """{bucket: count} over the last `hours`, plus an estimated p50 and p95."""
    current = int((now or time.time()) // 3600)
    labels = [f"le{limit}" for limit in LATENCY_BUCKETS_MS] + ["over"]
    totals = Counter()
    for hour in range(current - hours + 1, current + 1):
        values = cache.get_many([_latency_key(hour, label) for label in labels + ["count"]])
        for label in labels + ["count"]:
            totals[label] += values.get(_latency_key(hour, label), 0) or 0

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

    visible = set(
        visible_posts_for(viewer, Post.objects.filter(pk__in=merged))
        .exclude(author=viewer).values_list("pk", flat=True)
    )
    PostView.objects.bulk_create(
        [
            PostView(viewer=viewer, post_id=post_id, surface=merged[post_id]["surface"],
                     last_seen_at=now)
            for post_id in visible
        ],
        ignore_conflicts=True,
    )
    for post_id in visible:
        entry = merged[post_id]
        changes = {
            "view_count": F("view_count") + entry["count"],
            "total_dwell_ms": F("total_dwell_ms") + entry["dwell"],
            "last_seen_at": now,
            # The latest viewing decides. Coming back and staying is not a
            # skip, however the first pass went.
            "skipped": (
                entry["dwell"] < SKIP_MS * entry["count"]
                and not entry["completed"]
                and (entry["watch"] or 0) < SKIP_MS
            ),
        }
        if entry["completed"]:
            changes["completed"] = True
        if entry["watch"] is not None:
            changes["max_watch_ms"] = Greatest(Coalesce(F("max_watch_ms"), Value(0)), Value(entry["watch"]))
        PostView.objects.filter(viewer=viewer, post_id=post_id).update(**changes)
    return len(visible)
