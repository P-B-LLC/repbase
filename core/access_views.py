from django.db.models import Q
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema, OpenApiParameter
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from .access import ROLES, ALL_ROLES, assign_roles, capabilities, require, role_state, can_edit_access
from .access_moderation import decide_report, open_reports, report_data
from .models import RepbaseUser


class CapabilitiesSerializer(serializers.Serializer):
    manage_roles = serializers.BooleanField()
    manage_owners = serializers.BooleanField()
    view_analytics = serializers.BooleanField()
    moderate = serializers.BooleanField()


class AccountAccessSerializer(serializers.Serializer):
    roles = serializers.ListField(child=serializers.ChoiceField(choices=ALL_ROLES))
    version = serializers.IntegerField(min_value=0)


class MyAccessSerializer(AccountAccessSerializer):
    capabilities = CapabilitiesSerializer()


class ChangeAccessSerializer(AccountAccessSerializer):
    # The protected top-level role is never assignable over an HTTP endpoint.
    roles = serializers.ListField(child=serializers.ChoiceField(choices=ROLES))
    reason = serializers.CharField(max_length=500, allow_blank=True, required=False, default='')

    def validate_roles(self, value):
        if len(set(value)) != len(value):
            raise ValidationError('Each role may only be selected once.')
        return value


class AccessUserSerializer(AccountAccessSerializer):
    id = serializers.IntegerField(help_text='Public profile ID, as used by /users/.')
    username = serializers.CharField()
    display_name = serializers.CharField()
    editable = serializers.BooleanField()


class AccessUserPageSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    next = serializers.URLField(allow_null=True)
    previous = serializers.URLField(allow_null=True)
    results = AccessUserSerializer(many=True)


def user_data(profile, actor):
    user = profile.user
    return dict(id=profile.pk, username=user.username,
                display_name=user.get_full_name() or user.username,
                editable=can_edit_access(actor, user),
                **role_state(user))


class PrivateAccessView(APIView):
    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response['Cache-Control'] = 'no-store, private'
        return response


class MyAccessView(PrivateAccessView):
    @extend_schema(operation_id='my_access_retrieve', responses=MyAccessSerializer)
    def get(self, request):
        from .analytics import allowed
        grants = capabilities(request.user)
        # Include pre-existing operator-approved analytics access.
        grants['view_analytics'] = allowed(request.user)
        return Response(dict(**role_state(request.user), capabilities=grants))


class AccessUsersView(PrivateAccessView):
    @extend_schema(operation_id='access_users_list', responses=AccessUserPageSerializer,
                   parameters=[OpenApiParameter('search', str, required=True,
                                                description='Username or name, at least 2 characters.'),
                               OpenApiParameter('page', int)])
    def get(self, request):
        require(request.user, 'manage_roles')
        search = request.query_params.get('search', '').strip()
        if not 2 <= len(search) <= 100:
            raise ValidationError('Enter between 2 and 100 characters to find an account.')
        users = RepbaseUser.objects.select_related('user').filter(user__is_active=True).filter(
            Q(user__username__icontains=search) | Q(user__first_name__icontains=search)
            | Q(user__last_name__icontains=search)).order_by('user__username', 'pk')
        paginator = PageNumberPagination()
        paginator.page_size = 20
        page = paginator.paginate_queryset(users, request)
        return paginator.get_paginated_response([user_data(profile, request.user) for profile in page])


class UserAccessView(PrivateAccessView):
    @extend_schema(operation_id='user_access_retrieve', responses=AccessUserSerializer)
    def get(self, request, profile_id: int):
        require(request.user, 'manage_roles')
        return Response(user_data(get_object_or_404(RepbaseUser, pk=profile_id), request.user))

    @extend_schema(operation_id='user_access_update', request=ChangeAccessSerializer,
                   responses=AccessUserSerializer)
    def put(self, request, profile_id: int):
        require(request.user, 'manage_roles')
        body = ChangeAccessSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        profile = get_object_or_404(RepbaseUser.objects.select_related('user'), pk=profile_id)
        assign_roles(request.user, profile.user, **body.validated_data)
        return Response(user_data(profile, request.user))


class ModerationReportSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    kind = serializers.ChoiceField(choices=['post', 'comment'])
    author = serializers.CharField()
    content = serializers.CharField()
    image_url = serializers.CharField(allow_null=True)
    reason = serializers.CharField()
    detail = serializers.CharField()
    created_at = serializers.DateTimeField()


class ReportPageSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    next = serializers.URLField(allow_null=True)
    previous = serializers.URLField(allow_null=True)
    results = ModerationReportSerializer(many=True)


class ModerationDecisionSerializer(serializers.Serializer):
    decision = serializers.ChoiceField(choices=['hide', 'no_action'])
    reason = serializers.CharField(max_length=500, allow_blank=False)


class DecisionResultSerializer(serializers.Serializer):
    resolved = serializers.BooleanField()


class ModerationReportsView(PrivateAccessView):
    @extend_schema(operation_id='moderation_reports_list', responses=ReportPageSerializer,
                   parameters=[OpenApiParameter('kind', str, enum=['post', 'comment'], required=True),
                               OpenApiParameter('page', int)])
    def get(self, request):
        require(request.user, 'moderate')
        kind = request.query_params.get('kind')
        reports = open_reports(kind)
        paginator = PageNumberPagination()
        paginator.page_size = 20
        page = paginator.paginate_queryset(reports, request)
        return paginator.get_paginated_response([report_data(row, kind) for row in page])


class ModerationDecisionView(PrivateAccessView):
    @extend_schema(operation_id='moderation_decision_create', request=ModerationDecisionSerializer,
                   responses=DecisionResultSerializer)
    def post(self, request, kind: str, report_id: int):
        require(request.user, 'moderate')
        body = ModerationDecisionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        decide_report(request.user, kind, report_id, **body.validated_data)
        return Response(dict(resolved=True))
