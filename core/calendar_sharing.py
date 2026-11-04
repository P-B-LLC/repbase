"""Calendar sharing policy and bounded projections, without fan-out writes.

Nothing here hands a recipient the owner's PlannerEntry serializer. A grant
permits a small display-only projection, rechecked against live relationships
on every request. Reading another calendar never materializes database rows.
"""

from collections import defaultdict
from datetime import date, timedelta

from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone
from rest_framework.exceptions import APIException, NotFound, ValidationError

from .models import Block, CalendarShare, CalendarShareExclusion, Follow, PlannerEntry, PlannerRecurrence, RepbaseUser

MAX_RECIPIENT_ACTIVE_SHARES = 250
MAX_OWNER_ACTIVE_SHARES = 1000
MAX_SHARED_RULES = 1000
MAX_SHARED_OFFSETS = 100
MAX_SHARED_ROWS = 12000
ACTIVE_STATES = ("pending", "accepted")


class CalendarConflict(APIException):
    status_code = 409
    default_detail = "This calendar invitation has changed. Refresh before trying again."

    def __init__(self, detail=None, *, code="state_conflict"):
        super().__init__({"code": code, "detail": detail or self.default_detail})


class CalendarCapacity(APIException):
    status_code = 503
    default_detail = "This shared calendar is temporarily too large to display. Try a smaller date range."


def eligible(queryset):
    """A current accepted follow, never a pending request or reverse follow."""
    follows = Follow.objects.filter(follower_id=OuterRef("recipient_id"), following_id=OuterRef("owner_id"))
    blocks = Block.objects.filter(
        Q(blocker_id=OuterRef("owner_id"), blocked_id=OuterRef("recipient_id"))
        | Q(blocker_id=OuterRef("recipient_id"), blocked_id=OuterRef("owner_id"))
    )
    return queryset.filter(Exists(follows), ~Exists(blocks), owner__user__is_active=True, recipient__user__is_active=True)


def share_queryset():
    return CalendarShare.objects.select_related(
        "owner__user", "recipient__user", "source_entry__recurrence", "invited_entry__recurrence",
    )


def recipient_profiles(owner, search=""):
    follows = Follow.objects.filter(follower_id=OuterRef("pk"), following_id=owner.pk)
    blocks = Block.objects.filter(Q(blocker_id=owner.pk, blocked_id=OuterRef("pk")) | Q(blocker_id=OuterRef("pk"), blocked_id=owner.pk))
    result = RepbaseUser.objects.select_related("user").filter(Exists(follows), ~Exists(blocks), user__is_active=True).exclude(pk=owner.pk)
    query = search.strip().lstrip("@").strip()[:80]
    if query:
        from django.db.models import Value
        from django.db.models.functions import Concat
        reads_names = Follow.objects.filter(follower=owner, following_id=OuterRef("pk"))
        result = result.annotate(calendar_full_name=Concat("user__first_name", Value(" "), "user__last_name"),
                                 calendar_reads_names=Exists(reads_names)).filter(
            Q(user__username__icontains=query)
            | (Q(calendar_full_name__icontains=query) & (Q(is_profile_public=True) | Q(calendar_reads_names=True)))
        )
    return result.order_by("user__username", "pk")


def own_entry(owner, entry_id, *, lock=False):
    rows = PlannerEntry.objects.select_related("recurrence", "owner__user").filter(owner=owner, parent__isnull=True)
    if lock:
        rows = rows.select_for_update(of=("self",))
    try:
        return rows.get(pk=entry_id)
    except PlannerEntry.DoesNotExist:
        raise NotFound("That calendar item was not found.") from None


def occurrence_identity(entry):
    return entry.recurrence_date or entry.scheduled_date


def _share_target(entry, scope):
    if scope == "series":
        if entry.recurrence_id is None:
            raise ValidationError({"scope": "Only a repeating item can share future occurrences."})
        return {"scope": "series", "owner_id": entry.owner_id, "series_id": entry.recurrence.series_id}, occurrence_identity(entry)
    return {"scope": "occurrence", "source_entry_id": entry.pk}, None


