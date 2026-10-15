"""What the read paths made cheaper on 2026-10-15 must keep costing, and answering.

Costs are asserted the way `RepostFeedQueryCostTests` asserts them: the same
request at two sizes costs the same number of queries. A fixed number would
break on every unrelated query added to authentication; a cost that grows with
the page is the regression these exist to catch.

Recent foods and personal records had no tests before their reads were
rewritten, so their answers are pinned here too.
"""

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.db import connection
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APITestCase

from config.environment import database

from .access_models import AccountAccess
from .fixtures_for_tests import FAST_PASSWORDS, workout_post
from .models import (
    Exercise,
    Follow,
    FoodEntry,
    FoodMeal,
    PlannerEntry,
    PostComment,
    SessionExercise,
    SetEntry,
    WorkoutSession,
    WorkoutTemplate,
)
from .tests import RepbaseAPITestMixin


def queries_for(client, url):
    with CaptureQueriesContext(connection) as captured:
        response = client.get(url)
    assert response.status_code == 200, (url, response.status_code)
    return len(captured.captured_queries), captured.captured_queries, response


@FAST_PASSWORDS
class PublicProfileListCostTests(RepbaseAPITestMixin, APITestCase):
    """Every list of people reads each person's account, access row, gym and
    three short lists. Loaded lazily that was a query per relation per person:
    160 for a page of fifty followers."""

    def setUp(self):
        _, self.viewer, token = self.create_account("viewer")
        self.authenticate(token)
        self.made = 0

    def person(self):
        self.made += 1
        return self.create_account(f"lifter{self.made}")[1]

    def test_followers_and_following_cost_the_same_for_two_or_twelve(self):
        for _ in range(2):
            Follow.objects.create(follower=self.person(), following=self.viewer)
            Follow.objects.create(follower=self.viewer, following=self.person())
        followers, _, _ = queries_for(self.client, f"/api/v1/users/{self.viewer.pk}/followers/")
        following, _, _ = queries_for(self.client, f"/api/v1/users/{self.viewer.pk}/following/")

        for _ in range(10):
            Follow.objects.create(follower=self.person(), following=self.viewer)
            Follow.objects.create(follower=self.viewer, following=self.person())
        self.assertEqual(queries_for(self.client, f"/api/v1/users/{self.viewer.pk}/followers/")[0], followers)
        self.assertEqual(queries_for(self.client, f"/api/v1/users/{self.viewer.pk}/following/")[0], following)

    def test_search_costs_the_same_for_two_or_twelve(self):
        for _ in range(2):
            self.person()
        few, _, response = queries_for(self.client, "/api/v1/users/?search=lifter")
        self.assertEqual(response.data["count"], 2)
        for _ in range(10):
            self.person()
        many, _, response = queries_for(self.client, "/api/v1/users/?search=lifter")
        self.assertEqual(response.data["count"], 12)
        self.assertEqual(many, few)

    def test_comment_authors_cost_the_same_for_two_or_twelve(self):
        post = workout_post(self.viewer)
        url = f"/api/v1/social/comments/?post={post.pk}"
        for _ in range(2):
            parent = PostComment.objects.create(post=post, author=self.person(), body="nice")
            PostComment.objects.create(post=post, author=self.person(), parent=parent, body="agreed")
        few, _, _ = queries_for(self.client, url)
        for _ in range(10):
            parent = PostComment.objects.create(post=post, author=self.person(), body="nice")
            PostComment.objects.create(post=post, author=self.person(), parent=parent, body="agreed")
        many, _, response = queries_for(self.client, url)
        self.assertEqual(many, few)
        self.assertEqual(response.data["count"], 12)
        self.assertTrue(all(len(comment["replies"]) == 1 for comment in response.data["results"]))

    def test_flair_still_comes_from_the_access_row(self):
        coach = self.person()
        plain = self.person()
        AccountAccess.objects.create(user=coach.user, public_label="Coach", label_color="#123456")
        Follow.objects.create(follower=coach, following=self.viewer)
        Follow.objects.create(follower=plain, following=self.viewer)

        rows = {row["id"]: row for row in self.client.get(
            f"/api/v1/users/{self.viewer.pk}/followers/").data["results"]}
        self.assertEqual(rows[coach.pk]["community_label"], "Coach")
        self.assertEqual(rows[coach.pk]["community_color"], "#123456")
        # No access row is the ordinary case, and answers the declared defaults.
        self.assertEqual(rows[plain.pk]["community_label"], "")
        self.assertEqual(rows[plain.pk]["community_color"], "#F65A18")


