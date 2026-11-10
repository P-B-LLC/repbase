"""What a post says about itself beyond its caption and snapshot, and what it
may be made from.

Four things live here, each the single place that decides it:

- **Hashtags.** Read out of the caption, normalised, capped. Stored twice on
  purpose -- a list on the post for drawing a card, a row per tag for finding
  posts by tag -- and `write_hashtags` is the one function that writes
  either, so the two cannot disagree.
- **Topics and language.** Checked against a known vocabulary when given.
- **Takes.** `take_source_for` decides whether one post may be published as a
  response to another, and `sources_shown_to` decides, per reader, whether
  the credit on a take may still show the post it answers.
- **Sounds.** The same two questions for a clip's audio.

The rules for a take, in one place because they are the ones that protect
people:

1. A take can only be made of a post its maker can see, that anybody signed in
   could see (public, on an open profile), and whose author allows reuse. A
   post some people cannot see is never answered in front of them: that is
   how a private source would become public.
2. A take is its author's own post. It is not removed when its source is
   deleted, taken down, made private, or closed to reuse. What changes is the
   credit: a reader who may not see the source is told only that the original
   is unavailable -- no author, no title, no id.
3. Closing a post to reuse stops new takes. Takes already published stay, and
   keep their credit for as long as their readers may see the source.

A repost is not a source. A take of a repost is a take of the post it passes
on, as a like of one is.
"""

import re
import unicodedata

from django.db.models import Q
from rest_framework.exceptions import ValidationError

from .models import Block, Follow, Post, PostHashtag, RepbaseUser, Sound

#: The most hashtags one post carries. The rest of a caption stuffed with
#: them are left as the plain text they also are, rather than refused: the
#: caption is the author's to write, and what is capped is its reach.
MAX_HASHTAGS = 10
#: The longest a tag may be. A longer run of letters after a `#` is not a
#: shorter tag with a tail; it is not a tag.
MAX_HASHTAG_LENGTH = 50
#: The most topics an author may declare on one post.
MAX_TOPICS = 5

# A `#` that starts a word -- not one inside it, so the fragment of a URL and
# the `&#39;` of escaped text are not tags -- followed by letters, digits or
# underscores in any script. The full-width sign is the one a Japanese or
# Chinese keyboard types.
_HASHTAG = re.compile(r"(?<![\w&#＃/])[#＃](\w+)")

# The shape of a BCP 47 tag as far as this needs it: a language of two or
# three letters, then optional script, region or variant subtags.
_LANGUAGE = re.compile(r"^[a-z]{2,3}(-[a-z0-9]{2,8}){0,3}$")

#: The kinds a take can be, and can be made of: the three a reader is shown
#: as a card with something to try. A planner entry is not a thing to have a
#: take on, and a repost has nothing of its own.
TAKE_KINDS = (Post.Kind.MEDIA, Post.Kind.WORKOUT, Post.Kind.MEAL)


#: The prompts a post can answer: a code, and the words shown for it. A short
#: fixed list, so a prompt is something to browse by and never free text.
PROMPTS = (
    ("gym_lore", "Gym lore"),
    ("rack_taken", "Rack taken"),
    ("first_time", "First time trying this"),
    ("meal_prep", "Meal prep"),
    ("form_check", "Form check"),
)


def checked_prompt(value):
    """A prompt code, or "" for none. Raises ValueError for one not offered."""
    code = (value or "").strip()
    if code and code not in dict(PROMPTS):
        raise ValueError("Use one of: " + ", ".join(code for code, _ in PROMPTS) + ".")
    return code


