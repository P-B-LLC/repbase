"""Authenticated, followers-only calendar invitations and read projections."""

from collections import defaultdict

from django.db.models import Q
from django.utils.cache import patch_vary_headers
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .calendar_sharing import (create_invitations, eligible, grants_payload, make_private,
                               own_entry, people_payload, recipient_profiles, respond_invitation,
                               revoke_invitation, share_queryset, shared_items)
from .calendar_sharing_serializers import (CalendarPersonSerializer, CalendarShareBatchSerializer,
                                           CalendarShareCreateSerializer, CalendarShareConflictSerializer, CalendarSharePrivacySerializer,
                                           CalendarShareSerializer, SharedCalendarItemSerializer,
                                           SharedCalendarMonthSerializer)
from .models import CalendarShare, RepbaseUser
from .throttles import ThrottleByActionMixin


SHARE_ID_PARAMETER = OpenApiParameter("id", OpenApiTypes.INT, OpenApiParameter.PATH, required=True)


class CalendarPrivateResponses:
    permission_classes = [IsAuthenticated]

    def viewer(self):
        profile, _ = RepbaseUser.objects.get_or_create(user=self.request.user)
        return profile

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "private, no-store"
        patch_vary_headers(response, ("Authorization", "Cookie"))
        return response


class CalendarShareViewSet(CalendarPrivateResponses, ThrottleByActionMixin,
                           mixins.ListModelMixin, viewsets.GenericViewSet):
    """Owner management; never reads another account's private planner row."""

    serializer_class = CalendarShareSerializer
    queryset = CalendarShare.objects.none()
    action_throttle_scopes = {"create": "calendar_invite", "privacy": "social", "revoke": "social"}

    def get_queryset(self):
        viewer = self.viewer()
        rows = share_queryset().filter(owner=viewer)
        raw = self.request.query_params.get("source_entry_id")
        if raw is not None:
            try:
                identifier = int(raw)
                if identifier < 1:
                    raise ValueError()
            except (ValueError, TypeError):
                raise ValidationError({"source_entry_id": "Use a positive calendar item id."}) from None
            entry = own_entry(viewer, identifier)
            self._management_source = entry
            targets = Q(source_entry=entry)
            if entry.recurrence_id:
                # An earlier/private occurrence still manages the whole
                # lineage. Payload coverage explicitly distinguishes a grant
                # starting later or excluding the selected private occurrence.
                targets |= Q(series_id=entry.recurrence.series_id)
            rows = rows.filter(targets)
        return rows

    @extend_schema(parameters=[OpenApiParameter("source_entry_id", OpenApiTypes.INT, OpenApiParameter.QUERY)],
                   responses=CalendarShareSerializer(many=True))
    def list(self, request, *args, **kwargs):
        page = self.paginate_queryset(self.get_queryset())
        return self.get_paginated_response(CalendarShareSerializer(grants_payload(
            page, self.viewer(), request, management_source=getattr(self, "_management_source", None),
        ), many=True).data)

    @extend_schema(request=CalendarShareCreateSerializer,
                   responses={201: CalendarShareBatchSerializer, 200: CalendarShareBatchSerializer, 409: CalendarShareConflictSerializer},
                   description="Invite up to 20 current followers. Default scope is only this occurrence. An old declined/revoked invitation requires explicit reinvite=true; retries never silently reactivate it.")
    def create(self, request):
        body = CalendarShareCreateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        rows, changed = create_invitations(self.viewer(), body.validated_data)
        return Response(CalendarShareBatchSerializer({"shares": grants_payload(rows, self.viewer(), request)}).data,
                        status=status.HTTP_201_CREATED if changed else status.HTTP_200_OK)

    @extend_schema(request=None, responses=CalendarShareSerializer, parameters=[SHARE_ID_PARAMETER])
    @action(detail=True, methods=["post"])
    def revoke(self, request, pk=None):
        row = revoke_invitation(self.viewer(), pk)
        return Response(CalendarShareSerializer(grants_payload([row], self.viewer(), request)[0]).data)

    @extend_schema(request=CalendarSharePrivacySerializer, responses={204: None},
                   description="Make only this occurrence private, or stop sharing its entire repeating lineage. Privacy exceptions survive date moves and future-rule edits.")
    @action(detail=False, methods=["post"])
    def privacy(self, request):
        body = CalendarSharePrivacySerializer(data=request.data)
        body.is_valid(raise_exception=True)
        make_private(self.viewer(), body.validated_data)
        return Response(status=status.HTTP_204_NO_CONTENT)


