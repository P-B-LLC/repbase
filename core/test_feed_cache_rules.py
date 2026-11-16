"""Cached relevance can decide an order. It can never decide who sees what.

Sessions, shared counts and statistics rows hold ids and figures. Every rule
below has to win over whatever those say: blocks, private posts, moderation and
which account is asking.
"""

from unittest import mock
from urllib.parse import parse_qs, urlsplit

from django.core.cache import cache
from django.utils import timezone

from . import feed_stats, recommendations
from .models import Block, Post, PostEngagementStats
from .test_recommendations import ForYouTestCase


def cursor_of(response):
    return parse_qs(urlsplit(response.data["next"]).query)["cursor"][0]


class ReaderKeyTests(ForYouTestCase):
    def test_keys_belong_to_one_account(self):
        for key in (recommendations._session_key, lambda pk, _: recommendations._served_key(pk),
                    lambda pk, _: recommendations._features_key(pk)):
            self.assertNotEqual(key(1, "abc"), key(2, "abc"))
            self.assertIn(":1:", key(1, "abc"))

    def test_a_new_ranking_version_shares_nothing_with_the_last(self):
        before = recommendations._session_key(1, "abc"), recommendations._served_key(1), recommendations._features_key(1)
        with mock.patch.object(recommendations, "RANKING_VERSION", recommendations.RANKING_VERSION + 1):
            after = recommendations._session_key(1, "abc"), recommendations._served_key(1), recommendations._features_key(1)
        self.assertFalse(set(before) & set(after))

    def test_keys_have_a_place_for_language(self):
        self.assertIn(":all:", recommendations._session_key(1, "abc"))


class VisibilityBeatsTheCacheTests(ForYouTestCase):
    def begin(self, name="scroller"):
        profile, token = self.reader(name, ["Running"])
        first = self.feed(token, page_size=4)
        session_id, offset = recommendations.read_cursor(cursor_of(first))
        session = recommendations.load_session(profile, session_id)
        return profile, token, first, session_id, offset, session

    def retry_first_page(self, session_id):
        """Asks for what the session has already served, straight from the cache."""
        return self.client.get("/api/v1/social/for-you/", {
            "cursor": recommendations.make_cursor(session_id, 0), "page_size": 4,
        })

    def authors_of(self, post_ids):
        return dict(Post.objects.filter(pk__in=post_ids).values_list("pk", "author_id"))

    def test_a_block_removes_posts_the_session_already_served(self):
        profile, _, first, session_id, _, _ = self.begin()
        served = self.ids(first)
        Block.objects.create(blocker=profile, blocked_id=self.authors_of(served)[served[0]])
        again = self.ids(self.retry_first_page(session_id))
        self.assertNotIn(served[0], again)

    def test_being_blocked_removes_them_too(self):
        profile, _, first, session_id, _, _ = self.begin("blocked_scroller")
        served = self.ids(first)
        Block.objects.create(blocker_id=self.authors_of(served)[served[0]], blocked=profile)
        self.assertNotIn(served[0], self.ids(self.retry_first_page(session_id)))

    def test_a_block_removes_the_author_from_what_the_session_has_queued(self):
        profile, _, first, _, _, session = self.begin("queue_scroller")
        authors = self.authors_of(session.pool)
        blocked = authors[session.pool[0]]
        Block.objects.create(blocker=profile, blocked_id=blocked)
        later = self.ids(self.follow_next(first))
        self.assertTrue(later)
        self.assertNotIn(blocked, self.authors_of(later).values())

    def test_a_post_made_private_leaves_a_served_page(self):
        _, _, first, session_id, _, _ = self.begin("private_scroller")
        served = self.ids(first)
        Post.objects.filter(pk=served[0]).update(visibility=Post.Visibility.PRIVATE)
        self.assertNotIn(served[0], self.ids(self.retry_first_page(session_id)))

    def test_a_post_made_private_leaves_the_queue(self):
        _, _, first, _, _, session = self.begin("private_queue")
        Post.objects.filter(pk=session.pool[0]).update(visibility=Post.Visibility.PRIVATE)
        self.assertNotIn(session.pool[0], self.ids(self.follow_next(first)))

    def test_a_moderator_hiding_a_post_overrides_the_session(self):
        _, _, first, session_id, _, _ = self.begin("moderated_scroller")
        served = self.ids(first)
        Post.objects.filter(pk=served[0]).update(is_hidden=True, hidden_at=timezone.now())
        self.assertNotIn(served[0], self.ids(self.retry_first_page(session_id)))

    def test_fresh_statistics_cannot_bring_back_a_blocked_author(self):
        feed_stats.refresh()
        profile, token = self.reader("stats_scroller", ["Running"])
        target = self.running[0]
        PostEngagementStats.objects.filter(post=target).update(likes=10000, impressions=10000)
        cache.clear()
        Block.objects.create(blocker=profile, blocked=target.author)
        self.assertNotIn(target.pk, self.ids(self.feed(token, page_size=20)))

    def test_stale_statistics_cannot_bring_back_a_hidden_post(self):
        feed_stats.refresh()
        _, token = self.reader("stale_scroller", ["Running"])
        target = self.running[0]
        PostEngagementStats.objects.update(computed_at=timezone.now() - timezone.timedelta(hours=2))
        Post.objects.filter(pk=target.pk).update(is_hidden=True, hidden_at=timezone.now())
        cache.clear()
        self.assertNotIn(target.pk, self.ids(self.feed(token, page_size=20)))


class AccountSwitchingTests(ForYouTestCase):
    def test_a_session_opens_for_nobody_else(self):
        owner, token = self.reader("first_account", ["Running"])
        first = self.feed(token, page_size=4)
        session_id, _ = recommendations.read_cursor(cursor_of(first))
        other, _ = self.reader("second_account", ["Strength"])
        self.assertIsNone(recommendations.load_session(other, session_id))
        self.assertIsNotNone(recommendations.load_session(owner, session_id))

    def test_what_one_account_was_served_is_not_read_for_another(self):
        owner, token = self.reader("served_first", ["Running"])
        self.feed(token, page_size=4)
        other, _ = self.reader("served_second", ["Strength"])
        self.assertTrue(recommendations.served_reasons(owner))
        self.assertEqual(recommendations.served_reasons(other), {})

    def test_following_a_cursor_as_someone_else_starts_their_own_page(self):
        _, token = self.reader("cursor_owner", ["Running"])
        first = self.feed(token, page_size=4)
        _, other_token = self.reader("cursor_borrower", ["Strength"])
        self.authenticate(other_token)
        borrowed = self.follow_next(first)
        self.assertEqual(borrowed.status_code, 200)
        fresh = self.feed(other_token, page_size=4)
        self.assertEqual(self.ids(borrowed), self.ids(fresh))
