"""A Redis outage degrades the feed; it never reaches anybody's saved work."""

from datetime import date
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import SimpleTestCase, override_settings
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from . import resilient_cache
from .models import FoodMeal, PlannerEntry, RepbaseUser, WorkoutSession
from .resilient_cache import CircuitBreaker, ResilientRedisCache


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


class CircuitBreakerTests(SimpleTestCase):
    def setUp(self):
        self.clock = Clock()
        self.breaker = CircuitBreaker(failures=3, cooldown=5, clock=self.clock)

    def fail(self, times):
        for _ in range(times):
            self.breaker.failed()

    def test_closed_until_enough_failures_in_a_row(self):
        self.fail(2)
        self.assertTrue(self.breaker.allow())
        self.breaker.succeeded()
        self.fail(2)
        self.assertTrue(self.breaker.allow(), "the count restarts after a success")

    def test_open_keeps_calls_off_the_network(self):
        self.fail(3)
        self.assertFalse(self.breaker.allow())

    def test_only_one_call_probes_after_the_cooldown(self):
        self.fail(3)
        self.clock.now += 5
        self.assertTrue(self.breaker.allow())
        self.assertFalse(self.breaker.allow())

    def test_a_successful_probe_closes_it(self):
        self.fail(3)
        self.clock.now += 5
        self.breaker.allow()
        self.breaker.succeeded()
        self.assertTrue(self.breaker.allow())
        self.assertTrue(self.breaker.allow())

    def test_a_failed_probe_waits_out_another_cooldown(self):
        self.fail(3)
        self.clock.now += 5
        self.breaker.allow()
        self.breaker.failed()
        self.clock.now += 4
        self.assertFalse(self.breaker.allow())
        self.clock.now += 1
        self.assertTrue(self.breaker.allow())


class UnreachableClient:
    """Stands in for redis-py against a Redis that does not answer."""

    attempts = 0

    def __init__(self, *args, **kwargs):
        pass

    def __getattr__(self, name):
        def unreachable(*args, **kwargs):
            type(self).attempts += 1
            raise ConnectionError("Redis is down")

        return unreachable

    def get_client(self, *args, **kwargs):
        return self


class DownCache(ResilientRedisCache):
    def __init__(self, server, params):
        super().__init__(server, params)
        self._class = UnreachableClient


DOWN = {"default": {"BACKEND": "core.test_resilient_cache.DownCache", "LOCATION": "redis://down.invalid:6379/0"}}


class DownMixin:
    def setUp(self):
        resilient_cache.reset_breakers()
        UnreachableClient.attempts = 0
        super().setUp()

    def tearDown(self):
        super().tearDown()
        resilient_cache.reset_breakers()


@override_settings(CACHES=DOWN)
class EveryOperationIsAMissTests(DownMixin, SimpleTestCase):
    def cache(self):
        return DownCache("redis://down.invalid:6379/0", {})

    def test_operations_answer_as_a_miss_would(self):
        cache = self.cache()
        self.assertEqual(cache.get("key", "fallback"), "fallback")
        self.assertFalse(cache.add("key", 1))
        self.assertIsNone(cache.set("key", 1))
        self.assertFalse(cache.touch("key"))
        self.assertFalse(cache.delete("key"))
        self.assertEqual(cache.get_many(["a", "b"]), {})
        self.assertFalse(cache.has_key("key"))
        self.assertEqual(cache.set_many({"a": 1, "b": 2}), ["a", "b"])
        self.assertIsNone(cache.delete_many(["a"]))
        self.assertFalse(cache.clear())
        self.assertFalse(cache.is_available())

    def test_incr_raises_what_a_missing_key_raises(self):
        with self.assertRaises(ValueError):
            self.cache().incr("key")

    def test_a_programming_error_is_not_mistaken_for_an_outage(self):
        cache = self.cache()
        with mock.patch.object(UnreachableClient, "get", side_effect=TypeError("bug"), create=True):
            with self.assertRaises(TypeError):
                cache.get("key")

    def test_the_network_stops_being_tried(self):
        cache = self.cache()
        for _ in range(50):
            cache.get("key")
        self.assertEqual(UnreachableClient.attempts, resilient_cache.BREAKER_FAILURES)


@override_settings(CACHES=DOWN, ANALYTICS_ENABLED=False)
class SavesDuringAnOutageTests(DownMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user("outage", "outage@example.com", "StrongPass!234",
                                              first_name="O", last_name="U")
        self.profile = RepbaseUser.objects.create(user=self.user, height_cm=180, weight_kg=Decimal("80.00"))
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.create(user=self.user).key}")

    def test_a_workout_session_is_saved(self):
        self.assertEqual(self.client.post("/api/v1/sessions/", {}, format="json").status_code, 201)
        self.assertEqual(WorkoutSession.objects.filter(repbase_user=self.profile).count(), 1)

    def test_a_planner_task_is_saved(self):
        response = self.client.post("/api/v1/planner/", {
            "kind": "task", "title": "Stretch", "category": "habit", "scheduled_date": str(date.today()),
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(PlannerEntry.objects.filter(owner=self.profile).count(), 1)

    def test_a_meal_is_saved(self):
        response = self.client.post("/api/v1/food/meals/", {"name": "Lunch", "date": str(date.today())}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(FoodMeal.objects.filter(owner=self.profile).count(), 1)

    def test_saves_stop_touching_the_network_once_the_breaker_is_open(self):
        for _ in range(10):
            self.assertEqual(self.client.post("/api/v1/sessions/", {}, format="json").status_code, 201)
        self.assertEqual(UnreachableClient.attempts, resilient_cache.BREAKER_FAILURES)
