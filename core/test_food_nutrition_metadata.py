"""Exact reference mass and known-only additional nutrients, never inferred."""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APITestCase, APIClient

from . import food_sources as off
from .food_nutrition import FOOD_METADATA_FIELDS
from .fixtures_for_tests import FAST_PASSWORDS
from .models import Cookbook, FoodEntry, FoodMeal, FoodSearchCache, Post, SavedFoodMeal
from .test_open_food_facts import product
from .tests import RepbaseAPITestMixin


METADATA = {
    "source_id": "off:3017620422003", "serving_description": "1 tbsp (20 g)",
    "reference_quantity": "20.0000", "reference_unit": "g",
    "nutrients": {"fiber_g": "1.250000", "sodium_mg": "45.000000", "vitamin_d_ug": "0.200000"},
}


class FoodReferenceNormalizationTests(TestCase):
    def test_unknown_label_mass_uses_complete_100g_for_weight_entry(self):
        raw = product(serving_size="1 piece")
        raw["nutriments"].update({"energy-kcal_serving": 50, "proteins_serving": 1,
                                  "carbohydrates_serving": 2, "fat_serving": 1})
        result = off.normalize(raw)
        self.assertEqual(result["reference_quantity"], 100)
        self.assertEqual(result["reference_unit"], "g")
        self.assertEqual(result["calories"], 200)
        self.assertEqual(result["serving_description"], "100 g")

    def test_known_label_mass_is_copied_from_explicit_quantity(self):
        raw = product(serving_size="1 tbsp (20 g)", serving_quantity=20, serving_quantity_unit="g")
        raw["nutriments"].update({"energy-kcal_serving": 40, "proteins_serving": 2,
                                  "carbohydrates_serving": 6, "fat_serving": 1})
        result = off.normalize(raw)
        self.assertEqual(result["reference_quantity"], 20)
        self.assertEqual(result["reference_unit"], "g")
        self.assertEqual(result["serving_description"], "1 tbsp (20 g)")
        self.assertEqual(result["calories"], 40)

    def test_label_gram_text_can_supply_explicit_quantity_without_user_input(self):
        raw = product(serving_size="1 bar (25 g)")
        raw["nutriments"].update({"energy-kcal_serving": 50, "proteins_serving": 2.5,
                                  "carbohydrates_serving": 7.5, "fat_serving": 1.25})
        self.assertEqual(off.normalize(raw)["reference_quantity"], 25)

    def test_liquid_is_ml_never_assumed_grams(self):
        raw = product(serving_size="1 cup (250 ml)", nutrition_data_per="100ml")
        raw["nutriments"].update({"energy-kcal_serving": 500, "proteins_serving": 25,
                                  "carbohydrates_serving": 75, "fat_serving": 12.5})
        result = off.normalize(raw)
        self.assertEqual(result["reference_quantity"], 250)
        self.assertEqual(result["reference_unit"], "ml")

    def test_count_or_ambiguous_mass_is_never_guessed(self):
        for label in ("1 piece", "1 cup", "-20 g", "1,000 g", "1 serving (20 g or 30 g)"):
            raw = product(serving_size=label)
            raw["nutriments"] = {"energy-kcal_serving": 50, "proteins_serving": 1,
                                  "carbohydrates_serving": 2, "fat_serving": 1}
            result = off.normalize(raw)
            self.assertIsNone(result["reference_quantity"], label)
            self.assertEqual(result["reference_unit"], "", label)

    def test_unweighed_serving_never_turns_unknown_liquid_basis_into_grams(self):
        raw = product(serving_size="1 cup", serving_quantity=1, nutrition_data_per="serving")
        raw["nutriments"].update({"energy-kcal_serving": 50, "proteins_serving": 1,
                                  "carbohydrates_serving": 2, "fat_serving": 1,
                                  "fiber_serving": 3, "fiber_100g": 12, "calcium_100g": "0.2"})
        result = off.normalize(raw)
        self.assertEqual(result["calories"], 50)
        self.assertEqual(result["serving_description"], "1 cup")
        self.assertIsNone(result["reference_quantity"])
        self.assertEqual(result["reference_unit"], "")
        self.assertEqual(result["nutrients"]["fiber_g"], 3)
        self.assertNotIn("calcium_mg", result["nutrients"])

    def test_unknown_serving_unit_with_only_100g_values_is_not_loggable(self):
        raw = product(serving_size="1 cup", nutrition_data_per="serving")
        self.assertIsNone(off.normalize(raw))

    def test_explicit_100g_basis_still_supports_weight_when_label_is_a_count(self):
        result = off.normalize(product(serving_size="1 cup", nutrition_data_per="100g"))
        self.assertEqual(result["reference_quantity"], 100)
        self.assertEqual(result["reference_unit"], "g")

    def test_malformed_provider_unit_never_crashes_or_establishes_weight(self):
        for unit in (["g"], {"unit": "ml"}):
            raw = product(serving_size="1 cup", serving_quantity=1,
                          serving_quantity_unit=unit, nutrition_data_per="serving")
            raw["nutriments"].update({"energy-kcal_serving": 50, "proteins_serving": 1,
                                      "carbohydrates_serving": 2, "fat_serving": 1})
            result = off.normalize(raw)
            self.assertIsNone(result["reference_quantity"])
            self.assertEqual(result["reference_unit"], "")

    def test_nutrient_unit_conversion_uses_normalized_grams_not_input_unit(self):
        raw = product()
        raw["nutriments"].update({"fiber_100g": 4, "sodium_100g": "0.125",
            "sodium_unit": "mg", "calcium_100g": "0.2", "calcium_unit": "mg",
            "vitamin-c_100g": "0.09", "vitamin-d_100g": "0.000020",
            "vitamin-d_unit": "µg", "sugars_100g": 0})
        result = off.normalize(raw)["nutrients"]
        self.assertEqual(result["fiber_g"], 4)
        self.assertEqual(result["sodium_mg"], 125)
        self.assertEqual(result["calcium_mg"], 200)
        self.assertEqual(result["vitamin_c_mg"], 90)
        self.assertEqual(result["vitamin_d_ug"], 20)
        self.assertEqual(result["sugars_g"], 0)
        self.assertNotIn("iron_mg", result)

    def test_additional_nutrients_are_same_reference_as_macros(self):
        raw = product(serving_size="1 bar (25 g)")
        raw["nutriments"].update({"energy-kcal_serving": 50, "proteins_serving": 2.5,
            "carbohydrates_serving": 7.5, "fat_serving": 1.25,
            "fiber_100g": 12, "fiber_serving": 3, "calcium_100g": "0.2"})
        nutrients = off.normalize(raw)["nutrients"]
        self.assertEqual(nutrients["fiber_g"], 3)
        self.assertEqual(nutrients["calcium_mg"], 50, "Scale the known 25g reference, never copy the full 100g amount")

    def test_conflicting_gram_label_never_overrides_explicit_volume_reference(self):
        raw = product(serving_size="1 portion (20 g)", serving_quantity=20,
                      serving_quantity_unit="g", nutrition_data_per="100ml")
        raw["nutriments"].update({"energy-kcal_serving": 40, "proteins_serving": 2,
                                  "carbohydrates_serving": 6, "fat_serving": 1})
        result = off.normalize(raw)
        self.assertEqual(result["reference_quantity"], 100)
        self.assertEqual(result["reference_unit"], "ml")
        self.assertEqual(result["serving_description"], "100 ml")
        self.assertEqual(result["calories"], 200)

    def test_prepared_or_invalid_values_are_not_substituted(self):
        raw = product()
        raw["nutriments"].update({"sodium_prepared_100g": 1, "fiber_100g": -1,
                                  "iron_100g": "NaN", "calcium_100g": True})
        self.assertEqual(off.normalize(raw)["nutrients"], {})

    def test_old_product_cache_refreshes_metadata_and_keeps_outage_fallback(self):
        legacy = {key: value for key, value in off._cache_payload([off.normalize(product())])[0].items()
                  if key not in {"reference_quantity", "reference_unit", "nutrients"}}
        FoodSearchCache.objects.create(term="off:v1:barcode:3017620422003", payload=[legacy])
        with mock.patch.object(off, "search", side_effect=off.FoodSourceUnavailable("down")) as upstream:
            self.assertEqual(off.cached_lookup("barcode:3017620422003"), [legacy])
        upstream.assert_called_once()
        with mock.patch.object(off, "search", return_value=[off.normalize(product())]):
            fresh = off.cached_lookup("barcode:3017620422003")
        self.assertEqual(fresh[0]["reference_quantity"], "100")
        self.assertEqual(fresh[0]["reference_unit"], "g")


