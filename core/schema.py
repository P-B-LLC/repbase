"""Narrow schema adjustments for backward-compatible client rollout."""

from drf_spectacular.extensions import OpenApiSerializerExtension


class NotificationCompatibilitySchema(OpenApiSerializerExtension):
    target_class = "core.serializers.NotificationSerializer"

    def map_serializer(self, auto_schema, direction):
        schema = auto_schema._map_serializer(self.target, direction, bypass_extensions=True)
        if direction == "response":
            # These read-only additions are emitted by upgraded servers, but
            # clients must still decode the existing beta's list response.
            # drf-spectacular otherwise requires all read-only fields, even
            # when the serializer declares required=False. Do not relax every
            # other serializer/field just to support this staged rollout.
            optional = {"post_preview", "supports_scoped_read"}
            schema["required"] = [name for name in schema.get("required", []) if name not in optional]
        return schema
