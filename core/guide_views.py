"""Whether the welcome guide still has to open by itself, per app."""
from django.db import transaction
from django.http import Http404
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .access_views import PrivateAccessView
from .models import GuideCompletion


class GuideSerializer(serializers.Serializer):
    # A plain string on the way out, not a choice: a third app added later
    # must not be a value an older client's generated enum cannot decode.
    surface = serializers.CharField(read_only=True)
    completed = serializers.BooleanField()


SURFACE = OpenApiParameter(
    'surface', str, OpenApiParameter.PATH,
    enum=GuideCompletion.Surface.values,
    description='Which app the guide is for. Each is laid out differently, so each has its own.',
)


class GuideView(PrivateAccessView):
    permission_classes = [IsAuthenticated]

    @extend_schema(operation_id='guideRetrieve', parameters=[SURFACE], responses=GuideSerializer)
    def get(self, request, surface):
        completed = GuideCompletion.objects.filter(
            user=request.user, surface=self._known(surface)).exists()
        return Response(dict(surface=surface, completed=completed))

    @extend_schema(
        operation_id='guideUpdate', parameters=[SURFACE],
        request=GuideSerializer, responses=GuideSerializer,
        description=(
            'Finishing or skipping the guide sends `completed: true`, after which it no longer '
            'opens by itself in that app. `false` clears that, but only a device or browser '
            'that has not shown the guide yet opens it again: each also keeps its own note. '
            'Replaying it from settings needs neither.'
        ),
    )
    @transaction.atomic
    def put(self, request, surface):
        surface = self._known(surface)
        data = GuideSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        if data.validated_data['completed']:
            # Idempotent: the same tap retried after a lost answer is not a conflict.
            GuideCompletion.objects.get_or_create(user=request.user, surface=surface)
        else:
            GuideCompletion.objects.filter(user=request.user, surface=surface).delete()
        return Response(dict(surface=surface, completed=data.validated_data['completed']))

    @staticmethod
    def _known(surface):
        if surface not in GuideCompletion.Surface.values:
            raise Http404
        return surface
