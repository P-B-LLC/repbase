"""Buffered API metrics: nothing written per request, nothing counted twice.

`FakeRedis` implements the few commands the buffer uses, so the logic is covered
on every test job. `RealRedisTests` runs the same protocol against a real server
when TEST_REDIS_URL is set, which is where atomicity actually lives.
"""

import os
import threading
import uuid
from datetime import datetime, timedelta, timezone as datetime_timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock, skipUnless

from django.contrib.auth.models import User
from django.db import DatabaseError
from django.http import HttpResponse
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from . import metric_buffer
from .analytics import AnalyticsMiddleware, bucket_for
from .analytics_models import DailyActiveAccount, DailyApiMetric, MetricFlushReceipt, RecentApiMetric
from .checks import buffered_analytics_has_shared_redis


class FakeResponseError(Exception):
    pass


class FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.commands = []

    def __getattr__(self, name):
        def queue(*args, **kwargs):
            self.commands.append((name, args, kwargs))
            return self

        return queue

    def execute(self, raise_on_error=True):
        results = []
        with self.redis.lock:
            for name, args, kwargs in self.commands:
                try:
                    results.append(getattr(self.redis, name)(*args, **kwargs))
                except FakeResponseError as error:
                    if raise_on_error:
                        raise
                    results.append(error)
        return results


class FakeRedis:
    def __init__(self):
        self.store = {}
        self.lock = threading.RLock()

    def pipeline(self, transaction=True):
        return FakePipeline(self)

    def hincrby(self, key, field, amount):
        fields = self.store.setdefault(key, {})
        fields[field] = fields.get(field, 0) + amount

    def zadd(self, key, mapping, gt=False):
        members = self.store.setdefault(key, {})
        for member, score in mapping.items():
            if not gt or score > members.get(member, float("-inf")):
                members[member] = score

    def sadd(self, key, *members):
        self.store.setdefault(key, set()).update(members)

    def srem(self, key, *members):
        self.store.get(key, set()).difference_update(members)

    def expire(self, key, seconds):
        pass

    def rename(self, source, target):
        if source not in self.store:
            raise FakeResponseError("no such key")
        self.store[target] = self.store.pop(source)

    def hgetall(self, key):
        return dict(self.store.get(key, {}))

    def zrange(self, key, start, end, withscores=False):
        return list(self.store.get(key, {}).items())

    def smembers(self, key):
        return set(self.store.get(key, set()))

    def delete(self, *keys):
        for key in keys:
            self.store.pop(key, None)


NOON = datetime(2026, 11, 9, 12, 0, tzinfo=datetime_timezone.utc)


@override_settings(ANALYTICS_ENABLED=True, ANALYTICS_BUFFERED=True)
class Harness(TestCase):
    def setUp(self):
        metric_buffer.reset_process_state()
        self.redis = FakeRedis()
        patcher = mock.patch.object(metric_buffer, "_run", side_effect=lambda operation: operation(self.redis))
        self.run_patch = patcher.start()
        self.addCleanup(patcher.stop)
        self.user = User.objects.create_user("buffered", "buffered@example.com", "StrongPass!234")

    def record(self, status=200, ms=40, bucket=NOON, user=None, endpoint="planner-list"):
        metric_buffer.record(endpoint, "GET", status, ms, bucket, user)

    def daily(self, endpoint="planner-list"):
        return DailyApiMetric.objects.get(endpoint=endpoint)

    def recent(self, endpoint="planner-list"):
        return RecentApiMetric.objects.get(endpoint=endpoint)


