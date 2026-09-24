import uuid

from django.db import transaction
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.authentication import TokenAuthentication
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import PushDevice, RepbaseUser


class PushDeviceRegistrationSerializer(serializers.Serializer):
    token = serializers.RegexField(r"\A[0-9a-fA-F]{32,512}\Z", min_length=32, max_length=512, write_only=True)
    environment = serializers.ChoiceField(choices=["sandbox", "production"])
    community_enabled = serializers.BooleanField()

    def validate_token(self, value):
        if len(value) % 2:
            raise serializers.ValidationError("Invalid device token.")
        return value.lower()


class PushDeviceRegistrationView(APIView):
    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(operation_id="registerPushDevice", request=PushDeviceRegistrationSerializer, responses={204: None})
    @transaction.atomic
    def post(self, request):
        serializer = PushDeviceRegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        # Serialize with logout/revocation, not just other device registrations.
        if not Token.objects.select_for_update().filter(pk=request.auth.pk).exists():
            raise AuthenticationFailed()
        recipient = RepbaseUser.objects.get(user=request.user)
        device, created = PushDevice.objects.select_for_update().get_or_create(
            token=data["token"], environment=data["environment"],
            defaults={"recipient": recipient, "auth_token": request.auth},
        )
        if created or device.recipient_id != recipient.pk or device.auth_token_id != request.auth.pk or device.community_enabled != data["community_enabled"]:
            device.generation = uuid.uuid4()
        device.recipient = recipient
        device.auth_token = request.auth
        device.community_enabled = data["community_enabled"]
        device.registered_at = timezone.now()
        device.save()
        return Response(status=204)
