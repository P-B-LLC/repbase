import base64
import io
import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.core.management import call_command, CommandError
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from PIL import Image
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase

from .moderation import check_public_content, ModerationUnavailable, public_text
from .models import Block, FoodMeal, Post, PostComment, PostReport
from .tests import RepbaseAPITestMixin


@override_settings(MODERATION_ENABLED=True, MODERATION_API_KEY='fake-unit-test-key',
                   MODERATION_DISCLOSURE_CONFIRMED=True, MODERATION_CONTACT_EMAIL='aaronpio18@gmail.com')
class ProviderModerationTests(SimpleTestCase):
    def response(self, payload):
        result = MagicMock()
        result.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        return result

    def test_allowed_text_and_photo_are_checked_without_account_secrets(self):
        photo = io.BytesIO()
        Image.new('RGB', (12, 12), 'white').save(photo, format='PNG')
        with patch('core.moderation.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = self.response({'results': [{'flagged': False}]})
            check_public_content({'caption': 'Training today', 'email': 'private@test.test',
                                  'password': 'private-secret', 'weight_kg': '50'}, image=photo.getvalue())
            request = opener.return_value.open.call_args.args[0]
            body = json.loads(request.data)
            self.assertEqual(body['input'][0]['text'], 'Training today')
            self.assertTrue(body['input'][1]['image_url']['url'].startswith('data:image/jpeg;base64,'))
            self.assertNotIn('private', request.data.decode())

    def test_flagged_content_is_rejected_with_appeal_contact(self):
        with patch('core.moderation.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = self.response({'results': [{'flagged': True}]})
            with self.assertRaises(ValidationError) as caught:
                check_public_content({'caption': 'fixture'})
            self.assertIn('aaronpio18@gmail.com', str(caught.exception))

    def test_timeout_and_malformed_results_fail_closed(self):
        with patch('core.moderation.urllib.request.build_opener') as opener:
            for payload in [{}, {'results': []}, {'results': [{'flagged': 'false'}]}]:
                opener.return_value.open.return_value = self.response(payload)
                with self.assertRaises(ModerationUnavailable):
                    check_public_content({'body': 'fixture'})
            opener.return_value.open.side_effect = TimeoutError('secret provider diagnostic')
            with self.assertRaises(ModerationUnavailable) as caught:
                check_public_content({'body': 'fixture'})
            self.assertNotIn('secret', str(caught.exception))

    def test_missing_key_and_unconfirmed_disclosure_do_not_send_content(self):
        with patch('core.moderation.urllib.request.build_opener') as opener:
            for settings in [{'MODERATION_API_KEY': ''}, {'MODERATION_DISCLOSURE_CONFIRMED': False}]:
                with override_settings(**settings), self.assertRaises(ModerationUnavailable):
                    check_public_content({'body': 'fixture'})
            opener.assert_not_called()

    def test_public_field_allowlist_reaches_nested_snapshots(self):
        self.assertEqual(public_text({'meal': {'name': 'Lunch', 'entries': [{'name': 'Rice'}]},
                                      'token': 'secret'}), ['Lunch', 'Rice'])


class SocialSafetyIntegrationTests(RepbaseAPITestMixin, APITestCase):
    def setUp(self):
        _, self.owner, token = self.create_account('mod-owner')
        _, self.other, self.other_token = self.create_account('mod-other')
        _, self.third, self.third_token = self.create_account('mod-third')
        self.authenticate(token)
        self.post = Post.objects.create(author=self.third, kind='meal', caption='Third party')

    def test_blocked_comments_replies_and_reply_writes_are_excluded(self):
        parent = PostComment.objects.create(post=self.post, author=self.third, body='Parent')
        hidden = PostComment.objects.create(post=self.post, author=self.other, body='Blocked')
        PostComment.objects.create(post=self.post, author=self.other, parent=parent, body='Blocked reply')
        Block.objects.create(blocker=self.owner, blocked=self.other)
        response = self.client.get('/api/v1/social/comments/', {'post': self.post.pk})
        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data['results']
        self.assertEqual([row['id'] for row in rows], [parent.pk])
        self.assertEqual(rows[0]['replies'], [])
        response = self.client.post('/api/v1/social/comments/',
            {'post': self.post.pk, 'parent': hidden.pk, 'body': 'Reply'}, format='json')
        self.assertEqual(response.status_code, 404)

    def test_flagged_comment_creates_no_comment(self):
        with patch('core.serializers.check_public_content', side_effect=ValidationError('Rejected')):
            response = self.client.post('/api/v1/social/comments/', {'post': self.post.pk, 'body': 'Fixture'}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(PostComment.objects.exists())

    def test_snapshot_rejection_rolls_back_entire_post(self):
        meal = FoodMeal.objects.create(owner=self.owner, date=timezone.localdate(), name='Fixture', position=1)
        with patch('core.moderation.check_public_content', side_effect=ValidationError('Rejected snapshot')):
            response = self.client.post('/api/v1/social/posts/', {'kind': 'meal', 'source_id': meal.pk}, format='json')
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(Post.objects.filter(author=self.owner).exists())

    def test_queue_alerts_without_dumping_report_content(self):
        report = PostReport.objects.create(post=self.post, reporter=self.owner, reason='hate', detail='private report')
        PostReport.objects.filter(pk=report.pk).update(created_at=timezone.now() - timedelta(hours=25))
        output = io.StringIO()
        with self.assertRaises(CommandError):
            call_command('check_moderation_queue', stdout=output)
        self.assertNotIn('private report', output.getvalue())
        self.assertIn('overdue: 1', output.getvalue())