class RecordingTests(Harness):
    def test_counts_reach_both_tables(self):
        self.record(status=200, ms=40)
        self.record(status=404, ms=10)
        self.record(status=503, ms=90)
        self.assertEqual(metric_buffer.flush()["applied"], 1)
        for row in (self.daily(), self.recent()):
            self.assertEqual((row.requests, row.client_errors, row.server_errors, row.total_ms, row.max_ms),
                             (3, 1, 1, 140, 90))

    def test_a_flush_adds_to_what_is_already_there(self):
        self.record(ms=50)
        metric_buffer.flush()
        self.record(ms=20)
        metric_buffer.flush()
        self.assertEqual((self.daily().requests, self.daily().total_ms, self.daily().max_ms), (2, 70, 50))

    def test_a_request_lands_in_its_own_bucket_however_late_the_flush(self):
        earlier = NOON - timedelta(minutes=10)
        self.record(bucket=earlier)
        self.record(bucket=NOON)
        metric_buffer.flush()
        self.assertEqual(
            sorted(RecentApiMetric.objects.values_list("bucket", "requests")), [(earlier, 1), (NOON, 1)]
        )
        self.assertEqual(self.daily().requests, 2)

    def test_a_request_either_side_of_midnight_counts_on_its_own_day(self):
        midnight = datetime(2026, 11, 10, 0, 0, tzinfo=datetime_timezone.utc)
        self.record(bucket=midnight - timedelta(minutes=5))
        self.record(bucket=midnight)
        metric_buffer.flush()
        self.assertEqual(sorted(DailyApiMetric.objects.values_list("day", "requests")),
                         [(midnight.date() - timedelta(days=1), 1), (midnight.date(), 1)])

    def test_the_maximum_is_the_highest_seen_in_any_order(self):
        for ms in (10, 500, 30):
            self.record(ms=ms)
        metric_buffer.flush()
        self.assertEqual(self.recent().max_ms, 500)

    def test_routes_with_the_separator_in_their_name_survive(self):
        self.record(endpoint="odd|name")
        metric_buffer.flush()
        self.assertEqual(self.daily("odd|name").requests, 1)


class FlushOnceTests(Harness):
    def test_nothing_buffered_writes_nothing(self):
        summary = metric_buffer.flush()
        self.assertEqual(summary["applied"], 0)
        self.assertFalse(MetricFlushReceipt.objects.exists())
        self.assertFalse(DailyApiMetric.objects.exists())

    def test_applying_the_same_batch_twice_adds_once(self):
        self.record()
        batch_id = uuid.uuid4().hex
        batch = {"routes": {(int(NOON.timestamp()), "GET", "planner-list"): {
            "requests": 5, "client_errors": 0, "server_errors": 0, "total_ms": 100, "max_ms": 30,
        }}, "active": set(), "lost": 0}
        self.assertTrue(metric_buffer.apply_batch(batch_id, batch))
        self.assertFalse(metric_buffer.apply_batch(batch_id, batch))
        self.assertEqual(self.daily().requests, 5)

    def test_a_flush_that_stops_after_committing_is_not_counted_again(self):
        self.record()
        with mock.patch.object(metric_buffer, "_discard", side_effect=OSError("the process died here")):
            with self.assertRaises(OSError):
                metric_buffer.flush()
        self.assertEqual(self.daily().requests, 1)
        summary = metric_buffer.flush()
        self.assertEqual((summary["applied"], summary["already_applied"]), (0, 1))
        self.assertEqual(self.daily().requests, 1)
        self.assertFalse(self.redis.smembers(metric_buffer._registry_key()), "the retry cleared the batch")

    def test_a_database_failure_keeps_the_batch_for_the_next_run(self):
        self.record()
        with mock.patch.object(metric_buffer, "apply_batch", side_effect=DatabaseError("down")):
            self.assertEqual(metric_buffer.flush()["failed"], 1)
        self.assertFalse(DailyApiMetric.objects.exists())
        self.assertEqual(metric_buffer.flush()["applied"], 1)
        self.assertEqual(self.daily().requests, 1)

    def test_requests_arriving_during_a_flush_go_into_the_next_batch(self):
        self.record(ms=10)
        real = metric_buffer.apply_batch

        def arrive_meanwhile(batch_id, batch):
            self.record(ms=99)
            return real(batch_id, batch)

        with mock.patch.object(metric_buffer, "apply_batch", side_effect=arrive_meanwhile):
            metric_buffer.flush()
        self.assertEqual(self.daily().requests, 1)
        metric_buffer.flush()
        self.assertEqual((self.daily().requests, self.daily().max_ms), (2, 99))

    def test_receipts_are_pruned_only_after_their_retention(self):
        kept = MetricFlushReceipt.objects.create(batch_id="kept")
        old = MetricFlushReceipt.objects.create(batch_id="old")
        now = timezone.now()
        MetricFlushReceipt.objects.filter(pk=old.pk).update(
            applied_at=now - timedelta(days=metric_buffer.RECEIPT_RETENTION_DAYS + 1)
        )
        metric_buffer.prune_receipts(now)
        self.assertEqual(list(MetricFlushReceipt.objects.all()), [kept])

    def test_a_flush_cannot_outlive_its_receipt(self):
        self.assertLess(metric_buffer.BUFFER_TTL_SECONDS, metric_buffer.RECEIPT_RETENTION_DAYS * 86400)


