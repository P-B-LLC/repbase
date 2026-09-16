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

from django.conf import settings as django_settings

from .moderation import check_public_content, ModerationUnavailable, public_text


def consented():
    """A request carrying agreement to the current disclosure.

    The server refuses to transmit without it, so every test that expects
    content to reach the provider has to look like a client that asked.
    """
    class _Request:
        META = {'HTTP_X_MODERATION_CONSENT': django_settings.MODERATION_CONSENT_VERSION}
    return _Request()
from .models import Block, CommentReport, FoodEntry, FoodMeal, Post, PostComment, PostReport
from .tests import RepbaseAPITestMixin


@override_settings(MODERATION_ENABLED=True, MODERATION_API_KEY='fake-unit-test-key',
                   MODERATION_DISCLOSURE_CONFIRMED=True, MODERATION_CONTACT_EMAIL='support@rytivo.app')
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
                                  'password': 'private-secret', 'weight_kg': '50'}, image=photo.getvalue(), request=consented())
            request = opener.return_value.open.call_args.args[0]
            body = json.loads(request.data)
            self.assertEqual(body['input'][0]['text'], 'Training today')
            self.assertTrue(body['input'][1]['image_url']['url'].startswith('data:image/jpeg;base64,'))
            self.assertNotIn('private', request.data.decode())

    def test_flagged_content_is_rejected_with_appeal_contact(self):
        with patch('core.moderation.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = self.response({'results': [{'flagged': True}]})
            with self.assertRaises(ValidationError) as caught:
                check_public_content({'caption': 'fixture'}, request=consented())
            self.assertIn('support@rytivo.app', str(caught.exception))

    def test_timeout_and_malformed_results_fail_closed(self):
        with patch('core.moderation.urllib.request.build_opener') as opener:
            for payload in [{}, {'results': []}, {'results': [{'flagged': 'false'}]}]:
                opener.return_value.open.return_value = self.response(payload)
                with self.assertRaises(ModerationUnavailable):
                    check_public_content({'body': 'fixture'}, request=consented())
            opener.return_value.open.side_effect = TimeoutError('secret provider diagnostic')
            with self.assertRaises(ModerationUnavailable) as caught:
                check_public_content({'body': 'fixture'}, request=consented())
            self.assertNotIn('secret', str(caught.exception))

    def test_missing_key_and_unconfirmed_disclosure_do_not_send_content(self):
        with patch('core.moderation.urllib.request.build_opener') as opener:
            for settings in [{'MODERATION_API_KEY': ''}, {'MODERATION_DISCLOSURE_CONFIRMED': False}]:
                with override_settings(**settings), self.assertRaises(ModerationUnavailable):
                    check_public_content({'body': 'fixture'}, request=consented())
            opener.assert_not_called()

    def test_public_field_allowlist_reaches_nested_snapshots(self):
        self.assertEqual(public_text({'meal': {'name': 'Lunch', 'entries': [{'name': 'Rice'}]},
                                      'token': 'secret'}), ['Lunch', 'Rice'])

    def test_plain_strings_under_a_public_key_are_not_skipped(self):
        """A list of bare strings used to vanish, silently.

        Recursion loses the key, so by the time a string inside a list is
        looked at there is nothing left to say whether it was public. The
        result was text published having been checked by nobody, with no test
        anywhere going red. Nothing sends this shape today; the point is that
        the next caller to try it is not quietly unmoderated.
        """
        self.assertEqual(public_text({'caption': ['first', '  second  ', '', 'third']}),
                         ['first', 'second', 'third'])

    def test_a_list_under_a_private_key_is_still_ignored(self):
        """The negative control. Widening the public keys must not widen the
        private ones, or measurements start leaving the machine."""
        self.assertEqual(public_text({'weight_kg': ['50', '51'], 'password': ['hunter2']}), [])

    def test_mixed_entries_under_a_public_key_are_all_reached(self):
        self.assertEqual(public_text({'name': ['plain', {'name': 'nested'}]}),
                         ['plain', 'nested'])

    def test_a_dict_under_a_public_key_does_not_leak_its_field_names(self):
        """Iterating a dict yields keys. If the list handling caught dicts too,
        the field names themselves would be sent as if they were content."""
        self.assertEqual(public_text({'name': {'weight_kg': '50'}}), [])

    def test_the_documented_header_is_the_one_that_is_checked(self):
        """Two spellings of one header: the wire name the contract publishes,
        and Django's META key the server reads. Derived from a single constant
        so they cannot drift -- if they ever did, every submission would be
        refused for want of a header nobody had been told to send, and both
        halves would look correct in isolation."""
        from .moderation import CONSENT_HEADER, CONSENT_HEADER_NAME
        self.assertEqual(CONSENT_HEADER_NAME, 'X-Moderation-Consent')
        self.assertEqual(CONSENT_HEADER, 'HTTP_X_MODERATION_CONSENT')

    def test_the_configured_timeout_is_the_one_used(self):
        """It bounds how long a synchronous worker is held, so it is a capacity
        setting. A default hardcoded back into the call would not fail any
        other test -- it would just make the knob in the deployment docs a
        lie."""
        with override_settings(MODERATION_TIMEOUT_SECONDS=3):
            with patch('core.moderation.urllib.request.build_opener') as opener:
                opener.return_value.open.return_value = self.response({'results': [{'flagged': False}]})
                check_public_content({'caption': 'fixture'}, request=consented())
                self.assertEqual(opener.return_value.open.call_args.kwargs['timeout'], 3)


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

    def test_snapshot_rejection_creates_no_post_at_all(self):
        meal = FoodMeal.objects.create(owner=self.owner, date=timezone.localdate(), name='Fixture', position=1)
        FoodEntry.objects.create(meal=meal, name='Fixture food', calories=100, position=1)
        with patch('core.views.check_public_content', side_effect=ValidationError('Rejected snapshot')) as moderation:
            response = self.client.post('/api/v1/social/posts/', {'kind': 'meal', 'source_id': meal.pk}, format='json')
            moderation.assert_called_once()
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

    def test_comment_reporting_is_idempotent_private_and_does_not_require_provider(self):
        comment = PostComment.objects.create(post=self.post, author=self.other, body='Fixture')
        path = f'/api/v1/social/comments/{comment.pk}/report/'
        with patch('core.moderation.check_public_content', side_effect=AssertionError('Must not moderate reports')):
            first = self.client.post(path, {'reason': 'harassment', 'detail': 'Private report'}, format='json')
            second = self.client.post(path, {'reason': 'harassment'}, format='json')
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(CommentReport.objects.count(), 1)
        self.assertNotIn('Private report', str(first.data))

    def test_hidden_parent_hides_thread_and_refuses_reply(self):
        parent = PostComment.objects.create(post=self.post, author=self.other, body='Hidden', is_hidden=True)
        reply = PostComment.objects.create(post=self.post, author=self.third, parent=parent, body='Reply')
        self.assertEqual(self.client.get(f'/api/v1/social/comments/{reply.pk}/').status_code, 404)
        result = self.client.post('/api/v1/social/comments/',
            {'post': self.post.pk, 'parent': parent.pk, 'body': 'New reply'}, format='json')
        self.assertEqual(result.status_code, 404)

    def test_blocked_parent_prevents_direct_reply_read(self):
        parent = PostComment.objects.create(post=self.post, author=self.other, body='Blocked')
        reply = PostComment.objects.create(post=self.post, author=self.third, parent=parent, body='Reply')
        Block.objects.create(blocker=self.other, blocked=self.owner)
        self.assertEqual(self.client.get(f'/api/v1/social/comments/{reply.pk}/').status_code, 404)

    def test_repost_cannot_resurface_hidden_or_blocked_original(self):
        repost = Post.objects.create(author=self.other, kind='repost', repost_of=self.post)
        self.post.is_hidden = True
        self.post.save(update_fields=['is_hidden'])
        self.assertEqual(self.client.get(f'/api/v1/social/posts/{repost.pk}/').status_code, 404)
        self.post.is_hidden = False
        self.post.save(update_fields=['is_hidden'])
        Block.objects.create(blocker=self.owner, blocked=self.third)
        self.assertEqual(self.client.get(f'/api/v1/social/posts/{repost.pk}/').status_code, 404)

    def test_public_gym_and_social_handle_are_checked_before_save(self):
        from .serializers import GymSerializer, ProfileSocialLinksRequestSerializer
        with patch('core.serializers.check_public_content', side_effect=ValidationError('Rejected')) as moderation:
            gym = GymSerializer(data={'name': 'Fixture gym', 'city': 'Fixture city', 'country': 'US'})
            self.assertFalse(gym.is_valid())
            self.assertEqual(moderation.call_args.args[0]['city'], 'Fixture city')
            links = ProfileSocialLinksRequestSerializer(data={
                'social_links': [{'platform': 'instagram', 'url': 'fixturehandle'}]})
            self.assertFalse(links.is_valid())
            self.assertEqual(moderation.call_args.args[0][0]['handle'], 'fixturehandle')

    def test_hidden_and_blocked_comments_are_not_counted(self):
        PostComment.objects.create(post=self.post, author=self.third, body='Visible')
        parent = PostComment.objects.create(post=self.post, author=self.other, body='Hidden', is_hidden=True)
        PostComment.objects.create(post=self.post, author=self.third, parent=parent, body='Hidden thread')
        PostComment.objects.create(post=self.post, author=self.other, body='Blocked')
        Block.objects.create(blocker=self.owner, blocked=self.other)
        response = self.client.get(f'/api/v1/social/posts/{self.post.pk}/')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['comment_count'], 1)

    def test_edit_response_does_not_leak_hidden_or_blocked_replies(self):
        parent = PostComment.objects.create(post=self.post, author=self.owner, body='My parent')
        PostComment.objects.create(post=self.post, author=self.other, parent=parent, body='Blocked reply')
        PostComment.objects.create(post=self.post, author=self.third, parent=parent, body='Hidden reply', is_hidden=True)
        Block.objects.create(blocker=self.owner, blocked=self.other)
        response = self.client.patch(f'/api/v1/social/comments/{parent.pk}/', {'body': 'Edited'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['replies'], [])

    def test_read_only_moderator_cannot_close_or_hide_reports(self):
        from django.contrib.admin import AdminSite
        from django.test import RequestFactory
        from .admin import CommentReportAdmin, PostReportAdmin
        request = RequestFactory().get('/admin/')
        request.user = MagicMock()
        request.user.has_perm.return_value = False
        request.user.has_perms.return_value = False
        comment_admin = CommentReportAdmin(CommentReport, AdminSite())
        post_admin = PostReportAdmin(PostReport, AdminSite())
        self.assertNotIn('hide_comments', comment_admin.get_actions(request))
        self.assertNotIn('no_action', comment_admin.get_actions(request))
        self.assertNotIn('mark_no_action', post_admin.get_actions(request))

    def test_moderator_hides_comment_and_closes_all_its_reports(self):
        from django.contrib.admin import AdminSite
        from django.test import RequestFactory
        from .admin import CommentReportAdmin
        comment = PostComment.objects.create(post=self.post, author=self.other, body='Fixture')
        first = CommentReport.objects.create(comment=comment, reporter=self.owner, reason='hate')
        CommentReport.objects.create(comment=comment, reporter=self.third, reason='hate')
        request = RequestFactory().post('/admin/')
        request.user = self.owner.user
        moderator = CommentReportAdmin(CommentReport, AdminSite())
        with patch.object(moderator, 'log_change') as audit:
            moderator.hide_comments(request, CommentReport.objects.filter(pk=first.pk, reviewed_at__isnull=True))
            audit.assert_called_once()
        comment.refresh_from_db()
        self.assertTrue(comment.is_hidden)
        self.assertEqual(CommentReport.objects.filter(reviewed_at__isnull=False,
            reviewed_by=request.user, resolution='hidden').count(), 2)
