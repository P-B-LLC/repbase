from rest_framework import serializers

from .models import RepbaseUser, Session


class RepbaseUserSerializer(serializers.ModelSerializer):
    class Meta:
        model = RepbaseUser
        fields = [
            "id",
            "first_name",
            "last_name",
            "username",
            "email",
            "height_cm",
            "weight_kg",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]


class SessionSerializer(serializers.ModelSerializer):
    duration_seconds = serializers.ReadOnlyField()

    class Meta:
        model = Session
        fields = [
            "id",
            "repbase_user",
            "start_datetime",
            "end_datetime",
            "duration_seconds",
            "created_at",
        ]
        read_only_fields = ["id", "duration_seconds", "created_at"]