@transaction.atomic
def create_invitations(owner, data):
    # Serializes quotas and duplicate invitations from the same owner. Follow
    # row locks below also serialize against unfollow's delete + revoke signal.
    recipient_ids = sorted(data["recipient_ids"])
    list(RepbaseUser.objects.select_for_update(no_key=True).filter(pk__in=[owner.pk, *recipient_ids]).order_by("pk"))
    entry = own_entry(owner, data["source_entry_id"], lock=True)
    target, starts_on = _share_target(entry, data["scope"])
    # Lock the pair's follow before writing any new grant. A concurrent unfollow
    # cannot delete that row and miss this not-yet-committed invitation.
    relationships = list(Follow.objects.select_for_update(of=("self",)).filter(
        following=owner, follower_id__in=recipient_ids, follower__user__is_active=True,
    ).order_by("follower_id"))
    allowed = {row.follower_id for row in relationships}
    allowed -= set(Block.objects.filter(
        Q(blocker=owner, blocked_id__in=recipient_ids) | Q(blocked=owner, blocker_id__in=recipient_ids)
    ).values_list("blocker_id", flat=True))
    blocked_ids = set(Block.objects.filter(blocker=owner, blocked_id__in=recipient_ids).values_list("blocked_id", flat=True))
    allowed -= blocked_ids
    if not owner.user.is_active or allowed != set(recipient_ids) or owner.pk in recipient_ids:
        raise ValidationError({"recipient_ids": "Choose active followers who can receive this invitation."})
    existing = {row.recipient_id: row for row in CalendarShare.objects.select_for_update().filter(**target, recipient_id__in=recipient_ids)}
    owner_count = CalendarShare.objects.filter(owner=owner, status__in=ACTIVE_STATES).count()
    activating = sum(1 for identifier in recipient_ids if identifier not in existing or existing[identifier].status not in ACTIVE_STATES)
    if owner_count + activating > MAX_OWNER_ACTIVE_SHARES:
        raise ValidationError({"recipient_ids": "End an existing calendar share before adding more."})
    from django.db.models import Count
    counts = dict(CalendarShare.objects.filter(recipient_id__in=recipient_ids, status__in=ACTIVE_STATES).values("recipient_id").annotate(total=Count("pk")).values_list("recipient_id", "total"))
    result, changed = [], False
    for identifier in recipient_ids:
        row = existing.get(identifier)
        if row and row.status in ACTIVE_STATES:
            if starts_on is not None and row.starts_on > starts_on:
                raise CalendarConflict("An existing invitation starts later. Revoke it before changing the sharing boundary.", code="scope_conflict")
            result.append(row)
            continue
        if row and not data["reinvite"]:
            raise CalendarConflict("This invitation was declined or revoked. Explicitly invite this follower again to ask anew.", code="reinvite_required")
        if counts.get(identifier, 0) >= MAX_RECIPIENT_ACTIVE_SHARES:
            raise ValidationError({"recipient_ids": "A selected follower cannot receive more calendar invitations right now."})
        if row is None:
            row = CalendarShare.objects.create(owner=owner, recipient_id=identifier, starts_on=starts_on,
                                               invited_entry=entry, **{key: value for key, value in target.items() if key != "owner_id"})
        else:
            row.status, row.responded_at, row.starts_on = "pending", None, starts_on
            row.created_at, row.invited_entry = timezone.now(), entry
            row.save(update_fields=("status", "responded_at", "starts_on", "created_at", "invited_entry"))
        result.append(row)
        changed = True
    return list(share_queryset().filter(pk__in=[row.pk for row in result]).order_by("id")), changed


@transaction.atomic
def respond_invitation(viewer, share_id, answer):
    try:
        row = CalendarShare.objects.select_for_update().get(pk=share_id, recipient=viewer)
    except CalendarShare.DoesNotExist:
        raise NotFound() from None
    # The share lock serializes against the unfollow/block revoke signals.
    if not eligible(CalendarShare.objects.filter(pk=row.pk)).exists():
        raise NotFound()
    if row.status == answer:
        return share_queryset().get(pk=row.pk)
    if row.status != "pending" and not (answer == "declined" and row.status == "accepted"):
        raise CalendarConflict()
    row.status, row.responded_at = answer, timezone.now()
    row.save(update_fields=("status", "responded_at"))
    return share_queryset().get(pk=row.pk)


