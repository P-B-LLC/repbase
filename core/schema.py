"""The schema generator, with one thing drf-spectacular cannot be told.

drf-spectacular lists every read-only field of a response in its component's
`required`, whatever the field's default -- `SavedWorkoutResult.already_saved`
has `default=False` and is required all the same. For most fields that is
right, and it is what the generated clients rely on: the Swift client turns a
required property into a non-optional one and refuses a response without it.

It is wrong for a field being *added* to a response. A required property is a
promise the old server cannot keep: a new app talking to a backend deployed
before the field existed would fail to decode every response that lacks it,
which is a whole screen failing for want of one flag. The only safe way to
add a response field while both versions are in the wild is to document it as
optional, so the generated property is optional too and absent decodes as
nil.

So a serializer names those fields in `optional_response_fields`, beside the
fields themselves, and they are left out of the response component's
`required` here. Nothing else about the field changes: the server still always
sends it, and request components are untouched (a read-only field has no
place in those anyway). A name that is not one of the serializer's fields is
an error rather than a silent no-op, so a rename cannot quietly put a field
back into `required` and break the old-server case again.

NotificationCompatibilitySchema below does the same for the notification
list's preview fields, registered when core.apps imports this module.
"""

from drf_spectacular.extensions import OpenApiSerializerExtension
from drf_spectacular.openapi import AutoSchema as SpectacularAutoSchema
from drf_spectacular.plumbing import force_instance


class AutoSchema(SpectacularAutoSchema):
    def _map_basic_serializer(self, serializer, direction):
        schema = super()._map_basic_serializer(serializer, direction)
        optional = getattr(serializer, "optional_response_fields", ())
        if not optional or direction != "response":
            return schema

        unknown = set(optional) - set(force_instance(serializer).fields)
        if unknown:
            raise ValueError(
                f"{type(force_instance(serializer)).__name__}.optional_response_fields "
                f"names fields it does not have: {sorted(unknown)}"
            )

        required = [name for name in schema.get("required", []) if name not in optional]
        if required:
            schema["required"] = required
        else:
            schema.pop("required", None)
        return schema


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
