"""The For You endpoints. The ranking itself is in core.recommendations."""

from urllib.parse import urlencode

from django.db.models import Exists, OuterRef
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import PostFeedback
from .recommendations import (
    BadCursor,
    build_ranking,
    load_session,
    make_cursor,
    read_cursor,
    record_impressions,
    start_session,
)
from .serializers import (
    ForYouPageSerializer,
    ForYouPostSerializer,
    ImpressionBatchSerializer,
    ImpressionResultSerializer,
)
from .views import annotate_social_counts, posts_for_cards, profile_for, visible_posts_for

#: The same sizes the following feed pages in, and the same refusal past the
#: maximum: a client that asked for 200 and got 50 could not tell that from a
#: feed with 50 posts left in it.
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 50


def _page_size(request):
    raw = request.query_params.get("page_size")
    if raw is None:
        return DEFAULT_PAGE_SIZE
    try:
        size = int(raw)
    except (TypeError, ValueError):
        raise ValidationError({"page_size": "Page size must be a whole number."})
    if size < 1:
        raise ValidationError({"page_size": "Page size must be at least 1."})
    if size > MAX_PAGE_SIZE:
        raise ValidationError({"page_size": f"Page size may not be more than {MAX_PAGE_SIZE}."})
    return size


class ForYouView(APIView):
    """Posts picked for the reader, from everyone, best first.

    The first page ranks from scratch, so it always reflects what the reader
    did a moment ago. Later pages read the ranking that first page made, by
    position, so paging never repeats or skips a post. Each page is checked
    against the visibility rules again as it is served: a post hidden,
    deleted, dismissed or blocked since the ranking was made does not appear.
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
        timing = None

        if cursor:
            try:
                session, offset = read_cursor(cursor)
            except BadCursor:
                raise ValidationError({"cursor": "That cursor is not one this feed issued."})
            ranked = load_session(viewer, session)
            if ranked is None:
                # The ranking expired from the cache. Rank again under the same
                # name and carry on from the same position: the order may have
                # moved since, which is better than ending the feed.
                ranking = build_ranking(viewer)
                timing = ranking.duration_ms
                ranked = ranking.items
                session = start_session(viewer, ranking)
        else:
            ranking = build_ranking(viewer)
            timing = ranking.duration_ms
            ranked = ranking.items
            session = start_session(viewer, ranking)
            offset = 0

        window = ranked[offset:offset + size]
        wanted = [item.pk for item in window]
        servable = annotate_social_counts(
            viewer,
            visible_posts_for(viewer, posts_for_cards().filter(pk__in=wanted)),
        ).exclude(
            Exists(PostFeedback.objects.filter(viewer=viewer, post=OuterRef("pk")))
        )
        by_id = {post.pk: post for post in servable}
        page = []
        for item in window:
            post = by_id.get(item.pk)
            if post is not None:
                post._recommendation = (item.reason, item.label)
                page.append(post)

        following = offset + size
        next_link = None
        if following < len(ranked):
            next_link = request.build_absolute_uri(
                request.path + "?" + _query(make_cursor(session, following), request)
            )
        data = ForYouPostSerializer(page, many=True, context={"request": request}).data
        response = Response({"next": next_link, "previous": None, "results": data})
        if timing is not None:
            # Visible in any browser's network panel, and to anything in front
            # of this server that collects Server-Timing.
            response["Server-Timing"] = f"rank;dur={timing:.1f}"
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
