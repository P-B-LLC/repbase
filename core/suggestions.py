"""People worth following: the "suggested for you" beside the For You page.

Same approach as the page itself: named sources, each bounded, each saying
why. A person is suggested because

- the reader keeps liking their posts without following them (`engaged`);
- people the reader follows follow them (`mutual`);
- people with the reader's taste like their posts (`similar`);
- they train the way the reader does and post about it (`discipline`);
- they are new and posting (`new_creator`), or popular this week (`popular`).

What is never done, because a suggestion is shown to somebody else:

- Only open profiles are suggested. Somebody who closed theirs asked not to
  be found by strangers, and a suggestion is exactly that.
- "Followed by N people you follow" counts only followers whose own profile
  is open -- their follow lists are on show anyway -- so it can never reveal
  who a closed account follows.
- The people whose taste matches the reader's are never suggested
  themselves. That would turn one person's likes into a disclosure; they only
  point at whose posts are worth seeing.
- Blocks either way, suspended accounts, people already followed or asked,
  and people the reader dismissed are left out.

With personalisation off, only the last two sources are used: nothing about
what the reader did picks who they are shown.
"""

import math
from collections import Counter
from datetime import timedelta

from django.db.models import Count, Exists, OuterRef, Q
from django.utils import timezone

from .models import (
    SUGGESTION_DISMISSAL_DAYS,
    Block,
    Follow,
    FollowRequest,
    Post,
    RepbaseUser,
    SuggestionDismissal,
    UserDiscipline,
)
from .serializers import with_public_profiles
from .recommendations import (
    NEWCOMER_DAYS,
    WINDOW_DAYS,
    build_profile,
    preferences_for,
    similar_tastes,
    topic_name,
    trending_post_ids,
)

#: How many suggestions one request may ask for.
MAX_SUGGESTIONS = 20
#: Someone counts as posting if they posted within this many days. Suggesting
#: an account that went quiet in spring is suggesting an empty feed.
ACTIVE_DAYS = 60
#: Each source looks at no more than this many people.
SOURCE_LIMIT = 60

#: A source is given as the reason when it is at least this share of why
#: the person is suggested. The most personal such source wins: "you often
#: like their posts" says more than "popular this week", even when
#: popularity happened to add slightly more.
REASON_SHARE = 0.3

#: How much each source counts, and what it says. Ordered by how personal
#: the reason is, which is the order reasons are chosen in.
SOURCES = {
    "engaged": (3.0, "You often like their posts"),
    "mutual": (1.5, None),  # worded with the count, below
    "similar": (1.2, "Popular with people who like what you like"),
    "discipline": (1.0, None),  # worded with the discipline, below
    "new_creator": (0.6, "New to Rytivo"),
    "popular": (0.5, "Popular on Rytivo this week"),
}


def _excluded(viewer, now):
    """Everyone who must not be suggested to this reader."""
    out = {viewer.pk}
    out.update(Follow.objects.filter(follower=viewer).values_list("following_id", flat=True))
    out.update(FollowRequest.objects.filter(requester=viewer).values_list("target_id", flat=True))
    out.update(
        SuggestionDismissal.objects.filter(
            viewer=viewer, created_at__gte=now - timedelta(days=SUGGESTION_DISMISSAL_DAYS)
        ).values_list("person_id", flat=True)
    )
    for blocker, blocked in Block.objects.filter(Q(blocker=viewer) | Q(blocked=viewer)).values_list(
        "blocker_id", "blocked_id"
    ):
        out.add(blocked if blocker == viewer.pk else blocker)
    return out


