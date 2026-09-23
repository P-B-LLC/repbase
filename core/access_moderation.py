from django.contrib.auth import get_user_model
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .access import AccessConflict, require
from .access_models import AccessAudit, AccessPolicyLock
from .models import PostReport, CommentReport


def report_model(kind):
    if kind not in ('post', 'comment'):
        raise ValidationError('Choose post or comment reports.')
    return PostReport if kind == 'post' else CommentReport


def open_reports(kind):
    return report_model(kind).objects.filter(reviewed_at__isnull=True).select_related(
        f'{kind}__author__user').order_by('created_at', 'pk')


def report_data(row, kind):
    content = getattr(row, kind)
    from .serializers import feed_image_url_for
    return dict(id=row.pk, kind=kind, author=content.author.user.username,
                content=content.caption if kind == 'post' else content.body,
                image_url=feed_image_url_for(content, None) if kind == 'post' else None,
                reason=row.reason, detail=row.detail, created_at=row.created_at)


@transaction.atomic
def decide_report(actor, kind, report_id, decision, reason):
    AccessPolicyLock.objects.select_for_update().get(pk=1)
    actor = get_user_model().objects.get(pk=actor.pk)
    require(actor, 'moderate')
    if decision not in ('hide', 'no_action') or not reason.strip() or len(reason) > 500:
        raise ValidationError('Choose a decision and provide a reason of up to 500 characters.')
    model = report_model(kind)
    report = get_object_or_404(model.objects.select_for_update(), pk=report_id)
    if report.reviewed_at:
        raise AccessConflict('This report was already reviewed. Reload the queue.')
    content = getattr(report, kind)
    before = dict(is_hidden=content.is_hidden)
    hide = decision == 'hide'
    if hide:
        content.is_hidden = True
        fields = ['is_hidden']
        if kind == 'post':
            content.hidden_at = timezone.now()
            fields.append('hidden_at')
        content.save(update_fields=fields)
    reports = model.objects.filter(**{kind: content}, reviewed_at__isnull=True) if hide else model.objects.filter(pk=report.pk)
    reports.update(reviewed_at=timezone.now(), reviewed_by=actor,
                   resolution=PostReport.Resolution.HIDDEN if hide else PostReport.Resolution.NO_ACTION)
    AccessAudit.objects.create(actor=actor, target=content.author.user,
                               action='moderation_' + decision, subject=f'{kind}:{content.pk}',
                               before=before, after=dict(is_hidden=content.is_hidden), reason=reason)
