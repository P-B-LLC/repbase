"""The review's findings, each with the thing that stops it coming back.

Every one of these is a property somebody reproduced or reasoned their way to,
so every one gets a test that fails without the fix rather than a note in a
document that nobody re-reads.
"""

import io
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from PIL import Image
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase

from .media import signed_media_path, taken_down
from .moderation import ModerationConsentRequired, check_public_content
from .models import Post
from .tests import RepbaseAPITestMixin

LIVE = dict(
    MODERATION_ENABLED=True,
    MODERATION_API_KEY='fake-unit-test-key',
    MODERATION_DISCLOSURE_CONFIRMED=True,
)


def request_with(version):
    class _Request:
        META = {} if version is None else {'HTTP_X_MODERATION_CONSENT': version}

    return _Request()


def consented():
    return request_with(settings.MODERATION_CONSENT_VERSION)


def allowed_response():
    result = MagicMock()
    result.__enter__.return_value.read.return_value = b'{"results": [{"flagged": false}]}'
    return result


@override_settings(**LIVE)
class ConsentIsEnforcedByTheServerTests(SimpleTestCase):
    """The app asks per submission. That is a property of one client, and the
    server cannot rely on it: an older build, a script or curl reaches the
    same endpoint. So the submission carries the consent version, and without
    it nothing leaves the machine.
    """

    def test_no_consent_sends_nothing(self):
        with patch('core.moderation.urllib.request.build_opener') as opener:
            with self.assertRaises(ModerationConsentRequired):
                check_public_content({'caption': 'hello'}, request=request_with(None))
            opener.assert_not_called()

    def test_an_older_consent_version_is_refused(self):
        """Why it is versioned: agreement to last month's wording is not
        agreement to this month's."""
        with patch('core.moderation.urllib.request.build_opener') as opener:
            with self.assertRaises(ModerationConsentRequired):
                check_public_content({'caption': 'hello'}, request=request_with('2020-01-01'))
            opener.assert_not_called()

    def test_the_current_version_is_accepted(self):
        with patch('core.moderation.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = allowed_response()
            check_public_content({'caption': 'hello'}, request=consented())
            opener.return_value.open.assert_called_once()

    def test_nothing_to_review_needs_no_consent(self):
        """An edit carrying no public text is not a submission for review, and
        asking permission to send nothing would train people to dismiss the
        dialog."""
        check_public_content({'weight_kg': '80'}, request=request_with(None))


@override_settings(**LIVE)
class DamagedImagesAreAnsweredNotCrashedTests(SimpleTestCase):
    def photo(self):
        buffer = io.BytesIO()
        Image.new('RGB', (40, 40), 'white').save(buffer, format='JPEG')
        return buffer.getvalue()

    def test_a_truncated_image_is_a_validation_error(self):
        """Reproduced: it passed the type sniff, then raised OSError partway
        through preprocessing, which is a 500 for what is really a bad
        upload."""
        whole = self.photo()
        with patch('core.moderation.urllib.request.build_opener') as opener:
            with self.assertRaises(ValidationError) as caught:
                check_public_content(image=whole[: len(whole) // 2], request=consented())
            opener.assert_not_called()
        self.assertIn('could not be read', str(caught.exception))

    def test_bytes_that_are_not_an_image_at_all(self):
        with self.assertRaises(ValidationError):
            check_public_content(image=b'this is not a photograph', request=consented())

    def test_an_absurd_pixel_count_is_refused_before_it_is_allocated(self):
        with patch('core.moderation.Image.open') as opened:
            opened.return_value.__enter__.return_value.size = (30000, 30000)
            with self.assertRaises(ValidationError) as caught:
                check_public_content(image=b'x', request=consented())
        self.assertIn('too large', str(caught.exception))

    def test_a_good_image_still_goes_through(self):
        with patch('core.moderation.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = allowed_response()
            check_public_content(image=self.photo(), request=consented())
            opener.return_value.open.assert_called_once()


class HidingAPostRevokesItsPhotoTests(RepbaseAPITestMixin, APITestCase):
    """Reproduced: after hiding, the signed URL still answered 200, and the
    default window keeps a link alive for about a week. A takedown that leaves
    the photograph reachable has not taken anything down.
    """

    NAME = 'post-photos/visible.jpg'

    def setUp(self):
        _, self.owner, token = self.create_account('poster')
        self.authenticate(token)
        self.post = Post.objects.create(author=self.owner, kind=Post.Kind.MEAL)
        Post.objects.filter(pk=self.post.pk).update(image=self.NAME)

    def hide(self):
        Post.objects.filter(pk=self.post.pk).update(
            is_hidden=True, hidden_at=timezone.now()
        )

    def test_a_live_post_keeps_serving(self):
        self.assertFalse(taken_down(self.NAME))

    def test_hiding_the_post_revokes_the_photo(self):
        self.hide()
        self.assertTrue(taken_down(self.NAME))

    def test_a_signed_url_issued_beforehand_stops_working(self):
        """The part that matters: the link was already handed out."""
        signed = signed_media_path(self.NAME)
        self.hide()
        self.assertEqual(self.client.get(signed).status_code, 403)

    def test_unhiding_restores_it(self):
        self.hide()
        Post.objects.filter(pk=self.post.pk).update(is_hidden=False, hidden_at=None)
        self.assertFalse(taken_down(self.NAME))

    def test_a_profile_photo_never_reaches_the_query(self):
        """Profile photos cannot be hidden, so they must not pay for it."""
        with self.assertNumQueries(0):
            self.assertFalse(taken_down('profile-photos/face.jpg'))


class CustomExerciseNamesStayPrivateTests(RepbaseAPITestMixin, TestCase):
    """A public highlight showed the exercise name from a private workout.

    A highlight is matched by a keyword in the exercise name, which is exactly
    what makes this reachable: anybody can name a private movement "bench
    something", have it chosen for their public profile, and then edit the
    words afterwards. A custom name has never been through moderation. A
    shipped one has no author to change it.
    """

    def setUp(self):
        from .models import Exercise, ProfileHighlight

        _, self.owner, _ = self.create_account('lifter')
        self.Exercise = Exercise
        self.ProfileHighlight = ProfileHighlight

    def payload_for(self, suffix, created_by):
        from .models import (
            LIFT_KEYWORDS,
            SessionExercise,
            SetEntry,
            WorkoutSession,
        )
        from .serializers import highlight_payload

        lift = self.ProfileHighlight.Lift.values[0]
        # Named so the matcher actually picks it, which is the whole point:
        # the keyword is the only thing standing between a private name and a
        # public profile.
        name = f'{LIFT_KEYWORDS[lift]} {suffix}'
        exercise = self.Exercise.objects.create(name=name, created_by=created_by)
        session = WorkoutSession.objects.create(
            repbase_user=self.owner, status=WorkoutSession.Status.COMPLETED
        )
        item = SessionExercise.objects.create(session=session, exercise=exercise, order=1)
        SetEntry.objects.create(session_exercise=item, set_number=1, reps=5, weight_kg=100)
        highlight = self.ProfileHighlight.objects.create(owner=self.owner, lift=lift)
        payload = highlight_payload(highlight)
        self.assertEqual(payload['source'], 'logged', 'the logged path must be exercised')
        return payload, name

    def test_a_shipped_exercise_name_is_shown(self):
        payload, name = self.payload_for('press', created_by=None)
        self.assertEqual(payload['exercise_name'], name)

    def test_a_custom_name_falls_back_to_the_canonical_lift(self):
        payload, name = self.payload_for('words I chose myself', created_by=self.owner)
        self.assertNotEqual(payload['exercise_name'], name)
        self.assertEqual(payload['exercise_name'], payload['lift_label'])


class EveryRefusalTellsTheAppWhichKindItIsTests(SimpleTestCase):
    """The app decides whether to offer a retry from the code, not the status.

    This was wrong once and silently: only the rejection carried a code, so an
    outage and a consent failure both reached the app as an unexplained status
    and were shown as "unexpected response". DRF renders `exc.detail` as the
    body, and a bare string detail produces no code at all -- `default_code`
    never reaches the wire. The audit that found it was a print of the three
    bodies; this is that print, kept.
    """

    def bodies(self):
        from rest_framework.exceptions import ValidationError as DRFValidationError
        from rest_framework.views import exception_handler

        from .moderation import ModerationConsentRequired, ModerationUnavailable

        raised = [
            DRFValidationError({'detail': 'refused', 'code': 'moderation_rejected'}),
            ModerationUnavailable(),
            ModerationConsentRequired(),
        ]
        return [exception_handler(exception, {}) for exception in raised]

    def test_each_one_carries_a_code_and_a_sentence(self):
        for response in self.bodies():
            self.assertIn('code', response.data, response.data)
            self.assertIn('detail', response.data, response.data)
            self.assertTrue(str(response.data['detail']).strip())

    def test_the_codes_are_the_ones_the_client_knows(self):
        """The client matches on exactly these three. Renaming one here
        without renaming it there turns a useful message back into a status
        code, which is the failure this whole path exists to avoid."""
        codes = {str(response.data['code']) for response in self.bodies()}
        self.assertEqual(
            codes,
            {'moderation_rejected', 'moderation_unavailable', 'moderation_consent_required'},
        )

    def test_an_outage_is_the_only_one_worth_retrying(self):
        statuses = {
            str(response.data['code']): response.status_code
            for response in self.bodies()
        }
        self.assertEqual(statuses['moderation_unavailable'], 503)
        self.assertEqual(statuses['moderation_rejected'], 400)
        self.assertEqual(statuses['moderation_consent_required'], 403)


@override_settings(**LIVE)
class TheEndpointsStillWorkThroughTheRealStackTests(RepbaseAPITestMixin, APITestCase):
    """Consent is threaded from the request into the serializers, and a
    serializer that cannot see the request refuses everything.

    Registration is the one that would hurt: it moderates a username and a
    display name, it is the first thing anybody does, and nobody signing up
    can report that it is broken. So it gets exercised through the client
    rather than by calling the checker directly.
    """

    def headers(self, version=None):
        if version is None:
            version = settings.MODERATION_CONSENT_VERSION
        return {'HTTP_X_MODERATION_CONSENT': version}

    def register(self, username, **extra):
        with patch('core.moderation.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = allowed_response()
            return self.client.post(
                '/api/v1/auth/register/',
                {
                    'username': username,
                    'email': f'{username}@example.test',
                    'password': 'a-long-enough-password-1',
                    'first_name': 'Test',
                    'last_name': 'Person',
                },
                format='json',
                **extra,
            )

    def test_registering_works_when_the_client_declares_its_disclosure(self):
        response = self.register('newcomer', **self.headers())
        self.assertEqual(response.status_code, 201, response.data)

    def test_registering_without_it_is_refused_and_says_why(self):
        response = self.register('older-build')
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(response.data.get('code'), 'moderation_consent_required')

    def test_an_unknown_disclosure_version_is_refused(self):
        response = self.register('stale-build', **self.headers('1999-01-01'))
        self.assertEqual(response.status_code, 403, response.data)

    def test_editing_a_caption_works_through_the_real_stack(self):
        """Every serializer that moderates needs the request, and four of them
        were built without it -- registration, the profile photo, the prompts
        and the social links -- so consent could never be found and all four
        refused everything. A checker wired to a context nobody passed is
        indistinguishable from a working one until it is called this way."""
        _, owner, token = self.create_account("editor")
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')
        post = Post.objects.create(author=owner, kind=Post.Kind.MEAL)
        with patch('core.moderation.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = allowed_response()
            response = self.client.patch(
                f'/api/v1/social/posts/{post.pk}/',
                {'caption': 'a new caption'},
                format='json',
                **self.headers(),
            )
        self.assertEqual(response.status_code, 200, response.data)

    def test_creating_a_gym_works_through_the_real_stack(self):
        """Gyms, comments and the profile go through viewsets, which do pass a
        context. Asserted rather than assumed: assuming it is what left four
        other flows refusing everything."""
        _, _, token = self.create_account('gym-adder')
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')
        with patch('core.moderation.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = allowed_response()
            response = self.client.post(
                '/api/v1/gyms/',
                {'name': 'Iron Works', 'city': 'Boulder'},
                format='json',
                **self.headers(),
            )
        self.assertIn(response.status_code, (200, 201), response.data)

    def test_commenting_works_through_the_real_stack(self):
        _, owner, token = self.create_account('commenter')
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')
        post = Post.objects.create(author=owner, kind=Post.Kind.MEAL)
        with patch('core.moderation.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = allowed_response()
            response = self.client.post(
                '/api/v1/social/comments/',
                {'post': post.pk, 'body': 'Nice one'},
                format='json',
                **self.headers(),
            )
        self.assertEqual(response.status_code, 201, response.data)

    def test_editing_the_profile_works_through_the_real_stack(self):
        _, _, token = self.create_account('profile-editor')
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')
        with patch('core.moderation.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = allowed_response()
            response = self.client.patch(
                '/api/v1/me/', {'bio': 'Training for a half.'}, format='json', **self.headers()
            )
        self.assertEqual(response.status_code, 200, response.data)
