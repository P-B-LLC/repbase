"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.conf import settings
from django.contrib import admin
from django.urls import include, path, re_path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from core.media import serve_media
from core.analytics import dashboard, InsightsLoginView, InsightsLogoutView
from config.health import live, ready
from core.views import health, home, repbase_users

urlpatterns = [
    path('insights/', dashboard, name='insights'),
    path('insights/login/', InsightsLoginView.as_view(), name='insights-login'),
    path('insights/logout/', InsightsLogoutView.as_view(), name='insights-logout'),
    path('health/live/', live, name='health-live'),
    path('health/ready/', ready, name='health-ready'),
    path('', home, name='home'),
    path('health/', health, name='health'),
    path('admin/', admin.site.urls),
    path('api-auth/', include('rest_framework.urls')),
    path('api/repbase/', repbase_users, name='repbase-users'),
    path('api/v1/', include('core.urls')),
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path(
        'api/docs/',
        SpectacularSwaggerView.as_view(url_name='schema'),
        name='swagger-ui',
    ),
    # Not behind `if settings.DEBUG`, which is where this used to live and why
    # every photo in the app answered 404 the moment DEBUG was turned off --
    # which production requires. Development and production now take the same
    # path through the same signature check, so the thing that is tested is
    # the thing that runs.
    re_path(
        r'^%s(?P<path>.*)$' % settings.MEDIA_URL.lstrip('/'),
        serve_media,
        name='media',
    ),
]
