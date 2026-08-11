from rest_framework.permissions import SAFE_METHODS, BasePermission


class IsCustomExerciseOwnerOrAdmin(BasePermission):
    message = "Only the creator of a custom exercise can modify it."

    def has_object_permission(self, request, view, obj):
        if request.method in SAFE_METHODS:
            return True
        if request.user.is_superuser:
            return True
        return obj.created_by_id == request.user.repbase_profile.id
