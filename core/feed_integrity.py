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
from django.db.models import Count, F
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
#: BURST_FLOOR. Who is bursting is worked out once for everybody every
#: BURST_TTL_SECONDS.
LIKE_BURST = 60
BURST_FLOOR = 0.1
BURST_TTL_SECONDS = 5 * 60
#: An account younger than FOCUS_YOUNG_DAYS that liked at least FOCUS_MIN of
#: one author's posts in the last FOCUS_DAYS, with more than FOCUS_SHARE of
#: all its likes going to that author, is boosting somebody rather than
#: browsing. Its likes on that author's posts count less the more
#: single-minded it is, down to FOCUS_FLOOR; its likes on anybody else count
#: normally. A real fan likes other people too; a sockpuppet does not. Only
#: young accounts are asked: an account that has been around for a month and
#: likes one person is a superfan, and a ring that has to age its accounts
#: for a month first is a ring that costs a great deal more to run.
FOCUS_MIN = 5
FOCUS_SHARE = 0.6
FOCUS_FLOOR = 0.2
FOCUS_DAYS = 30
FOCUS_YOUNG_DAYS = 30
#: Trust is the same whoever is asking, so it is worked out once per account
#: and shared for this long.
TRUST_TTL_SECONDS = 15 * 60
#: Accounts looked up per query, so a very long list never meets a
#: database's limit on parameters.
TRUST_CHUNK = 5000
#: The shared list of suspended accounts is worked out once for everybody
#: this often. Suspending an account reaches the counts within this long;
#: everything it posted leaves every feed at once, as it always has.
INACTIVE_TTL_SECONDS = 5 * 60


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


def young_since(now):
    """Accounts that joined after this are young enough to be looked at
    (FOCUS_YOUNG_DAYS, and never less than NEW_ACCOUNT_HOURS)."""
    return now - max(timedelta(days=FOCUS_YOUNG_DAYS), timedelta(hours=NEW_ACCOUNT_HOURS))


def new_since(now):
    """Accounts that joined after this are brand new (NEW_ACCOUNT_HOURS)."""
    return now - timedelta(hours=NEW_ACCOUNT_HOURS)


def bursting_accounts(now=None):
    """{account id: likes in the last hour} for every account over
    LIKE_BURST, worked out once for everybody.

    Likes are numbered in the order they were made, so the last hour of them
    is the newest stretch of ids: the boundary is found by walking back from
    the newest like to the first one older than an hour, and only the likes
    after it are counted. The work is the last hour's likes, not the table."""
    found = cache.get("for-you:bursting")
    if found is None:
        now = now or timezone.now()
        cutoff = now - timedelta(hours=1)
        boundary = (
            PostLike.objects.filter(created_at__lt=cutoff).order_by("-pk").values_list("pk", flat=True).first()
        ) or 0
        found = dict(
            PostLike.objects.filter(pk__gt=boundary, created_at__gte=cutoff)
            .order_by().values("user_id").annotate(n=Count("pk")).filter(n__gt=LIKE_BURST)
            .values_list("user_id", "n")
        )
        cache.set("for-you:bursting", found, BURST_TTL_SECONDS)
    return found


def inactive_accounts():
    """The ids of suspended accounts, worked out once for everybody: there
    are few of them, and a count that leaves them out is then a filter on a
    short list rather than a join through every row's account."""
    found = cache.get("for-you:inactive")
    if found is None:
        found = sorted(RepbaseUser.objects.filter(user__is_active=False).values_list("pk", flat=True))
        cache.set("for-you:inactive", found, INACTIVE_TTL_SECONDS)
    return found


def _compute_focus(account_ids, now):
    """{account id: (author id, share) or None} for these accounts: the one
    author each is single-minded about, if any (FOCUS_*)."""
    per_author, totals = {}, {}
    for chunk in _chunks(account_ids):
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
    found = {}
    for pk in account_ids:
        top = per_author.get(pk)
        share = top[1] / totals[pk] if top is not None and totals.get(pk) else 0.0
        found[pk] = (top[0], round(share, 4)) if top and top[1] >= FOCUS_MIN and share > FOCUS_SHARE else None
    return found


