"""Session-authorized moderation preview; never issues ordinary photo URLs."""
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count
from django.http import FileResponse, HttpResponseForbidden, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe
from rest_framework.exceptions import APIException

from .access import capabilities
from .media_publication import decide
from .models import Post


def queue(request):
    # Called only inside the reports view's login/permission decorators.
    error, status = None, 200
    selected = request.GET.get('status', 'flagged')
    if selected not in ('flagged', 'pending', 'approved', 'rejected'):
        selected = 'flagged'
    if request.method == 'POST':
        post = get_object_or_404(Post, pk=request.POST.get('post_id', '') if request.POST.get('post_id', '').isdigit() else 0)
        try:
            decision = request.POST.get('decision')
            if decision not in ('approved', 'rejected'):
                from rest_framework.exceptions import ValidationError
                raise ValidationError('Choose Approve or Reject')
            decide(post.pk, request.POST.get('generation'), decision,
                   actor=request.user, reason=request.POST.get('reason', ''))
        except APIException as caught:
            error, status = str(caught.detail), caught.status_code
        else:
            return redirect('/access/reports/?kind=media&status=' + selected)
    base = Post.objects.filter(screening_job__isnull=False)
    totals = {row['moderation_status']: row['total'] for row in
              base.values('moderation_status').annotate(total=Count('pk'))}
    rows = base.filter(moderation_status=selected).select_related(
        'author__user', 'screening_job', 'video', 'workout', 'meal', 'planner').order_by('created_at', 'pk')
    page = Paginator(rows, 20).get_page(request.GET.get('page'))
    import json
    from django.core.serializers.json import DjangoJSONEncoder
    from .serializers import PostWorkoutSerializer, PostMealSerializer, PostPlannerEntrySerializer
    for post in page.object_list:
        post.review_snapshots = []
        for name, serializer in (('workout', PostWorkoutSerializer), ('meal', PostMealSerializer),
                                 ('planner', PostPlannerEntrySerializer)):
            value = getattr(post, name, None)
            if value is not None:
                post.review_snapshots.append(dict(name=name, data=json.dumps(
                    serializer(value, context={'request': request}).data, cls=DjangoJSONEncoder, indent=2)))
    return render(request, 'access/media_review.html', dict(page=page, error=error,
        selected=selected, totals=totals), status=status)


@never_cache
@login_required(login_url='/access/login/')
@require_safe
def preview(request, post_id, generation, asset):
    if not request.user.is_active or not capabilities(request.user)['moderate']:
        return HttpResponseForbidden()
    post = get_object_or_404(Post, pk=post_id, moderation_generation=generation,
                            moderation_child_hold=False)
    if asset == 'image' and post.image:
        response = FileResponse(post.image.open('rb'))
    elif asset == 'video' and getattr(post, 'video', None):
        video = post.video
        if video.file:
            from .media import _video_response
            response = _video_response(request, video.file.name)
        elif video.variants and video.processing_state == 'ready':
            from . import video_store
            key = video.variants[0]['key']
            if settings.VIDEO_STORAGE_BACKEND == 'local':
                from . import video_local_store
                from .media import _video_response
                response = _video_response(request, key, storage=video_local_store.storage())
            else:
                response = HttpResponseRedirect(video_store.playback(key))
        else:
            return HttpResponseForbidden()
    else:
        return HttpResponseForbidden()
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    response['Referrer-Policy'] = 'no-referrer'
    return response
