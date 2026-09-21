"""Cookie-authenticated workspace for the in-app browser; never uses URL tokens."""
from django import forms
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.views import LogoutView
from django.core.exceptions import ValidationError as FormValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods
from rest_framework.exceptions import APIException

from .access import assign_roles, capabilities, role_state
from .access_views import user_data
from .access_moderation import decide_report, open_reports, report_data
from .analytics import InsightsLoginView, allowed
from .models import RepbaseUser


def portal_allowed(user):
    return any(capabilities(user).values()) or allowed(user)


class AccessLoginForm(AuthenticationForm):
    def confirm_login_allowed(self, user):
        if not portal_allowed(user):
            raise FormValidationError('This account is not authorized for administration.')


class AccessLoginView(InsightsLoginView):
    authentication_form = AccessLoginForm
    template_name = 'access/login.html'
    next_page = '/access/'


class AccessLogoutView(LogoutView):
    next_page = '/access/login/'


class RoleForm(forms.Form):
    roles = forms.MultipleChoiceField(required=False, widget=forms.CheckboxSelectMultiple,
        choices=[('owner', 'Owner — assign roles, analytics, and moderation'),
                 ('analytics', 'Analytics — private aggregate dashboard only'),
                 ('moderator', 'Moderator — review and hide reported content')])
    version = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    reason = forms.CharField(required=False, max_length=500, label='Reason (optional)')


@never_cache
@login_required(login_url='/access/login/')
@require_http_methods(['GET'])
def workspace(request):
    if not portal_allowed(request.user):
        return HttpResponseForbidden('Access denied. Ask an Owner to grant the role you need.')
    grants = capabilities(request.user)
    grants['view_analytics'] = allowed(request.user)
    search = request.GET.get('search', '').strip()
    page = None
    error = None
    if grants['manage_roles'] and search:
        if not 2 <= len(search) <= 100:
            error = 'Enter between 2 and 100 characters.'
        else:
            users = RepbaseUser.objects.select_related('user').filter(user__is_active=True).filter(
                Q(user__username__icontains=search) | Q(user__first_name__icontains=search)
                | Q(user__last_name__icontains=search)).order_by('user__username', 'pk')
            page = Paginator(users, 20).get_page(request.GET.get('page'))
            page.object_list = [user_data(profile, request.user) for profile in page.object_list]
    return render(request, 'access/workspace.html', dict(grants=grants, search=search, page=page, error=error))


@never_cache
@login_required(login_url='/access/login/')
@require_http_methods(['GET', 'POST'])
def edit_access(request, profile_id):
    if not capabilities(request.user)['manage_roles']:
        return HttpResponseForbidden('Access denied.')
    profile = get_object_or_404(RepbaseUser.objects.select_related('user'), pk=profile_id)
    account = user_data(profile, request.user)
    form = RoleForm(request.POST if request.method == 'POST' else None, initial=role_state(profile.user))
    status = 200
    if request.method == 'POST' and form.is_valid():
        try:
            assign_roles(request.user, profile.user, **form.cleaned_data)
        except APIException as error:
            form.add_error(None, str(error.detail))
            status = error.status_code
        else:
            return redirect('access-edit', profile_id=profile_id)
    return render(request, 'access/edit.html', dict(account=account, form=form), status=status)


@never_cache
@login_required(login_url='/access/login/')
@require_http_methods(['GET', 'POST'])
def reports(request):
    if not capabilities(request.user)['moderate']:
        return HttpResponseForbidden('Access denied.')
    kind = request.GET.get('kind', 'post')
    error = None
    status = 200
    try:
        if request.method == 'POST':
            try:
                report_id = int(request.POST.get('report_id', ''))
            except ValueError:
                raise FormValidationError('Choose a report to review.')
            decide_report(request.user, kind, report_id, request.POST.get('decision'), request.POST.get('reason', ''))
            return redirect('/access/reports/?kind=' + kind)
        rows = open_reports(kind)
    except (APIException, FormValidationError) as caught:
        error = str(getattr(caught, 'detail', caught))
        status = getattr(caught, 'status_code', 400)
        kind = kind if kind in ('post', 'comment') else 'post'
        rows = open_reports(kind)
    page = Paginator(rows, 20).get_page(request.GET.get('page'))
    page.object_list = [report_data(row, kind) for row in page.object_list]
    return render(request, 'access/reports.html', dict(page=page, kind=kind, error=error), status=status)
