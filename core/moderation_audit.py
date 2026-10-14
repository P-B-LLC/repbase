"""Every moderation decision on a post or its clip, written where it outlives
the post.

The access portal has always written an `AccessAudit` row for its hide
decisions. The Django admin did not, so a takedown made there was only ever
the post's own `is_hidden` flag -- and an author who deleted the hidden post
deleted the only record that it had been taken down. These helpers give every
path the same record, and an undo its own record, so that what a moderator
decided can always be read back: by the For You page's moderation cooldown
(`core.feed_integrity.recently_moderated_authors`), and by anybody reviewing
what happened to an account.

Subjects are `post:<id>`, as the portal already writes them. Two families of
decision are kept apart, because a post can be shown again while its clip
stays rejected:

| Family | Puts an author under the cooldown | Lifts it |
| --- | --- | --- |
| the post itself | `moderation_hide` | `moderation_unhide` |
| the post's clip | `clip_rejected` | `clip_approved` |
"""

from django.core.cache import cache

from .access_models import AccessAudit

HIDE = "moderation_hide"
UNHIDE = "moderation_unhide"
CLIP_REJECTED = "clip_rejected"
CLIP_APPROVED = "clip_approved"

#: action -> (family, whether it puts the author under the cooldown)
FAMILIES = {
    HIDE: ("post", True),
    UNHIDE: ("post", False),
    CLIP_REJECTED: ("clip", True),
    CLIP_APPROVED: ("clip", False),
}

#: What the admin writes as the reason, which its actions do not ask for.
ADMIN_REASON = "Django admin"


def _changed():
    """A decision reaches the For You page now, not in a minute."""
    cache.delete("for-you:moderated")


def record_post_decisions(actor, rows, action, reason=ADMIN_REASON):
    """Write one audit row per post decided. `rows` is an iterable of
    (post id, author's auth user id, state before, state after), each state a
    small dict such as {"is_hidden": False}. Posts whose state did not change
    are skipped: hiding a hidden post decides nothing."""
    entries = [
        AccessAudit(
            actor=actor, target_id=user_id, action=action, subject=f"post:{post_id}",
            before=before, after=after, reason=(reason or ADMIN_REASON)[:500],
        )
        for post_id, user_id, before, after in rows
        if before != after
    ]
    if entries:
        AccessAudit.objects.bulk_create(entries)
        _changed()
    return len(entries)


def record_hide(actor, posts, hide, reason=ADMIN_REASON):
    """Audit hiding (or, with `hide` false, unhiding) these posts. Call it
    with the posts as they were before the change."""
    return record_post_decisions(
        actor,
        (
            (pk, user_id, {"is_hidden": hidden}, {"is_hidden": hide})
            for pk, user_id, hidden in posts.values_list("pk", "author__user_id", "is_hidden")
        ),
        HIDE if hide else UNHIDE,
        reason,
    )


def record_clip_review(actor, videos, status_value, reason=ADMIN_REASON):
    """Audit a decision on clips. Only rejection and approval are decisions
    about the author; call it with the clips as they were before."""
    from .models import PostVideo

    if status_value == PostVideo.Status.REJECTED:
        action = CLIP_REJECTED
    elif status_value == PostVideo.Status.APPROVED:
        action = CLIP_APPROVED
    else:
        return 0
    return record_post_decisions(
        actor,
        (
            (post_id, user_id, {"clip": status}, {"clip": str(status_value)})
            for post_id, user_id, status in videos.values_list("post_id", "post__author__user_id", "status")
        ),
        action,
        reason,
    )


def latest_decisions(since):
    """{(post id, family): (action, author's auth user id)} for the newest
    decision of each family on each post since `since`. A later undo
    replaces an earlier decision, so the state each post ended up in is what
    comes back."""
    found = {}
    for subject, action, target, pk in (
        AccessAudit.objects.filter(
            action__in=list(FAMILIES), created_at__gte=since, subject__startswith="post:",
            target__isnull=False,
        )
        .order_by("created_at", "pk")
        .values_list("subject", "action", "target_id", "pk")
    ):
        try:
            post_id = int(subject.split(":", 1)[1])
        except ValueError:
            continue
        family, _ = FAMILIES[action]
        found[(post_id, family)] = (action, target)
    return found
