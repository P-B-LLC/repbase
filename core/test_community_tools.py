from unittest.mock import patch
from django.test import override_settings
from rest_framework.test import APITestCase
from rest_framework.exceptions import PermissionDenied, ValidationError
from .tests import RepbaseAPITestMixin
from .access import AccessConflict, capabilities
from .access_models import AccountAccess, CommunitySpotlight
from .community_tools import set_flair, set_spotlight
from .models import Post, Block


@override_settings(DEBUG=True, MODERATION_ENABLED=False)
class CommunityToolsTests(RepbaseAPITestMixin, APITestCase):
    def setUp(self):
        self.owner, _, _ = self.create_account('owner')
        AccountAccess.objects.create(user=self.owner, owner=True)
        self.member, self.profile, token = self.create_account('member')
        self.viewer, self.viewer_profile, self.viewer_token = self.create_account('viewer')
        self.profile.is_profile_public = True
        self.profile.save()
        self.post = Post.objects.create(author=self.profile, kind='meal', caption='Lunch', visibility='public')
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.viewer_token.key}')

    def spotlight(self, version=0):
        set_spotlight(self.owner, 'Welcome', self.post.pk, version, 'Community highlight')

    def test_cosmetics_do_not_grant_permissions_and_cannot_impersonate(self):
        set_flair(self.owner, self.member, 'Trail runner', 'Founding member', '#ff7700', 0, 'Welcome')
        self.assertFalse(any(capabilities(self.member).values()))
        self.assertEqual(AccountAccess.objects.get(user=self.member).label_color, '#FF7700')
        with self.assertRaises(ValidationError):
            set_flair(self.owner, self.member, 'Owner', '', '#ffffff', 1, 'Invalid')
        with self.assertRaises(AccessConflict):
            set_flair(self.owner, self.member, 'Runner', '', '#ffffff', 0, 'Stale')

    def test_moderator_cannot_publish_or_grant_flair(self):
        AccountAccess.objects.create(user=self.member, moderator=True)
        with self.assertRaises(PermissionDenied):
            set_spotlight(self.member, 'No', None, 0, 'No')
        with self.assertRaises(PermissionDenied):
            set_flair(self.member, self.viewer, 'Runner', '', '#ffffff', 0, 'No')

    def test_publication_safety_failure_leaves_no_record(self):
        with patch('core.community_tools.check_public_content', side_effect=ValidationError('Blocked')):
            with self.assertRaises(ValidationError):
                self.spotlight()
        self.assertFalse(CommunitySpotlight.objects.exists())

    def test_feature_cannot_override_privacy_hidden_or_blocked_content(self):
        self.spotlight()
        path = '/api/v1/social/spotlight/'
        self.assertEqual(self.client.get(path).data['featured_post']['id'], self.post.pk)
        self.post.visibility = 'private'
        self.post.save()
        self.assertIsNone(self.client.get(path).data['featured_post'])
        self.post.visibility = 'public'
        self.post.is_hidden = True
        self.post.save()
        self.assertIsNone(self.client.get(path).data['featured_post'])
        self.post.is_hidden = False
        self.post.save()
        Block.objects.create(blocker=self.viewer_profile, blocked=self.profile)
        self.assertIsNone(self.client.get(path).data['featured_post'])

    def test_private_post_cannot_be_selected(self):
        self.post.visibility = 'private'
        self.post.save()
        with self.assertRaises(ValidationError):
            self.spotlight()

    def test_stale_studio_update_rejected(self):
        self.spotlight()
        with self.assertRaises(AccessConflict):
            self.spotlight()
