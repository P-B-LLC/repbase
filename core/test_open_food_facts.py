from decimal import Decimal
import json
from unittest import mock

from django.test import TestCase, override_settings
from rest_framework.test import APITestCase, APIClient
from . import food_sources as off
from .models import FoodSearchCache
from .tests import RepbaseAPITestMixin


def product(**updates):
    data = {"code": "3017620422003", "product_name": "Test food", "brands": "Test brand",
            "nutrition_data_per": "100g", "nutriments": {
                "energy-kcal_100g": 200, "proteins_100g": 10,
                "carbohydrates_100g": 30, "fat_100g": 5}}
    data.update(updates)
    return data


class OpenFoodFactsTests(TestCase):
    def test_complete_product_decimal_values(self):
        result = off.normalize(product())
        self.assertEqual(result["source_id"], "off:3017620422003")
        self.assertEqual(result["calories"], Decimal("200.00"))
        self.assertEqual(result["serving_description"], "100 g")

    def test_missing_macro_is_not_zero(self):
        raw = product()
        del raw["nutriments"]["fat_100g"]
        self.assertIsNone(off.normalize(raw))

    def test_zero_is_valid(self):
        raw = product()
        raw["nutriments"] = dict.fromkeys(raw["nutriments"], 0)
        self.assertEqual(off.normalize(raw)["calories"], 0)

    def test_invalid_and_implausible_numbers_are_rejected(self):
        for value in (-1, "NaN", "Infinity", True, {}, 1001):
            with self.subTest(value=value):
                raw = product()
                raw["nutriments"]["energy-kcal_100g"] = value
                self.assertIsNone(off.normalize(raw))

    def test_explicit_kj_conversion(self):
        raw = product()
        del raw["nutriments"]["energy-kcal_100g"]
        raw["nutriments"]["energy-kj_100g"] = "836.8"
        self.assertEqual(off.normalize(raw)["calories"], Decimal("200.00"))

    def test_no_calorie_guess_from_macros(self):
        raw = product()
        del raw["nutriments"]["energy-kcal_100g"]
        self.assertIsNone(off.normalize(raw))

    def test_serving_basis_is_not_mixed(self):
        raw = product(serving_size="1 bar (25 g)")
        raw["nutriments"].update({"energy-kcal_serving": 50, "proteins_serving": 2.5,
                                  "carbohydrates_serving": 7.5, "fat_serving": 1.25})
        self.assertEqual(off.normalize(raw)["calories"], 50)
        del raw["nutriments"]["fat_serving"]
        result = off.normalize(raw)
        self.assertEqual(result["calories"], 200)
        self.assertEqual(result["serving_description"], "100 g")

    def test_liquid_is_not_mislabeled_as_grams(self):
        self.assertEqual(off.normalize(product(serving_size="250 ml"))["serving_description"], "100 ml")
        self.assertEqual(off.normalize(product(nutrition_data_per="100ml"))["serving_description"], "100 ml")

    def test_malformed_records_are_skipped(self):
        for raw in (None, [], "food", {}, product(code="bad"), product(nutriments=[]), product(nutrition_data_per="unknown")):
            self.assertIsNone(off.normalize(raw))

    def test_barcode_checksum_and_leading_zero(self):
        self.assertEqual(off.barcode("034000470693"), "0034000470693")
        for code in ("3017620422004", "https://example.com", "123", "１２３４５６７８", "../3017620422003"):
            with self.assertRaises(ValueError):
                off.barcode(code)

    def test_global_request_gate_is_shared_and_separate_by_kind(self):
        off._reserve_slot("search")
        with self.assertRaises(off.FoodSourceUnavailable):
            off._reserve_slot("search")
        off._reserve_slot("barcode")

    def test_search_uses_documented_text_endpoint_and_deduplicates(self):
        with mock.patch.object(off, "_request", return_value={"products": [product(), product(), {}]}) as request:
            result = off.search("test food")
        self.assertEqual(len(result), 1)
        self.assertEqual(request.call_args.args[0], "/cgi/search.pl")
        self.assertEqual(request.call_args.args[1]["search_terms"], "test food")

    def test_barcode_uses_product_endpoint(self):
        with mock.patch.object(off, "_request", return_value={"product": product()}) as request:
            self.assertEqual(len(off.search("barcode:3017620422003")), 1)
        self.assertEqual(request.call_args.args[0], "/api/v3/product/3017620422003.json")

    def test_missing_and_malformed_response_are_distinct(self):
        with mock.patch.object(off, "_request", return_value={"status": 0}):
            self.assertEqual(off.search("barcode:3017620422003"), [])
        with mock.patch.object(off, "_request", return_value={}):
            with self.assertRaises(off.FoodSourceUnavailable):
                off.search("oats")

    def test_malformed_product_result_is_unavailable_not_server_error(self):
        for result in (None, [], "broken"):
            with mock.patch.object(off, "_request", return_value={"result": result}):
                with self.assertRaises(off.FoodSourceUnavailable):
                    off.search("barcode:3017620422003")

    def test_upstream_rate_limit_is_unavailable(self):
        error = off.urllib.error.HTTPError("https://world.openfoodfacts.org", 429, "limited", {}, None)
        with mock.patch.object(off.urllib.request, "build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaises(off.FoodSourceUnavailable):
                off.search("oats")

    def test_oversized_response_is_unavailable(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b" " * (off.MAX_BYTES + 1)
        with mock.patch.object(off.urllib.request, "build_opener") as opener:
            opener.return_value.open.return_value = response
            with self.assertRaises(off.FoodSourceUnavailable):
                off.search("oats")

    @override_settings(OPEN_FOOD_FACTS_ORIGIN="https://untrusted.example")
    def test_non_provider_origin_never_receives_request(self):
        with mock.patch.object(off.urllib.request, "build_opener") as opener:
            with self.assertRaises(off.FoodSourceUnavailable):
                off.search("oats")
            opener.assert_not_called()

    @override_settings(OPEN_FOOD_FACTS_ORIGIN="https://world.openfoodfacts.net")
    def test_transport_has_identity_timeout_and_bounded_read(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"products": []}).encode()
        with mock.patch.object(off.urllib.request, "build_opener") as opener:
            opener.return_value.open.return_value = response
            self.assertEqual(off.search("oats"), [])
        request = opener.return_value.open.call_args.args[0]
        self.assertTrue(request.full_url.startswith("https://world.openfoodfacts.net/"))
        self.assertIn("Rytivo", request.get_header("User-agent"))
        self.assertEqual(opener.return_value.open.call_args.kwargs["timeout"], 8)
        response.__enter__.return_value.read.assert_called_once_with(off.MAX_BYTES + 1)


class OpenFoodFactsAPITests(RepbaseAPITestMixin, APITestCase):
    URL = "/api/v1/food/search/"

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        _, _, token = self.create_account("off-search")
        self.authenticate(token)

    def test_legacy_usda_cache_is_never_returned(self):
        FoodSearchCache.objects.create(term="oats", payload=[{"source_id": "usda:1"}])
        with mock.patch.object(off, "search", return_value=[off.normalize(product())]):
            response = self.client.get(self.URL, {"q": "oats"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data[0]["source_id"].startswith("off:"))

    def test_invalid_barcode_is_400_without_network(self):
        with mock.patch.object(off, "search") as search:
            response = self.client.get(self.URL, {"q": "barcode:123"})
        self.assertEqual(response.status_code, 400)
        search.assert_not_called()

    def test_barcode_is_cached_and_requires_authentication(self):
        with mock.patch.object(off, "search", return_value=[off.normalize(product())]) as search:
            self.assertEqual(self.client.get(self.URL, {"q": "barcode:3017620422003"}).status_code, 200)
            self.assertEqual(self.client.get(self.URL, {"q": "3017620422003"}).status_code, 200)
            search.assert_called_once()
        self.assertIn(APIClient().get(self.URL, {"q": "3017620422003"}).status_code, (401, 403))