def hashtags_in(caption):
    """[tag] the hashtags written in a caption, normalised, once each, in the
    order they first appear, `MAX_HASHTAGS` at most.

    Normalised with NFKC and casefold, so `#LegDay`, `#legday` and the
    full-width form somebody's keyboard produced are one tag. A tag that is
    only digits is left out: "#1" is a ranking, not a subject.
    """
    found = []
    for raw in _HASHTAG.findall(caption or ""):
        tag = unicodedata.normalize("NFKC", raw).casefold()
        if not tag or len(tag) > MAX_HASHTAG_LENGTH or tag.isdigit() or tag in found:
            continue
        found.append(tag)
        if len(found) == MAX_HASHTAGS:
            break
    return found


def write_hashtags(post, save=True):
    """Make the post's stored hashtags match its caption, in both places.

    Called inside the transaction that wrote the caption. The rows are
    replaced only where they differ, so re-saving an unchanged caption writes
    nothing.
    """
    wanted = hashtags_in(post.caption)
    if post.hashtags != wanted:
        post.hashtags = wanted
        if save:
            post.save(update_fields=["hashtags"])
    existing = set(PostHashtag.objects.filter(post=post).values_list("tag", flat=True))
    stale = existing - set(wanted)
    if stale:
        PostHashtag.objects.filter(post=post, tag__in=stale).delete()
    PostHashtag.objects.bulk_create(
        [PostHashtag(post=post, tag=tag) for tag in wanted if tag not in existing]
    )
    return wanted


def normalised_language(value):
    """A language tag in canonical case ("pt-BR"), or "" for none given.

    Raises ValueError for something that is not a tag. Not checked against a
    registry: the point is that it is a tag a client can match on, not that
    this server knows every language there is.
    """
    text = (value or "").strip().replace("_", "-")
    if not text:
        return ""
    if not _LANGUAGE.match(text.lower()):
        raise ValueError("Use a language tag such as en or pt-BR.")
    primary, *rest = text.lower().split("-")
    cased = [
        part.upper() if len(part) == 2 else part.title() if len(part) == 4 else part
        for part in rest
    ]
    return "-".join([primary, *cased])


def declarable_topics():
    """[topic code] an author may put on a post: what kind of training it is,
    and the disciplines. A fixed vocabulary, so a declared topic is always one
    the ranking can match and never free text.

    What a post *is* -- its kind, whether it has a clip, the exercises in its
    workout -- is not declared; the ranking reads that off the post itself.
    """
    from .recommendations import HIDEABLE_TOPICS

    types = [topic for topic in HIDEABLE_TOPICS if topic.startswith("type:")]
    disciplines = [
        f"discipline:{value}"
        for value, _ in RepbaseUser.TrainingStyle.choices
        if value != RepbaseUser.TrainingStyle.OTHER
    ]
    return [*types, *disciplines]


def checked_topics(values):
    """The topics given, once each, in the catalogue's order. Raises
    ValueError naming any that are not in it."""
    offered = declarable_topics()
    unknown = sorted(set(values) - set(offered))
    if unknown:
        raise ValueError(
            f"Not a topic a post can carry: {', '.join(unknown)}. "
            f"Use any of: {', '.join(offered)}."
        )
    chosen = [topic for topic in offered if topic in set(values)]
    if len(chosen) > MAX_TOPICS:
        raise ValueError(f"A post can carry at most {MAX_TOPICS} topics.")
    return chosen


def may_reuse(viewer, owner_id, permission, viewer_follows_owner):
    """Whether `viewer` may reuse something of `owner_id`'s with this
    permission. The owner always may; `viewer_follows_owner` is a callable so
    the follow table is only asked when the answer depends on it."""
    if viewer.pk == owner_id:
        return True
    if permission == Post.Reuse.EVERYONE:
        return True
    if permission == Post.Reuse.FOLLOWERS:
        return bool(viewer_follows_owner())
    return False


