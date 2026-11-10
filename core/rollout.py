"""Who gets which part of the new Social, decided on the server.

Six parts, each released by itself, so any one can be held back or pulled
without the others:

- `new_feed`, `autoplay`: the app's media-first feed and whether its clips
  start by themselves. The app asks (`GET social/features/`) and obeys.
- `measurement_v2`: measured attention summaries. Off, the app sends plain
  view events, and summaries that still arrive are set aside, not recorded.
- `measured_ranking`: the Milestone 5 signals, as `MEASURED_VARIANT`.
- `sounds`: publishing a sound and setting a clip to one.
- `takes`: posting a take.

And one that is on unless somebody turns it off: `ranked_for_you`. Off, the
For You page is the newest public posts in order -- no ranking, no cache, no
statistics -- which is the page to fall back to when the ranking is the
thing that is wrong.

Each part is at one stage: `off`, `staff`, `cohort` (staff and the invited
accounts in `SocialCohortMember`) or `everyone`. A part with no row is at
`SOCIAL_ROLLOUT_DEFAULT`, which is `off` in production and `everyone` where
DEBUG is on, so nothing new reaches a production account until somebody
releases it. `measured_ranking` is off everywhere until released. What a part being off means is narrow: nothing already
published is hidden. A take stays a take and a sound stays credited; only
making new ones stops.

`social_rollout` changes a stage. Widening one runs the release gates
(`gates`) first and refuses if any fails, so expansion pauses by itself when
latency, errors, queue age or the statistics are already in trouble.
"""

import logging
import secrets
import time
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import models
from django.utils import timezone

logger = logging.getLogger(__name__)

STAGES = ("off", "staff", "cohort", "everyone")

#: name -> (the stage with no row, or None for SOCIAL_ROLLOUT_DEFAULT; what it is).
FLAGS = {
    "new_feed": (None, "The media-first feed in the app."),
    "autoplay": (None, "Clips in the new feed start by themselves."),
    "measurement_v2": (None, "Measured attention summaries are sent and recorded."),
    # An arm of the ranking, not a feature: off until it is released, in
    # development too, so the weights as they stand remain the control.
    "measured_ranking": ("off", "For You reads measured attention, tags, sounds and formats."),
    "sounds": (None, "Publishing a sound and setting a clip to one."),
    "takes": (None, "Posting a take of a post."),
    "ranked_for_you": ("everyone", "For You is ranked. Off: newest public posts, in order."),
}

_VERSION_KEY = "social-rollout:version"
#: How long a worker trusts what it read when the cache cannot tell it
#: whether anything changed.
_MEMO_SECONDS = 10 * 60

#: What this process last read: (rows, the version it was read under, when).
_memo = None


def default_stage(name):
    own, _ = FLAGS[name]
    if own is not None:
        return own
    chosen = getattr(settings, "SOCIAL_ROLLOUT_DEFAULT", "off")
    return chosen if chosen in STAGES else "off"


def _version():
    try:
        return cache.get(_VERSION_KEY)
    except Exception:  # noqa: BLE001
        return None


def stages():
    """{flag: stage} for every flag.

    Kept in the process, and read from the database again only when the
    shared version says a stage has changed (or, with the cache away, every
    ten minutes). So a request asks the cache one small question and the
    database none, and a change reaches every worker on its next request.
    """
    global _memo
    from .models import SocialFlag

    version = _version()
    now = time.monotonic()
    if _memo is None or _memo[1] != version or now - _memo[2] > _MEMO_SECONDS:
        _memo = (dict(SocialFlag.objects.values_list("name", "stage")), version, now)
    found = _memo[0]
    return {
        name: found[name] if found.get(name) in STAGES else default_stage(name) for name in FLAGS
    }


def forget_stages():
    """A stage changed: this process reads again, and so does every other."""
    global _memo
    _memo = None
    try:
        cache.set(_VERSION_KEY, secrets.token_hex(8), None)
    except Exception:  # noqa: BLE001
        logger.warning("The rollout version could not be published; workers catch up within ten minutes.")


def assume_no_rows():
    """For a test process: start from an empty table without asking for it,
    which is what a test database is before a test puts a row there."""
    global _memo
    _memo = ({}, _version(), time.monotonic())


def _standing(profile):
    """(is staff, is invited) for an account, asked once per request."""
    from .models import SocialCohortMember

    known = getattr(profile, "_rollout_standing", None)
    if known is None:
        staff = bool(profile.user.is_staff)
        known = (staff, staff or SocialCohortMember.objects.filter(user_id=profile.pk).exists())
        profile._rollout_standing = known
    return known


def _reaches(stage, profile):
    if stage == "everyone":
        return True
    if stage == "off" or profile is None:
        return False
    staff, invited = _standing(profile)
    return staff if stage == "staff" else invited


def enabled(name, profile):
    """Whether `name` is on for this account (a `RepbaseUser`)."""
    if name not in FLAGS:
        raise ValueError(f"{name!r} is not a rollout flag.")
    return _reaches(stages()[name], profile)


def enabled_for_id(name, profile_id):
    """The same, from an id. No query at all while the flag is at `off` or
    `everyone`, which is where a flag spends nearly all of its life."""
    stage = stages()[name]
    if stage in ("off", "everyone"):
        return stage == "everyone"
    from .models import RepbaseUser

    profile = RepbaseUser.objects.select_related("user").filter(pk=profile_id).first()
    return _reaches(stage, profile)


