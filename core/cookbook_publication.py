"""Bounded immutable cookbook snapshots; no recipe expansion in feed pages."""
from django.db import transaction
from rest_framework.exceptions import ValidationError

from .cookbooks import MAX_COOKBOOK_RECIPES, available_name, lock_owner
from .cookbook_serializers import CookbookRecipeSerializer
from .models import Cookbook, PostCookbook, SavedFoodIngredient, SavedFoodMeal


def recipe_snapshot(cookbook):
    recipes = list(cookbook.recipes.all())
    if not 1 <= len(recipes) <= MAX_COOKBOOK_RECIPES:
        raise ValidationError({"source_id": "A cookbook needs 1 to 20 recipes before it can be posted."})
    rows = []
    for recipe in recipes:
        ingredients = list(recipe.ingredients.all())
        if not ingredients:
            raise ValidationError({"source_id": "Every recipe needs at least one ingredient."})
        rows.append({"name": recipe.name, "cooking_instructions": recipe.cooking_instructions,
                     "ingredients": [{field: str(getattr(item, field)) for field in
                        ("name", "servings", "calories", "protein_grams", "carbohydrate_grams", "fat_grams")}
                                     for item in ingredients]})
    return list(CookbookRecipeSerializer(rows, many=True).data)


@transaction.atomic
def save_publication(owner, source):
    lock_owner(owner)
    existing = Cookbook.objects.filter(owner=owner, source_post=source).first()
    if existing:
        return existing, False
    snapshot = source.cookbook
    cookbook = Cookbook.objects.create(owner=owner, source_post=source,
        name=available_name(Cookbook, owner, snapshot.name, maximum=80))
    # A fresh cookbook can preserve the published names even when another
    # collection contains recipes with the same names.
    taken = set()
    recipes = []
    for item in snapshot.recipes:
        base = item["name"][:150]
        name, suffix = base, 2
        while name.casefold() in taken:
            tail = f" {suffix}"
            name = base[:150-len(tail)] + tail
            suffix += 1
        taken.add(name.casefold())
        recipes.append(SavedFoodMeal(owner=owner, cookbook=cookbook, name=name,
                                     cooking_instructions=item["cooking_instructions"]))
    SavedFoodMeal.objects.bulk_create(recipes)
    SavedFoodIngredient.objects.bulk_create([
        SavedFoodIngredient(saved_meal=recipe, position=index+1, **ingredient)
        for recipe, item in zip(recipes, snapshot.recipes)
        for index, ingredient in enumerate(item["ingredients"])
    ])
    return cookbook, True