def account_trust(joined, now=None):
    """{account id: Trust} for {account id: when it joined}.

    The caller passes when each account joined, read with the rows it is
    counting, so an account made a minute ago is known to be new however
    recently anything was cached. From that, the shared lists of suspended
    and bursting accounts, and -- for young accounts only -- how
    single-minded each is (cached per account for TRUST_TTL_SECONDS). An
    established account that is not bursting is simply trusted, which is
    what nearly every account is."""
    from .recommendations import _cached_many

    now = now or timezone.now()
    brand_new, young = new_since(now), young_since(now)
    inactive = set(inactive_accounts())
    bursts = bursting_accounts(now)
    asked = sorted(
        pk for pk, when in joined.items() if when is not None and when > young and pk not in inactive
    )
    focus = (
        _cached_many("focus", asked, lambda ids: _compute_focus(ids, now), TRUST_TTL_SECONDS) if asked else {}
    )
    found = {}
    for pk, when in joined.items():
        if pk in inactive:
            found[pk] = Trust(base=0.0)
            continue
        new = when is not None and when > brand_new
        base = NEW_ACCOUNT_TRUST if new else 1.0
        burst = bursts.get(pk, 0)
        if burst > LIKE_BURST:
            base *= max(BURST_FLOOR, LIKE_BURST / burst)
        single = focus.get(pk)
        if base == 1.0 and not new and single is None:
            found[pk] = FULL_TRUST
        else:
            found[pk] = Trust(
                base=round(base, 4), focus_author=single[0] if single else None,
                focus_share=single[1] if single else 0.0, new=new,
            )
    return found


def trust_by_id(account_ids, now=None):
    """`account_trust` for accounts the caller has only the ids of. One
    query for when they joined."""
    joined = dict(RepbaseUser.objects.filter(pk__in=list(account_ids)).values_list("pk", "created_at"))
    return account_trust({pk: joined.get(pk) for pk in account_ids}, now)


# -------------------------------------------------------- recent moderation

#: An author with a post taken down, or a clip rejected, by a moderator this
#: recently is not recommended to people who do not follow them. Their
#: followers chose them and still see them. Worked out once for everybody
#: every MODERATED_TTL_SECONDS, so a decision reaches For You within a
#: minute; the post a moderator took down leaves every feed at once, as it
#: always has.
MODERATION_COOLDOWN_DAYS = 30
MODERATED_TTL_SECONDS = 60


def recently_moderated_authors(now=None):
    """The ids of authors a moderator acted against within
    MODERATION_COOLDOWN_DAYS. A short list, so the ranking can leave them out
    with a plain IN rather than a correlated subquery on every candidate.

    Read from three places:

    - posts still hidden by a moderator (`hidden_at` is cleared by undoing
      it, which lifts the cooldown);
    - clips still rejected;
    - the access portal's audit of hide decisions, which outlives the post:
      an author who deletes the post a moderator hid does not delete the
      decision with it. A decision whose post is still there and no longer
      hidden was undone, and does not count.

    A takedown made in the Django admin, or a rejected clip the author then
    deletes, leaves no record that outlives the post, so deleting it ends
    the cooldown early (see RECOMMENDATIONS.md, "Known limits")."""
    from .access_models import AccessAudit

    found = cache.get("for-you:moderated")
    if found is None:
        now = now or timezone.now()
        since = now - timedelta(days=MODERATION_COOLDOWN_DAYS)
        authors = set(
            Post.objects.filter(is_hidden=True, hidden_at__gte=since).values_list("author_id", flat=True)
        )
        authors |= set(
            PostVideo.objects.filter(status=PostVideo.Status.REJECTED, reviewed_at__gte=since)
            .values_list("post__author_id", flat=True)
        )
        decisions = {}
        for subject, target in AccessAudit.objects.filter(
            action="moderation_hide", created_at__gte=since, subject__startswith="post:", target__isnull=False,
        ).values_list("subject", "target_id"):
            try:
                decisions[int(subject.split(":", 1)[1])] = target
            except ValueError:
                continue
        if decisions:
            undone = set(
                Post.objects.filter(pk__in=list(decisions), is_hidden=False).values_list("pk", flat=True)
            )
            users = {target for post_id, target in decisions.items() if post_id not in undone}
            authors |= set(RepbaseUser.objects.filter(user_id__in=users).values_list("pk", flat=True))
        found = sorted(authors)
        cache.set("for-you:moderated", found, MODERATED_TTL_SECONDS)
    return found


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
    # Asking for the like itself -- but not "I would like if...".
    r"(?<!would )(?<!'d )(?<!to )like\s+(this\s+(post\s+)?)?if\s+you\b|"
    r"double[\s-]?tap\s+if|smash\s+(that\s+)?like|like\s+and\s+(follow|share)|"
    r"like\s+for\s+like|l4l\b|follow\s+for\s+follow|f4f\b|"
    r"follow\s+back(?=\s*(?:[!?.,]|$))|follow\s+(me\s+)?for\s+more|"
    r"tag\s+(a|\d+|your)\s+(friends?|mates?|buddy|buddies|bros?|people)|"
    r"share\s+(this\s+)?(with|to)\s+(a|your)\s+(friends?|mates?)\s+(who|that)|"
    r"share\s+this\s+if\s+you\b|"
    r"comment\s+[\"']?(yes|no|me|done|amen|\d+)[\"']?\s+(if|for|to)\b|"
    r"drop\s+an?\s+\S+\s+if\s+you\b|"
    r"type\s+[\"']?(yes|me|done|amen|\d+)[\"']?\s+(below|in\s+the\s+comments)"
    r")",
    re.IGNORECASE | re.MULTILINE,
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
