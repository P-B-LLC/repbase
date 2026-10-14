"""The For You endpoints, and the people suggested beside it.

The ranking itself is in core.recommendations, and the suggestions in
core.suggestions. What is here is the HTTP: paging, the reader's controls
over their page, and impressions.
"""

from urllib.parse import urlencode

from django.db import transaction
from django.db.models import Exists, OuterRef
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import PostFeedback, PostView, RecommendationPreferences, RepbaseUser, SuggestionDismissal
from .recommendations import (
    HIDEABLE_TOPICS,
    BadCursor,
    build_profile,
    forget_reasons,
    interests,
    make_cursor,
    preferences_for,
    read_cursor,
    record_impressions,
    serve_page,
    topic_label,
)
from .serializers import (
    ForYouPageSerializer,
    ForYouPostSerializer,
    ForYouPreferencesSerializer,
    ImpressionBatchSerializer,
    ImpressionResultSerializer,
    SuggestedPersonSerializer,
    UpdateForYouPreferencesSerializer,
)
from .suggestions import MAX_SUGGESTIONS, suggest_people
from .views import (
    FeedCursorPagination,
    annotate_social_counts,
    posts_for_cards,
    profile_for,
    visible_posts_for,
    without_blocked_authors,
)

#: The same sizes the following feed pages in, and the same refusal past the
#: maximum: a client that asked for 200 and got 50 could not tell that from a
#: feed with 50 posts left in it. Read from the following feed's paginator, so
#: the two cannot drift apart.
MAX_PAGE_SIZE = FeedCursorPagination.max_page_size


def _page_size(request):
    return FeedCursorPagination().get_page_size(request)


class WritesAreSocialMixin:
    # Reading is under the per-user ceiling alone; changing something is
    # spent from the "social" budget, like a like or a "not interested".
    # Comments rather than a docstring, for the reason ThrottleByActionMixin
    # gives: drf-spectacular would describe the view with this text.

    @property
    def throttle_scope(self):
        return None if self.request.method in ("GET", "HEAD", "OPTIONS") else "social"


class ForYouView(APIView):
    """Posts picked for the reader, from everyone, best first.

    The first page ranks from scratch, so it always reflects what the reader
    did a moment ago. Each later page is ranked from what that first ranking
    left, re-scored for what the reader has done since -- liked, dismissed,
    scrolled past -- while what earlier pages served stays where it was, so
    paging never repeats or skips a post. Each page is checked against the
    visibility rules again as it is served.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="social_for_you_list",
        parameters=[
            OpenApiParameter(
                name="cursor",
                type=str,
                location=OpenApiParameter.QUERY,
                description=(
                    "The position carried by the previous page's `next` link. "
                    "Absent means rank afresh and start at the top."
                ),
            ),
            OpenApiParameter(
                name="page_size",
                type=int,
                location=OpenApiParameter.QUERY,
                description=(
                    f"How many posts to return, at most {MAX_PAGE_SIZE}. Asking "
                    "for more is refused rather than reduced."
                ),
            ),
        ],
        responses={200: ForYouPageSerializer},
    )
    def get(self, request):
        viewer = profile_for(request.user)
        size = _page_size(request)
        cursor = request.query_params.get("cursor")
        session, offset = None, 0
        if cursor:
            try:
                session, offset = read_cursor(cursor)
            except BadCursor:
                raise ValidationError({"cursor": "That cursor is not one this feed issued."})

        page = serve_page(viewer, session, offset, size)
        wanted = [item.pk for item in page.items]
        servable = annotate_social_counts(
            viewer,
            visible_posts_for(viewer, posts_for_cards().filter(pk__in=wanted)),
        ).exclude(
            # Turned down since it was ranked. "More like this" stays where it
            # is: the reader wants it, and a retried page should show it.
            Exists(PostFeedback.objects.filter(
                viewer=viewer, post=OuterRef("pk"), kind__in=PostFeedback.REJECTING
            ))
        )
        by_id = {post.pk: post for post in servable}
        posts = []
        for item in page.items:
            post = by_id.get(item.pk)
            if post is not None:
                post._recommendation = (item.reason, item.label)
                posts.append(post)

        next_link = None
        if page.next_offset is not None:
            next_link = request.build_absolute_uri(
                request.path + "?" + _query(make_cursor(page.session, page.next_offset), request)
            )
        data = ForYouPostSerializer(posts, many=True, context={"request": request}).data
        response = Response({"next": next_link, "previous": None, "results": data})
        if page.duration_ms is not None:
            # Visible in any browser's network panel, and to anything in front
            # of this server that collects Server-Timing.
            response["Server-Timing"] = f"rank;dur={page.duration_ms:.1f}"
        return response


def _query(cursor, request):
    params = {"cursor": cursor}
    if "page_size" in request.query_params:
        params["page_size"] = request.query_params["page_size"]
    return urlencode(params)


class ImpressionsView(APIView):
    """What was on the reader's screen, and for how long.

    Sent by the client in batches as the reader scrolls. This is what lets
    the For You page tell a post somebody stopped on from one they flicked
    past, and a clip watched to the end from one abandoned after a second.
    """

    permission_classes = [IsAuthenticated]
    throttle_scope = "impression"

    @extend_schema(
        request=ImpressionBatchSerializer,
        responses={202: ImpressionResultSerializer},
        description=(
            "Record a batch of views. Posts the reader cannot see, and their "
            "own posts, are ignored rather than refused, so a client does not "
            "need to know which of the posts it showed have since gone."
        ),
    )
    def post(self, request):
        body = ImpressionBatchSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        recorded = record_impressions(profile_for(request.user), body.validated_data["events"])
        return Response(
            ImpressionResultSerializer({"recorded": recorded}).data,
            status=status.HTTP_202_ACCEPTED,
        )


def _preferences_payload(viewer, preferences=None):
    preferences = preferences or preferences_for(viewer)
    profile = build_profile(viewer, preferences=preferences)
    return ForYouPreferencesSerializer({
        "personalized": preferences.personalized,
        "hidden_topics": [topic for topic in preferences.hidden_topics or () if topic in HIDEABLE_TOPICS],
        "history_cleared_at": preferences.history_cleared_at,
        "interests": interests(profile),
        "hideable_topics": [{"topic": topic, "label": topic_label(topic)} for topic in HIDEABLE_TOPICS],
    }).data


class ForYouPreferencesView(WritesAreSocialMixin, APIView):
    """The reader's say over their For You page.

    What the page has picked up about them (topics only, strongest first),
    which topics they have hidden, and whether it is personalised at all.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="social_for_you_preferences_retrieve",
        responses={200: ForYouPreferencesSerializer},
    )
    def get(self, request):
        return Response(_preferences_payload(profile_for(request.user)))

    @extend_schema(
        operation_id="social_for_you_preferences_partial_update",
        request=UpdateForYouPreferencesSerializer,
        responses={200: ForYouPreferencesSerializer},
        description=(
            "Turn personalisation on or off, or replace the list of hidden "
            "topics. Either field may be left out. Takes effect on the next "
            "page ranked."
        ),
    )
    def patch(self, request):
        viewer = profile_for(request.user)
        body = UpdateForYouPreferencesSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        with transaction.atomic():
            preferences, _ = RecommendationPreferences.objects.select_for_update().get_or_create(
                viewer=viewer
            )
            for name, value in body.validated_data.items():
                setattr(preferences, name, value)
            preferences.save()
        return Response(_preferences_payload(viewer, preferences))