@transaction.atomic
def revoke_invitation(owner, share_id):
    try:
        row = CalendarShare.objects.select_for_update().get(pk=share_id, owner=owner)
    except CalendarShare.DoesNotExist:
        raise NotFound() from None
    if row.status != "revoked":
        row.status, row.responded_at = "revoked", timezone.now()
        row.save(update_fields=("status", "responded_at"))
    return share_queryset().get(pk=row.pk)


@transaction.atomic
def make_private(owner, data):
    RepbaseUser.objects.select_for_update(no_key=True).get(pk=owner.pk)
    entry = own_entry(owner, data["source_entry_id"], lock=True)
    if data["scope"] == "series":
        if entry.recurrence_id is None:
            raise ValidationError({"scope": "This calendar item does not repeat."})
        # Entire lineage, including grants attached before a future-rule split.
        CalendarShare.objects.filter(owner=owner, series_id=entry.recurrence.series_id, status__in=ACTIVE_STATES).update(status="revoked", responded_at=timezone.now())
        CalendarShare.objects.filter(owner=owner, source_entry__recurrence__series_id=entry.recurrence.series_id, status__in=ACTIVE_STATES).update(status="revoked", responded_at=timezone.now())
    else:
        if entry.recurrence_id:
            CalendarShareExclusion.objects.get_or_create(owner=owner, series_id=entry.recurrence.series_id, occurrence_date=occurrence_identity(entry))
        CalendarShare.objects.filter(owner=owner, source_entry=entry, status__in=ACTIVE_STATES).update(status="revoked", responded_at=timezone.now())


def people_payload(profiles, viewer, request):
    """Compact identities honor closed profiles without per-row relationship IO."""
    from .serializers import profile_photo_url_for
    profiles = {profile.pk: profile for profile in profiles}
    followed = set(Follow.objects.filter(follower=viewer, following_id__in=profiles).values_list("following_id", flat=True))
    blocks = Block.objects.filter(Q(blocker=viewer, blocked_id__in=profiles) | Q(blocked=viewer, blocker_id__in=profiles))
    blocked = {other for pair in blocks.values_list("blocker_id", "blocked_id") for other in pair if other != viewer.pk}
    result = {}
    for identifier, profile in profiles.items():
        readable = identifier == viewer.pk or (identifier not in blocked and (profile.is_profile_public or identifier in followed))
        result[identifier] = {
            "id": identifier, "username": profile.user.username,
            "display_name": profile.user.get_full_name().strip() if readable else "",
            "profile_photo_url": profile_photo_url_for(profile, request) if readable else None,
        }
    return result


def _item(grant, person, source, *, day=None, identity=None, rule=None):
    recurring = rule is not None or getattr(source, "recurrence_id", None) is not None
    actual_rule = rule or (source.recurrence if recurring else None)
    identity = identity or (occurrence_identity(source) if rule is None else day)
    unique = f"{grant.owner_id}:series:{actual_rule.series_id}:{identity}" if recurring else f"{grant.owner_id}:entry:{source.pk}"
    return {
        "id": unique, "share_id": grant.pk, "owner": person,
        "kind": source.kind, "title": source.title, "category": source.category,
        "color": source.color, "scheduling_timezone": source.scheduling_timezone,
        "scheduled_date": day or source.scheduled_date, "scheduled_time": source.scheduled_time,
        "duration_minutes": source.duration_minutes, "is_recurring": recurring,
        "is_complete": source.completed_at is not None if rule is None and source.kind == "task" else False,
    }