@FAST_PASSWORDS
class SessionListCostTests(RepbaseAPITestMixin, APITestCase):
    """A page of lifting sessions -- no track, so no stored summary -- used to
    ask the point table seven times a session, and counted every set the
    account had ever logged before cutting the page."""

    def setUp(self):
        _, self.profile, token = self.create_account("lifter")
        self.authenticate(token)
        self.exercise = Exercise.objects.create(name="Bench Press")

    def lifting_session(self, logged=3, empty=1):
        started = timezone.now() - timedelta(hours=1)
        session = WorkoutSession.objects.create(
            repbase_user=self.profile, status=WorkoutSession.Status.COMPLETED,
            started_at=started, ended_at=started + timedelta(minutes=45),
        )
        line = SessionExercise.objects.create(session=session, exercise=self.exercise, order=1)
        for number in range(1, logged + 1):
            SetEntry.objects.create(session_exercise=line, set_number=number,
                                    weight_kg=Decimal("100"), reps=5, completed_at=timezone.now())
        for number in range(logged + 1, logged + empty + 1):
            SetEntry.objects.create(session_exercise=line, set_number=number)
        return session

    def test_more_lifting_sessions_do_not_cost_more_queries(self):
        for _ in range(2):
            self.lifting_session()
        few, _, _ = queries_for(self.client, "/api/v1/sessions/")
        for _ in range(8):
            self.lifting_session()
        many, captured, response = queries_for(self.client, "/api/v1/sessions/")
        self.assertEqual(many, few)
        self.assertEqual(len(response.data["results"]), 10)
        # Asked once for the whole page, whether any of it has a track.
        touched = [q for q in captured if "sessionroutepoint" in q["sql"].lower()]
        self.assertEqual(len(touched), 1)

    def test_the_list_still_counts_only_logged_sets(self):
        self.lifting_session(logged=3, empty=2)
        self.lifting_session(logged=0, empty=1)
        counts = sorted(row["logged_set_count"] for row in self.client.get("/api/v1/sessions/").data["results"])
        self.assertEqual(counts, [0, 3])


@FAST_PASSWORDS
class RecentFoodTests(RepbaseAPITestMixin, APITestCase):
    URL = "/api/v1/food/meals/recent-foods/"

    def setUp(self):
        _, self.profile, token = self.create_account("eater")
        self.authenticate(token)
        self.meal = FoodMeal.objects.create(owner=self.profile, date=timezone.now().date(), name="Lunch")

    def logged(self, name, calories, minutes_ago, meal=None):
        entry = FoodEntry.objects.create(meal=meal or self.meal, name=name, calories=Decimal(calories))
        FoodEntry.objects.filter(pk=entry.pk).update(created_at=timezone.now() - timedelta(minutes=minutes_ago))
        return entry

    def test_newest_first_one_per_name_and_only_your_own(self):
        self.logged("Oats", "300", minutes_ago=30)
        self.logged("Banana", "100", minutes_ago=20)
        self.logged("oats", "350", minutes_ago=10)
        _, other, _ = self.create_account("someone")
        self.logged("Steak", "600", minutes_ago=1,
                    meal=FoodMeal.objects.create(owner=other, date=timezone.now().date(), name="Dinner"))

        rows = self.client.get(self.URL).data
        self.assertEqual([(row["name"], row["calories"]) for row in rows], [("oats", "350.00"), ("Banana", "100.00")])

    def test_at_most_a_hundred(self):
        for n in range(120):
            self.logged(f"Food {n}", "10", minutes_ago=n)
        rows = self.client.get(self.URL).data
        self.assertEqual(len(rows), 100)
        self.assertEqual(rows[0]["name"], "Food 0")
        self.assertEqual(rows[-1]["name"], "Food 99")


