"""Cookbook invariants shared by recipe edits and Social saves.

All writers lock the owner first, then collections. Locks are per account and
cover short SQL work only, so simultaneous saves cannot exceed the 20 limit.
"""
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import Cookbook, RepbaseUser, SavedFoodMeal

MAX_COOKBOOK_RECIPES = 20


def lock_owner(owner):
    return RepbaseUser.objects.select_for_update().get(pk=owner.pk)


def destination_for(owner, cookbook_id, *, excluding=None):
    cookbook = Cookbook.objects.select_for_update().filter(owner=owner, pk=cookbook_id).first()
    if cookbook is None:
        raise ValidationError({"cookbook": "Choose one of your cookbooks."})
    recipes = SavedFoodMeal.objects.filter(cookbook=cookbook)
    if excluding is not None:
        recipes = recipes.exclude(pk=excluding)
    if recipes.count() >= MAX_COOKBOOK_RECIPES:
        raise ValidationError({"cookbook": "This cookbook is full. Choose another cookbook or create one (20 recipes maximum)."})
    return cookbook


def touch(cookbook_ids):
    Cookbook.objects.filter(pk__in=set(cookbook_ids)).update(updated_at=timezone.now())


def available_name(model, owner, title, *, maximum=150):
    base = title.strip()[:maximum] or "Recipe"
    # One owner-scoped query, not one query per collision.
    taken = set(model.objects.filter(owner=owner).values_list("name", flat=True))
    keys = {name.casefold() for name in taken}
    candidate = base
    suffix = 2
    while candidate.casefold() in keys:
        tail = f" {suffix}"
        candidate = base[:maximum - len(tail)] + tail
        suffix += 1
    return candidate
