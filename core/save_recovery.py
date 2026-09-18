import hashlib
import json
import uuid

from django.db import transaction
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.exceptions import ValidationError
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response

from .models import RepbaseUser, SaveReceipt


class IdempotentCreateMixin:
    @extend_schema(parameters=[OpenApiParameter(
        name="Idempotency-Key", type=OpenApiTypes.UUID, location=OpenApiParameter.HEADER,
        required=False, description=(
            "Persist a fresh UUID per intended create and reuse it with the same JSON "
            "body on retry. Successful responses are replayed without a second create. "
            "Reusing the key with changed input returns 409. Keys are scoped to the "
            "authenticated account and endpoint and retained until account deletion."
        ),
    )])
    def create(self, request, *args, **kwargs):
        raw_key = request.headers.get("Idempotency-Key")
        if not raw_key:
            return super().create(request, *args, **kwargs)
        try:
            key = uuid.UUID(raw_key)
        except (ValueError, AttributeError):
            raise ValidationError({"Idempotency-Key": "Use a UUID for this save."})
        # Compare the media type, not the raw header. DRF hands back
        # CONTENT_TYPE verbatim -- parameters and all -- and a JSON body is
        # entitled to carry one. swift-openapi-generator sends
        # "application/json; charset=utf-8", so every save from the iOS app
        # arrived with a charset and was refused by this equality: planner
        # entries, workouts and food alike, because those are exactly the
        # requests that carry an Idempotency-Key. The 400 said the body was
        # not JSON when it was, and the app, having no better word for it,
        # showed "please try again" -- advice that could never work.
        media_type = request.content_type.split(";")[0].strip().lower()
        if media_type != "application/json":
            raise ValidationError("Idempotent saves require a JSON request body.")
        # Key order must not alter the meaning of a retried JSON object.
        canonical = json.dumps(json.loads(JSONRenderer().render(request.data)), sort_keys=True, separators=(",", ":"))
        fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
        with transaction.atomic():
            # All receipt writers for an account take this lock, including the
            # first request (where there is no receipt row to lock yet).
            owner = RepbaseUser.objects.select_for_update().get(pk=self.owner_profile().pk)
            receipt = SaveReceipt.objects.filter(owner=owner, path=request.path, key=key).first()
            if receipt:
                if receipt.request_hash != fingerprint:
                    return Response({"detail": "This save key was already used with different values."}, status=409)
                return Response(receipt.response, status=receipt.status_code)
            response = super().create(request, *args, **kwargs)
            if 200 <= response.status_code < 300:
                SaveReceipt.objects.create(
                    owner=owner, path=request.path, key=key, request_hash=fingerprint,
                    response=json.loads(JSONRenderer().render(response.data)), status_code=response.status_code,
                )
            return response
