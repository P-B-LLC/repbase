from django.contrib import admin

from .models import RepbaseUser


@admin.register(RepbaseUser)
class RepbaseUserAdmin(admin.ModelAdmin):
    list_display = (
        "username",
        "first_name",
        "last_name",
        "email",
        "height_cm",
        "weight_kg",
        "created_at",
    )
    search_fields = ("username", "first_name", "last_name", "email")
    ordering = ("-created_at",)
