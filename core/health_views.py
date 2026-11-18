from django.db import transaction
from django.utils import timezone
from drf_spectacular.utils import extend_schema, OpenApiParameter
from rest_framework import generics, pagination
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .health_imports import lock_health_owner
from .health_models import AppleHealthConnection, HealthWorkoutRecord
from .health_serializers import (AppleHealthConnectionSerializer,
    AppleHealthConnectionUpdateSerializer, HealthWorkoutSummarySerializer, HEALTH_CONSENT_VERSION)
from .params import date_param


def _owner(request):
    # Same creation-safe profile lookup used by the existing import endpoint.
    from .views import profile_for
    return profile_for(request.user)


def _status(connection):
    return dict(enabled=bool(connection and connection.enabled and connection.consent_version == HEALTH_CONSENT_VERSION),
        consent_version=connection.consent_version if connection else None,
        last_sync_at=connection.last_sync_at if connection else None,
        last_sync_imported_count=connection.last_sync_imported_count if connection else 0,
        last_sync_updated_count=connection.last_sync_updated_count if connection else 0,
        schema_version=1, garmin_direct_available=False)


class AppleHealthConnectionView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=AppleHealthConnectionSerializer)
    def get(self, request):
        value = AppleHealthConnection.objects.filter(owner=_owner(request)).first()
        return Response(AppleHealthConnectionSerializer(_status(value)).data)

    @extend_schema(request=AppleHealthConnectionUpdateSerializer, responses=AppleHealthConnectionSerializer)
    def patch(self, request):
        payload = AppleHealthConnectionUpdateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        owner = _owner(request)
        with transaction.atomic():
            lock_health_owner(owner)
            value, _ = AppleHealthConnection.objects.get_or_create(owner=owner)
            value.enabled = payload.validated_data["enabled"]
            if value.enabled:
                value.consent_version = HEALTH_CONSENT_VERSION
                value.consented_at = timezone.now()
            value.save()
        return Response(AppleHealthConnectionSerializer(_status(value)).data)


class HealthWorkoutPagination(pagination.PageNumberPagination):
    page_size = 50


class HealthWorkoutListView(generics.ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = HealthWorkoutSummarySerializer
    pagination_class = HealthWorkoutPagination

    @extend_schema(parameters=[
        OpenApiParameter("source", str, enum=["apple_health", "garmin"]),
        OpenApiParameter("since", str, description="Inclusive start date, YYYY-MM-DD."),
    ])
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        query = HealthWorkoutRecord.objects.filter(owner=_owner(self.request))
        source = self.request.query_params.get("source")
        if source is not None:
            if source not in ("apple_health", "garmin"):
                raise ValidationError({"source": "Choose apple_health or garmin."})
            query = query.filter(source=source)
        since = date_param(self.request, "since")
        if since:
            query = query.filter(started_at__date__gte=since)
        return query
