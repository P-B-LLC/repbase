"""Keeping the For You page honest.

Two questions the ranking has to answer before it can trust a number:

1. **Whose engagement counts, and for how much.** A like is a vote that some
   stranger may want to see this post. A vote from a suspended account, from
   the author, from an account made an hour ago, from an account liking two
   hundred posts an hour, or from an account that exists to like one
   person's posts is not that. `account_trust` says how much each account's
   likes count; `core.recommendations` counts comments, reposts and saves
   once per person and never the author's own.

2. **What is not recommended, however much engagement it has.** An author a
   moderator just took something down from is not put in front of strangers
   for a while (their followers still see them). A caption written to
   harvest likes, or to sell something, is pushed down. These are demotions
   and eligibility rules, not removals: removal is moderation's job, and
   nothing here deletes or hides anything.

Instagram's published account of this is the model: engagement bought or
farmed is discounted, recommendation is a privilege with its own rules that
sit above ordinary ranking, and an account that broke the rules lately is not
recommended for a time. Every threshold is a named constant with a sentence
saying why, so a moderator can be told exactly what happened to a post.

Nothing here is shown to anyone. A trust figure is never displayed,
returned by the API, or logged with an identity: it only scales counts that
are themselves only ever used inside a ranking.
"""

import re
from dataclasses import dataclass
from datetime import timedelta

from django.core.cache import cache
from django.db.models import Count, Exists, F, OuterRef
from django.utils import timezone

from .models import Post, PostLike, PostVideo, RepbaseUser

# ----------------------------------------------------------- account trust

#: An account younger than this counts for NEW_ACCOUNT_TRUST of a like.
#: Freshly made accounts are where bought and farmed engagement comes from;
#: a real new member loses nothing by it except a little weight for two days.
NEW_ACCOUNT_HOURS = 48
NEW_ACCOUNT_TRUST = 0.5
#: More likes than this in the last hour is not somebody reading. Beyond it
#: each like counts LIKE_BURST / likes-that-hour, but never less than
#: BURST_FLOOR.
LIKE_BURST = 60
BURST_FLOOR = 0.1
#: An account that liked at least FOCUS_MIN of one author's posts in the
#: last FOCUS_DAYS, with more than FOCUS_SHARE of all its likes going to
#: that author, is boosting somebody rather than browsing. Its likes on that
#: author's posts count less the more single-minded it is, down to
#: FOCUS_FLOOR. Its likes on anybody else count normally. A real fan likes
#: other people too; a sockpuppet does not.
FOCUS_MIN = 5
FOCUS_SHARE = 0.6
FOCUS_FLOOR = 0.2
FOCUS_DAYS = 30
#: Trust is the same whoever is asking, so it is worked out once per account
#: and shared for this long.
TRUST_TTL_SECONDS = 15 * 60
#: Accounts looked up per query, so a very long list never meets a
#: database's limit on parameters.
TRUST_CHUNK = 5000


@dataclass(frozen=True)
class Trust:
    """How much one account's engagement counts. Never shown to anyone."""

    #: 0-1. Zero for a suspended account.
    base: float = 1.0
    #: The one author this account mostly likes, when it is single-minded
    #: about them (see FOCUS_*), and how single-minded: the share of its
    #: likes that went to them.
    focus_author: object = None
    focus_share: float = 0.0
    #: Younger than NEW_ACCOUNT_HOURS.
    new: bool = False

    def for_author(self, author_id):
        """How much a like from this account counts on this author's post."""
        if self.focus_author is not None and self.focus_author == author_id:
            return self.base * focus_factor(self.focus_share)
        return self.base

    def as_neighbour(self):
        """How much this account's likes say about anybody else's taste.
        Nothing for a brand-new account -- it has no taste to share yet, and
        a crowd of them is the cheapest way to fake one -- and less for one
        that exists to like one person's posts."""
        if self.new:
            return 0.0
        if self.focus_author is not None:
            return self.base * focus_factor(self.focus_share)
        return self.base


FULL_TRUST = Trust()


def focus_factor(share):
    """1 up to FOCUS_SHARE, falling linearly to FOCUS_FLOOR at 100%."""
    if share <= FOCUS_SHARE:
        return 1.0
    fall = (share - FOCUS_SHARE) / (1 - FOCUS_SHARE)
    return max(FOCUS_FLOOR, 1 - fall * (1 - FOCUS_FLOOR))


def _chunks(ids):
    ids = sorted(ids)
    for start in range(0, len(ids), TRUST_CHUNK):
        yield ids[start:start + TRUST_CHUNK]


