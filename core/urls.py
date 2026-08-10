from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import RepbaseUserViewSet, SessionViewSet, repbase_users

router = DefaultRouter()
router.register("users", RepbaseUserViewSet)
router.register("sessions", SessionViewSet)

urlpatterns = [
    path("repbase/", repbase_users, name="repbase-users"),
] + router.urls
