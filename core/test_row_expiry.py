"""Two tables that would otherwise only ever grow.

The interesting case is not that old rows go. It is that the food cache has
two clocks and they are far apart on purpose: a stale entry is still the
answer served when FoodData Central cannot be reached, so deleting on
staleness would remove the outage fallback and nothing would say so until an
outage.
"""

from datetime import timedelta
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from io import StringIO
from rest_framework.test import APITestCase

from .food_sources import FoodSourceUnavailable
from .models import FoodSearchCache, SaveReceipt
from .tests import RepbaseAPITestMixin


def aged(instance, field, delta):
    """Move a row's clock back past an auto_now field, which ignores saves."""
    type(instance).objects.filter(pk=instance.pk).update(
        **{field: timezone.now() - delta}
    )
    instance.refresh_from_db()
    return instance


class TheFoodCacheKeepsItsFallbackTests(TestCase):
    def entry(self, age):
        entry = FoodSearchCache.objects.create(term="oats", payload=[{"n": 1}])
        return aged(entry, "fetched_at", age)

    def test_a_stale_entry_survives_pruning(self):
        """The one that matters. Past LIFETIME the entry is re-fetched rather
        than trusted, but it is still what an outage falls back to."""
        self.entry(FoodSearchCache.LIFETIME + timedelta(days=1))
        self.assertEqual(FoodSearchCache.prune(), 0)
        self.assertTrue(FoodSearchCache.objects.filter(term="oats").exists())

    def test_an_entry_nobody_has_wanted_in_months_goes(self):
        self.entry(FoodSearchCache.RETENTION + timedelta(days=1))
        self.assertEqual(FoodSearchCache.prune(), 1)
        self.assertFalse(FoodSearchCache.objects.exists())

    def test_a_fresh_entry_is_left_alone(self):
        self.entry(timedelta(hours=1))
        self.assertEqual(FoodSearchCache.prune(), 0)

    def test_the_two_clocks_are_not_the_same_clock(self):
        """A guard on the mistake this design exists to avoid: pruning on
        LIFETIME would have looked correct and deleted the fallback."""
        self.assertGreater(FoodSearchCache.RETENTION, FoodSearchCache.LIFETIME)


class SearchingStillFallsBackAfterPruningTests(RepbaseAPITestMixin, APITestCase):
    """End to end, because the fallback is the reason for the whole design."""

    def setUp(self):
        _, _, token = self.create_account("hungry")
        self.authenticate(token)

    def test_an_outage_serves_the_stale_entry_a_prune_left_behind(self):
        entry = FoodSearchCache.objects.create(
            term="oats", payload=[{"description": "Oats, rolled"}]
        )
        aged(entry, "fetched_at", FoodSearchCache.LIFETIME + timedelta(days=1))
        FoodSearchCache.prune()

        with mock.patch(
            "core.views.food_sources.search",
            side_effect=FoodSourceUnavailable("down"),
        ):
            response = self.client.get("/api/v1/food/search/?q=oats")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data[0]["description"], "Oats, rolled")


class ReceiptsStopReplayingEventuallyTests(RepbaseAPITestMixin, APITestCase):
    def receipt(self, age):
        _, profile, _ = self.create_account("lifter")
        made = SaveReceipt.objects.create(
            owner=profile,
            key="11111111-1111-1111-1111-111111111111",
            path="/api/v1/sessions/",
            request_hash="x" * 64,
            response={"id": 1},
        )
        return aged(made, "created_at", age)

    def test_a_receipt_inside_the_window_is_kept(self):
        self.receipt(SaveReceipt.RETENTION - timedelta(days=1))
        self.assertEqual(SaveReceipt.prune(), 0)

    def test_a_receipt_past_the_window_goes(self):
        self.receipt(SaveReceipt.RETENTION + timedelta(days=1))
        self.assertEqual(SaveReceipt.prune(), 1)
        self.assertFalse(SaveReceipt.objects.exists())


class TheCommandTests(TestCase):
    def test_it_runs_on_an_empty_database(self):
        out = StringIO()
        call_command("prune_expired_rows", stdout=out)
        self.assertIn("deleted 0 save receipts", out.getvalue())

    def test_dry_run_counts_without_deleting(self):
        entry = FoodSearchCache.objects.create(term="oats", payload=[])
        aged(entry, "fetched_at", FoodSearchCache.RETENTION + timedelta(days=1))

        out = StringIO()
        call_command("prune_expired_rows", "--dry-run", stdout=out)
        self.assertIn("would delete 1 food search cache entries", out.getvalue())
        self.assertTrue(FoodSearchCache.objects.exists())