class ActiveAccountTests(Harness):
    def test_marks_coalesce_by_account_and_day(self):
        for _ in range(5):
            self.record(user=self.user.pk)
        metric_buffer.flush()
        self.record(user=self.user.pk)
        metric_buffer.flush()
        self.assertEqual(DailyActiveAccount.objects.count(), 1)

    def test_a_process_stops_sending_a_mark_it_has_already_sent(self):
        self.record(user=self.user.pk)
        commands = []
        real = FakePipeline.execute

        def spy(pipeline, raise_on_error=True):
            commands.extend(name for name, _, _ in pipeline.commands)
            return real(pipeline, raise_on_error)

        with mock.patch.object(FakePipeline, "execute", spy):
            self.record(user=self.user.pk)
        self.assertNotIn("sadd", commands)

    def test_a_new_day_marks_again(self):
        tomorrow = NOON + timedelta(days=1)
        self.record(user=self.user.pk)
        self.record(user=self.user.pk, bucket=tomorrow)
        metric_buffer.flush()
        self.assertEqual(sorted(DailyActiveAccount.objects.values_list("day", flat=True)),
                         [NOON.date(), tomorrow.date()])

    def test_an_account_deleted_before_the_flush_is_left_out(self):
        self.record(user=self.user.pk)
        self.user.delete()
        self.assertEqual(metric_buffer.flush()["applied"], 1)
        self.assertFalse(DailyActiveAccount.objects.exists())

    def test_an_account_deleted_after_the_flush_takes_its_row_with_it(self):
        self.record(user=self.user.pk)
        metric_buffer.flush()
        self.user.delete()
        self.assertFalse(DailyActiveAccount.objects.exists())


class FailureTests(Harness):
    def down(self):
        self.run_patch.side_effect = lambda operation: metric_buffer._unavailable

    def up(self):
        self.run_patch.side_effect = lambda operation: operation(self.redis)

    def test_record_never_raises(self):
        self.run_patch.side_effect = RuntimeError("anything at all")
        self.record()

    def test_a_lost_observation_is_counted_and_reported_with_the_next_batch(self):
        self.down()
        self.record()
        self.record()
        self.up()
        self.record()
        summary = metric_buffer.flush()
        self.assertEqual(summary["lost"], 2)
        self.assertEqual(MetricFlushReceipt.objects.get().lost_observations, 2)
        self.assertEqual(self.daily().requests, 1)

    def test_losing_observations_is_logged(self):
        self.down()
        with self.assertLogs("core.metric_buffer", level="WARNING") as logs:
            self.record()
        self.assertIn("lost", logs.output[0])

    def test_a_mark_that_could_not_be_sent_is_sent_again(self):
        self.down()
        self.record(user=self.user.pk)
        self.up()
        self.record(user=self.user.pk)
        metric_buffer.flush()
        self.assertEqual(DailyActiveAccount.objects.count(), 1)

    def test_a_flush_with_redis_down_changes_nothing(self):
        self.record()
        self.down()
        self.assertFalse(metric_buffer.flush()["redis_available"])
        self.up()
        self.assertEqual(metric_buffer.flush()["applied"], 1)


class MiddlewareTests(Harness):
    def request(self, user=None, status=200, view=None):
        factory = RequestFactory().generic("GET", "/api/v1/planner/1/")
        factory.resolver_match = SimpleNamespace(view_name="planner-detail")
        factory.user = user or self.user
        return AnalyticsMiddleware(view or (lambda request: HttpResponse(status=status)))(factory)

    def test_ordinary_requests_write_no_sql_to_shared_rows(self):
        with self.assertNumQueries(0):
            for _ in range(20):
                self.request()
        self.assertFalse(DailyApiMetric.objects.exists())
        self.assertFalse(RecentApiMetric.objects.exists())

    def test_the_flush_is_what_reaches_the_tables(self):
        for _ in range(3):
            self.request()
        metric_buffer.flush()
        self.assertEqual(DailyApiMetric.objects.get().requests, 3)
        live = RecentApiMetric.objects.get()
        self.assertEqual(live.bucket, bucket_for(timezone.now()))
        self.assertEqual(DailyActiveAccount.objects.count(), 1)

    def test_a_server_error_is_still_counted(self):
        self.request(status=500)
        metric_buffer.flush()
        self.assertEqual(DailyApiMetric.objects.get().server_errors, 1)

    def test_staff_and_failures_are_not_active_accounts(self):
        staff = User.objects.create_user("staffer", is_staff=True)
        self.request(user=staff)
        self.request(status=404)
        metric_buffer.flush()
        self.assertFalse(DailyActiveAccount.objects.exists())

    def test_a_buffer_failure_never_changes_the_response(self):
        self.run_patch.side_effect = RuntimeError("anything at all")
        self.assertEqual(self.request(status=201).status_code, 201)

    def test_an_unreadable_account_never_changes_the_response(self):
        class Broken:
            @property
            def is_authenticated(self):
                raise DatabaseError("connection went away")

        self.assertEqual(self.request(user=Broken(), status=201).status_code, 201)

    @override_settings(ANALYTICS_BUFFERED=False)
    def test_with_the_flag_off_requests_write_to_sql_as_before(self):
        self.request()
        self.assertEqual(DailyApiMetric.objects.get().requests, 1)
        self.assertFalse(self.redis.store)

    @override_settings(ANALYTICS_BUFFERED=False)
    def test_with_the_flag_off_any_failure_is_contained(self):
        from django.db import InterfaceError

        with mock.patch("core.analytics.transaction.atomic", side_effect=InterfaceError("connection closed")):
            self.assertEqual(self.request(status=201).status_code, 201)

    @override_settings(ANALYTICS_ENABLED=False)
    def test_collection_off_buffers_nothing(self):
        self.request()
        self.assertFalse(self.redis.store)


