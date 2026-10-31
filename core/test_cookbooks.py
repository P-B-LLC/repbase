from concurrent.futures import ThreadPoolExecutor
from unittest import skipUnless

from django.db import close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.db.models import Count
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APITestCase, APIClient

from .models import Cookbook, SavedFoodMeal, SavedFoodIngredient, Post, PostCookbook, Block
from .tests import RepbaseAPITestMixin


class CookbookTests(RepbaseAPITestMixin, APITestCase):
    def setUp(self):
        self.user, self.profile, self.token = self.create_account("cookbook-owner")
        self.other_user, self.other, self.other_token = self.create_account("cookbook-reader")
        self.authenticate(self.token)
        self.book = Cookbook.objects.create(owner=self.profile, name="Weeknight favorites")
        self.other_book = Cookbook.objects.create(owner=self.other, name="Private recipes")

    def recipe_data(self, name="Dinner", cookbook=None):
        return {"name": name, "cookbook": self.book.pk if cookbook is None else cookbook,
                "cooking_instructions": "Roast until cooked.", "ingredients": [
                    {"name": "Oats", "servings": "1.50", "calories": "150.00",
                     "protein_grams": "5.00", "carbohydrate_grams": "27.00", "fat_grams": "3.00"}]}

    def add(self, name="Dinner", cookbook=None):
        return self.client.post("/api/v1/food/saved-meals/", self.recipe_data(name, cookbook), format="json")

    def fill(self, book, count=20):
        SavedFoodMeal.objects.bulk_create([
            SavedFoodMeal(owner=book.owner, cookbook=book, name=f"Filled recipe {i}") for i in range(count)])

    def publish(self, visibility="public"):
        return self.client.post("/api/v1/social/posts/", {"kind": "cookbook", "source_id": self.book.pk,
                                "caption": "My dinners", "visibility": visibility}, format="json")

    def test_recipe_requires_a_destination_and_rejects_null(self):
        data = self.recipe_data()
        data.pop("cookbook")
        self.assertEqual(self.client.post("/api/v1/food/saved-meals/", data, format="json").status_code, 400)
        data["cookbook"] = None
        self.assertEqual(self.client.post("/api/v1/food/saved-meals/", data, format="json").status_code, 400)
        self.assertFalse(SavedFoodMeal.objects.exists())

    def test_foreign_destination_cannot_be_written(self):
        response = self.add(cookbook=self.other_book.pk)
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(SavedFoodMeal.objects.exists())

    def test_social_recipe_saves_require_owned_destination_and_respect_capacity(self):
        from .fixtures_for_tests import meal_post
        post = meal_post(self.other)
        url = f"/api/v1/social/posts/{post.pk}/save-meal/"
        self.assertEqual(self.client.post(url).status_code, 400)
        self.assertEqual(self.client.post(url, {"cookbook": self.other_book.pk}, format="json").status_code, 400)
        self.fill(self.book)
        self.assertEqual(self.client.post(url, {"cookbook": self.book.pk}, format="json").status_code, 400)
        self.assertFalse(SavedFoodMeal.objects.filter(source_post=post).exists())

    def test_twentieth_recipe_succeeds_twenty_first_fails(self):
        self.fill(self.book, 19)
        self.assertEqual(self.add().status_code, 201)
        response = self.add("Another")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(self.book.recipes.count(), 20)

    def test_editing_recipe_in_full_cookbook_succeeds(self):
        self.fill(self.book, 19)
        recipe = self.add().data
        response = self.client.patch(f"/api/v1/food/saved-meals/{recipe['id']}/", {"name": "Updated"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)

    def test_move_into_full_cookbook_is_atomic(self):
        recipe = self.add().data
        full = Cookbook.objects.create(owner=self.profile, name="Full")
        self.fill(full)
        response = self.client.patch(f"/api/v1/food/saved-meals/{recipe['id']}/",
                                     {"cookbook": full.pk, "name": "Should not change"}, format="json")
        self.assertEqual(response.status_code, 400)
        saved = SavedFoodMeal.objects.get(pk=recipe["id"])
        self.assertEqual(saved.cookbook_id, self.book.pk)
        self.assertEqual(saved.name, "Dinner")

    def test_recipe_names_are_scoped_to_their_cookbook(self):
        self.assertEqual(self.add().status_code, 201)
        another = Cookbook.objects.create(owner=self.profile, name="Lunches")
        self.assertEqual(self.add(cookbook=another.pk).status_code, 201)
        self.assertEqual(self.add("dinner").status_code, 400)

    def test_move_into_name_collision_keeps_original_recipe(self):
        recipe = self.add().data
        another = Cookbook.objects.create(owner=self.profile, name="Lunches")
        self.add(cookbook=another.pk)
        response = self.client.patch(f"/api/v1/food/saved-meals/{recipe['id']}/", {"cookbook": another.pk}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(SavedFoodMeal.objects.get(pk=recipe["id"]).cookbook_id, self.book.pk)

    def test_long_recipe_titles_can_be_saved_without_suffix_truncation(self):
        from .fixtures_for_tests import meal_post
        for index in range(3):
            post = meal_post(self.other)
            post.meal.name = "L" * 150
            post.meal.save(update_fields=["name"])
            response = self.client.post(f"/api/v1/social/posts/{post.pk}/save-meal/", {"cookbook": self.book.pk}, format="json")
            self.assertEqual(response.status_code, 201, response.data)
        names = list(self.book.recipes.values_list("name", flat=True))
        self.assertEqual(len(set(names)), 3)
        self.assertTrue(all(len(name) <= 150 for name in names))

    def test_move_counts_sync_on_a_second_client(self):
        recipe = self.add().data
        new_book = Cookbook.objects.create(owner=self.profile, name="Lunches")
        response = self.client.patch(f"/api/v1/food/saved-meals/{recipe['id']}/", {"cookbook": new_book.pk}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        second = APIClient()
        second.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")
        books = second.get("/api/v1/food/cookbooks/").data["results"]
        counts = {b["id"]: b["recipe_count"] for b in books}
        self.assertEqual(counts, {self.book.pk: 0, new_book.pk: 1})
        self.assertEqual(second.get("/api/v1/food/saved-meals/").data["results"][0]["cookbook"], new_book.pk)

    def test_recipe_filter_is_bounded_to_selected_owned_cookbook(self):
        self.add()
        other = Cookbook.objects.create(owner=self.profile, name="Another cookbook")
        self.add("Lunch", other.pk)
        response = self.client.get(f"/api/v1/food/saved-meals/?cookbook={self.book.pk}")
        self.assertEqual([item["name"] for item in response.data["results"]], ["Dinner"])
        self.assertEqual(self.client.get(f"/api/v1/food/saved-meals/?cookbook={self.other_book.pk}").data["results"], [])
        self.assertEqual(self.client.get("/api/v1/food/saved-meals/?cookbook=bad").status_code, 400)

    def test_collection_names_trim_and_duplicate_case_is_rejected(self):
        response = self.client.post("/api/v1/food/cookbooks/", {"name": "  Breakfast  "}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["name"], "Breakfast")
        self.assertEqual(response.data["recipe_count"], 0)
        self.assertEqual(response.data["capacity"], 20)
        self.assertEqual(self.client.post("/api/v1/food/cookbooks/", {"name": "breakfast"}, format="json").status_code, 400)

    def test_nonempty_collection_cannot_be_deleted(self):
        self.add()
        response = self.client.delete(f"/api/v1/food/cookbooks/{self.book.pk}/")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(self.book.recipes.count(), 1)

    def test_private_collection_is_not_readable_or_mutable_by_another_account(self):
        for verb in ("get", "delete"):
            response = getattr(self.client, verb)(f"/api/v1/food/cookbooks/{self.other_book.pk}/")
            self.assertEqual(response.status_code, 404)
        self.assertEqual(len(self.client.get("/api/v1/food/cookbooks/").data["results"]), 1)

    def test_publish_empty_collection_fails(self):
        self.assertEqual(self.publish().status_code, 400)
        self.assertFalse(Post.objects.exists())

    def test_publication_preserves_complete_recipes_after_private_edits_and_deletion(self):
        recipe = self.add().data
        response = self.publish()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["cookbook"], {"name": self.book.name, "recipe_count": 1})
        self.assertNotIn("recipes", response.data["cookbook"])
        SavedFoodMeal.objects.filter(pk=recipe["id"]).delete()
        self.book.delete()
        self.authenticate(self.other_token)
        detail = self.client.get(f"/api/v1/social/posts/{response.data['id']}/cookbook/")
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertEqual(detail.data["recipes"][0]["ingredients"][0]["servings"], "1.50")
        self.assertEqual(detail.data["recipes"][0]["cooking_instructions"], "Roast until cooked.")
        self.assertNotIn("cookbook", detail.data["recipes"][0])

    def test_private_or_blocked_publication_cannot_be_opened_or_saved(self):
        self.add()
        private_id = self.publish("private").data["id"]
        public_id = self.publish().data["id"]
        self.authenticate(self.other_token)
        for endpoint, method in (("cookbook", "get"), ("save-cookbook", "post")):
            self.assertEqual(getattr(self.client, method)(f"/api/v1/social/posts/{private_id}/{endpoint}/").status_code, 404)
        Block.objects.create(blocker=self.other, blocked=self.profile)
        self.assertEqual(self.client.get(f"/api/v1/social/posts/{public_id}/cookbook/").status_code, 404)

    def test_only_authorized_report_review_expands_full_cookbook(self):
        from django.test import RequestFactory
        from rest_framework.exceptions import PermissionDenied
        from .access_models import AccountAccess
        from .access_moderation import report_data, report_review_data
        from .models import PostReport
        self.add()
        post_id = self.publish().data["id"]
        report = PostReport.objects.create(post_id=post_id, reporter=self.other, reason="other")
        request = RequestFactory().get("/access/reports/")
        request.user = self.other_user
        self.assertNotIn("recipes", report_data(report, "post"))
        with self.assertRaises(PermissionDenied):
            report_review_data(report, "post", request)
        AccountAccess.objects.create(user=self.other_user, moderator=True)
        data = report_review_data(report, "post", request)
        self.assertEqual(data["post"]["cookbook"]["recipes"][0]["cooking_instructions"], "Roast until cooked.")
        self.assertEqual(data["post"]["snapshots"][0]["name"], "Cookbook")

    def test_save_cookbook_twice_makes_one_collection_and_recipe_copy(self):
        self.add()
        published = self.publish().data["id"]
        self.authenticate(self.other_token)
        url = f"/api/v1/social/posts/{published}/save-cookbook/"
        first = self.client.post(url)
        second = self.client.post(url)
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(first.data["id"], second.data["id"])
        self.assertEqual(SavedFoodMeal.objects.filter(owner=self.other).count(), 1)
        self.assertTrue(self.client.get(f"/api/v1/social/posts/{published}/").data["viewer_saved"])

    def test_cookbook_list_query_count_does_not_expand_with_recipe_contents(self):
        self.fill(self.book)
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get("/api/v1/food/cookbooks/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("ingredients", str(response.data))
        self.assertLess(len(queries), 10)

    def test_account_deletion_removes_private_cookbooks_without_restrict_failure(self):
        self.add()
        self.user.delete()
        self.assertFalse(Cookbook.objects.filter(owner_id=self.profile.pk).exists())


@skipUnless(connection.vendor == "postgresql", "Requires real row locks")
class ConcurrentCookbookTests(RepbaseAPITestMixin, TransactionTestCase):
    def test_two_simultaneous_saves_cannot_claim_the_twentieth_slot(self):
        _, owner, token = self.create_account("concurrent-cook")
        book = Cookbook.objects.create(owner=owner, name="Nearly full")
        SavedFoodMeal.objects.bulk_create([SavedFoodMeal(owner=owner, cookbook=book, name=f"Recipe {i}") for i in range(19)])
        def save(index):
            close_old_connections()
            try:
                client = APIClient()
                client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
                return client.post("/api/v1/food/saved-meals/", {"name": f"Concurrent {index}", "cookbook": book.pk,
                    "ingredients": [{"name": "Oats", "calories": "10.00"}]}, format="json").status_code
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = sorted(pool.map(save, range(2)))
        self.assertEqual(results, [201, 400])
        self.assertEqual(book.recipes.count(), 20)


class CookbookMigrationTests(TransactionTestCase):
    def test_existing_recipes_are_preserved_in_collections_of_at_most_twenty(self):
        executor = MigrationExecutor(connection)
        executor.migrate([("core", "0086_media_posts")])
        try:
            old = executor.loader.project_state([("core", "0086_media_posts")]).apps
            user = old.get_model("auth", "User").objects.create(username="migration-cook")
            owner = old.get_model("core", "RepbaseUser").objects.create(user_id=user.pk, height_cm=180, weight_kg=80)
            Recipe = old.get_model("core", "SavedFoodMeal")
            Ingredient = old.get_model("core", "SavedFoodIngredient")
            original_ids = []
            for index in range(43):
                recipe = Recipe.objects.create(owner_id=owner.pk, name=f"Recipe {index}", cooking_instructions="Keep this method.")
                original_ids.append(recipe.pk)
                Ingredient.objects.create(saved_meal_id=recipe.pk, name="Oats", servings="1.50", calories="150.00")
            executor = MigrationExecutor(connection)
            executor.migrate([("core", "0087_cookbooks")])
            self.assertEqual(sorted(Cookbook.objects.filter(owner_id=owner.pk).annotate(n=Count("recipes")).values_list("n", flat=True)), [3,20,20])
            self.assertEqual(list(SavedFoodMeal.objects.order_by("pk").values_list("pk", flat=True)), original_ids)
            self.assertEqual(SavedFoodIngredient.objects.count(), 43)
            self.assertFalse(SavedFoodMeal.objects.filter(cookbook__isnull=True).exists())
            self.assertEqual(SavedFoodMeal.objects.first().cooking_instructions, "Keep this method.")
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())
