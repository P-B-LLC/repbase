"""Attention summaries: what a device measured about one exposure of one post,
added to that reader's PostView row so that it can be sent twice, late, out of
order or by somebody lying without the totals coming out wrong.

A version 2 client measures on the device and sends one summary when an
exposure ends. This module is the other half: it decides what a summary may
claim, and adds it.

**What a summary may claim.** Every figure is the client's own report, so
every figure is bounded by something the server knows:

- an exposure shorter than 250 ms is an accident and is not recorded at all;
- active dwell cannot exceed the time on the card;
- playback cannot exceed active dwell, and happens only on a post with an
  approved clip -- on anything else the playback figures are null, which is
  "does not apply", not zero;
- coverage cannot exceed the clip, nor twice the time spent playing (the
  fastest the player goes);
- a clip was watched through only if it ended naturally *and* nine tenths of
  it was covered, whatever the client says;
- a summary observed more than two days ago, or in the future, is dropped.

A figure over its bound is brought down to it rather than refused. Refusing
would throw away a whole honest batch for one clock that jumped.

**Sent twice.** A batch carries an id. The receipt for it is written in the
transaction that adds the batch, so a second delivery finds the receipt and
adds nothing. Receipts are per account: one account cannot spend another's.

**Out of order.** Totals are sums and coverage is a maximum, so the order
they arrive in does not matter. `skipped` is a state, not a total, and
belongs to the exposure observed last -- by the time the device reported,
then by session and sequence so two with the same instant always resolve the
same way -- not to whichever batch arrived last.

**Somebody lying.** A reader's summaries reach only their own row, only for
posts they can see, never their own posts. What they can do to anybody
else's ranking goes through rates smoothed over every reader.

Version 1 reports go through `recommendations.record_impressions`, untouched,
and write only the legacy columns. Version 2 also keeps those columns fed
with the honest equivalents, so the ranking in use today keeps working until
it is taught the new ones.
"""

from datetime import timedelta

from django.db import connection, transaction
from django.db.models import F, OuterRef, Subquery
from django.utils import timezone

from .models import ImpressionBatchReceipt, Post, PostVideo, PostView

#: The most a request body may be. Fifty summaries are about 15 kB; this is
#: room for that several times over and nothing like room for abuse.
MAX_BODY_BYTES = 64 * 1024
#: Shorter than this on screen is a flick past, and is not an exposure.
MIN_EXPOSURE_MS = 250
#: At least this long visible is an impression that counts.
QUALIFIED_MS = 1000
#: The share of a clip that must have been covered for it to be watched
#: through.
COMPLETION_COVERAGE = 0.9
#: The fastest the player plays. Coverage beyond this many times the time
#: spent playing was not played.
MAX_PLAYBACK_RATE = 2
#: A little room for a clock and a player disagreeing about a frame.
SLACK_MS = 250
#: Rewatching counts up to this many times the clip, per row, ever.
REWATCH_CAP_TIMES = 3
#: How old a summary may be: twice the 24 hours a device keeps one.
MAX_AGE = timedelta(hours=48)
#: How far ahead of the server a device's clock may be.
MAX_AHEAD = timedelta(minutes=5)


def record_summaries(viewer, batch_id, session_id, events, now=None):
    """Add one batch of summaries. Returns (posts recorded, was a duplicate).

    One transaction: the receipt and the rows change together or not at all.
    """
    from . import social_insights
    from .ranking_model import sampled
    from .recommendations import served_features, served_reasons
    from .views import visible_posts_for

    now = now or timezone.now()
    with transaction.atomic():
        receipt, created = ImpressionBatchReceipt.objects.get_or_create(
            viewer=viewer, batch_id=batch_id
        )
        if not created:
            return receipt.recorded, True

        timely = [
            event for event in events
            if now - MAX_AGE <= event["observed_at"] <= now + MAX_AHEAD
            and (event.get("dwell_ms") or 0) >= MIN_EXPOSURE_MS
        ]
        wanted = {event["post"] for event in timely}
        facts = {
            pk: (status == PostVideo.Status.APPROVED and bool(duration), duration or 0, kind)
            for pk, status, duration, kind in visible_posts_for(
                viewer, Post.objects.filter(pk__in=wanted)
            ).exclude(author=viewer).values_list("pk", "video__status", "video__duration_ms", "kind")
        }
        merged = {}
        folded = 0
        for event in timely:
            post_id = event["post"]
            if post_id in facts:
                _fold(merged, post_id, _bounded(event, *facts[post_id]), str(session_id))
                folded += 1
        # How much arrived and how much of it was set aside, as two numbers
        # for the day: nothing about which reader or which post.
        social_insights.count("attention_events", len(events))
        social_insights.count("attention_events_ignored", len(events) - folded)

        # In post order, so two batches for the same reader take their row
        # locks in the same order and cannot wait on each other.
        visible = sorted(merged)
        if visible:
            reasons, features = {}, {}
            if any(merged[pk]["surface"] == PostView.Surface.FOR_YOU for pk in visible):
                reasons, features = served_reasons(viewer), served_features(viewer)
            for pk in visible:
                entry = merged[pk]
                for_you = entry["surface"] == PostView.Surface.FOR_YOU
                entry["reason"] = reasons.get(pk, "") if for_you else ""
                entry["features"] = sampled(viewer.pk, pk, features.get(pk)) if for_you else None
                entry["rewatch_cap"] = REWATCH_CAP_TIMES * facts[pk][1] if facts[pk][0] else None
            _upsert(viewer, [(pk, merged[pk]) for pk in visible], now)
        receipt.recorded = len(visible)
        receipt.save(update_fields=["recorded"])
        return len(visible), False


