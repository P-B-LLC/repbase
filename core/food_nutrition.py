"""Bounded, decimal-preserving nutrition metadata shared by every food copy.

Amounts here are per the same reference as calories/macros. A missing key is
unknown, never a zero. Canonical units are part of each key, not user labels.
"""
from copy import deepcopy
from decimal import Decimal

from rest_framework import serializers


NUTRIENT_KEYS = (
    "fiber_g", "saturated_fat_g", "sugars_g", "sodium_mg", "potassium_mg",
    "calcium_mg", "iron_mg", "magnesium_mg", "vitamin_c_mg", "vitamin_d_ug",
)
FOOD_METADATA_FIELDS = (
    "source_id", "serving_description", "reference_quantity", "reference_unit", "nutrients",
)


def food_metadata_snapshot(food):
    """Own the JSON snapshot; later edits must not mutate another food."""
    return {field: deepcopy(getattr(food, field)) for field in FOOD_METADATA_FIELDS}


class NutrientMapField(serializers.DictField):
    def __init__(self, **kwargs):
        kwargs.setdefault("required", False)
        kwargs.setdefault("help_text", "Per-reference amounts. Missing keys are unknown; keys carry canonical g/mg/ug units.")
        super().__init__(child=serializers.DecimalField(
            max_digits=14, decimal_places=6, min_value=Decimal("0"),
            max_value=Decimal("99999999"),
        ), **kwargs)

    def to_internal_value(self, value):
        if not isinstance(value, dict) or len(value) > len(NUTRIENT_KEYS):
            self.fail("not_a_dict", input_type=type(value).__name__)
        if set(value) - set(NUTRIENT_KEYS):
            raise serializers.ValidationError("Unsupported nutrient key.")
        values = super().to_internal_value(value)
        # JSONField's encoder must never receive floats or bare Decimal values.
        return {key: format(amount, "f") for key, amount in values.items()}


class FoodMetadataSerializerMixin(serializers.Serializer):
    source_id = serializers.CharField(required=False, allow_blank=True, max_length=40)
    serving_description = serializers.CharField(required=False, allow_blank=True, max_length=160)
    reference_quantity = serializers.DecimalField(
        required=False, allow_null=True, max_digits=12, decimal_places=4,
        min_value=Decimal("0.0001"), max_value=Decimal("100000"),
    )
    # Plain string rather than response enum: older clients remain decodable.
    reference_unit = serializers.CharField(required=False, allow_blank=True, max_length=2)
    nutrients = NutrientMapField()
    optional_response_fields = FOOD_METADATA_FIELDS

    def to_representation(self, instance):
        result = super().to_representation(instance)
        servings = result.get("servings")
        if isinstance(servings, str) and "." in servings:
            whole, fraction = servings.split(".", 1)
            # Keep existing clients' familiar 1.00/1.50 strings, but retain
            # the extra digits required for a weighed portion such as 73/7.
            result["servings"] = whole + "." + fraction.rstrip("0").ljust(2, "0")
        return result

    def validate_source_id(self, value):
        if value:
            from .food_sources import barcode
            if not value.startswith("off:"):
                raise serializers.ValidationError("Use a valid Open Food Facts product identifier, or leave it blank.")
            try:
                canonical = "off:" + barcode(value[4:])
            except ValueError:
                raise serializers.ValidationError("Use a valid Open Food Facts product identifier.") from None
            if value != canonical:
                raise serializers.ValidationError("Use the canonical Open Food Facts product identifier.")
        return value

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if self.instance is not None and not any(field in attrs for field in FOOD_METADATA_FIELDS):
            macro_fields = ("calories", "protein_grams", "carbohydrate_grams", "fat_grams")
            if any(field in attrs and attrs[field] != getattr(self.instance, field) for field in macro_fields):
                # An older client cannot maintain new metadata when it edits
                # the reference macros. Keep the edited macros, not stale
                # micronutrients tied to an earlier food/basis.
                attrs.update(source_id="", serving_description="", reference_quantity=None, reference_unit="", nutrients={})
        if attrs.get("reference_unit") == "" and "reference_quantity" not in attrs:
            attrs["reference_quantity"] = None
        if "reference_quantity" in attrs and attrs["reference_quantity"] is None:
            attrs.setdefault("reference_unit", "")
        quantity = attrs.get("reference_quantity", getattr(self.instance, "reference_quantity", None))
        unit = attrs.get("reference_unit", getattr(self.instance, "reference_unit", ""))
        if (quantity is None and unit) or (quantity is not None and unit not in {"g", "ml"}):
            raise serializers.ValidationError({"reference_unit": "A known reference quantity needs g or ml; unknown quantity needs a blank unit."})
        return attrs
