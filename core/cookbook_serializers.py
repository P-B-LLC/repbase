from django.db import transaction
from django.db.models import Count
from rest_framework import serializers

from .cookbooks import MAX_COOKBOOK_RECIPES, lock_owner
from .models import Cookbook, PostCookbook
from .params import MAX_ID


class CookbookSerializer(serializers.ModelSerializer):
    recipe_count = serializers.IntegerField(read_only=True)
    capacity = serializers.SerializerMethodField()

    class Meta:
        model = Cookbook
        fields = ["id", "name", "recipe_count", "capacity", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]
        validators = []

    def get_capacity(self, cookbook) -> int:
        return MAX_COOKBOOK_RECIPES

    def validate_name(self, name):
        name = name.strip()
        if not name:
            raise serializers.ValidationError("Name your cookbook.")
        return name

    def _validate_name_locked(self, owner, name):
        matches = Cookbook.objects.filter(owner=owner, name__iexact=name)
        if self.instance:
            matches = matches.exclude(pk=self.instance.pk)
        if matches.exists():
            raise serializers.ValidationError({"name": "You already have a cookbook with this name."})

    @transaction.atomic
    def create(self, validated_data):
        owner = lock_owner(validated_data["owner"])
        self._validate_name_locked(owner, validated_data["name"])
        cookbook = Cookbook.objects.create(**validated_data)
        cookbook.recipe_count = 0
        return cookbook

    @transaction.atomic
    def update(self, instance, validated_data):
        lock_owner(instance.owner)
        instance = Cookbook.objects.annotate(recipe_count=Count("recipes")).get(pk=instance.pk)
        self._validate_name_locked(instance.owner, validated_data.get("name", instance.name))
        instance.name = validated_data.get("name", instance.name)
        instance.save(update_fields=["name", "updated_at"])
        return instance


class CookbookDestinationSerializer(serializers.Serializer):
    cookbook = serializers.IntegerField(min_value=1, max_value=MAX_ID)


class PostCookbookSerializer(serializers.ModelSerializer):
    class Meta:
        model = PostCookbook
        fields = ["name", "recipe_count"]
        read_only_fields = fields


class CookbookIngredientSerializer(serializers.Serializer):
    name = serializers.CharField()
    servings = serializers.DecimalField(max_digits=6, decimal_places=2)
    calories = serializers.DecimalField(max_digits=8, decimal_places=2)
    protein_grams = serializers.DecimalField(max_digits=8, decimal_places=2)
    carbohydrate_grams = serializers.DecimalField(max_digits=8, decimal_places=2)
    fat_grams = serializers.DecimalField(max_digits=8, decimal_places=2)


class CookbookRecipeSerializer(serializers.Serializer):
    name = serializers.CharField()
    cooking_instructions = serializers.CharField(allow_blank=True)
    ingredients = CookbookIngredientSerializer(many=True)


class PublishedCookbookSerializer(serializers.ModelSerializer):
    recipes = CookbookRecipeSerializer(many=True, read_only=True)

    class Meta:
        model = PostCookbook
        fields = ["name", "recipe_count", "recipes"]
        read_only_fields = fields
