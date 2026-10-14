from django.conf import settings
from django.core import signing
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_safe
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.exceptions import ValidationError, APIException
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from .models import Post, PostVideo
from .views import visible_posts_for
from .video_jobs_models import VideoJob
from .video_pipeline import begin, complete, metric
from . import video_store as store


class UploadRequest(serializers.Serializer):
    size_bytes = serializers.IntegerField(min_value=1, max_value=100 * 1024 * 1024)
    content_type = serializers.ChoiceField(choices=['video/mp4', 'video/quicktime', 'video/x-m4v'])


class UploadResponse(serializers.Serializer):
    id = serializers.UUIDField()
    state = serializers.CharField()
    upload_url = serializers.URLField(required=False)
    fields = serializers.DictField(child=serializers.CharField(), required=False)


class VideoUnavailable(APIException):
    status_code = 503
    default_detail = 'Video uploads are temporarily unavailable.'


class UploadStart(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'upload'

    @extend_schema(request=UploadRequest, responses={201: UploadResponse})
    def post(self, request, post_id):
        if not settings.VIDEO_PIPELINE_ENABLED or settings.VIDEO_STORAGE_BACKEND != 's3' or not settings.VIDEO_STORAGE_BUCKET:
            raise VideoUnavailable()
        post = get_object_or_404(Post, pk=post_id, author__user=request.user)
        data = UploadRequest(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            job, policy = begin(post, data.validated_data['size_bytes'], data.validated_data['content_type'])
        except ValueError as error:
            raise ValidationError(str(error))
        except Exception:
            metric('upload_start_failed')
            raise VideoUnavailable()
        metric('upload_started')
        return Response({'id': job.pk, 'state': job.state, 'upload_url': policy['url'], 'fields': policy['fields']}, status=201)


class UploadFinish(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'upload'

    @extend_schema(request=None, responses={202: UploadResponse})
    def post(self, request, job_id):
        get_object_or_404(VideoJob, pk=job_id, video__post__author__user=request.user)
        try:
            job = complete(job_id, request.user.pk)
        except ValueError as error:
            raise ValidationError(str(error))
        except Exception:
            metric('upload_complete_failed')
            raise VideoUnavailable()
        return Response({'id': job.pk, 'state': job.state}, status=202)


def asset_url(video, key, request):
    viewer = getattr(getattr(request, 'user', None), 'pk', None)
    if viewer is None:
        return None
    token = signing.dumps({'video': video.pk, 'key': key, 'viewer': viewer}, salt='video-playback', compress=True)
    return request.build_absolute_uri('/api/v1/video-assets/?token=' + token)


@require_safe
def serve_asset(request):
    try:
        data = signing.loads(request.GET.get('token', ''), salt='video-playback', max_age=3600)
        video = PostVideo.objects.select_related('post').get(pk=data['video'], processing_state='ready', status='approved')
        user = get_user_model().objects.get(pk=data['viewer'], is_active=True)
        if not visible_posts_for(user.repbase_profile, Post.objects.all()).filter(pk=video.post_id).exists():
            return HttpResponse(status=403)
        allowed = [v['key'] for v in video.variants] + [video.thumbnail_key]
        if not data['key'] or data['key'] not in allowed:
            return HttpResponse(status=403)
        if settings.VIDEO_STORAGE_BACKEND == 'local':
            from . import video_local_store
            key = data['key']
            target = video_local_store.path(key)
            if not target.is_file():
                return HttpResponse(status=404)
            if settings.VIDEO_LOCAL_ACCEL_PREFIX:
                response = HttpResponse()
                response['X-Accel-Redirect'] = settings.VIDEO_LOCAL_ACCEL_PREFIX.rstrip('/') + '/' + key
                del response['Content-Type']
            elif key.endswith('.mp4'):
                from .media import _video_response
                response = _video_response(request, key, storage=video_local_store.storage())
            else:
                from django.http import FileResponse
                response = FileResponse(target.open('rb'), content_type='image/jpeg')
            response['Cache-Control'] = 'private, no-store'
            response['X-Content-Type-Options'] = 'nosniff'
            metric('playback_local')
            return response
        response = HttpResponseRedirect(store.playback(data['key']))
        response.status_code = 307  # Preserve Range/HEAD; object storage streams bytes.
        response['Cache-Control'] = 'private, no-store'
        response['Referrer-Policy'] = 'no-referrer'
        metric('playback_redirect')
        return response
    except (signing.BadSignature, KeyError, PostVideo.DoesNotExist, get_user_model().DoesNotExist):
        return HttpResponse(status=403)
    except Exception:
        metric('playback_redirect_failed')
        return HttpResponse(status=503)


class PlaybackEventRequest(serializers.Serializer):
    post_id = serializers.IntegerField(min_value=1)
    event = serializers.ChoiceField(choices=['upload_failed', 'playback_error', 'playback_started'])


class PlaybackEvent(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    # Not 'upload': the app sends one of these per clip played, and sharing
    # that budget meant watching twenty clips in an hour refused the next
    # real upload with 429.
    throttle_scope = 'impression'

    @extend_schema(request=PlaybackEventRequest, responses={204: None})
    def post(self, request):
        data = PlaybackEventRequest(data=request.data)
        data.is_valid(raise_exception=True)
        values = data.validated_data
        get_object_or_404(visible_posts_for(request.user.repbase_profile, Post.objects.all()), pk=values['post_id'])
        try:
            if cache.add(f'video-event:{request.user.pk}:{values["post_id"]}:{values["event"]}', True, 60):
                metric(values['event'])
        except Exception:
            pass
        return Response(status=204)
