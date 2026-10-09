import tempfile
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.core.files.storage import InMemoryStorage
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APITestCase

from .media_storage import ReservedNameFileSystemStorage
from .models import (
    Gear,
    PlannerEntry,
    Post,
    PostMeal,
    ProfilePrompt,
    ProfileSocialLink,
    SaveReceipt,
    UserDiscipline,
    WorkoutSession,
)
from .tests import RepbaseAPITestMixin


class ReceiptAndGearReviewTests(RepbaseAPITestMixin, APITestCase):
    def setUp(self):
        _, self.owner, token = self.create_account('review')
        self.authenticate(token)

    def test_expired_retry_cannot_duplicate_a_saved_task(self):
        key = str(uuid.uuid4())
        body = dict(title='Walk', scheduled_date='2026-09-13', kind='task', category='other')
        def send(payload):
            return self.client.post('/api/v1/planner/', payload, format='json', HTTP_IDEMPOTENCY_KEY=key)
        first = send(body)
        self.assertEqual(first.status_code, 201, first.data)
        SaveReceipt.objects.update(created_at=timezone.now() - timedelta(days=31))
        self.assertEqual(SaveReceipt.prune(), 1)
        for _ in range(2):
            self.assertEqual(send(body).status_code, 409)
            self.assertEqual(PlannerEntry.objects.count(), 1)
        self.assertEqual(send({**body, 'title': 'Changed'}).status_code, 409)
        self.assertEqual(SaveReceipt.prune(), 0)
        self.assertEqual(SaveReceipt.objects.count(), 1)
        self.owner.delete()
        self.assertFalse(SaveReceipt.objects.exists())

    def test_gear_update_and_retirement_keep_history_totals(self):
        gear = Gear.objects.create(owner=self.owner, kind='shoe', name='Shoe', initial_distance_km=2)
        started = timezone.now() - timedelta(hours=1)
        WorkoutSession.objects.create(repbase_user=self.owner, gear=gear, status='completed',
                                      started_at=started, recorded_distance_km=5)
        path = f'/api/v1/gear/{gear.pk}/'
        before = self.client.get(path).data
        for change in [{'is_default': True}, {'name': 'Renamed'}, {'retired_at': timezone.now().isoformat()}]:
            result = self.client.patch(path, change, format='json')
            self.assertEqual(result.status_code, 200, result.data)
            for field in ['total_distance_km', 'session_count', 'last_used_at']:
                self.assertEqual(result.data[field], before[field])
        self.assertEqual(before['total_distance_km'], 7)
        self.assertEqual(before['session_count'], 1)


class ReservedStorageReviewTests(APITestCase):
    def test_deployment_check_requires_exact_name_storage(self):
        from .checks import publication_storage_preserves_names
        with patch('django.core.files.storage.default_storage', InMemoryStorage()):
            self.assertEqual([error.id for error in publication_storage_preserves_names(None)], ['core.E005'])
        with tempfile.TemporaryDirectory() as location:
            with patch('django.core.files.storage.default_storage', ReservedNameFileSystemStorage(location=location)):
                self.assertEqual(publication_storage_preserves_names(None), [])

    def test_filesystem_collision_does_not_rename_or_overwrite(self):
        with tempfile.TemporaryDirectory() as location:
            storage = ReservedNameFileSystemStorage(location=location)
            self.assertEqual(storage.save_reserved('photo.jpg', ContentFile(b'first')), 'photo.jpg')
            with self.assertRaises(FileExistsError):
                storage.save_reserved('photo.jpg', ContentFile(b'second'))
            with storage.open('photo.jpg') as file:
                self.assertEqual(file.read(), b'first')
            self.assertEqual(storage.listdir('')[1], ['photo.jpg'])

    def test_unsupported_renaming_storage_fails_before_upload(self):
        from .post_publication import publication_media, PublicationMediaError
        from .models import PendingMediaDeletion
        storage = InMemoryStorage()
        with patch('core.post_publication.default_storage', storage), patch.object(storage, 'save') as save:
            with self.assertRaises(PublicationMediaError):
                with publication_media(b'photo', '.jpg'):
                    self.fail('Must not publish')
            save.assert_not_called()
        self.assertFalse(PendingMediaDeletion.objects.exists())


