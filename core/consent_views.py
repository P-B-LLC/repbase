"""Private, versioned safety-review preference shared by app and web."""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from .access_views import PrivateAccessView
from .models import ModerationConsent, MediaScreeningJob


class SafetyConsentSerializer(serializers.Serializer):
    version = serializers.CharField(max_length=100)
    granted = serializers.BooleanField()


class SafetyConsentView(PrivateAccessView):
    permission_classes = [IsAuthenticated]

    @extend_schema(operation_id='safetyConsentRetrieve', responses=SafetyConsentSerializer)
    def get(self, request):
        granted = ModerationConsent.objects.filter(user=request.user, granted=True,
            version=settings.MODERATION_ACCOUNT_CONSENT_VERSION).exists()
        return Response(dict(version=settings.MODERATION_ACCOUNT_CONSENT_VERSION, granted=granted))

    @extend_schema(operation_id='safetyConsentUpdate', request=SafetyConsentSerializer,
                   responses=SafetyConsentSerializer)
    @transaction.atomic
    def put(self, request):
        data = SafetyConsentSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        if data.validated_data['version'] != settings.MODERATION_ACCOUNT_CONSENT_VERSION:
            raise serializers.ValidationError({'version': 'Please review the current safety disclosure.'})
        # Serialize grant/withdraw operations, including creation of the first row.
        get_user_model().objects.select_for_update().get(pk=request.user.pk)
        ModerationConsent.objects.update_or_create(user=request.user, defaults=data.validated_data)
        if not data.validated_data['granted']:
            # Invalidates leases too: an in-flight provider call cannot be unsent,
            # but its result may no longer publish this post. Regrant never revives it.
            MediaScreeningJob.objects.filter(post__author__user=request.user,
                state__in=['waiting', 'queued', 'processing']).update(
                    state='cancelled', error_code='consent_withdrawn', lease=None, lease_until=None)
        return Response(data.validated_data)