def features_for(profile):
    """{flag: bool} for an account: what the app is told."""
    current = stages()
    return {name: _reaches(current[name], profile) for name in FLAGS}


# -------------------------------------------------------------------- gates

#: A ranking slower than this at the 95th percentile is a reason to stop.
FIRST_PAGE_P95_MS = 1000
NEXT_PAGE_P95_MS = 500
#: Waiting work older than this means a worker has stopped.
QUEUE_AGE_SECONDS = 30 * 60
#: A snapshot older than this means the daily job has stopped, and with it
#: the retention sweep that runs beside it.
SNAPSHOT_AGE_DAYS = 2


def gates(now=None):
    """[(name, passed, detail)]: what has to hold before a flag is widened.

    These are the pause conditions: core latency, error rates, queue age,
    statistics and the privacy job. They are read from what the running
    system recorded, so passing here is evidence about this deployment and
    not about the code -- and a gate with nothing recorded does not pass.
    """
    from . import feed_stats
    from .analytics_models import ApiAlert, SocialInsightSnapshot
    from .checks import cleanup_is_overdue
    from .recommendations import latency_summary
    from .social_insights import _queues

    now = now or timezone.now()
    found = []

    alerts = list(ApiAlert.objects.filter(resolved_at__isnull=True).values_list("endpoint", "kind"))
    found.append((
        "no_open_api_alerts", not alerts,
        "no route is failing or slow" if not alerts
        else "open alerts: " + ", ".join(f"{endpoint} ({kind})" for endpoint, kind in alerts[:5]),
    ))
    recording = bool(settings.ANALYTICS_ENABLED)
    found.append((
        "api_health_is_recorded", recording,
        "API metrics are being collected" if recording
        else "ANALYTICS_ENABLED is off, so no alert could have opened",
    ))

    for kind, label, budget in (
        ("first", "first_page_latency", FIRST_PAGE_P95_MS), ("next", "next_page_latency", NEXT_PAGE_P95_MS),
    ):
        summary = latency_summary(hours=24, kind=kind)
        p95 = summary["p95_ms_at_most"]
        if not summary["count"]:
            # No later pages is what a quiet day looks like; no first pages
            # is no evidence at all.
            found.append((label, kind == "next", "no rankings recorded in the last day"))
        else:
            found.append((
                label, p95 is not None and p95 <= budget,
                f"p95 at most {p95} ms over {summary['count']} rankings (budget {budget} ms)"
                if p95 is not None else f"p95 over the largest bucket (budget {budget} ms)",
            ))

    age = feed_stats.staleness(now)
    limit = feed_stats.stale_seconds()
    found.append((
        "feed_statistics_fresh", age is not None and age <= limit,
        "never written" if age is None else f"{round(age / 60)} minutes old (stale after {round(limit / 60)})",
    ))

    stuck = {
        name: row["oldest_seconds"] for name, row in _queues(now).items()
        if row["oldest_seconds"] is not None and row["oldest_seconds"] > QUEUE_AGE_SECONDS
    }
    found.append((
        "queues_moving", not stuck,
        "no queue has work older than 30 minutes" if not stuck
        else ", ".join(f"{name}: oldest {round(seconds / 60)} min" for name, seconds in stuck.items()),
    ))

    overdue = cleanup_is_overdue()
    found.append((
        "retention_running", not overdue,
        "nothing is past its retention" if not overdue else "; ".join(overdue),
    ))
    newest = SocialInsightSnapshot.objects.first()
    fresh = newest is not None and newest.day >= now.date() - timedelta(days=SNAPSHOT_AGE_DAYS)
    found.append((
        "daily_maintenance_running", fresh,
        f"last snapshot {newest.day.isoformat()}" if newest else "no insight snapshot has ever been written",
    ))
    return found


def wider(new, old):
    return STAGES.index(new) > STAGES.index(old)


class GatesFailed(Exception):
    def __init__(self, failed):
        self.failed = failed
        super().__init__("; ".join(f"{name}: {detail}" for name, _, detail in failed))


def set_stage(name, stage, by="", backend_revision="", client_build="", force=False, now=None):
    """Move a flag to a stage and record it. Widening checks the gates."""
    from .models import SocialFlag, SocialRolloutEvent

    if name not in FLAGS:
        raise ValueError(f"{name!r} is not a rollout flag.")
    if stage not in STAGES:
        raise ValueError(f"{stage!r} is not a stage.")
    before = stages()[name]
    results = gates(now)
    failed = [result for result in results if not result[1]]
    widening = wider(stage, before)
    if widening and failed and not force:
        raise GatesFailed(failed)
    SocialFlag.objects.update_or_create(name=name, defaults={"stage": stage})
    forget_stages()
    event = SocialRolloutEvent.objects.create(
        flag=name, from_stage=before, to_stage=stage, changed_by=by[:150],
        backend_revision=backend_revision[:64], client_build=client_build[:64],
        forced=bool(widening and failed and force),
        gates=[{"name": gate, "passed": passed, "detail": detail} for gate, passed, detail in results],
    )
    logger.warning("social_rollout %s: %s -> %s forced=%s", name, before, stage, event.forced)
    return event
