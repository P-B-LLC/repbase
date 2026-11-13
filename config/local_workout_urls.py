"""Only loaded by local_workout_testing; no hosted URL or policy changes."""
import ipaddress

from django.http import HttpResponseForbidden, JsonResponse
from django.urls import path

from .urls import urlpatterns as application_urls


class LoopbackOnlyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            local = ipaddress.ip_address(request.META.get("REMOTE_ADDR", "")).is_loopback
        except ValueError:
            local = False
        if not local:
            return HttpResponseForbidden("This testing workspace is available on this computer only.")
        return self.get_response(request)


def identity(request):
    from core.models import WorkoutExercise
    response = JsonResponse({
        "environment": "rytivo-local-workout-testing",
        "per_set_targets": any(field.name == "planned_sets" for field in WorkoutExercise._meta.fields),
    })
    response["Cache-Control"] = "no-store"
    return response


urlpatterns = [path("__local_workout_testing__/", identity), *application_urls]