class ConfigurationTests(TestCase):
    @override_settings(ANALYTICS_ENABLED=True, ANALYTICS_BUFFERED=True)
    def test_buffering_without_redis_is_an_error(self):
        self.assertEqual([error.id for error in buffered_analytics_has_shared_redis(None)], ["core.E014"])

    @override_settings(ANALYTICS_ENABLED=True, ANALYTICS_BUFFERED=False)
    def test_the_flag_off_needs_no_redis(self):
        self.assertEqual(buffered_analytics_has_shared_redis(None), [])

    def test_buffering_is_off_unless_asked_for(self):
        from django.conf import settings

        self.assertFalse(settings.ANALYTICS_BUFFERED)


@skipUnless(os.environ.get("TEST_REDIS_URL"), "needs TEST_REDIS_URL")
@override_settings(ANALYTICS_ENABLED=True, ANALYTICS_BUFFERED=True)
class RealRedisTests(TestCase):
    """The same protocol against a real server: its atomicity is the point."""

    def setUp(self):
        self.prefix = f"test-{uuid.uuid4().hex}"
        settings_override = override_settings(CACHES={"default": {
            "BACKEND": "core.resilient_cache.ResilientRedisCache",
            "LOCATION": os.environ["TEST_REDIS_URL"],
            "KEY_PREFIX": self.prefix,
            "OPTIONS": {"socket_connect_timeout": 1, "socket_timeout": 1},
        }})
        settings_override.enable()
        self.addCleanup(settings_override.disable)
        metric_buffer.reset_process_state()
        from django.core.cache import caches

        self.client_redis = caches["default"]._cache.get_client(write=True)
        self.addCleanup(self.empty)

    def empty(self):
        keys = list(self.client_redis.scan_iter(f"{self.prefix}:*"))
        if keys:
            self.client_redis.delete(*keys)

    def record(self, ms=10):
        metric_buffer.record("planner-list", "GET", 200, ms, NOON)

    def test_a_full_round_trip(self):
        self.record(ms=30)
        self.record(ms=70)
        self.assertEqual(metric_buffer.flush()["applied"], 1)
        row = DailyApiMetric.objects.get()
        self.assertEqual((row.requests, row.total_ms, row.max_ms), (2, 100, 70))
        self.assertEqual(self.client_redis.smembers(metric_buffer._registry_key()), set())

    def test_concurrent_writers_lose_nothing(self):
        def write():
            for _ in range(100):
                self.record(ms=5)

        threads = [threading.Thread(target=write) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        metric_buffer.flush()
        self.assertEqual(DailyApiMetric.objects.get().requests, 800)

    def test_the_maximum_only_rises_on_a_real_server(self):
        for ms in (10, 500, 30):
            self.record(ms=ms)
        metric_buffer.flush()
        self.assertEqual(DailyApiMetric.objects.get().max_ms, 500)

    def test_a_retried_claim_is_not_applied_twice(self):
        self.record()
        # The cache treats this as an outage, so the flush carries on and the
        # batch stays behind, exactly as it would after losing the connection.
        with mock.patch.object(metric_buffer, "_discard", side_effect=OSError("stopped here")):
            self.assertEqual(metric_buffer.flush()["applied"], 1)
        self.assertEqual(metric_buffer.flush()["already_applied"], 1)
        self.assertEqual(DailyApiMetric.objects.get().requests, 1)
        self.assertEqual(self.client_redis.smembers(metric_buffer._registry_key()), set())
