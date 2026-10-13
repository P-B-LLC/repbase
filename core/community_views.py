from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.response import Response
from .access_views import PrivateAccessView
from .access_models import CommunitySpotlight
from .serializers import PostSerializer


class CommunitySpotlightSerializer(serializers.Serializer):
    announcement = serializers.CharField(allow_blank=True)
    featured_post = PostSerializer(allow_null=True)


class CommunitySpotlightView(PrivateAccessView):
    @extend_schema(operation_id='community_spotlight_retrieve', responses=CommunitySpotlightSerializer)
    def get(self, request):
        from .views import visible_posts_for, posts_for_cards, annotate_social_counts, profile_for
        row = CommunitySpotlight.objects.filter(pk=1).first()
        featured = None
        if row and row.featured_post_id:
            viewer = profile_for(request.user)
            # Re-check everything at read time. Featuring never widens visibility.
            post = annotate_social_counts(viewer, visible_posts_for(viewer, posts_for_cards())).filter(
                pk=row.featured_post_id, visibility='public', author__is_profile_public=True,
                author__user__is_active=True, repost_of__isnull=True).first()
            if post:
                featured = PostSerializer(post, context={'request': request}).data
        return Response(dict(announcement=row.announcement if row else '', featured_post=featured))
