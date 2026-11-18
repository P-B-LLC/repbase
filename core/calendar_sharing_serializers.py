"""Small calendar wire shapes: private notes and training logs have no field."""

from rest_framework import serializers


class CalendarPersonSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    username = serializers.CharField()
    display_name = serializers.CharField(allow_blank=True)
    profile_photo_url = serializers.CharField(allow_null=True)


class SharedCalendarItemSerializer(serializers.Serializer):
    id = serializers.CharField()
    share_id = serializers.IntegerField()
    owner = CalendarPersonSerializer()
    kind = serializers.CharField()
    title = serializers.CharField()
    category = serializers.CharField()
    color = serializers.CharField(allow_blank=True)
    scheduling_timezone = serializers.CharField()
    scheduled_date = serializers.DateField()
    scheduled_time = serializers.TimeField(allow_null=True)
    duration_minutes = serializers.IntegerField(allow_null=True)
    is_recurring = serializers.BooleanField()
    is_complete = serializers.BooleanField()


class CalendarShareSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    owner = CalendarPersonSerializer()
    recipient = CalendarPersonSerializer()
    scope = serializers.CharField()
    status = serializers.CharField()
    starts_on = serializers.DateField(allow_null=True)
    created_at = serializers.DateTimeField()
    responded_at = serializers.DateTimeField(allow_null=True)
    item = SharedCalendarItemSerializer(allow_null=True)
    applies_to_selected_occurrence = serializers.BooleanField(required=False,
        help_text="Owner management only: whether an active or pending grant covers the selected occurrence. False for a later series boundary or a private exception; acceptance is still required for recipient visibility.")


class CalendarShareConflictSerializer(serializers.Serializer):
    code = serializers.CharField(help_text="reinvite_required, scope_conflict, or state_conflict. Plain string for forward compatibility.")
    detail = serializers.CharField()


class CalendarShareCreateSerializer(serializers.Serializer):
    source_entry_id = serializers.IntegerField(min_value=1)
    recipient_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), min_length=1, max_length=20)
    scope = serializers.ChoiceField(choices=("occurrence", "series"), default="occurrence")
    reinvite = serializers.BooleanField(default=False)

    def validate_recipient_ids(self, value):
        if len(set(value)) != len(value):
            raise serializers.ValidationError("Choose each follower only once.")
        return value


class CalendarShareBatchSerializer(serializers.Serializer):
    shares = CalendarShareSerializer(many=True)


class CalendarSharePrivacySerializer(serializers.Serializer):
    source_entry_id = serializers.IntegerField(min_value=1)
    scope = serializers.ChoiceField(choices=("occurrence", "series"), default="occurrence")


class SharedCalendarDaySummarySerializer(serializers.Serializer):
    date = serializers.DateField()
    total = serializers.IntegerField(min_value=0)
    items = SharedCalendarItemSerializer(many=True, max_length=2)


class SharedCalendarMonthSerializer(serializers.Serializer):
    days = SharedCalendarDaySummarySerializer(many=True)