@FAST_PASSWORDS
class FoodMetadataAPITests(RepbaseAPITestMixin, APITestCase):
    def setUp(self):
        self.user, self.profile, self.token = self.create_account("nutrient-owner")
        _, self.other, self.other_token = self.create_account("nutrient-other")
        self.authenticate(self.token)
        self.today = timezone.now().date()
        self.meal = FoodMeal.objects.create(owner=self.profile, date=self.today, name="Lunch")
        self.book = Cookbook.objects.create(owner=self.profile, name="Recipes")

    def entry_data(self, **updates):
        data = {"meal": self.meal.pk, "name": "Spread", "servings": "3.650000",
            "calories": "40.00", "protein_grams": "2.00", "carbohydrate_grams": "6.00",
            "fat_grams": "1.00", **METADATA}
        data.update(updates)
        return data

    def create_entry(self, **updates):
        response = self.client.post("/api/v1/food/entries/", self.entry_data(**updates), format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return response

    def assert_metadata(self, row):
        self.assertEqual(row["source_id"], METADATA["source_id"])
        self.assertEqual(Decimal(row["reference_quantity"]), 20)
        self.assertEqual(row["reference_unit"], "g")
        self.assertEqual(Decimal(row["nutrients"]["fiber_g"]), Decimal("1.25"))
        self.assertNotIn("iron_mg", row["nutrients"])

    def test_create_read_recent_preserve_metadata_and_exact_portion(self):
        response = self.create_entry()
        self.assert_metadata(response.data)
        self.assertEqual(response.data["servings"], "3.65")
        self.assertEqual(response.data["total_calories"], "146.00")
        self.assert_metadata(self.client.get(f"/api/v1/food/entries/{response.data['id']}/").data)
        self.assert_metadata(self.client.get("/api/v1/food/meals/recent-foods/").data[0])

    def test_legacy_create_is_valid_and_unknown_not_zero(self):
        data = self.entry_data()
        for field in FOOD_METADATA_FIELDS:
            data.pop(field)
        response = self.client.post("/api/v1/food/entries/", data, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNone(response.data["reference_quantity"])
        self.assertEqual(response.data["nutrients"], {})

    def test_invalid_metadata_and_nutrients_are_rejected(self):
        for update in (
            {"reference_quantity": "0"}, {"reference_quantity": "NaN"}, {"reference_unit": "kg"},
            {"reference_quantity": None, "reference_unit": "g"},
            {"source_id": "usda:123"}, {"source_id": "off:3017620422004"},
            {"nutrients": {"unsupported": "1"}}, {"nutrients": {"iron_mg": "NaN"}},
            {"nutrients": {"fiber_g": "-1"}}, {"nutrients": {"fiber_g": True}},
            {"nutrients": []}, {"nutrients": {"fiber_g": "999999999"}},
        ):
            with self.subTest(update=update):
                response = self.client.post("/api/v1/food/entries/", self.entry_data(**update), format="json")
                self.assertEqual(response.status_code, 400, response.data)

    def test_serving_edits_keep_metadata_but_old_macro_edit_clears_it(self):
        created = self.create_entry().data
        url = f"/api/v1/food/entries/{created['id']}/"
        weighed = self.client.patch(url, {"servings": "10.428571"}, format="json")
        self.assert_metadata(weighed.data)
        self.assertEqual(weighed.data["servings"], "10.428571")
        edited = self.client.patch(url, {"calories": "41.00"}, format="json")
        self.assertEqual(edited.status_code, 200, edited.data)
        self.assertIsNone(edited.data["reference_quantity"])
        self.assertEqual(edited.data["nutrients"], {})
        self.assertEqual(edited.data["source_id"], "")

    def test_explicit_blank_unit_clears_reference_even_if_null_is_omitted(self):
        created = self.create_entry().data
        response = self.client.patch(f"/api/v1/food/entries/{created['id']}/", {
            "source_id": "", "serving_description": "", "reference_unit": "", "nutrients": {},
        }, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNone(response.data["reference_quantity"])

    def test_copy_day_preserves_independent_nutrient_snapshot(self):
        original = self.create_entry().data
        response = self.client.post("/api/v1/food/meals/copy-day/", {
            "source_date": self.today.isoformat(), "target_date": (self.today + timedelta(days=1)).isoformat(),
        }, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        copied = response.data[0]["entries"][0]
        self.assert_metadata(copied)
        self.assertNotEqual(copied["id"], original["id"])

    def test_saved_recipe_apply_and_cookbook_publication_preserve_metadata(self):
        ingredient = {key: value for key, value in self.entry_data().items() if key != "meal"}
        created = self.client.post("/api/v1/food/saved-meals/", {
            "name": "Spread recipe", "cookbook": self.book.pk, "ingredients": [ingredient],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        self.assert_metadata(created.data["ingredients"][0])
        applied = self.client.post(f"/api/v1/food/saved-meals/{created.data['id']}/apply/", {
            "dates": [self.today.isoformat()], "position": 1,
        }, format="json")
        self.assertEqual(applied.status_code, 200, applied.data)
        self.assert_metadata(applied.data[0]["entries"][0])
        from .cookbook_publication import recipe_snapshot, save_publication
        from .models import PostCookbook
        frozen = recipe_snapshot(self.book)
        self.assert_metadata(frozen[0]["ingredients"][0])
        post = Post.objects.create(author=self.profile, kind="cookbook", visibility="public", published_at=timezone.now())
        PostCookbook.objects.create(post=post, name="Recipes", recipes=frozen, recipe_count=1)
        saved, _ = save_publication(self.other, post)
        stored = saved.recipes.first().ingredients.first()
        self.assertEqual(stored.nutrients, METADATA["nutrients"])
        self.assertEqual(stored.reference_quantity, 20)

    def test_meal_post_and_social_save_preserve_metadata(self):
        self.create_entry()
        from .views import snapshot_meal
        post = Post.objects.create(author=self.profile, kind="meal", visibility="public", published_at=timezone.now())
        snapshot_meal(post, self.meal)
        snapshot = post.meal.entries.first()
        self.assertEqual(snapshot.nutrients, METADATA["nutrients"])
        self.authenticate(self.other_token)
        destination = Cookbook.objects.create(owner=self.other, name="Copied recipes")
        response = self.client.post(f"/api/v1/social/posts/{post.pk}/save-meal/", {"cookbook": destination.pk}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        stored = SavedFoodMeal.objects.get(pk=response.data["meal"]).ingredients.first()
        self.assertEqual(stored.nutrients, METADATA["nutrients"])
        self.assertEqual(stored.reference_quantity, 20)

    def test_foreign_food_remains_private(self):
        created = self.create_entry().data
        self.authenticate(self.other_token)
        self.assertEqual(self.client.get(f"/api/v1/food/entries/{created['id']}/").status_code, 404)
        self.assertEqual(self.client.patch(f"/api/v1/food/entries/{created['id']}/", {"nutrients": {}}, format="json").status_code, 404)

    def test_new_response_metadata_is_optional_in_contract(self):
        from drf_spectacular.generators import SchemaGenerator
        components = SchemaGenerator().get_schema(request=None, public=True)["components"]["schemas"]
        for name in ("FoodEntry", "SavedFoodIngredient", "RecentFood", "PostMealEntry", "CookbookIngredient"):
            self.assertFalse(set(FOOD_METADATA_FIELDS) & set(components[name].get("required", [])), name)
        self.assertTrue({"source_id", "serving_description"} <= set(components["FoodSearchResult"]["required"]))