def grants_payload(grants, viewer, request, management_source=None):
    grants = list(grants)
    people = people_payload([person for row in grants for person in (row.owner, row.recipient)], viewer, request)
    active_ids = set(eligible(CalendarShare.objects.filter(pk__in=[row.pk for row in grants], status__in=ACTIVE_STATES)).values_list("pk", flat=True))
    exclusion_query = Q(pk__in=[])
    for row in grants:
        source = management_source or row.invited_entry
        if row.scope == "series" and source and source.recurrence_id:
            exclusion_query |= Q(owner_id=row.owner_id, series_id=row.series_id, occurrence_date=occurrence_identity(source))
    exclusions = set(CalendarShareExclusion.objects.filter(exclusion_query).values_list("owner_id", "series_id", "occurrence_date"))
    result = []
    for row in grants:
        source, item = row.source_entry if row.scope == "occurrence" else (management_source or row.invited_entry), None
        if row.pk in active_ids and source is not None and source.owner_id == row.owner_id and not source.is_subtask:
            can_preview = row.scope == "occurrence" or (
                source.recurrence_id is not None and source.recurrence.series_id == row.series_id
                and occurrence_identity(source) >= row.starts_on
                and (row.owner_id, row.series_id, occurrence_identity(source)) not in exclusions
            )
            if can_preview:
                item = _item(row, people[row.owner_id], source)
        payload = {"id": row.pk, "owner": people[row.owner_id], "recipient": people[row.recipient_id],
                   "scope": row.scope, "status": row.status, "starts_on": row.starts_on,
                   "created_at": row.created_at, "responded_at": row.responded_at, "item": item}
        if management_source is not None:
            payload["applies_to_selected_occurrence"] = item is not None
        result.append(payload)
    return result


def _shift(day, offset):
    try:
        return day + timedelta(days=offset)
    except OverflowError:
        return date.min if offset < 0 else date.max


def _days(rule, start, end):
    if rule.ends_on is not None and rule.ends_on <= rule.starts_on:
        return
    day = max(start, rule.starts_on)
    last = min(end, rule.ends_on - timedelta(days=1)) if rule.ends_on is not None else end
    while day <= last:
        if rule.covers(day):
            yield day
        if day == last:
            break
        day += timedelta(days=1)