def suggest_people(viewer, limit=10, now=None):
    """[(profile, reason, label)], best first."""
    now = now or timezone.now()
    limit = max(1, min(limit, MAX_SUGGESTIONS))
    preferences = preferences_for(viewer)
    excluded = _excluded(viewer, now)
    #: person -> {source: (contribution, label)}. Each source speaks about a
    #: person at most once.
    found = {}

    def add(person, source, strength, label=None):
        if person in excluded or strength <= 0:
            return
        weight, default_label = SOURCES[source]
        found.setdefault(person, {})[source] = (weight * strength, label or default_label)

    if preferences.personalized:
        profile = build_profile(viewer, now=now, preferences=preferences)

        for author, affinity in profile.authors.items():
            if affinity > 0.2 and author not in profile.followed:
                add(author, "engaged", affinity)

        open_followed = list(
            RepbaseUser.objects.filter(pk__in=profile.followed, is_profile_public=True)
            .values_list("pk", flat=True)
        )
        if open_followed:
            for person, n in (
                Follow.objects.filter(follower_id__in=open_followed)
                .exclude(following_id__in=excluded)
                .order_by().values("following_id").annotate(n=Count("pk"))
                .order_by("-n", "following_id").values_list("following_id", "n")[:SOURCE_LIMIT]
            ):
                label = (
                    "Followed by someone you follow" if n == 1
                    else f"Followed by {n} people you follow"
                )
                add(person, "mutual", math.log1p(n), label)

        taste = similar_tastes(viewer, profile, now)
        if taste:
            liked_authors = Counter()
            for post_id, author in Post.objects.filter(pk__in=list(taste)).values_list("pk", "author_id"):
                liked_authors[author] += taste[post_id].score
            for author, strength in liked_authors.most_common(SOURCE_LIMIT):
                add(author, "similar", min(strength, 1.0))

        mine = [topic.split(":", 1)[1] for topic in profile.declared if topic.startswith("discipline:")]
        if mine:
            shared = Counter()
            names = {}
            for person, discipline in (
                UserDiscipline.objects.filter(discipline__in=mine)
                .exclude(profile_id__in=excluded)
                .filter(Exists(Post.objects.filter(
                    author=OuterRef("profile"), created_at__gte=now - timedelta(days=WINDOW_DAYS)
                )))
                .order_by("-profile__created_at").values_list("profile_id", "discipline")[:SOURCE_LIMIT * 2]
            ):
                shared[person] += 1
                names.setdefault(person, discipline)
            for person, n in shared.items():
                add(person, "discipline", n, f"Also into {topic_name('discipline:' + names[person])}")

    trending = trending_post_ids(now)
    if trending:
        authors = list(
            Post.objects.filter(pk__in=trending[:SOURCE_LIMIT * 2]).values_list("author_id", flat=True)
        )
        for position, (author, _) in enumerate(Counter(authors).most_common(SOURCE_LIMIT)):
            add(author, "popular", 1 - position / SOURCE_LIMIT)

    for position, person in enumerate(
        RepbaseUser.objects.filter(created_at__gte=now - timedelta(days=NEWCOMER_DAYS))
        .exclude(pk__in=excluded)
        .filter(Exists(Post.objects.filter(
            author=OuterRef("pk"), created_at__gte=now - timedelta(days=WINDOW_DAYS),
            is_hidden=False, visibility=Post.Visibility.PUBLIC,
        )))
        .order_by("-created_at").values_list("pk", flat=True)[:SOURCE_LIMIT]
    ):
        add(person, "new_creator", 1 - position / SOURCE_LIMIT)

    if not found:
        return []
    score = {person: sum(value for value, _ in sources.values()) for person, sources in found.items()}

    def why(person):
        sources = found[person]
        for source in SOURCES:
            if source in sources and sources[source][0] >= REASON_SHARE * score[person]:
                return source, sources[source][1]
        source = max(sources, key=lambda name: sources[name][0])
        return source, sources[source][1]

    # Only open, active profiles that are posting. Asked once, for the best
    # few times the limit, rather than per source.
    shortlist = sorted(score, key=lambda person: (-score[person], person))[:limit * 4]
    allowed = {
        profile.pk: profile
        for profile in with_public_profiles(RepbaseUser.objects.filter(
            pk__in=shortlist, is_profile_public=True, user__is_active=True
        ))
        # A private post is its author's alone, so it says nothing about
        # whether following them would show anybody anything.
        .filter(Exists(Post.objects.filter(
            author=OuterRef("pk"), created_at__gte=now - timedelta(days=ACTIVE_DAYS), is_hidden=False,
        ).exclude(visibility=Post.Visibility.PRIVATE)))
    }
    ranked = sorted(allowed, key=lambda person: (-score[person], person))[:limit]
    return [(allowed[person], *why(person)) for person in ranked]