def _compute_trust(account_ids, now):
    """{account id: Trust}. Three grouped queries per chunk of accounts."""
    found = {}
    young = now - timedelta(hours=NEW_ACCOUNT_HOURS)
    for chunk in _chunks(account_ids):
        accounts = {
            pk: (created_at, active)
            for pk, created_at, active in RepbaseUser.objects.filter(pk__in=chunk)
            .values_list("pk", "created_at", "user__is_active")
        }
        bursts = dict(
            PostLike.objects.filter(user_id__in=chunk, created_at__gte=now - timedelta(hours=1))
            .order_by().values("user_id").annotate(n=Count("pk")).values_list("user_id", "n")
        )
        per_author = {}
        totals = {}
        for user_id, author_id, n in (
            PostLike.objects.filter(user_id__in=chunk, created_at__gte=now - timedelta(days=FOCUS_DAYS))
            # Liking your own posts says nothing about being single-minded
            # about anybody.
            .exclude(post__author_id=F("user_id"))
            .order_by().values("user_id", "post__author_id").annotate(n=Count("pk"))
            .values_list("user_id", "post__author_id", "n")
        ):
            totals[user_id] = totals.get(user_id, 0) + n
            best = per_author.get(user_id)
            if best is None or n > best[1] or (n == best[1] and author_id < best[0]):
                per_author[user_id] = (author_id, n)

        for pk in chunk:
            created_at, active = accounts.get(pk, (None, False))
            if not active:
                found[pk] = Trust(base=0.0)
                continue
            base = 1.0
            new = created_at is not None and created_at > young
            if new:
                base *= NEW_ACCOUNT_TRUST
            burst = bursts.get(pk, 0)
            if burst > LIKE_BURST:
                base *= max(BURST_FLOOR, LIKE_BURST / burst)
            focus_author, focus_share = None, 0.0
            top = per_author.get(pk)
            if top is not None and top[1] >= FOCUS_MIN and totals.get(pk):
                share = top[1] / totals[pk]
                if share > FOCUS_SHARE:
                    focus_author, focus_share = top[0], share
            found[pk] = Trust(
                base=round(base, 4), focus_author=focus_author, focus_share=round(focus_share, 4), new=new,
            )
    return found


def account_trust(account_ids, now=None):
    """{account id: Trust}, shared across readers for TRUST_TTL_SECONDS."""
    from .recommendations import _cached_many

    now = now or timezone.now()
    return _cached_many("trust", account_ids, lambda ids: _compute_trust(ids, now), TRUST_TTL_SECONDS)


# -------------------------------------------------------- recent moderation

#: An author with a post taken down, or a clip rejected, by a moderator this
#: recently is not recommended to people who do not follow them. Their
#: followers chose them and still see them. Undoing the takedown restores
#: them at once, because `hidden_at` is cleared with it.
MODERATION_COOLDOWN_DAYS = 30


def recently_moderated(author_ref="author", now=None):
    """An Exists() that is true for an author a moderator acted against
    within MODERATION_COOLDOWN_DAYS. `author_ref` names the author column of
    the queryset it is used in."""
    now = now or timezone.now()
    since = now - timedelta(days=MODERATION_COOLDOWN_DAYS)
    taken_down = Post.objects.filter(author=OuterRef(author_ref), is_hidden=True, hidden_at__gte=since)
    rejected = PostVideo.objects.filter(
        post__author=OuterRef(author_ref), status=PostVideo.Status.REJECTED, reviewed_at__gte=since
    )
    return Exists(taken_down) | Exists(rejected)


# ---------------------------------------------------------------- captions
#
# Engagement bait and sales pitches, read off the caption. Instagram's
# Recommendation Guidelines keep both out of recommendations: a post that
# asks for likes is gaming the very count that ranks it, and a post selling
# a supplement or a coaching plan to strangers is an advert. Deliberately a
# short list of unambiguous phrasings rather than a classifier: every match
# can be explained, and a false positive costs a demotion, not a removal.
# Asking a real question ("comment your PR below?") is fine and not listed.

_BAIT = re.compile(
    r"\b("
    r"like\s+(this\s+)?if|double[\s-]?tap\s+if|smash\s+(that\s+)?like|"
    r"like\s+for\s+like|l4l|follow\s+for\s+follow|f4f|follow\s+back|"
    r"tag\s+(a|\d+|your)\s+(friends?|mates?|buddy|buddies|bros?|people)|"
    r"share\s+(this\s+)?if|comment\s+(yes|\S+)\s+if|"
    r"type\s+[\"']?\S+[\"']?\s+(below|in\s+the\s+comments)|"
    r"follow\s+(me\s+)?for\s+more|like\s+and\s+follow"
    r")\b",
    re.IGNORECASE,
)
_PITCH = re.compile(
    r"\b("
    r"dm\s+(me\s+)?(for|to)\s+(price|prices|pricing|details|info|coaching|plan|plans|order)|"
    r"(promo|discount|coupon)\s+code|use\s+(my\s+)?code|link\s+in\s+(my\s+)?bio|"
    r"giveaway|buy\s+now|limited\s+offer|order\s+now|"
    r"sarms|fat\s+burners?|detox\s+teas?|lose\s+\d+\s*(lb|lbs|kg|kgs|pounds|kilos)\s+in\s+\d+\s+(days?|weeks?)"
    r")\b",
    re.IGNORECASE,
)

#: How much of the `bait` weight each kind of caption costs.
BAIT_SHARE = 1.0
PITCH_SHARE = 0.7


def caption_flags(caption):
    """0-1: how much a caption reads as bait or a pitch."""
    if not caption:
        return 0.0
    flags = 0.0
    if _BAIT.search(caption):
        flags = max(flags, BAIT_SHARE)
    if _PITCH.search(caption):
        flags = max(flags, PITCH_SHARE)
    return flags


def blend(raw, trusted, amount):
    """`amount` of the way from a raw count to its trusted one: 0 is the
    count as it stands, 1 the count with only trusted engagement in it."""
    return raw + amount * (trusted - raw)


# ------------------------------------------------------------- shared cache

def forget_trust(account_ids):
    """Drop cached trust, for when an account is suspended or reinstated
    and should not wait out the cache."""
    cache.delete_many([f"for-you:trust:{pk}" for pk in account_ids])