def shared_items(viewer, request, start, end, owner_ids=None):
    """At most 42 requested days and 250 grants; constant batches, no fan-out."""
    rows = eligible(share_queryset().filter(recipient=viewer, status="accepted"))
    if owner_ids is not None:
        rows = rows.filter(owner_id__in=owner_ids)
    grants = list(rows[:MAX_RECIPIENT_ACTIVE_SHARES + 1])
    if len(grants) > MAX_RECIPIENT_ACTIVE_SHARES:
        raise CalendarCapacity()
    if not grants:
        return []
    people = people_payload([row.owner for row in grants], viewer, request)
    by_series = defaultdict(list)
    items = {}

    def include(grant, source, *, day=None, identity=None, rule=None):
        item = _item(grant, people[grant.owner_id], source, day=day, identity=identity, rule=rule)
        # An explicit occurrence grant wins over an overlapping series grant,
        # but the stable display identity is identical in either direction.
        previous = items.get(item["id"])
        if previous is None or grant.scope == "occurrence":
            items[item["id"]] = item

    for grant in grants:
        if grant.scope == "series":
            by_series[(grant.owner_id, grant.series_id)].append(grant)
        elif grant.source_entry and grant.source_entry.owner_id == grant.owner_id and not grant.source_entry.is_subtask and start <= grant.source_entry.scheduled_date <= end:
            include(grant, grant.source_entry)
    if by_series:
        series_ids = [key[1] for key in by_series]
        owners = [key[0] for key in by_series]
        moved_here = PlannerEntry.objects.filter(recurrence_id=OuterRef("pk"), parent__isnull=True, scheduled_date__range=(start, end))
        rules = list(PlannerRecurrence.objects.defer("notes").filter(owner_id__in=owners, series_id__in=series_ids).filter(
            (Q(starts_on__lte=end) & (Q(ends_on__isnull=True) | Q(ends_on__gt=start))) | Exists(moved_here)
        ).order_by("id")[:MAX_SHARED_RULES + 1])
        if len(rules) > MAX_SHARED_RULES:
            raise CalendarCapacity()
        offsets = defaultdict(list)
        for rule in rules:
            offsets[rule.identity_offset_days].append(rule.pk)
        if len(offsets) > MAX_SHARED_OFFSETS:
            raise CalendarCapacity()
        entry_window = Q(scheduled_date__range=(start, end))
        for offset, rule_ids in offsets.items():
            entry_window |= Q(recurrence_id__in=rule_ids, recurrence_date__range=(_shift(start, offset), _shift(end, offset)))
        entries = list(PlannerEntry.objects.select_related("recurrence").only(
            "id", "owner_id", "kind", "title", "category", "color", "scheduling_timezone",
            "scheduled_date", "scheduled_time", "duration_minutes", "completed_at", "recurrence_id", "recurrence_date",
            "recurrence__id", "recurrence__series_id",
        ).filter(
            entry_window, owner_id__in=owners, recurrence__series_id__in=series_ids, parent__isnull=True,
        ).order_by("pk")[:MAX_SHARED_ROWS + 1])
        if len(entries) > MAX_SHARED_ROWS:
            raise CalendarCapacity()
        exclusion_dates = defaultdict(set)
        for rule in rules:
            key = (rule.owner_id, rule.series_id)
            if key in by_series:
                exclusion_dates[key].update(_shift(day, rule.identity_offset_days) for day in _days(rule, start, end))
        for entry in entries:
            # A date moved into this window can have an identity outside the
            # ordinary rule window; include its exact privacy key as well.
            key = (entry.owner_id, entry.recurrence.series_id)
            if key in by_series:
                exclusion_dates[key].add(occurrence_identity(entry))
        if sum(map(len, exclusion_dates.values())) > MAX_SHARED_ROWS:
            raise CalendarCapacity()
        exclusion_query = Q(pk__in=[])
        for (owner_id, series_id), days in exclusion_dates.items():
            exclusion_query |= Q(owner_id=owner_id, series_id=series_id, occurrence_date__in=days)
        private_rows = list(CalendarShareExclusion.objects.filter(exclusion_query).values_list(
            "owner_id", "series_id", "occurrence_date",
        )[:MAX_SHARED_ROWS + 1])
        if len(private_rows) > MAX_SHARED_ROWS:
            raise CalendarCapacity()
        exclusions = set(private_rows)
        present = {(row.recurrence_id, occurrence_identity(row)) for row in entries}
        for entry in entries:
            key = (entry.owner_id, entry.recurrence.series_id)
            identity = occurrence_identity(entry)
            if key + (identity,) in exclusions or not start <= entry.scheduled_date <= end:
                continue
            for grant in by_series.get(key, ()):
                if identity >= grant.starts_on:
                    include(grant, entry)
        for rule in rules:
            key = (rule.owner_id, rule.series_id)
            for day in _days(rule, start, end):
                identity = _shift(day, rule.identity_offset_days)
                if not 1 <= day.toordinal() + rule.identity_offset_days <= date.max.toordinal():
                    continue
                if (rule.pk, identity) in present or key + (identity,) in exclusions:
                    continue
                # Missing at/before the cursor means deliberately deleted (or
                # already consumed), never permission to reconstruct it.
                if rule.materialized_through is not None and day <= rule.materialized_through:
                    continue
                for grant in by_series.get(key, ()):
                    if identity >= grant.starts_on:
                        include(grant, rule, day=day, identity=identity, rule=rule)
    # Relationship changes may commit while the bounded projection is being
    # built. Revalidate before returning; never trust a captured grant list
    # after a revoke, unfollow, block or account suspension.
    visible_ids = set(eligible(CalendarShare.objects.filter(
        pk__in=[row.pk for row in grants], status="accepted",
    )).values_list("pk", flat=True))
    return sorted((item for item in items.values() if item["share_id"] in visible_ids),
                  key=lambda item: (item["scheduled_date"], item["scheduled_time"] is not None,
                                    item["scheduled_time"] or "", item["id"]))
