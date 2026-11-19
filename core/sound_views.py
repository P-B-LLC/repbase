"""Sounds over the API: publishing one, reading one, the posts set to it, and
taking it down. The rules are in `core.sounds` and `core.publication`; this
is their HTTP surface.

A sound a reader may not hear -- removed, its owner suspended, a block
between them -- is a 404, like a post they may not see. Its owner still sees
their own removed sound, so they can put it back.
"""

from django.db.models import Count, OuterRef, Subquery, Value
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import rollout
from .models import Post, Sound
from .publication import PROMPTS, sounds_shown_to
from .serializers import (
    CreateSoundSerializer,
    PostSerializer,
    PromptSerializer,
    SoundSerializer,
    UpdateSoundSerializer,
)
from .sounds import publish_sound, set_removed
from .views import (
    ATTACHMENTS_PARAMETER,
    PUBLIC_AND_STANDING,
    FeedCursorPagination,
    annotate_social_counts,
    posts_for_cards,
    profile_for,
    visible_posts_for,
    wants_summaries,
)


def counted_sounds():
    """Sounds with how many posts anybody could see are set to each."""
    uses = (
        Post.objects.filter(sound=OuterRef("pk")).filter(PUBLIC_AND_STANDING)
        .order_by().values("sound").annotate(n=Count("pk")).values("n")[:1]
    )
    return Sound.objects.select_related("owner__user").annotate(
        use_total=Coalesce(Subquery(uses), Value(0))
    )


def sound_for_reader(viewer, pk):
    """The sound, if this reader may hear it; their own, removed or not."""
    sound = get_object_or_404(counted_sounds(), pk=pk)
    if sound.owner_id != viewer.pk and sound.pk not in sounds_shown_to(viewer, [sound]):
        raise NotFound()
    return sound


def owned_sound(viewer, pk):
    """Somebody else's sound is a 404 to a change, as their post is."""
    return get_object_or_404(counted_sounds(), pk=pk, owner=viewer)


class SoundsView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_scope = "post"

    @extend_schema(
        operation_id="social_sounds_create",
        request=CreateSoundSerializer,
        responses={201: SoundSerializer, 200: SoundSerializer},
        description=(
            "Publish the audio of one of your own posts as a sound others may "
            "set their clips to. The post needs an approved video with audio. "
            "Asking again for the same post answers 200 with the sound "
            "already made. The audio is prepared shortly afterwards; until "
            "then `audio_url` is null."
        ),
    )
    def post(self, request):
        body = CreateSoundSerializer(data=request.data, context={"request": request})
        body.is_valid(raise_exception=True)
        viewer = profile_for(request.user)
        if not rollout.enabled("sounds", viewer):
            raise PermissionDenied("Sounds are not available on this account yet.")
        # Among the author's own, so somebody else's post id is refused in
        # the words used for one that does not exist.
        post = Post.objects.filter(pk=body.validated_data["post"], author=viewer).first()
        if post is None:
            raise ValidationError({"post": "No post of yours with that id."})
        sound, created = publish_sound(
            viewer, post, body.validated_data["title"], body.validated_data["reuse_permission"]
        )
        data = SoundSerializer(
            counted_sounds().get(pk=sound.pk), context={"request": request}
        ).data
        return Response(data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class SoundView(APIView):
    permission_classes = [IsAuthenticated]

    def get_throttles(self):
        self.throttle_scope = None if self.request.method == "GET" else "post"
        return super().get_throttles()

    @extend_schema(operation_id="social_sounds_retrieve", responses={200: SoundSerializer})
    def get(self, request, pk):
        sound = sound_for_reader(profile_for(request.user), pk)
        return Response(SoundSerializer(sound, context={"request": request}).data)

    @extend_schema(
        operation_id="social_sounds_partial_update",
        request=UpdateSoundSerializer,
        responses={200: SoundSerializer},
        description=(
            "Rename your sound, or change who may use it. Closing it stops "
            "new uses; posts already set to it keep it."
        ),
    )
    def patch(self, request, pk):
        sound = owned_sound(profile_for(request.user), pk)
        body = UpdateSoundSerializer(data=request.data, partial=True, context={"request": request})
        body.is_valid(raise_exception=True)
        for name, value in body.validated_data.items():
            setattr(sound, name, value)
        if body.validated_data:
            sound.save(update_fields=list(body.validated_data))
        return Response(SoundSerializer(sound, context={"request": request}).data)

    @extend_schema(
        operation_id="social_sounds_destroy",
        responses={204: None},
        description=(
            "Take your sound down. Posts set to it stay and lose its credit, "
            "its audio stops being served, and nobody new can use it. "
            "`restore` puts all of that back."
        ),
    )
    def delete(self, request, pk):
        set_removed(owned_sound(profile_for(request.user), pk), True, by_moderator=False)
        return Response(status=status.HTTP_204_NO_CONTENT)


class RestoreSoundView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_scope = "post"

    @extend_schema(
        operation_id="social_sounds_restore_create",
        request=None,
        responses={200: SoundSerializer},
        description="Put back a sound you took down. Not one a moderator removed.",
    )
    def post(self, request, pk):
        viewer = profile_for(request.user)
        set_removed(owned_sound(viewer, pk), False, by_moderator=False)
        return Response(SoundSerializer(owned_sound(viewer, pk), context={"request": request}).data)


class SoundPostsView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="social_sounds_posts_list",
        parameters=[
            OpenApiParameter(name="cursor", type=str, location=OpenApiParameter.QUERY),
            OpenApiParameter(name="page_size", type=int, location=OpenApiParameter.QUERY),
            ATTACHMENTS_PARAMETER,
        ],
        responses=PostSerializer(many=True),
        description=(
            "The posts set to this sound that the reader may see, newest "
            "first. A sound that has been taken down has no such list."
        ),
    )
    def get(self, request, pk):
        viewer = profile_for(request.user)
        sound = sound_for_reader(viewer, pk)
        if sound.status != Sound.Status.ACTIVE:
            # Its owner can read their removed sound; nobody is shown what
            # was set to it while it is down.
            raise NotFound()
        summary = wants_summaries(request)
        posts = annotate_social_counts(
            viewer, visible_posts_for(viewer, posts_for_cards(summary=summary))
        ).filter(sound=sound)
        paginator = FeedCursorPagination()
        page = paginator.paginate_queryset(posts, request, view=self)
        context = {"request": request, **({"attachments": "summary"} if summary else {})}
        return paginator.get_paginated_response(PostSerializer(page, many=True, context=context).data)


class PromptsView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="social_prompts_list",
        responses={200: PromptSerializer(many=True)},
        description=(
            "The prompts a post can answer. Send a code as `prompt` when "
            "creating a post; browse one with `?prompt=` on the post list."
        ),
    )
    def get(self, request):
        return Response(PromptSerializer(
            [{"code": code, "label": label} for code, label in PROMPTS], many=True
        ).data)