@FAST_PASSWORDS
class PersonalRecordTests(RepbaseAPITestMixin, APITestCase):
    def setUp(self):
        _, self.profile, token = self.create_account("lifter")
        self.authenticate(token)
        self.exercise = Exercise.objects.create(name="Back Squat")

    def session(self, days_ago, sets):
        ended = timezone.now() - timedelta(days=days_ago)
        session = WorkoutSession.objects.create(
            repbase_user=self.profile, status=WorkoutSession.Status.COMPLETED,
            started_at=ended - timedelta(minutes=45), ended_at=ended,
        )
        line = SessionExercise.objects.create(session=session, exercise=self.exercise, order=1)
        for number, (weight, reps) in enumerate(sets, start=1):
            SetEntry.objects.create(session_exercise=line, set_number=number, weight_kg=Decimal(weight),
                                    reps=reps, completed_at=ended - timedelta(minutes=5))
        return session

    def records(self, session):
        return {row["kind"]: row for row in self.client.get(f"/api/v1/sessions/{session.pk}/records/").data}

    def test_beating_the_history_reports_both_kinds(self):
        self.session(days_ago=7, sets=[("100", 5)])
        today = self.session(days_ago=0, sets=[("105", 3), ("95", 8)])
        records = self.records(today)
        self.assertEqual((records["heaviest_weight"]["value"], records["heaviest_weight"]["previous_value"]),
                         (105.0, 100.0))
        # 95 x 8 is an estimated 120.3 against 100 x 5's 116.7.
        self.assertEqual((records["best_estimated_1rm"]["value"], records["best_estimated_1rm"]["previous_value"]),
                         (120.3, 116.7))
        self.assertEqual(records["best_estimated_1rm"]["reps"], 8)

    def test_the_first_time_has_no_previous_value(self):
        records = self.records(self.session(days_ago=0, sets=[("60", 5)]))
        self.assertIsNone(records["heaviest_weight"]["previous_value"])
        self.assertIsNone(records["best_estimated_1rm"]["previous_value"])

    def test_nothing_is_reported_when_nothing_was_beaten(self):
        self.session(days_ago=7, sets=[("140", 5)])
        self.assertEqual(self.records(self.session(days_ago=0, sets=[("100", 5)])), {})


@FAST_PASSWORDS
class RangePageSizeTests(RepbaseAPITestMixin, APITestCase):
    """A range read whole can ask for it in one page instead of several."""

    URL = "/api/v1/planner/?start=2026-01-01&end=2026-12-31"

    def setUp(self):
        _, self.profile, token = self.create_account("planner")
        self.authenticate(token)

    def entries(self, count):
        start = timezone.datetime(2026, 1, 1).date()
        PlannerEntry.objects.bulk_create([
            PlannerEntry(owner=self.profile, kind="task", title=f"Task {n}", category="study",
                         scheduled_date=start + timedelta(days=n % 300))
            for n in range(count)
        ])

    def test_fifty_unless_asked(self):
        self.entries(60)
        page = self.client.get(self.URL).data
        self.assertEqual(len(page["results"]), 50)
        self.assertIsNotNone(page["next"])

    def test_the_whole_range_in_one_page_when_asked(self):
        self.entries(60)
        page = self.client.get(self.URL + "&page_size=200").data
        self.assertEqual(len(page["results"]), 60)
        self.assertIsNone(page["next"])

    def test_never_more_than_two_hundred(self):
        self.entries(210)
        page = self.client.get(self.URL + "&page_size=1000").data
        self.assertEqual(len(page["results"]), 200)
        self.assertEqual(page["count"], 210)

    def test_the_other_whole_range_lists_take_it_too(self):
        workout = WorkoutTemplate.objects.create(owner=self.profile, name="Legs")
        self.client.post("/api/v1/schedules/", {"workout": workout.pk, "scheduled_date": "2026-03-02"})
        for url in ("/api/v1/schedules/?page_size=200", "/api/v1/food/meals/?page_size=200",
                    "/api/v1/set-entries/?page_size=200"):
            self.assertEqual(self.client.get(url).status_code, 200, url)


class SqliteWriteLockTests(SimpleTestCase):
    def test_sqlite_transactions_take_the_write_lock_up_front(self):
        """Two writers at once wait their turn instead of one failing with
        "database is locked". See config.environment.database."""
        self.assertEqual(database({}, Path("/tmp"))["OPTIONS"], {"transaction_mode": "IMMEDIATE"})

    def test_postgresql_is_left_alone(self):
        options = database({"DATABASE_URL": "postgresql://app:secret@db.example:5432/rytivo"}, Path("/tmp"))["OPTIONS"]
        self.assertNotIn("transaction_mode", options)
