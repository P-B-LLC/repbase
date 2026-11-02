"""No network: bounded modern discovery, authoritative details and shared gates."""
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
from threading import Barrier
import time
from unittest import mock
from unittest import skipUnless

from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from . import food_sources as off
from .models import FoodSearchCache
from .test_open_food_facts import product
from .tests import RepbaseAPITestMixin


def candidate(code="3017620422003", name="Rice", **extra):
    return {"code": code, "product_name": name, "brands": "Brand", **extra}


class FoodDiscoveryTests(TestCase):
    def test_candidates_never_claim_nutrition_or_basis(self):
        raw = candidate(nutriments={"energy-kcal_100g": 200})
        with mock.patch.object(off, "_request", return_value={"hits": [raw, raw, {}, None, candidate(code="bad")]}) as upstream:
            rows = off.discover("rice")
        self.assertEqual(rows, [{"source_id": "off:3017620422003", "name": "Rice", "brand": "Brand"}])
        self.assertEqual(upstream.call_args.args[0], "/search")
        self.assertEqual(upstream.call_args.args[2], "discover")

    def test_plain_text_is_escaped_not_an_operator_query(self):
        with mock.patch.object(off, "_request", return_value={"hits": []}) as upstream:
            off.discover('name:rice OR * AND (x) "quote"')
        query = upstream.call_args.args[1]["q"]
        self.assertFalse(query.startswith('"'))
        self.assertIn(r"name\:rice", query)
        self.assertIn(r"\*", query)
        self.assertIn(r"\(x\)", query)
        self.assertIn(r'\"quote\"', query)
        self.assertIn(r"\OR", query)
        self.assertEqual(off._literal_query("rice"), "rice")

    def test_discovery_is_bounded_and_upc_leading_zero_is_preserved(self):
        with mock.patch.object(off, "_request", return_value={"hits": [candidate(code="034000470693")]}) as upstream:
            rows = off.discover("rice", limit=100)
        self.assertEqual(rows[0]["source_id"], "off:0034000470693")
        self.assertEqual(upstream.call_args.args[1]["page_size"], 25)

    def test_incomplete_search_is_not_a_false_empty_result(self):
        for payload in ({}, {"hits": {}}, {"hits": [], "timed_out": True}, {"hits": [], "error": "down"}):
            with self.subTest(payload=payload), mock.patch.object(off, "_request", return_value=payload):
                with self.assertRaises(off.FoodSourceUnavailable):
                    off.discover("rice")

    def test_discovery_origin_is_fixed_and_redirects_are_refused(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read1.side_effect = [b'{"hits":[]}', b""]
        with mock.patch.object(off.urllib.request, "build_opener") as opener:
            opener.return_value.open.return_value = response
            off.discover("rice")
        request = opener.return_value.open.call_args.args[0]
        self.assertTrue(request.full_url.startswith("https://search.openfoodfacts.org/search?"))
        self.assertIs(opener.call_args.args[0], off._NoRedirect)
        self.assertIsNone(off._NoRedirect().redirect_request(None, None, None, None, None, None))


class FoodWindowAndDeadlineTests(TestCase):
    def test_sequential_searches_do_not_artificially_fail_for_seven_seconds(self):
        for _ in range(10):
            off._reserve_slot("discover")
        with self.assertRaises(off.FoodSourceUnavailable) as caught:
            off._reserve_slot("search")
        self.assertGreater(caught.exception.retry_after, 0)
        self.assertLessEqual(caught.exception.retry_after, 60)
        for _ in range(15):
            off._reserve_slot("barcode")
        with self.assertRaises(off.FoodSourceUnavailable):
            off._reserve_slot("barcode")

    def test_capacity_returns_after_rolling_minute(self):
        now = timezone.now()
        with mock.patch.object(off.timezone, "now", return_value=now):
            for _ in range(10):
                off._reserve_slot("search")
        with mock.patch.object(off.timezone, "now", return_value=now + timedelta(seconds=61)):
            off._reserve_slot("search")

    def test_legacy_retry_after_does_not_block_distinct_modern_service(self):
        off._reserve_slot("search")
        off._defer_gate("search", 120)
        with self.assertRaises(off.FoodSourceUnavailable) as caught:
            off._reserve_slot("search")
        self.assertGreaterEqual(caught.exception.retry_after, 119)
        off._reserve_slot("discover")

    def test_provider_rate_limit_uses_shared_cooldown(self):
        error = off.urllib.error.HTTPError("https://search.openfoodfacts.org", 429, "limited", {"Retry-After": "90"}, None)
        with mock.patch.object(off.urllib.request, "build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaises(off.FoodSourceUnavailable) as caught:
                off.discover("rice")
            self.assertEqual(caught.exception.retry_after, 90)
            with self.assertRaises(off.FoodSourceUnavailable):
                off.discover("oats")
            self.assertEqual(opener.return_value.open.call_count, 1)

    def test_retry_after_is_bounded_and_malformed_values_are_not_trusted(self):
        self.assertEqual(off._retry_after({"Retry-After": "9000"}), 300)
        self.assertIsNone(off._retry_after({"Retry-After": "garbage"}))
        self.assertIsNone(off._retry_after({"Retry-After": "NaN"}))

    def test_cooldown_storage_failure_is_unavailable_not_server_error(self):
        with mock.patch.object(FoodSearchCache.objects, "select_for_update", side_effect=RuntimeError("database unavailable")):
            with self.assertRaises(off.FoodSourceUnavailable):
                off._defer_gate("discover", 20)

    def test_body_reader_checks_whole_deadline_between_chunks(self):
        response = mock.MagicMock()
        response.read1.return_value = b"x"
        with mock.patch.object(off.time, "monotonic", side_effect=[1, 3]):
            with self.assertRaises(TimeoutError):
                off._read_body(response, deadline=2)
        response.read1.assert_called_once()


class FoodCacheAndFallbackTests(TestCase):
    def test_published_answer_wins_a_released_lease_race(self):
        rows = [{"source_id": "off:3017620422003", "name": "Rice", "brand": ""}]
        def finish_other_lookup(key):
            FoodSearchCache.objects.create(term=key, payload=rows)
            raise off.FoodSourceUnavailable("Lease was released", 1)
        with mock.patch.object(off, "_lease", side_effect=finish_other_lookup), mock.patch.object(off, "discover") as upstream:
            self.assertEqual(off.cached_lookup("rice", discovery=True), rows)
        upstream.assert_not_called()

    def test_retained_answer_survives_lookup_reservation_failure(self):
        rows = off._cache_payload([off.normalize(product())])
        row = FoodSearchCache.objects.create(term="off:v1:search:rice", payload=rows)
        FoodSearchCache.objects.filter(pk=row.pk).update(fetched_at=timezone.now() - timedelta(days=8))
        with mock.patch.object(off, "_lease", side_effect=off.FoodSourceUnavailable("Busy", 1)):
            self.assertEqual(off.cached_lookup("rice"), rows)

    def test_discovery_and_complete_food_caches_are_separate(self):
        complete = off.normalize(product())
        identity = {"source_id": complete["source_id"], "name": "Rice", "brand": ""}
        with mock.patch.object(off, "search", return_value=[complete]) as full, mock.patch.object(off, "discover", return_value=[identity]) as discover:
            self.assertIn("calories", off.cached_lookup("rice")[0])
            self.assertNotIn("calories", off.cached_lookup("rice", discovery=True)[0])
            off.cached_lookup("rice")
            off.cached_lookup("rice", discovery=True)
        full.assert_called_once()
        discover.assert_called_once()

    def test_negative_cache_refreshes_after_fifteen_minutes(self):
        row = FoodSearchCache.objects.create(term="off:v2:discover:rice", payload=[])
        FoodSearchCache.objects.filter(pk=row.pk).update(fetched_at=timezone.now() - timedelta(minutes=16))
        with mock.patch.object(off, "discover", return_value=[]) as discover:
            off.cached_lookup("rice", discovery=True)
            off.cached_lookup("rice", discovery=True)
        discover.assert_called_once()

    def test_true_stale_answer_is_returned_during_failure(self):
        expected = off._cache_payload([off.normalize(product())])
        row = FoodSearchCache.objects.create(term="off:v1:search:rice", payload=expected)
        FoodSearchCache.objects.filter(pk=row.pk).update(fetched_at=timezone.now() - timedelta(days=8))
        with mock.patch.object(off, "search", side_effect=off.FoodSourceUnavailable("down")) as upstream:
            self.assertEqual(off.cached_lookup("rice"), expected)
        upstream.assert_called_once()

    def test_malformed_cache_is_never_returned_or_allowed_to_crash_serializer(self):
        FoodSearchCache.objects.create(term="off:v1:search:rice", payload=[{"description": "old shape"}])
        with mock.patch.object(off, "search", side_effect=off.FoodSourceUnavailable("down")):
            with self.assertRaises(off.FoodSourceUnavailable):
                off.cached_lookup("rice")

    def test_wrong_product_in_an_older_barcode_cache_is_not_reused(self):
        FoodSearchCache.objects.create(term="off:v1:barcode:3017620422003",
            payload=off._cache_payload([off.normalize(product(code="0034000470693"))]))
        with mock.patch.object(off, "search", return_value=[off.normalize(product())]) as upstream:
            rows = off.cached_lookup("barcode:3017620422003")
        self.assertEqual(rows[0]["source_id"], "off:3017620422003")
        upstream.assert_called_once()

    def test_failures_are_not_negative_cached(self):
        with mock.patch.object(off, "discover", side_effect=off.FoodSourceUnavailable("down")):
            with self.assertRaises(off.FoodSourceUnavailable):
                off.cached_lookup("rice", discovery=True)
        self.assertFalse(FoodSearchCache.objects.filter(term="off:v2:discover:rice").exists())
        self.assertFalse(FoodSearchCache.objects.filter(term__startswith="off:lease:").exists())

    def test_distinct_failed_queries_do_not_accumulate_lease_rows(self):
        with mock.patch.object(off, "discover", side_effect=off.FoodSourceUnavailable("down")):
            for term in ("rice", "oats", "milk", "bread"):
                with self.assertRaises(off.FoodSourceUnavailable):
                    off.cached_lookup(term, discovery=True)
        self.assertFalse(FoodSearchCache.objects.filter(term__startswith="off:lease:").exists())

    def test_pruning_removes_only_abandoned_not_active_lookup_leases(self):
        old_key, _ = off._lease("abandoned")
        active_key, _ = off._lease("active")
        FoodSearchCache.objects.filter(term=old_key).update(fetched_at=timezone.now() - timedelta(minutes=3))
        self.assertEqual(FoodSearchCache.prune(), 1)
        self.assertFalse(FoodSearchCache.objects.filter(term=old_key).exists())
        self.assertTrue(FoodSearchCache.objects.filter(term=active_key).exists())

    def test_follower_reuses_completed_shared_answer_without_provider_call(self):
        key = "off:v2:discover:rice"
        off._lease(key)
        rows = [{"source_id": "off:3017620422003", "name": "Rice", "brand": ""}]
        def finish(_):
            FoodSearchCache.objects.update_or_create(term=key, defaults={"payload": rows})
        with mock.patch.object(off.time, "sleep", side_effect=finish), mock.patch.object(off, "discover") as upstream:
            self.assertEqual(off.cached_lookup("rice", discovery=True), rows)
        upstream.assert_not_called()

    def test_follower_wait_is_bounded_and_never_duplicates_request(self):
        off._lease("off:v2:discover:rice")
        with mock.patch.object(off, "FOLLOWER_WAIT", 0), mock.patch.object(off, "discover") as upstream:
            with self.assertRaises(off.FoodSourceUnavailable) as caught:
                off.cached_lookup("rice", discovery=True)
        self.assertEqual(caught.exception.retry_after, 1)
        upstream.assert_not_called()

    def test_expired_lease_is_recoverable_and_release_is_owner_scoped(self):
        key, token = off._lease("test")
        FoodSearchCache.objects.filter(term=key).update(payload={"until": 0, "owner": token})
        _, replacement = off._lease("test")
        self.assertIsNotNone(replacement)
        self.assertNotEqual(token, replacement)

    def test_old_owner_neither_writes_cache_nor_deletes_a_replacement_lease(self):
        rows = [{"source_id": "off:3017620422003", "name": "Rice", "brand": ""}]
        def replace_owner(*args, **kwargs):
            lease = FoodSearchCache.objects.get(term__startswith="off:lease:")
            lease.payload = {"owner": "replacement", "until": timezone.now().timestamp() + 12}
            lease.save()
            return rows
        with mock.patch.object(off, "discover", side_effect=replace_owner):
            self.assertEqual(off.cached_lookup("rice", discovery=True), rows)
        self.assertEqual(FoodSearchCache.objects.get(term__startswith="off:lease:").payload["owner"], "replacement")
        self.assertFalse(FoodSearchCache.objects.filter(term="off:v2:discover:rice").exists())

    def test_legacy_cgi_failure_hydrates_at_most_three_candidates(self):
        hits = [candidate(code=code) for code in ["3017620422003", "0034000470693", "5449000000996", "5000159461122"]]
        def respond(path, params, kind, timeout):
            if kind == "search":
                raise off.FoodSourceUnavailable("CGI down")
            if kind == "discover":
                return {"hits": hits}
            return {"product": product(code=path.split("/")[-1].split(".")[0])}
        with mock.patch.object(off, "_request", side_effect=respond) as upstream:
            rows = off.search("rice")
        self.assertEqual(len(rows), 3)
        self.assertEqual(sum(call.args[2] == "barcode" for call in upstream.call_args_list), 3)
        self.assertTrue(all(row["serving_description"] == "100 g" and row["calories"] == "200.00" for row in rows))

    def test_malformed_cgi_success_still_uses_working_modern_fallback(self):
        def respond(path, params, kind, timeout):
            if kind == "search":
                return {"error": "provider problem"}
            if kind == "discover":
                return {"hits": [candidate()]}
            return {"product": product()}
        with mock.patch.object(off, "_request", side_effect=respond):
            self.assertEqual(off.search("rice")[0]["calories"], "200.00")

    def test_fallback_has_overall_deadline_not_three_fresh_timeouts(self):
        clock = [0.0]
        calls = []
        def respond(path, params, kind, timeout):
            calls.append((kind, timeout))
            clock[0] += timeout
            if kind == "search":
                raise off.FoodSourceUnavailable("slow CGI")
            if kind == "discover":
                return {"hits": [candidate(code=code) for code in ["3017620422003", "0034000470693", "5449000000996"]]}
            return {"product": product(code=path.split("/")[-1].split(".")[0])}
        with mock.patch.object(off.time, "monotonic", side_effect=lambda: clock[0]), mock.patch.object(off, "_request", side_effect=respond):
            rows = off.search("rice")
        self.assertEqual(clock[0], 8)
        self.assertEqual(calls, [("search", 2), ("discover", 3), ("barcode", 2), ("barcode", 1)])
        self.assertIsInstance(rows, off.PartialFoodResults)

    def test_partial_transient_fallback_does_not_poison_exact_query_cache(self):
        rows = off.PartialFoodResults([off.normalize(product())])
        with mock.patch.object(off, "search", return_value=rows):
            self.assertEqual(len(off.cached_lookup("rice")), 1)
        self.assertFalse(FoodSearchCache.objects.filter(term="off:v1:search:rice").exists())

    def test_all_hydration_failures_are_not_false_no_matches(self):
        with mock.patch.object(off, "_request", side_effect=off.FoodSourceUnavailable("CGI")), mock.patch.object(off, "cached_lookup", side_effect=[[{"source_id": "off:3017620422003"}], off.FoodSourceUnavailable("product down")]):
            with self.assertRaises(off.FoodSourceUnavailable):
                off.search("rice")

    def test_liquid_basis_is_authoritative_and_mismatched_barcode_is_refused(self):
        with mock.patch.object(off, "_request", return_value={"product": product(nutrition_data_per="100ml")}):
            self.assertEqual(off.search("barcode:3017620422003")[0]["serving_description"], "100 ml")
        with mock.patch.object(off, "_request", return_value={"product": product(code="0034000470693")}):
            with self.assertRaises(off.FoodSourceUnavailable):
                off.search("barcode:3017620422003")


class FoodDiscoveryAPITests(RepbaseAPITestMixin, APITestCase):
    URL = "/api/v1/food/discover/"

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        _, _, token = self.create_account("discover")
        self.authenticate(token)

    def test_discovery_requires_login_and_short_query_has_no_network(self):
        self.assertIn(APIClient().get(self.URL, {"q": "rice"}).status_code, (401, 403))
        with mock.patch.object(off, "discover") as upstream:
            self.assertEqual(self.client.get(self.URL, {"q": "r"}).data, [])
        upstream.assert_not_called()

    def test_discovery_identity_only_and_normalized_cache(self):
        rows = [{"source_id": "off:3017620422003", "name": "Rice", "brand": "Brand"}]
        with mock.patch.object(off, "discover", return_value=rows) as upstream:
            first = self.client.get(self.URL, {"q": "rice"})
            second = self.client.get(self.URL, {"q": " RICE "})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.data, rows)
        self.assertEqual(first.data, second.data)
        upstream.assert_called_once()

    def test_busy_lookup_exposes_retry_after_without_sending_internal_exception(self):
        with mock.patch.object(off, "discover", side_effect=off.FoodSourceUnavailable("private internal detail", 17)):
            response = self.client.get(self.URL, {"q": "rice"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response["Retry-After"], "17")
        self.assertNotIn("private", str(response.data))

    def test_existing_product_endpoint_keeps_required_legacy_decimal_strings(self):
        with mock.patch.object(off, "search", return_value=[off.normalize(product())]):
            response = self.client.get("/api/v1/food/search/", {"q": "barcode:3017620422003"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data[0]["calories"], "200.00")
        self.assertEqual(response.data[0]["serving_description"], "100 g")

    def test_schema_candidates_have_only_identity_strings(self):
        from drf_spectacular.generators import SchemaGenerator
        schema = SchemaGenerator().get_schema(request=None, public=True)
        candidate_schema = schema["components"]["schemas"]["FoodDiscoveryResult"]
        self.assertEqual(set(candidate_schema["required"]), {"source_id", "name", "brand"})
        self.assertEqual(set(candidate_schema["properties"]), {"source_id", "name", "brand"})


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locks and independent committed connections")
class FoodLookupConcurrencyTests(TransactionTestCase):
    def test_twenty_workers_share_exactly_ten_search_slots(self):
        barrier = Barrier(20)

        def reserve():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                try:
                    off._reserve_slot("discover")
                    return "accepted"
                except off.FoodSourceUnavailable as error:
                    # A DB error is not evidence of correct capacity control.
                    if error.retry_after is None:
                        raise
                    self.assertGreater(error.retry_after, 0)
                    self.assertLessEqual(error.retry_after, 60)
                    return "busy"
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=20) as pool:
            futures = [pool.submit(reserve) for _ in range(20)]
            results = [future.result(timeout=20) for future in futures]
        self.assertEqual(results.count("accepted"), 10)
        self.assertEqual(results.count("busy"), 10)
        self.assertEqual(len(FoodSearchCache.objects.get(term="off:gate:search").payload["requests"]), 10)

    def test_two_cold_readers_share_one_upstream_operation_without_http_transaction(self):
        barrier = Barrier(2)
        rows = [{"source_id": "off:3017620422003", "name": "Rice", "brand": "Brand"}]

        def discover(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block, "No row lock/transaction may remain open during HTTP")
            time.sleep(0.1)
            return rows

        def lookup():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return off.cached_lookup("rice", discovery=True)
            finally:
                connection.close()

        with mock.patch.object(off, "discover", side_effect=discover) as upstream:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(lookup) for _ in range(2)]
                results = [future.result(timeout=20) for future in futures]
        self.assertEqual(results, [rows, rows])
        upstream.assert_called_once()
        self.assertFalse(FoodSearchCache.objects.filter(term__startswith="off:lease:").exists())