def _bounded(event, has_clip, duration_ms, kind):
    """One summary, with every figure brought within what could be true."""
    from .recommendations import SKIP_MS

    dwell = event.get("dwell_ms") or 0
    active = min(event.get("active_dwell_ms") or 0, dwell)
    failed = bool(event.get("playback_failed"))
    playback = coverage = rewatch = None
    through = False
    if has_clip:
        playback = min(event.get("playback_ms") or 0, active)
        coverage = min(
            event.get("coverage_ms") or 0,
            duration_ms,
            playback * MAX_PLAYBACK_RATE + SLACK_MS if playback else 0,
        )
        rewatch = min(event.get("rewatch_ms") or 0, playback)
        through = bool(event.get("completed")) and coverage >= COMPLETION_COVERAGE * duration_ms
    has_attachment = kind in (Post.Kind.WORKOUT, Post.Kind.MEAL)
    opened = has_attachment and bool(event.get("attachment_opened"))
    read = min(event.get("attachment_read_ms") or 0, 10 * 60 * 1000) if opened else 0
    return {
        "active": active,
        "qualified": dwell >= QUALIFIED_MS,
        "playback": playback,
        "coverage": coverage,
        "rewatch": rewatch,
        "through": through,
        "opened": opened,
        "read": read,
        "failed": failed and has_clip,
        # Weak evidence, and only after a real chance to look: a clip that
        # would not play, or a reader who went into the recipe, did not skip.
        "skipped": (
            active < SKIP_MS and not through and (coverage or 0) < SKIP_MS
            and not opened and not (failed and has_clip)
        ),
        "observed_at": event["observed_at"],
        "sequence": event["sequence"],
        "surface": event.get("surface") or PostView.Surface.OTHER,
    }


def _fold(merged, post_id, one, session):
    """Add one bounded summary to its post's entry for this batch."""
    # Zero-padded, so comparing the keys as text compares the sequences as
    # numbers within a session.
    key = f"{session[:36]}:{one['sequence']:010d}"
    entry = merged.get(post_id)
    if entry is None:
        merged[post_id] = entry = {
            "count": 0, "qualified": 0, "active": 0, "playback": None, "coverage": None,
            "rewatch": None, "through": False, "opens": 0, "read": 0, "failures": 0,
            "observed_at": None, "key": "", "skipped": False, "surface": one["surface"],
        }
    entry["count"] += 1
    entry["qualified"] += int(one["qualified"])
    entry["active"] += one["active"]
    for name in ("playback", "rewatch"):
        if one[name] is not None:
            entry[name] = (entry[name] or 0) + one[name]
    if one["coverage"] is not None:
        entry["coverage"] = max(entry["coverage"] or 0, one["coverage"])
    entry["through"] = entry["through"] or one["through"]
    entry["opens"] += int(one["opened"])
    entry["read"] += one["read"]
    entry["failures"] += int(one["failed"])
    if entry["observed_at"] is None or (one["observed_at"], key) > (entry["observed_at"], entry["key"]):
        entry["observed_at"], entry["key"], entry["skipped"] = one["observed_at"], key, one["skipped"]