def take_source_for(author, source_post_id):
    """The post a take is being made of, or a refusal under `source_post_id`.

    A post the author cannot see is refused in the words used for one that
    does not exist, so the refusal says nothing about what is there. The
    other two refusals are about a post the author is looking at, and say
    what is wrong with it.
    """
    from .views import visible_posts_for

    missing = ValidationError({"source_post_id": "No post you can see with that id."})
    found = (
        visible_posts_for(author, Post.objects.filter(pk=source_post_id))
        .select_related("author", "repost_of__author")
        .first()
    )
    if found is None:
        raise missing
    source = found.repost_of or found
    if source.kind not in TAKE_KINDS:
        raise ValidationError(
            {"source_post_id": "Only a photo or video, workout or recipe post can be answered with a take."}
        )
    if not source.is_open_to_everyone:
        raise ValidationError(
            {"source_post_id": "A take can only be made of a post everybody can see."}
        )
    allowed = may_reuse(
        author, source.author_id, source.reuse_permission,
        lambda: Follow.objects.filter(follower=author, following_id=source.author_id).exists(),
    )
    if not allowed:
        raise ValidationError(
            {"source_post_id": "Its author has not allowed takes of that post."}
        )
    return source


def lineage_from(source):
    """The lineage columns for a take of `source`.

    The canonical original is the source's own, or the source itself when it
    is not a take -- or is one whose original has since been deleted, where
    the source is the oldest link still standing.
    """
    has_attachment = source.kind in (Post.Kind.WORKOUT, Post.Kind.MEAL)
    return {
        "is_take": True,
        "source_post": source,
        "original_post_id": source.original_post_id or source.pk,
        "source_attachment_version": source.attachment_version if has_attachment else None,
    }


def sound_for(author, sound_id):
    """The sound a post is being set to, or a refusal under `sound_id`.

    One refusal for missing, removed, blocked and suspended alike: none of
    those is the author's business. A sound they can find and may not use
    says so.
    """
    sound = Sound.objects.filter(
        pk=sound_id, status=Sound.Status.ACTIVE, owner__user__is_active=True
    ).first()
    if sound is None or sound.owner_id in _blocked_people(author, [sound.owner_id]):
        raise ValidationError({"sound_id": "No sound you can use with that id."})
    allowed = may_reuse(
        author, sound.owner_id, sound.reuse_permission,
        lambda: Follow.objects.filter(follower=author, following_id=sound.owner_id).exists(),
    )
    if not allowed:
        raise ValidationError({"sound_id": "Its owner has not allowed that sound to be reused."})
    return sound


def _blocked_people(viewer, people_ids):
    """The ids among `people_ids` with a block between them and `viewer`, in
    either direction."""
    others = set(people_ids) - {viewer.pk}
    if not others:
        return set()
    pairs = Block.objects.filter(
        Q(blocker=viewer, blocked__in=others) | Q(blocker__in=others, blocked=viewer)
    ).values_list("blocker_id", "blocked_id")
    return {blocked if blocker == viewer.pk else blocker for blocker, blocked in pairs}


def sources_shown_to(viewer, source_ids):
    """The ids among `source_ids` whose post `viewer` may see right now.

    Asked of `visible_posts_for` rather than restated, as the repost preview
    does: a second opinion about who may see what is the thing that drifts,
    silently and in the unsafe direction. One query for a page, and none for
    a page with no takes on it.
    """
    from .views import visible_posts_for

    wanted = {pk for pk in source_ids if pk is not None}
    if not wanted or viewer is None:
        return set()
    return set(
        visible_posts_for(viewer, Post.objects.filter(pk__in=wanted)).values_list("pk", flat=True)
    )


def sounds_shown_to(viewer, sounds):
    """The ids among these `Sound` rows whose credit `viewer` may see: still
    active, its owner not suspended, and no block between them. The rows are
    the ones the page already loaded; the only query is for blocks, and only
    when there is a sound on the page."""
    live = [
        sound for sound in sounds
        if sound.status == Sound.Status.ACTIVE and sound.owner.user.is_active
    ]
    if not live or viewer is None:
        return set()
    blocked = _blocked_people(viewer, [sound.owner_id for sound in live])
    return {sound.pk for sound in live if sound.owner_id not in blocked}
