from datetime import timedelta
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from .tests import RepbaseAPITestMixin
from .fixtures_for_tests import workout_post
from .models import Post, Notification
from .access import AccessConflict
from .access_models import AccessAudit, AccountAccess
from .media_publication import mark_pending, decide
from .media import taken_down
from .views import visible_posts_for


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class MediaPublicationTests(RepbaseAPITestMixin, TestCase):
    def setUp(self):
        _, self.author, _ = self.create_account('media_author')
        self.reviewer, _, _ = self.create_account('media_reviewer')
        AccountAccess.objects.create(user=self.reviewer, moderator=True)
        self.post = workout_post(self.author)
        self.post.image = 'post-photos/pending.jpg'
        self.post.save()
        self.generation = mark_pending(self.post.pk)

    def test_pending_and_flagged_are_private(self):
        for state in ('pending', 'flagged'):
            if state == 'flagged':
                decide(self.post.pk, self.generation, 'flagged')
            self.assertFalse(visible_posts_for(self.author, Post.objects.all()).exists())
            self.assertTrue(taken_down(self.post.image.name))

    def test_author_can_read_status_but_not_pending_media_urls(self):
        from rest_framework.test import APIClient
        client = APIClient()
        client.force_authenticate(self.author.user)
        response = client.get(f'/api/v1/social/posts/{self.post.pk}/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['moderation_status'], 'pending')
        self.assertIsNone(response.data['image_url'])
        self.assertIsNone(response.data['feed_image_url'])
        self.assertIsNone(response.data['published_at'])
        own = client.get('/api/v1/social/posts/', {'author': self.author.pk})
        self.assertEqual(own.status_code, 200)
        rows = own.data['results'] if isinstance(own.data, dict) else own.data
        self.assertIn(self.post.pk, [row['id'] for row in rows])

    def test_other_user_cannot_read_pending_status_or_profile_post(self):
        from rest_framework.test import APIClient
        client = APIClient()
        client.force_authenticate(self.reviewer)
        self.assertEqual(client.get(f'/api/v1/social/posts/{self.post.pk}/').status_code, 404)
        response = client.get('/api/v1/social/posts/', {'author': self.author.pk})
        self.assertEqual(response.status_code, 200)
        rows = response.data['results'] if isinstance(response.data, dict) else response.data
        self.assertNotIn(self.post.pk, [row['id'] for row in rows])

    def test_delayed_approval_sets_current_publication_time_once(self):
        old = timezone.now() - timedelta(days=3)
        Post.objects.filter(pk=self.post.pk).update(created_at=old)
        decide(self.post.pk, self.generation, 'flagged')
        before = timezone.now()
        approved = decide(self.post.pk, self.generation, 'approved', actor=self.reviewer, reason='Reviewed')
        self.assertEqual(approved.created_at, old)
        self.assertGreaterEqual(approved.published_at, before)
        self.assertIsNotNone(approved.reviewed_at)
        repeated = decide(self.post.pk, self.generation, 'approved', actor=self.reviewer, reason='Retry')
        self.assertEqual(repeated.published_at, approved.published_at)
        self.assertEqual(AccessAudit.objects.filter(action='media_approved').count(), 1)

    def test_rejection_keeps_unpublished(self):
        rejected = decide(self.post.pk, self.generation, 'rejected', actor=self.reviewer, reason='Reviewed')
        self.assertIsNone(rejected.published_at)
        self.assertFalse(visible_posts_for(self.author, Post.objects.all()).exists())
        decide(self.post.pk, self.generation, 'rejected', actor=self.reviewer, reason='Retry')
        notice = Notification.objects.get(kind=Notification.Kind.MODERATION_REJECTED)
        self.assertEqual(notice.recipient, self.author)
        self.assertEqual(notice.actor, self.author)  # Reviewer identity is confidential.

    def test_delayed_post_is_fresh_in_recommendations(self):
        from .recommendations import eligible_posts
        Post.objects.filter(pk=self.post.pk).update(created_at=timezone.now() - timedelta(days=90))
        decide(self.post.pk, self.generation, 'approved', actor=self.reviewer, reason='Reviewed')
        _, viewer, _ = self.create_account('fresh_reader')
        self.assertTrue(eligible_posts(viewer).filter(pk=self.post.pk).exists())

    @override_settings(DEBUG=True)
    def test_dashboard_requires_moderator_and_current_revision(self):
        self.client.force_login(self.author.user)
        self.assertEqual(self.client.get('/access/reports/?kind=media').status_code, 403)
        self.client.force_login(self.reviewer)
        self.assertEqual(self.client.get('/access/reports/?kind=media').status_code, 200)
        mark_pending(self.post.pk)
        response = self.client.post('/access/reports/?kind=media', dict(post_id=self.post.pk,
            generation=str(self.generation), decision='approved', reason='Reviewed'))
        self.assertEqual(response.status_code, 409)

    def test_new_revision_invalidates_old_approval(self):
        mark_pending(self.post.pk)
        with self.assertRaises(AccessConflict):
            decide(self.post.pk, self.generation, 'approved')

    def test_child_safety_hold_prevents_approval(self):
        Post.objects.filter(pk=self.post.pk).update(moderation_child_hold=True)
        with self.assertRaises(PermissionDenied):
            decide(self.post.pk, self.generation, 'approved')

    def test_automatic_result_cannot_override_human_review_queue(self):
        decide(self.post.pk, self.generation, 'flagged')
        with self.assertRaises(AccessConflict):
            decide(self.post.pk, self.generation, 'approved')

    @override_settings(DEBUG=True)
    def test_preview_rejects_nonmoderator_and_safety_hold(self):
        url = f'/access/reports/media/{self.post.pk}/{self.generation}/image/'
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.author.user)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.reviewer)
        Post.objects.filter(pk=self.post.pk).update(moderation_child_hold=True)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_deploy_gate_requires_screening_prerequisites(self):
        from .checks import async_media_screening_ready
        with override_settings(MEDIA_SCREENING_ENABLED=True, MODERATION_API_KEY='',
                               MEDIA_SCREENING_THRESHOLD=float('nan')):
            self.assertEqual(async_media_screening_ready(None)[0].id, 'core.E013')
        with override_settings(MEDIA_SCREENING_ENABLED=False):
            self.assertEqual(async_media_screening_ready(None), [])

    @override_settings(DEBUG=True)
    def test_dashboard_role_matrix_and_flagged_review(self):
        from django.conf import settings
        from .media_screening_jobs import enqueue
        job = enqueue(self.post.pk, consent_version=settings.MODERATION_CONSENT_VERSION, threshold=0.5)
        decide(self.post.pk, job.generation, 'flagged')
        url = '/access/reports/?kind=media'
        self.client.force_login(self.author.user)
        self.assertEqual(self.client.get(url).status_code, 403)
        for role in ('analytics', 'moderator', 'owner', 'superowner'):
            user, _, _ = self.create_account('review_' + role)
            AccountAccess.objects.create(user=user, **{role: True})
            self.client.force_login(user)
            response = self.client.get(url)
            self.assertEqual(response.status_code, 403 if role == 'analytics' else 200)
            if role != 'analytics':
                self.assertContains(response, 'Approve &amp; publish')
                self.assertContains(response, str(job.generation))
        self.client.force_login(self.reviewer)
        response = self.client.post(url, dict(post_id=self.post.pk,
            generation=str(job.generation), decision='approved', reason='Meets policy'))
        self.assertEqual(response.status_code, 302)
        self.post.refresh_from_db()
        self.assertEqual(self.post.moderation_status, 'approved')
        history = self.client.get(url + '&status=approved')
        self.assertContains(history, 'Read-only.')
        self.assertNotContains(history, 'name="decision"')

    @override_settings(DEBUG=True)
    def test_revoked_moderator_cannot_submit_decision(self):
        self.client.force_login(self.reviewer)
        AccountAccess.objects.filter(user=self.reviewer).update(moderator=False)
        response = self.client.post('/access/reports/?kind=media', dict(post_id=self.post.pk,
            generation=str(self.generation), decision='approved', reason='Must fail'))
        self.assertEqual(response.status_code, 403)
        self.post.refresh_from_db()
        self.assertEqual(self.post.moderation_status, 'pending')
