from django import forms
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods
from rest_framework.exceptions import APIException
from .access import capabilities
from .access_models import AccountAccess, CommunitySpotlight
from .community_tools import set_flair, set_spotlight
from .models import RepbaseUser
from .moderation import CONSENT_HEADER


class PublicationForm(forms.Form):
    version = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    reason = forms.CharField(max_length=500)
    consent = forms.BooleanField(label='I agree to send this public text to OpenAI for automated safety review before publication. Do not enter private information.')
    consent_version = forms.ChoiceField(widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['consent_version'].choices = [(settings.MODERATION_CONSENT_VERSION, settings.MODERATION_CONSENT_VERSION)]
        self.fields['consent_version'].initial = settings.MODERATION_CONSENT_VERSION


class SpotlightForm(PublicationForm):
    announcement = forms.CharField(max_length=500, required=False, widget=forms.Textarea,
        help_text='Leave blank to remove the community announcement.')
    featured_post_id = forms.IntegerField(required=False, min_value=1,
        help_text='A public original post ID. Leave blank to remove the featured post.')


class FlairForm(PublicationForm):
    label = forms.CharField(max_length=32, required=False, label='Custom role label (cosmetic only)')
    badge = forms.CharField(max_length=32, required=False, label='Profile badge')
    color = forms.RegexField(r'^#[0-9a-fA-F]{6}$', max_length=7, label='Accent color, e.g. #F65A18')


@never_cache
@login_required(login_url='/access/login/')
@require_http_methods(['GET', 'POST'])
def community_studio(request, profile_id=None):
    if not capabilities(request.user)['manage_roles']:
        return HttpResponseForbidden('Community tools are available to Owners only.')
    profile = None
    if profile_id is not None:
        profile = get_object_or_404(RepbaseUser.objects.select_related('user'), pk=profile_id)
        row = AccountAccess.objects.filter(user=profile.user).first()
        initial = dict(version=row.version if row else 0, label=row.public_label if row else '',
            badge=row.public_badge if row else '', color=row.label_color if row else '#F65A18')
        form_type = FlairForm
    else:
        row = CommunitySpotlight.objects.filter(pk=1).first()
        initial = dict(version=row.version if row else 0, announcement=row.announcement if row else '',
                       featured_post_id=row.featured_post_id if row else None)
        form_type = SpotlightForm
    form = form_type(request.POST if request.method == 'POST' else None, initial=initial)
    status = 200
    if request.method == 'POST' and form.is_valid():
        data = dict(form.cleaned_data)
        data.pop('consent')
        request.META[CONSENT_HEADER] = data.pop('consent_version')
        try:
            if profile:
                set_flair(request.user, profile.user, request=request, **data)
            else:
                set_spotlight(request.user, request=request, **data)
        except APIException as error:
            form.add_error(None, str(error.detail))
            status = error.status_code
        else:
            return redirect(request.path + '?saved=1')
    return render(request, 'access/community.html', dict(form=form, profile=profile, saved=request.GET.get('saved') == '1'), status=status)
