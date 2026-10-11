"""Same-origin browser sessions. Native clients keep their existing token API."""
from django.contrib.auth import get_user_model, login, logout
from django.db import transaction
from django.middleware.csrf import get_token
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.authentication import SessionAuthentication
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from .serializers import LoginSerializer, RegisterSerializer, RepbaseUserSerializer
from .access import capabilities


class BrowserSessionSerializer(serializers.Serializer):
    user = RepbaseUserSerializer(allow_null=True)
    csrf_token = serializers.CharField()


def session_response(request, user=None, status=200):
    from .views import profile_for
    data = RepbaseUserSerializer(profile_for(user), context={'request': request}).data if user else None
    return Response({'user': data, 'csrf_token': get_token(request)}, status=status)


@method_decorator(csrf_protect, name='dispatch')
class BrowserSessionView(APIView):
    authentication_classes = [SessionAuthentication]
    permission_classes = [AllowAny]

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response['Cache-Control'] = 'no-store, private'
        return response

    @extend_schema(responses=BrowserSessionSerializer)
    def get(self, request):
        return session_response(request, request.user if request.user.is_authenticated else None)


class BrowserLoginView(BrowserSessionView):
    http_method_names = ['post', 'options']
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'login'

    @extend_schema(request=LoginSerializer, responses=BrowserSessionSerializer)
    @transaction.atomic
    def post(self, request):
        body = LoginSerializer(data=request.data, context={'request': request})
        body.is_valid(raise_exception=True)
        user = get_user_model().objects.select_for_update().get(pk=body.validated_data['user'].pk)
        if not user.is_active:
            raise AuthenticationFailed()
        login(request._request, user, backend='django.contrib.auth.backends.ModelBackend')
        request.session.set_expiry(30 * 60 if any(capabilities(user).values()) else 12 * 60 * 60)
        return session_response(request, user)


class BrowserRegisterView(BrowserSessionView):
    http_method_names = ['post', 'options']
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'register'

    @extend_schema(request=RegisterSerializer, responses={201: BrowserSessionSerializer})
    def post(self, request):
        body = RegisterSerializer(data=request.data, context={'request': request})
        body.is_valid(raise_exception=True)
        with transaction.atomic():
            profile = body.save()
            login(request._request, profile.user, backend='django.contrib.auth.backends.ModelBackend')
            request.session.set_expiry(12 * 60 * 60)
        return session_response(request, profile.user, status=201)


class BrowserLogoutView(BrowserSessionView):
    http_method_names = ['post', 'options']

    @extend_schema(request=None, responses=BrowserSessionSerializer)
    def post(self, request):
        logout(request._request)
        return session_response(request)