class ForYouHistoryView(WritesAreSocialMixin, APIView):
    """Start fresh."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="social_for_you_history_destroy",
        request=None,
        responses={200: ForYouPreferencesSerializer},
        description=(
            "Start the For You page fresh. Everything the reader did before "
            "now stops counting toward what they are shown, and their viewing "
            "history -- which posts were on screen, for how long, how much of "
            "a clip they watched -- is deleted. Likes, comments, follows and "
            "posts marked not interested stay: they are the reader's own "
            "actions, not a record kept about them. Answers with the "
            "preferences, not 204."
        ),
    )
    def delete(self, request):
        viewer = profile_for(request.user)
        with transaction.atomic():
            preferences, _ = RecommendationPreferences.objects.select_for_update().get_or_create(
                viewer=viewer
            )
            preferences.history_cleared_at = timezone.now()
            preferences.save()
            PostView.objects.filter(viewer=viewer).delete()
        forget_reasons(viewer)
        return Response(_preferences_payload(viewer, preferences))


class SuggestedPeopleView(APIView):
    """People the reader might want to follow, each with why."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="social_suggested_people_list",
        parameters=[
            OpenApiParameter(
                name="limit",
                type=int,
                location=OpenApiParameter.QUERY,
                description=f"How many to suggest, 1 to {MAX_SUGGESTIONS}. Ten if left out.",
            ),
        ],
        responses={200: SuggestedPersonSerializer(many=True)},
    )
    def get(self, request):
        raw = request.query_params.get("limit", "10")
        try:
            limit = int(raw)
        except (TypeError, ValueError):
            raise ValidationError({"limit": "Limit must be a whole number."})
        if not 1 <= limit <= MAX_SUGGESTIONS:
            raise ValidationError({"limit": f"Limit must be between 1 and {MAX_SUGGESTIONS}."})
        viewer = profile_for(request.user)
        found = suggest_people(viewer, limit=limit)
        return Response(SuggestedPersonSerializer(
            [{"person": person, "reason": reason, "label": label} for person, reason, label in found],
            many=True,
            context={"request": request},
        ).data)


class SuggestionDismissalView(APIView):
    """"Not this person", and taking it back."""

    permission_classes = [IsAuthenticated]
    throttle_scope = "social"

    def _person(self, request, pk):
        viewer = profile_for(request.user)
        # The same 404 for somebody blocked either way as for nobody at all.
        people = without_blocked_authors(
            viewer, RepbaseUser.objects.filter(user__is_active=True).exclude(pk=viewer.pk), "pk"
        )
        return viewer, get_object_or_404(people, pk=pk)

    @extend_schema(
        operation_id="social_suggested_people_dismiss_create",
        request=None,
        responses={204: None},
        description=(
            "Stop suggesting this person, for six months. Nobody is told. "
            "Dismissing twice is not an error."
        ),
    )
    def post(self, request, pk):
        viewer, person = self._person(request, pk)
        SuggestionDismissal.objects.get_or_create(viewer=viewer, person=person)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(
        operation_id="social_suggested_people_dismiss_destroy",
        request=None,
        responses={204: None},
        description="Take a dismissal back, so this person may be suggested again.",
    )
    def delete(self, request, pk):
        viewer, person = self._person(request, pk)
        SuggestionDismissal.objects.filter(viewer=viewer, person=person).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