def _upsert(viewer, entries, now):
    """Add a batch to its PostView rows in one statement.

    The same `INSERT ... ON CONFLICT DO UPDATE` the version 1 path uses, for
    the same reason: every counter is added to, and one statement is one
    round trip and one ordered set of row locks.
    """
    ops = connection.ops
    q = ops.quote_name
    table = q(PostView._meta.db_table)
    columns = [
        "viewer_id", "post_id", "view_count", "total_dwell_ms", "max_watch_ms", "completed",
        "skipped", "surface", "reason", "features", "first_seen_at", "last_seen_at",
        "qualified_views", "active_dwell_ms", "playback_ms", "coverage_ms", "rewatch_ms",
        "watched_through", "attachment_opens", "attachment_read_ms", "playback_failures",
        "last_observed_at", "last_observed_key",
    ]
    features_field = PostView._meta.get_field("features")
    stamp = ops.adapt_datetimefield_value(now)
    params, caps = [], {}
    for post_id, entry in entries:
        if entry["rewatch_cap"] is not None:
            caps[post_id] = entry["rewatch_cap"]
        rewatch = entry["rewatch"]
        if rewatch is not None and entry["rewatch_cap"] is not None:
            rewatch = min(rewatch, entry["rewatch_cap"])
        params += [
            viewer.pk, post_id,
            # The legacy columns, fed with what is true of them: an exposure
            # is a view, active dwell is dwell, and the most actually covered
            # is the honest reading of "how far they got".
            entry["count"], entry["active"], entry["coverage"], entry["through"],
            entry["skipped"], str(entry["surface"]), str(entry.get("reason") or ""),
            None if entry.get("features") is None
            else features_field.get_db_prep_value(entry["features"], connection),
            stamp, stamp,
            entry["qualified"], entry["active"], entry["playback"], entry["coverage"], rewatch,
            entry["through"], entry["opens"], entry["read"], entry["failures"],
            ops.adapt_datetimefield_value(entry["observed_at"]), entry["key"],
        ]

    def summed(name):
        return f"{name} = {table}.{name} + excluded.{name}"

    def summed_nullable(name):
        return (
            f"{name} = CASE WHEN excluded.{name} IS NULL THEN {table}.{name} "
            f"ELSE COALESCE({table}.{name}, 0) + excluded.{name} END"
        )

    def greatest(name):
        # CASE rather than GREATEST, which SQLite does not have.
        return (
            f"{name} = CASE WHEN excluded.{name} IS NULL THEN {table}.{name} "
            f"WHEN {table}.{name} IS NULL OR excluded.{name} > {table}.{name} THEN excluded.{name} "
            f"ELSE {table}.{name} END"
        )

    # Whether the incoming exposure is the latest this row has heard of.
    newer = (
        f"({table}.last_observed_at IS NULL OR excluded.last_observed_at > {table}.last_observed_at "
        f"OR (excluded.last_observed_at = {table}.last_observed_at "
        f"AND excluded.last_observed_key > {table}.last_observed_key))"
    )
    row = "(" + ", ".join(["%s"] * len(columns)) + ")"
    sql = (
        f"INSERT INTO {table} ({', '.join(q(c) for c in columns)}) "
        f"VALUES {', '.join([row] * len(entries))} "
        f"ON CONFLICT ({q('viewer_id')}, {q('post_id')}) DO UPDATE SET "
        + ", ".join([
            summed("view_count"), summed("total_dwell_ms"), greatest("max_watch_ms"),
            f"completed = ({table}.completed OR excluded.completed)",
            f"skipped = CASE WHEN {newer} THEN excluded.skipped ELSE {table}.skipped END",
            f"features = COALESCE({table}.{q('features')}, excluded.{q('features')})",
            greatest("last_seen_at"),
            summed("qualified_views"), summed("active_dwell_ms"), summed_nullable("playback_ms"),
            greatest("coverage_ms"), summed_nullable("rewatch_ms"),
            f"watched_through = ({table}.watched_through OR excluded.watched_through)",
            summed("attachment_opens"), summed("attachment_read_ms"), summed("playback_failures"),
            # After `skipped`, which reads the row as it was: in one UPDATE
            # every right-hand side sees the values from before it.
            f"last_observed_key = CASE WHEN {newer} THEN excluded.last_observed_key "
            f"ELSE {table}.last_observed_key END",
            f"last_observed_at = CASE WHEN {newer} THEN excluded.last_observed_at "
            f"ELSE {table}.last_observed_at END",
        ])
    )
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
    # The lifetime cap on rewatching, which a sum of capped batches can pass.
    # One statement for the batch, and only when a clip was in it.
    if caps:
        cap = Subquery(
            PostVideo.objects.filter(post=OuterRef("post")).values(
                cap=F("duration_ms") * REWATCH_CAP_TIMES
            )[:1]
        )
        PostView.objects.filter(
            viewer=viewer, post_id__in=caps, rewatch_ms__gt=cap
        ).update(rewatch_ms=cap)
