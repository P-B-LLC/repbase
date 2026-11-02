from django.db import transaction
from django.db.models import Count
from rest_framework import viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated

from .cookbooks import lock_owner
from .cookbook_serializers import CookbookSerializer
from .models import Cookbook
from .save_recovery import IdempotentCreateMixin


class CookbookViewSet(IdempotentCreateMixin, viewsets.ModelViewSet):
    queryset = Cookbook.objects.all()
    serializer_class = CookbookSerializer
    permission_classes = [IsAuthenticated]

    def owner_profile(self):
        return self.request.user.repbase_profile

    def get_queryset(self):
        return super().get_queryset().filter(owner=self.owner_profile()).annotate(
            recipe_count=Count("recipes")
        ).order_by("name", "pk")

    def perform_create(self, serializer):
        serializer.save(owner=self.owner_profile())

    @transaction.atomic
    def perform_destroy(self, instance):
        lock_owner(self.owner_profile())
        if instance.recipes.exists():
            raise ValidationError({"cookbook": "Move or delete this cookbook's recipes before deleting it."})
        instance.delete()