class RepostFeedQueryCostTests(RepbaseAPITestMixin, APITestCase):
    """A repost draws the original's card a second time, nested.

    `posts_for_cards` prefetches the top-level author's user, gym, disciplines,
    workout/meal rows -- but used to stop there. `RepostedPostSerializer` reads
    the same chain off `repost_of`, so a feed of reposts paid for it a row at a
    time: each one its author's user and gym (`select_related` misses), plus
    disciplines, prompts and social links, plus the reposted workout's
    exercises or meal's entries (`prefetch_related` misses). A page of fifty
    reposts was several hundred extra queries for a page that was never asked
    to be unusable.
    """

    URL = "/api/v1/social/posts/"

    def setUp(self):
        _, self.reader, self.reader_token = self.create_account("reader")
        self.authenticate(self.reader_token)
        self._n = 0

    def add_repost(self):
        """One more repost in the feed, of a fresh original by a fresh author.

        The original author carries a full public profile -- a gym, a
        discipline, a prompt, a social link -- so the nested author in a
        repost's payload exercises every relation `PublicRepbaseUserSerializer`
        reads, not just the ones that happen to already be joined.
        """
        self._n += 1
        suffix = self._n

        _, original_author, _ = self.create_account(f"original{suffix}")
        original_author.is_profile_public = True
        original_author.save(update_fields=["is_profile_public"])
        UserDiscipline.objects.create(
            profile=original_author, discipline="powerlifting"
        )
        ProfilePrompt.objects.create(
            owner=original_author,
            question=ProfilePrompt.Question.WHY_I_TRAIN,
            answer="Because it is fun",
        )
        ProfileSocialLink.objects.create(
            owner=original_author,
            platform=ProfileSocialLink.Platform.INSTAGRAM,
            url="https://instagram.com/original",
        )

        original = Post.objects.create(
            author=original_author,
            kind=Post.Kind.MEAL,
            caption=f"original {suffix}",
        )
        PostMeal.objects.create(
            post=original, name="Meal", date=timezone.localdate()
        )

        _, reposter, _ = self.create_account(f"reposter{suffix}")
        Post.objects.create(
            author=reposter, kind=Post.Kind.REPOST, repost_of=original
        )

    #: The tables `posts_for_cards` is responsible for loading once rather
    #: than once per row. Not the whole response: a repost's nested author
    #: also pays its own, unrelated per-row cost for the follower lookups
    #: behind `viewer_follows`/`viewer_has_requested`, because `get_repost_of`
    #: builds a fresh `RepostedPostSerializer` -- and so a fresh nested author
    #: field with nothing memoised -- for every row. That is a real cost, and
    #: it is not this one: it is a property of how a repost is serialized, not
    #: of what `posts_for_cards` fetches, and fixing it would mean changing
    #: `get_repost_of` itself rather than adding a prefetch. Scoping the
    #: assertion to these four tables is what keeps this test about the
    #: prefetch and not about that separate, pre-existing cost.
    PREFETCHED_TABLES = (
        "core_userdiscipline",
        "core_profileprompt",
        "core_profilesociallink",
        "core_postmealentry",
    )

    def _prefetched_query_count(self, captured_queries):
        return sum(
            1
            for query in captured_queries
            if any(table in query["sql"] for table in self.PREFETCHED_TABLES)
        )

    def test_more_reposts_do_not_cost_more_queries(self):
        self.add_repost()
        with CaptureQueriesContext(connection) as first:
            response = self.client.get(self.URL)
        self.assertEqual(response.status_code, 200, response.data)
        first_count = self._prefetched_query_count(first.captured_queries)

        for _ in range(4):
            self.add_repost()
        with CaptureQueriesContext(connection) as second:
            response = self.client.get(self.URL)
        self.assertEqual(response.status_code, 200, response.data)
        # Five reposts and the five originals they point at: both are public
        # posts by distinct authors, so both are in the default list.
        self.assertEqual(len(response.data["results"]), 10)

        self.assertEqual(
            self._prefetched_query_count(second.captured_queries),
            first_count,
            "the reposted author's disciplines, prompts, social links or "
            "meal entries are no longer fully prefetched",
        )

    def test_the_reposted_author_still_renders_in_full(self):
        """The assertion that the saved queries did not cost the data."""
        self.add_repost()
        response = self.client.get(self.URL)
        self.assertEqual(response.status_code, 200, response.data)
        row = response.data["results"][0]
        original_author = row["repost_of"]["author"]
        self.assertEqual(original_author["username"], "original1")
        self.assertEqual(original_author["disciplines"], ["powerlifting"])
        self.assertEqual(len(original_author["prompts"]), 1)
        self.assertEqual(len(original_author["social_links"]), 1)
        self.assertEqual(row["repost_of"]["meal"]["name"], "Meal")
