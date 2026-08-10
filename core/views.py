from django.http import JsonResponse
from django.shortcuts import render
from rest_framework.authentication import BasicAuthentication, SessionAuthentication
from rest_framework import viewsets

from .models import RepbaseUser, Session
from .permissions import IsSuperUserOrReadOnly
from .serializers import RepbaseUserSerializer, SessionSerializer


def home(request):
    return JsonResponse(
        {
            "message": "Welcome to the Repbase API",
            "users": "/api/users/",
            "sessions": "/api/sessions/",
            "repbase": "/api/repbase/",
            "admin": "/admin/",
        }
    )


def repbase_users(request):
    users = RepbaseUser.objects.all().order_by("-created_at")
    return render(request, "core/repbase_users.html", {"users": users})


class RepbaseUserViewSet(viewsets.ModelViewSet):
    queryset = RepbaseUser.objects.all().order_by("-created_at")
    serializer_class = RepbaseUserSerializer
    authentication_classes = [SessionAuthentication, BasicAuthentication]
    permission_classes = [IsSuperUserOrReadOnly]


class SessionViewSet(viewsets.ModelViewSet):
    queryset = Session.objects.select_related("repbase_user").order_by("-created_at")
    serializer_class = SessionSerializer
    authentication_classes = [SessionAuthentication, BasicAuthentication]
    permission_classes = [IsSuperUserOrReadOnly]
