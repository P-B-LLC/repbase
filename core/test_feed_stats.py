"""The per-post counts table: kept up to date by a job, read while fresh,
and never the only way to a ranking.

- the job writes what the ranking would have worked out, and the ranking
  reads it instead of counting;
- a later run catches what changed since, and old rows are refreshed --
  even after a run that stopped half way;
- a post deleted while the job counts it is left out, not a failed run;
- every row comes round in time, however big the window;
- a stale row, or one written by an older way of counting, is not read;
- with no job at all, the page is the same;
- the deploy check notices a job that has stopped.
"""

from datetime import timedelta
from unittest import mock

from django.core import checks
from django.core.cache import cache
from django.core.management import call_command
from django.db import connection
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from . import feed_stats, recommendations
from .fixtures_for_tests import make_account, meal_post
from .models import Post, PostComment, PostEngagementStats, PostLike, PostView
from .test_recommendations import ForYouTestCase


class FeedStatsTests(ForYouTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.post = self.meals[0]

    def counts_without_the_table(self, pk):
        cache.clear()
        return recommendations._count_engagement([pk])[pk]

    def test_the_job_writes_what_the_ranking_would_work_out(self):
        PostLike.objects.create(post=self.post, user=self.runners[0])
        PostComment.objects.create(post=self.post, author=self.runners[1], body="nice")
        call_command("refresh_feed_stats", stdout=open("/dev/null", "w"))
        row = PostEngagementStats.objects.get(post=self.post)
        expected = self.counts_without_the_table(self.post.pk)
        self.assertEqual((row.likes, row.comments, row.trusted_likes), (expected.likes, expected.comments, expected.trusted_likes))
        self.assertEqual(PostEngagementStats.objects.count(), len(self.running + self.lifting + self.meals))

    def test_the_ranking_reads_the_table_instead_of_counting(self):
        feed_stats.refresh()
        cache.clear()
        with CaptureQueriesContext(connection) as from_table:
            found = recommendations.engagement_for([post.pk for post in self.meals])
        self.assertEqual(set(found), {post.pk for post in self.meals})
        self.assertLessEqual(len(from_table), 2, "one read of the table, nothing counted")

    def test_a_later_run_catches_what_changed(self):
        feed_stats.refresh()
        PostLike.objects.create(post=self.post, user=self.runners[2])
        PostView.objects.create(viewer=self.runners[3], post=self.meals[1], view_count=1)
        summary = feed_stats.refresh()
        self.assertGreaterEqual(summary["active"], 2)
        self.assertFalse(summary["full"])
        self.assertEqual(PostEngagementStats.objects.get(post=self.post).likes, 1)

    def test_a_run_that_stopped_half_way_leaves_nothing_behind(self):
        start = timezone.now()
        feed_stats.refresh(now=start)
        first, second = sorted([self.meals[1], self.meals[2]], key=lambda post: post.pk)
        PostLike.objects.create(post=first, user=self.runners[0])
        PostLike.objects.create(post=second, user=self.runners[1])
        real = feed_stats._write

        def stops_at_the_second(chunk, now):
            if second.pk in chunk:
                raise RuntimeError("the job was stopped")
            return real(chunk, now)

        with mock.patch.object(feed_stats, "_write", stops_at_the_second), self.assertRaises(RuntimeError):
            feed_stats.refresh(now=start + timedelta(minutes=10), batch=1)
        self.assertEqual(PostEngagementStats.objects.get(post=first).likes, 1, "the first batch was written")
        # Ten minutes on, the newest row says a run happened then; it did
        # not finish, and the like on the second post is older than that.
        feed_stats.refresh(now=start + timedelta(minutes=11))
        self.assertEqual(PostEngagementStats.objects.get(post=second).likes, 1)

    def test_without_a_record_of_the_last_run_it_looks_back_further(self):
        start = timezone.now()
        feed_stats.refresh(now=start)
        PostLike.objects.create(post=self.post, user=self.runners[0])
        cache.clear()
        summary = feed_stats.refresh(now=start + timedelta(minutes=10))
        self.assertGreaterEqual(summary["active"], 1)
        self.assertEqual(PostEngagementStats.objects.get(post=self.post).likes, 1)

    def test_a_post_deleted_while_it_is_counted_is_left_out(self):
        doomed = meal_post(self.cooks[2])
        real = recommendations._count_engagement

        def count_then_delete(ids, now=None):
            counted = real(ids, now)
            Post.objects.filter(pk=doomed.pk).delete()
            return counted

        with mock.patch.object(recommendations, "_count_engagement", count_then_delete):
            summary = feed_stats.refresh()
        self.assertEqual(summary["deleted_while_counting"], 1)
        self.assertFalse(PostEngagementStats.objects.filter(post_id=doomed.pk).exists())
        self.assertTrue(PostEngagementStats.objects.filter(post=self.post).exists(), "the rest of the batch was written")

    def test_every_row_comes_round_in_time_whatever_the_window(self):
        feed_stats.refresh()
        size = Post.objects.exclude(kind=Post.Kind.REPOST).count()
        with mock.patch.object(feed_stats, "REFRESH_SECONDS", 3 * feed_stats.RUN_EVERY_SECONDS):
            PostEngagementStats.objects.update(computed_at=timezone.now() - timedelta(hours=1))
            summary = feed_stats.refresh(limit=1)
        # A third of the window a run, at least, when a row must come round
        # every three runs.
        self.assertGreaterEqual(summary["written"], size // 3)

    def test_old_rows_are_refreshed_oldest_first_and_new_posts_get_one(self):
        feed_stats.refresh()
        PostEngagementStats.objects.filter(post=self.post).update(
            computed_at=timezone.now() - timedelta(seconds=feed_stats.REFRESH_SECONDS + 60)
        )
        fresh_post = meal_post(self.cooks[2])
        feed_stats.refresh()
        self.assertTrue(PostEngagementStats.objects.filter(post=fresh_post).exists())
        row = PostEngagementStats.objects.get(post=self.post)
        self.assertGreater(row.computed_at, timezone.now() - timedelta(minutes=1))

    def test_stale_or_old_shaped_rows_are_never_read(self):
        feed_stats.refresh()
        PostLike.objects.create(post=self.post, user=self.runners[0])
        PostEngagementStats.objects.filter(post=self.post).update(
            computed_at=timezone.now() - timedelta(seconds=feed_stats.FRESH_SECONDS + 60)
        )
        PostEngagementStats.objects.filter(post=self.meals[1]).update(version=feed_stats.STATS_VERSION - 1)
        cache.clear()
        found = recommendations.engagement_for([self.post.pk, self.meals[1].pk])
        self.assertEqual(found[self.post.pk].likes, 1, "worked out afresh, not read from the stale row")

    def test_without_the_job_the_page_is_the_same(self):
        _, token = self.reader("same_either_way")
        without = self.ids(self.feed(token, page_size=10))
        feed_stats.refresh()
        cache.clear()
        _, token = self.reader("same_either_way_2")
        self.assertEqual(self.ids(self.feed(token, page_size=10)), without)

    def test_the_deploy_check_notices_a_job_that_stopped(self):
        def warnings():
            return [w.id for w in checks.run_checks(include_deployment_checks=True) if w.id == "core.W009"]

        self.assertEqual(warnings(), [], "no table yet is not a warning")
        feed_stats.refresh()
        self.assertEqual(warnings(), [])
        PostEngagementStats.objects.update(computed_at=timezone.now() - timedelta(hours=2))
        self.assertEqual(warnings(), ["core.W009"])


class FeedStatsCommitTests(TransactionTestCase):
    """The job as it runs for real: each batch commits on its own, which is
    where a broken foreign key would otherwise surface."""

    def test_a_post_deleted_under_a_committing_batch_is_left_out(self):
        cache.clear()
        _, cook, _ = make_account("committing_cook")
        posts = [meal_post(cook) for _ in range(3)]
        real = recommendations._count_engagement

        def count_then_delete(ids, now=None):
            counted = real(ids, now)
            Post.objects.filter(pk=posts[0].pk).delete()
            return counted

        with mock.patch.object(recommendations, "_count_engagement", count_then_delete):
            summary = feed_stats.refresh()
        self.assertEqual(summary["deleted_while_counting"], 1)
        self.assertEqual(
            set(PostEngagementStats.objects.values_list("post_id", flat=True)), {posts[1].pk, posts[2].pk}
        )
