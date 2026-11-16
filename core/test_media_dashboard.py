"""Dashboard acceptance checks using disposable accounts and synthetic media."""
import tempfile
from django.conf import settings
from django.core.files.base import ContentFile
from django.test import Client, TestCase, override_settings
from .tests import RepbaseAPITestMixin
from .fixtures_for_tests import workout_post
from .access_models import AccountAccess, AccessAudit
from .models import Notification
from .media_screening_jobs import enqueue
from .media_publication import decide


@override_settings(DEBUG=True, PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class MediaDashboardTests(RepbaseAPITestMixin, TestCase):
    url = '/access/reports/?kind=media'

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        storage = override_settings(MEDIA_ROOT=folder.name)
        storage.enable()
        self.addCleanup(storage.disable)
        _, self.owner, _ = self.create_account('dashboard_author')
        self.reviewer, _, _ = self.create_account('dashboard_reviewer')
        AccountAccess.objects.create(user=self.reviewer, moderator=True)
        self.post = workout_post(self.owner)
        self.post.image.save('synthetic.jpg', ContentFile(b'synthetic-fixture-only'))
        self.job = enqueue(self.post.pk, consent_version=settings.MODERATION_CONSENT_VERSION, threshold=0.5)
        decide(self.post.pk, self.job.generation, 'flagged')
        self.client.force_login(self.reviewer)

    def submit(self, **overrides):
        data = dict(post_id=self.post.pk, generation=str(self.job.generation),
                    decision='approved', reason='Policy reviewed')
        data.update(overrides)
        return self.client.post(self.url, data)

    def test_anonymous_and_disabled_accounts_are_denied(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.client.force_login(self.reviewer)
        self.reviewer.is_active = False
        self.reviewer.save(update_fields=['is_active'])
        self.assertNotEqual(self.client.get(self.url).status_code, 200)

    def test_csrf_required_for_decisions(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.reviewer)
        self.assertEqual(client.post(self.url, dict(post_id=self.post.pk,
            generation=str(self.job.generation), decision='approved', reason='Reviewed')).status_code, 403)

    def test_invalid_inputs_never_change_post(self):
        for changes, expected in [({'decision': 'delete'}, 400), ({'reason': ''}, 403),
                ({'reason': ' '}, 403), ({'reason': 'a' * 501}, 403),
                ({'generation': 'invalid'}, 409), ({'post_id': 'invalid'}, 404)]:
            with self.subTest(changes=changes):
                self.assertEqual(self.submit(**changes).status_code, expected)
        self.post.refresh_from_db()
        self.assertEqual(self.post.moderation_status, 'flagged')

    def test_reject_is_idempotent_and_notes_stay_confidential(self):
        for _ in range(2):
            self.assertEqual(self.submit(decision='rejected', reason='Confidential reviewer note').status_code, 302)
        self.assertEqual(Notification.objects.filter(post=self.post, kind='moderation_rejected').count(), 1)
        self.assertEqual(AccessAudit.objects.filter(subject=f'post:{self.post.pk}', action='media_rejected').count(), 1)
        notice = Notification.objects.get(post=self.post, kind='moderation_rejected')
        self.assertEqual(notice.actor_id, self.owner.pk)
        self.assertEqual(notice.recipient_id, self.owner.pk)
        self.post.refresh_from_db()
        self.assertIsNone(self.post.published_at)

    def test_conflicting_final_decision_is_rejected(self):
        self.assertEqual(self.submit().status_code, 302)
        self.assertEqual(self.submit(decision='rejected').status_code, 409)

    def test_author_cannot_review_own_post(self):
        AccountAccess.objects.create(user=self.owner.user, moderator=True)
        self.client.force_login(self.owner.user)
        self.assertContains(self.client.get(self.url), 'You cannot review your own post')
        self.assertEqual(self.submit().status_code, 403)

    def test_image_preview_private_and_generation_bound(self):
        url = f'/access/reports/media/{self.post.pk}/{self.job.generation}/image/'
        response = self.client.get(url)
        # Consume before assertions: the test client's iterator closes resources
        # and emits request_finished safely. Never close this response twice.
        body = b''.join(response.streaming_content)
        self.assertEqual(response.status_code, 200)
        directives = set(response['Cache-Control'].split(', '))
        self.assertTrue({'private', 'no-store'} <= directives)
        self.assertEqual(body, b'synthetic-fixture-only')
        enqueue(self.post.pk, consent_version=settings.MODERATION_CONSENT_VERSION, threshold=0.5)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_hold_disables_preview_and_decisions(self):
        self.post.moderation_child_hold = True
        self.post.save(update_fields=['moderation_child_hold'])
        page = self.client.get(self.url)
        self.assertContains(page, 'Safety hold')
        self.assertNotContains(page, 'name="decision"')
        self.assertEqual(self.submit().status_code, 403)

    def test_queue_filter_fallback_empty_and_pagination(self):
        for _ in range(20):
            post = workout_post(self.owner)
            job = enqueue(post.pk, consent_version=settings.MODERATION_CONSENT_VERSION, threshold=0.5)
            decide(post.pk, job.generation, 'flagged')
        first = self.client.get(self.url + '&status=invalid')
        self.assertEqual(first.context['selected'], 'flagged')
        self.assertEqual(len(first.context['page'].object_list), 20)
        self.assertEqual(first.context['totals']['flagged'], 21)
        second = self.client.get(self.url + '&page=2')
        self.assertEqual(len(second.context['page'].object_list), 1)
        self.assertContains(self.client.get(self.url + '&status=rejected'), 'No posts in this queue')

    def test_caption_is_escaped(self):
        self.post.caption = '<script>alert(1)</script>'
        self.post.save(update_fields=['caption'])
        page = self.client.get(self.url)
        self.assertNotContains(page, '<script>alert(1)</script>')
        self.assertContains(page, '&lt;script&gt;')