class CalendarInvitationViewSet(CalendarPrivateResponses, ThrottleByActionMixin,
                                mixins.ListModelMixin, viewsets.GenericViewSet):
    """Questions addressed only to the signed-in follower; no social push."""

    serializer_class = CalendarShareSerializer
    queryset = CalendarShare.objects.none()
    action_throttle_scopes = {"accept": "social", "decline": "social"}

    def get_queryset(self):
        return eligible(share_queryset().filter(recipient=self.viewer(), status="pending"))

    def list(self, request, *args, **kwargs):
        page = self.paginate_queryset(self.get_queryset())
        return self.get_paginated_response(CalendarShareSerializer(grants_payload(page, self.viewer(), request), many=True).data)

    @extend_schema(request=None, responses={200: CalendarShareSerializer, 409: CalendarShareConflictSerializer}, parameters=[SHARE_ID_PARAMETER])
    @action(detail=True, methods=["post"])
    def accept(self, request, pk=None):
        row = respond_invitation(self.viewer(), pk, "accepted")
        return Response(CalendarShareSerializer(grants_payload([row], self.viewer(), request)[0]).data)

    @extend_schema(request=None, responses={200: CalendarShareSerializer, 409: CalendarShareConflictSerializer}, parameters=[SHARE_ID_PARAMETER])
    @action(detail=True, methods=["post"])
    def decline(self, request, pk=None):
        row = respond_invitation(self.viewer(), pk, "declined")
        return Response(CalendarShareSerializer(grants_payload([row], self.viewer(), request)[0]).data)


class CalendarShareRecipientViewSet(CalendarPrivateResponses, mixins.ListModelMixin, viewsets.GenericViewSet):
    serializer_class = CalendarPersonSerializer

    def get_queryset(self):
        return recipient_profiles(self.viewer(), self.request.query_params.get("search", ""))

    @extend_schema(parameters=[OpenApiParameter("search", str, OpenApiParameter.QUERY, description="Search your followers, not the people you follow. At most 80 characters are matched.")],
                   responses=CalendarPersonSerializer(many=True))
    def list(self, request, *args, **kwargs):
        page = self.paginate_queryset(self.get_queryset())
        people = people_payload(page, self.viewer(), request)
        return self.get_paginated_response(CalendarPersonSerializer([people[row.pk] for row in page], many=True).data)


class CalendarMonthQuerySerializer(serializers.Serializer):
    start = serializers.DateField()
    end = serializers.DateField()

    def validate(self, attrs):
        if attrs["end"] < attrs["start"] or (attrs["end"] - attrs["start"]).days > 41:
            raise serializers.ValidationError("Choose an ordered calendar window of at most 42 days.")
        return attrs


class CalendarDayQuerySerializer(serializers.Serializer):
    date = serializers.DateField()


def owner_filter(request):
    raw = request.query_params.get("owner_ids")
    if raw is None:
        return None
    if raw == "":
        return []
    parts = raw.split(",")
    if len(raw) > 3000 or len(parts) > 250:
        raise ValidationError({"owner_ids": "Choose at most 250 shared calendar owners."})
    try:
        if any(not item.isascii() or not item.isdecimal() or int(item) < 1 for item in parts):
            raise ValueError()
        return sorted(set(int(item) for item in parts))
    except ValueError:
        raise ValidationError({"owner_ids": "Use comma-separated positive owner ids."}) from None


OWNER_FILTER_PARAMETER = OpenApiParameter("owner_ids", str, OpenApiParameter.QUERY,
    description="Optional comma-separated visible shared owner ids (at most 250). Omit for all accepted calendars; explicit empty value hides all. Applied before counts and the two-item preview.")


class SharedCalendarViewSet(CalendarPrivateResponses, viewsets.GenericViewSet):
    """Read-only shared items, separate from own tasks, counters and reminders."""

    serializer_class = SharedCalendarItemSerializer

    @extend_schema(parameters=[OpenApiParameter("start", OpenApiTypes.DATE, OpenApiParameter.QUERY, required=True),
                               OpenApiParameter("end", OpenApiTypes.DATE, OpenApiParameter.QUERY, required=True), OWNER_FILTER_PARAMETER],
                   responses=SharedCalendarMonthSerializer)
    @action(detail=False, methods=["get"])
    def month(self, request):
        query = CalendarMonthQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        items = shared_items(self.viewer(), request, **query.validated_data, owner_ids=owner_filter(request))
        grouped = defaultdict(list)
        for item in items:
            grouped[item["scheduled_date"]].append(item)
        return Response(SharedCalendarMonthSerializer({"days": [
            {"date": day, "total": len(rows), "items": rows[:2]} for day, rows in sorted(grouped.items())
        ]}).data)

    @extend_schema(parameters=[OpenApiParameter("date", OpenApiTypes.DATE, OpenApiParameter.QUERY, required=True), OWNER_FILTER_PARAMETER],
                   responses=SharedCalendarItemSerializer(many=True))
    @action(detail=False, methods=["get"])
    def day(self, request):
        query = CalendarDayQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        day = query.validated_data["date"]
        items = shared_items(self.viewer(), request, day, day, owner_ids=owner_filter(request))
        page = self.paginate_queryset(items)
        return self.get_paginated_response(SharedCalendarItemSerializer(page, many=True).data)

    @extend_schema(responses=CalendarPersonSerializer(many=True))
    @action(detail=False, methods=["get"])
    def people(self, request):
        viewer = self.viewer()
        owners = eligible(CalendarShare.objects.filter(recipient=viewer, status="accepted")).values("owner_id")
        rows = RepbaseUser.objects.select_related("user").filter(pk__in=owners).order_by("user__username", "pk")
        page = self.paginate_queryset(rows)
        people = people_payload(page, viewer, request)
        return self.get_paginated_response(CalendarPersonSerializer([people[row.pk] for row in page], many=True).data)
