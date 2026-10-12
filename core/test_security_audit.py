"""Regression tests for the October 2026 security audit.

Each class pins one finding. Its docstring says what an attacker could do
before the fix; the tests are written as the attack, so they failed against
the code as it was and pass against the fix.
"""

import importlib.util
import os
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APITestCase
from rest_framework.throttling import SimpleRateThrottle

from . import models as core_models
from .models import (
    Block,
    BodyWeightEntry,
    Exercise,
    Follow,
    Gym,
    PlannerEntry,
    PlannerRecurrence,
    WorkoutCycle,
    WorkoutCycleSlot,
    WorkoutRecurrence,
    WorkoutSchedule,
    WorkoutTemplate,
    today_for,
)
from .tests import RepbaseAPITestMixin

User = get_user_model()

PASSWORD = "StrongPass!234"


class PlanningHorizonTests(RepbaseAPITestMixin, APITestCase):
    """Three endpoints wrote one row per day (or week) up to a date the
    client chose, with no upper bound.

    `GET /planner/?end=9999-12-31` with one daily repeating task wrote close to
    three million rows in a single request; `cycles/{id}/plan-ahead/` and
    `schedules/plan-week/` did the same for rotations and weekly repeats. Any
    account could fill the database, and hold a worker, with one call.
    """

    def setUp(self):
        self.user, self.profile, self.token = self.create_account("planner")
        self.authenticate(self.token)
        self.today = today_for(self.profile)
        self.far = self.today + timedelta(days=3000)
        self.limit = self.today + timedelta(days=core_models.PLAN_AHEAD_LIMIT_DAYS)

    def daily_task(self):
        response = self.client.post(
            "/api/v1/planner/",
            {
                "kind": "task",
                "title": "Stretch",
                "category": "habit",
                "scheduled_date": self.today.isoformat(),
                "repeat_every_days": 1,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        return response

    def test_reading_the_planner_far_ahead_writes_at_most_the_horizon(self):
        self.daily_task()
        response = self.client.get(
            "/api/v1/planner/",
            {"start": self.far.isoformat(), "end": self.far.isoformat()},
        )
        self.assertEqual(response.status_code, 200)
        written = PlannerEntry.objects.filter(owner=self.profile)
        self.assertLessEqual(written.count(), core_models.PLAN_AHEAD_LIMIT_DAYS + 1)
        self.assertFalse(written.filter(scheduled_date__gt=self.limit).exists())

    def test_the_last_representable_day_is_not_an_error(self):
        self.daily_task()
        response = self.client.get("/api/v1/planner/", {"end": "9999-12-31"})
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(
            PlannerEntry.objects.filter(owner=self.profile).count(),
            core_models.PLAN_AHEAD_LIMIT_DAYS + 1,
        )

    def test_a_month_ahead_still_fills_in(self):
        """The clamp must not hide anything an ordinary calendar asks for."""
        self.daily_task()
        month = self.today + timedelta(days=90)
        response = self.client.get(
            "/api/v1/planner/",
            {"start": month.isoformat(), "end": month.isoformat()},
        )
        self.assertEqual(len(response.data["results"]), 1)

    def test_a_repeat_cannot_start_years_ahead(self):
        """Used to be a 500: the first fill-in overflowed the date type."""
        response = self.client.post(
            "/api/v1/planner/",
            {
                "kind": "task",
                "title": "Later",
                "category": "habit",
                "scheduled_date": "9999-12-31",
                "repeat_every_days": 1,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("repeat_every_days", response.data)

    def test_active_repeating_tasks_are_capped(self):
        with mock.patch.object(core_models, "MAX_ACTIVE_PLANNER_REPEATS", 2):
            self.daily_task()
            self.daily_task()
            response = self.client.post(
                "/api/v1/planner/",
                {
                    "kind": "task",
                    "title": "Third",
                    "category": "habit",
                    "scheduled_date": self.today.isoformat(),
                    "repeat_every_days": 1,
                },
                format="json",
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(PlannerRecurrence.objects.filter(owner=self.profile).count(), 2)

    def rotation(self):
        workout = WorkoutTemplate.objects.create(owner=self.profile, name="Legs")
        cycle = WorkoutCycle.objects.create(
            owner=self.profile,
            name="Every day",
            length=1,
            anchor_date=self.today,
            effective_from=self.today,
        )
        WorkoutCycleSlot.objects.create(cycle=cycle, position=1, workout=workout)
        return cycle

    def test_planning_a_rotation_ahead_is_bounded(self):
        cycle = self.rotation()
        response = self.client.post(
            f"/api/v1/cycles/{cycle.pk}/plan-ahead/",
            {"through": self.far.isoformat()},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        written = WorkoutSchedule.objects.filter(owner=self.profile)
        self.assertLessEqual(written.count(), core_models.PLAN_AHEAD_LIMIT_DAYS + 1)
        self.assertFalse(written.filter(scheduled_date__gt=self.limit).exists())

    def test_a_rotation_cannot_be_switched_to_years_ahead(self):
        """Used to be a 500 for a date near the end of the calendar."""
        cycle = self.rotation()
        response = self.client.post(
            f"/api/v1/cycles/{cycle.pk}/activate/",
            {"start_on": "9999-12-01"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("start_on", response.data)

    def test_planning_a_far_week_writes_nothing_past_the_horizon(self):
        workout = WorkoutTemplate.objects.create(owner=self.profile, name="Push")
        response = self.client.post(
            "/api/v1/recurrences/",
            {"workout": workout.pk, "weekday": 0},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        response = self.client.post(
            "/api/v1/schedules/plan-week/",
            {"start": self.far.isoformat()},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        written = WorkoutSchedule.objects.filter(owner=self.profile)
        self.assertFalse(written.filter(scheduled_date__gt=self.limit).exists())
        self.assertLessEqual(written.count(), core_models.PLAN_AHEAD_LIMIT_DAYS // 7 + 2)
        self.assertTrue(WorkoutRecurrence.objects.filter(owner=self.profile).exists())


class FormSignInLimitTests(RepbaseAPITestMixin, APITestCase):
    """Django's own sign-in pages had no limit of any kind.

    `/api-auth/login/` (DRF's browsable API) signs any account in, and the
    session it sets is accepted by every API endpoint -- so it was an
    unthrottled password oracle beside the carefully limited
    `/api/v1/auth/login/`. `/admin/login/` was the same for staff accounts.
    """

    API_AUTH = "/api-auth/login/"
    ADMIN = "/admin/login/"

    def setUp(self):
        self.user, self.profile, self.token = self.create_account("target")
        self.staff = User.objects.create_user(
            "operator", "operator@example.com", PASSWORD, is_staff=True
        )

    def post(self, path, username, password, **extra):
        return self.client.post(path, {"username": username, "password": password}, **extra)

    def signed_in(self):
        return "_auth_user_id" in self.client.session

    def test_the_browsable_api_sign_in_shares_the_username_limit(self):
        with mock.patch.dict(
            SimpleRateThrottle.THROTTLE_RATES,
            {"login": "1000/hour", "login_username": "3/hour"},
        ), override_settings(DJANGO_LOGIN_ATTEMPTS_PER_ADDRESS=1000):
            for attempt in range(3):
                self.post(self.API_AUTH, "target", f"guess-{attempt}")
            self.post(self.API_AUTH, "target", PASSWORD)
        self.assertFalse(self.signed_in(), "the right password must not work past the limit")

    def test_failures_anywhere_count_against_the_admin_sign_in(self):
        with mock.patch.dict(
            SimpleRateThrottle.THROTTLE_RATES,
            {"login": "1000/hour", "login_username": "2/hour"},
        ), override_settings(DJANGO_LOGIN_ATTEMPTS_PER_ADDRESS=1000):
            for attempt in range(2):
                self.client.post(
                    "/api/v1/auth/login/",
                    {"username": "operator", "password": f"guess-{attempt}"},
                    format="json",
                )
            self.post(self.ADMIN, "operator", PASSWORD)
        self.assertFalse(self.signed_in())

    def test_the_api_sign_in_still_counts_each_failure_once(self):
        with mock.patch.dict(
            SimpleRateThrottle.THROTTLE_RATES,
            {"login": "1000/hour", "login_username": "3/hour"},
        ):
            codes = [
                self.client.post(
                    "/api/v1/auth/login/",
                    {"username": "target", "password": f"guess-{attempt}"},
                    format="json",
                ).status_code
                for attempt in range(4)
            ]
        self.assertEqual(codes, [400, 400, 400, 429])

    def test_django_sign_in_pages_are_limited_per_address(self):
        with override_settings(DJANGO_LOGIN_ATTEMPTS_PER_ADDRESS=3):
            codes = [
                self.post(self.ADMIN, f"nobody-{attempt}", "wrong").status_code
                for attempt in range(4)
            ]
            api_auth = self.post(self.API_AUTH, "nobody-else", "wrong").status_code
        self.assertEqual(codes[:3], [200, 200, 200])
        self.assertEqual(codes[3], 429)
        self.assertEqual(api_auth, 429)

    def test_the_address_limit_is_per_client_behind_the_proxy(self):
        """It used to key on REMOTE_ADDR, which behind nginx is nginx: six
        bad attempts from anywhere locked every member of staff out."""
        rest = {**settings.REST_FRAMEWORK, "NUM_PROXIES": 1}
        with override_settings(REST_FRAMEWORK=rest, DJANGO_LOGIN_ATTEMPTS_PER_ADDRESS=3):
            for _ in range(4):
                self.post(self.ADMIN, "nobody", "wrong", HTTP_X_FORWARDED_FOR="203.0.113.9")
            other = self.post(
                self.ADMIN, "operator", PASSWORD, HTTP_X_FORWARDED_FOR="198.51.100.4"
            )
        self.assertEqual(other.status_code, 302)

    def test_a_made_up_forwarded_address_does_not_reset_the_limit(self):
        rest = {**settings.REST_FRAMEWORK, "NUM_PROXIES": 1}
        with override_settings(REST_FRAMEWORK=rest, DJANGO_LOGIN_ATTEMPTS_PER_ADDRESS=3):
            codes = [
                self.post(
                    self.ADMIN,
                    "nobody",
                    "wrong",
                    HTTP_X_FORWARDED_FOR=f"10.9.9.{attempt}, 203.0.113.9",
                ).status_code
                for attempt in range(4)
            ]
        self.assertEqual(codes[-1], 429)

    def test_without_a_proxy_count_a_forwarded_header_is_not_believed(self):
        """Where NUM_PROXIES is unset (development, or a misconfigured host)
        the header is the client's own word, so the socket address decides."""
        with override_settings(DJANGO_LOGIN_ATTEMPTS_PER_ADDRESS=3):
            codes = [
                self.post(
                    self.ADMIN, "nobody", "wrong", HTTP_X_FORWARDED_FOR=f"10.1.1.{attempt}"
                ).status_code
                for attempt in range(4)
            ]
        self.assertEqual(codes[-1], 429)


class PasswordResetAccountLimitTests(RepbaseAPITestMixin, APITestCase):
    """Reset limits were per address only.

    Spread across addresses, an attacker could have a fresh code issued as
    often as they liked -- each one spending the last -- and make five
    guesses at each, with no ceiling on the total for one account. The same
    loop flooded the owner's inbox.
    """

    REQUEST = "/api/v1/auth/password-reset/"
    CONFIRM = "/api/v1/auth/password-reset/confirm/"

    def setUp(self):
        self.user, self.profile, self.token = self.create_account("forgot")
        self.sent = []

    def capture(self, **kwargs):
        self.sent.append(kwargs)
        return 1

    def ask(self, address, email=None):
        with mock.patch("core.views.send_mail", side_effect=self.capture):
            return self.client.post(
                self.REQUEST,
                {"email": email or self.user.email},
                format="json",
                HTTP_X_FORWARDED_FOR=address,
            )

    def confirm(self, code, address="198.51.100.1", email=None):
        return self.client.post(
            self.CONFIRM,
            {"email": email or self.user.email, "code": code, "new_password": "Fresh-Secret-991"},
            format="json",
            HTTP_X_FORWARDED_FOR=address,
        )

    def latest_code(self):
        return self.sent[-1]["message"].split("code is ")[1][:6]

    def rates(self, **rates):
        merged = {"password_reset": "1000/hour", "password_reset_confirm": "1000/hour", **rates}
        return mock.patch.dict(SimpleRateThrottle.THROTTLE_RATES, merged)

    def proxied(self):
        return override_settings(REST_FRAMEWORK={**settings.REST_FRAMEWORK, "NUM_PROXIES": 1})

    def test_codes_for_one_account_are_limited_whatever_the_source(self):
        with self.proxied(), self.rates(password_reset_account="2/hour"):
            answers = [self.ask(f"203.0.113.{n}").status_code for n in range(4)]
        self.assertEqual(answers, [204] * 4, "the answer must not change")
        self.assertEqual(len(self.sent), 2)

    def test_wrong_codes_are_limited_across_codes(self):
        with self.proxied(), self.rates(password_reset_account_failures="3/day"):
            self.ask("203.0.113.1")
            code = self.latest_code()
            wrong = "000000" if code != "000000" else "111111"
            for n in range(3):
                self.confirm(wrong, address=f"198.51.100.{n}")
            # A new code does not buy new guesses.
            self.ask("203.0.113.2")
            response = self.confirm(self.latest_code(), address="198.51.100.50")
        self.assertEqual(response.status_code, 429)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))

    def test_an_unknown_address_is_limited_the_same_way(self):
        """Counted whether or not the account exists, so the 429 says nothing."""
        with self.proxied(), self.rates(password_reset_account_failures="2/day"):
            codes = [
                self.confirm("123456", email="nobody@example.invalid").status_code
                for _ in range(3)
            ]
        self.assertEqual(codes, [400, 400, 429])

    def test_a_successful_reset_clears_the_count(self):
        with self.proxied(), self.rates(password_reset_account_failures="2/day"):
            self.ask("203.0.113.1")
            code = self.latest_code()
            wrong = "000000" if code != "000000" else "111111"
            self.confirm(wrong)
            self.assertEqual(self.confirm(code).status_code, 200)
            self.ask("203.0.113.2")
            self.confirm(wrong)
            self.assertEqual(self.confirm(self.latest_code()).status_code, 200)


class SuperuserScopeTests(RepbaseAPITestMixin, APITestCase):
    """`scope_to_owner` let a superuser through every owner check.

    A superuser's API token therefore read, edited and deleted every account's
    workouts, meals, sessions, GPS routes and body weight through the
    consumer API -- and the superuser's own app showed all of it mixed into
    their lists. The admin site is where staff work with other people's data.
    """

    def setUp(self):
        self.admin_user, self.admin, self.admin_token = self.create_account("rootish")
        self.admin_user.is_superuser = True
        self.admin_user.is_staff = True
        self.admin_user.save(update_fields=["is_superuser", "is_staff"])
        _, self.member, _ = self.create_account("member")
        self.workout = WorkoutTemplate.objects.create(owner=self.member, name="Private plan")
        self.weight = BodyWeightEntry.objects.create(
            owner=self.member, weight_kg="70.00"
        )
        self.exercise = Exercise.objects.create(name="Member's own lift", created_by=self.member)
        self.authenticate(self.admin_token)

    def test_lists_hold_only_the_superusers_own_records(self):
        workouts = self.client.get("/api/v1/workouts/").data["results"]
        weights = self.client.get("/api/v1/body-weight/").data["results"]
        exercises = self.client.get(
            "/api/v1/exercises/", {"name": self.exercise.name}
        ).data["results"]
        self.assertNotIn(self.workout.pk, [row["id"] for row in workouts])
        self.assertNotIn(self.weight.pk, [row["id"] for row in weights])
        self.assertNotIn(self.exercise.pk, [row["id"] for row in exercises])

    def test_another_accounts_record_is_not_found(self):
        self.assertEqual(self.client.get(f"/api/v1/workouts/{self.workout.pk}/").status_code, 404)
        self.assertEqual(
            self.client.delete(f"/api/v1/body-weight/{self.weight.pk}/").status_code, 404
        )
        self.assertTrue(BodyWeightEntry.objects.filter(pk=self.weight.pk).exists())


class OwnerlessGymTests(RepbaseAPITestMixin, APITestCase):
    """A gym whose creator deleted their account (the foreign key is SET_NULL)
    could be renamed or deleted by anybody signed in -- including a gym other
    people list as theirs."""

    def setUp(self):
        _, self.creator, self.creator_token = self.create_account("founder")
        _, self.stranger, self.stranger_token = self.create_account("vandal")

    def test_a_gym_left_without_its_creator_is_not_anybodys_to_rename(self):
        self.authenticate(self.creator_token)
        gym_id = self.client.post(
            "/api/v1/gyms/", {"name": "Iron Works", "city": "Austin"}, format="json"
        ).data["id"]
        self.client.delete("/api/v1/me/")
        self.assertIsNone(Gym.objects.get(pk=gym_id).created_by_id)

        self.authenticate(self.stranger_token)
        renamed = self.client.patch(f"/api/v1/gyms/{gym_id}/", {"name": "Closed"}, format="json")
        deleted = self.client.delete(f"/api/v1/gyms/{gym_id}/")
        self.assertEqual(renamed.status_code, 403)
        self.assertEqual(deleted.status_code, 403)
        self.assertEqual(Gym.objects.get(pk=gym_id).name, "Iron Works")

    def test_a_superuser_can_still_correct_one(self):
        gym = Gym.objects.create(name="Old Name", city="Austin")
        admin, _, admin_token = self.create_account("gymadmin")
        admin.is_superuser = True
        admin.save(update_fields=["is_superuser"])
        self.authenticate(admin_token)
        response = self.client.patch(f"/api/v1/gyms/{gym.pk}/", {"name": "New Name"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)


class GymMemberPrivacyTests(RepbaseAPITestMixin, APITestCase):
    """A closed profile hides its gym, but the gym's member list named the
    person anyway -- telling a stranger, or somebody they had blocked, where
    they train."""

    def setUp(self):
        self.gym = Gym.objects.create(name="Corner Gym", city="Leeds")
        _, self.private, self.private_token = self.create_account("quiet")
        self.private.gym = self.gym
        self.private.is_profile_public = False
        self.private.save(update_fields=["gym", "is_profile_public"])
        _, self.open, _ = self.create_account("loud")
        self.open.gym = self.gym
        self.open.save(update_fields=["gym"])
        _, self.stranger, self.stranger_token = self.create_account("onlooker")
        _, self.friend, self.friend_token = self.create_account("pal")
        Follow.objects.create(follower=self.friend, following=self.private)

    def members(self, token):
        self.authenticate(token)
        response = self.client.get(f"/api/v1/gyms/{self.gym.pk}/members/")
        self.assertEqual(response.status_code, 200)
        return {row["username"] for row in response.data}

    def test_a_stranger_does_not_see_a_closed_profile(self):
        self.assertEqual(self.members(self.stranger_token), {"loud"})

    def test_a_follower_and_the_person_themself_do(self):
        self.assertEqual(self.members(self.friend_token), {"loud", "quiet"})
        self.assertEqual(self.members(self.private_token), {"loud", "quiet"})

    def test_a_block_either_way_hides_them(self):
        Block.objects.create(blocker=self.open, blocked=self.stranger)
        self.assertEqual(self.members(self.stranger_token), set())


class EmailChangeTests(RepbaseAPITestMixin, APITestCase):
    """Changing the account's email took only a token.

    Whoever held a stolen token could move the email to an address they
    control and then reset the password -- turning a token that can be
    revoked into an account that cannot be recovered.
    """

    def setUp(self):
        self.user, self.profile, self.token = self.create_account("mover")
        self.authenticate(self.token)

    def patch(self, **data):
        return self.client.patch("/api/v1/me/", data, format="json")

    def test_a_new_email_needs_the_current_password(self):
        response = self.patch(email="attacker@example.com")
        self.assertEqual(response.status_code, 400)
        self.assertIn("current_password", response.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "mover@example.com")

    def test_a_wrong_password_is_refused(self):
        response = self.patch(email="attacker@example.com", current_password="guess")
        self.assertEqual(response.status_code, 400)
        self.assertIn("current_password", response.data)

    def test_the_right_password_changes_it(self):
        response = self.patch(email="New@Example.com", current_password=PASSWORD)
        self.assertEqual(response.status_code, 200, response.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "new@example.com")
        self.assertNotIn("current_password", response.data)

    def test_sending_the_same_email_back_needs_nothing(self):
        self.assertEqual(self.patch(email="MOVER@example.com", bio="hi").status_code, 200)
        self.assertEqual(self.patch(bio="still fine").status_code, 200)

    def test_guesses_count_toward_the_sign_in_limit(self):
        with mock.patch.dict(
            SimpleRateThrottle.THROTTLE_RATES, {"login": "1000/hour", "login_username": "2/hour"}
        ):
            self.patch(email="a@example.com", current_password="guess-1")
            self.patch(email="a@example.com", current_password="guess-2")
            response = self.patch(email="a@example.com", current_password=PASSWORD)
        self.assertEqual(response.status_code, 429)


class BatchSizeTests(RepbaseAPITestMixin, APITestCase):
    """Lists in request bodies had no length limit. An 8 MB body held about
    230,000 step counts, each an upsert, in one transaction -- minutes of
    database work bought with one request."""

    def setUp(self):
        self.user, self.profile, self.token = self.create_account("batcher")
        self.authenticate(self.token)

    def test_step_counts(self):
        days = [
            {"day": (date(2026, 1, 1) + timedelta(days=n)).isoformat(), "steps": 1}
            for n in range(401)
        ]
        response = self.client.post("/api/v1/step-counts/record/", {"days": days}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_health_imports(self):
        start = "2026-01-01T08:00:00Z"
        workouts = [
            {"external_id": f"w{n}", "activity": "running", "started_at": start, "ended_at": start}
            for n in range(501)
        ]
        response = self.client.post(
            "/api/v1/sessions/import-health/", {"workouts": workouts}, format="json"
        )
        self.assertEqual(response.status_code, 400)

    def test_saved_meal_ingredients(self):
        ingredients = [{"name": f"Thing {n}"} for n in range(101)]
        response = self.client.post(
            "/api/v1/food/saved-meals/",
            {"name": "Feast", "ingredients": ingredients},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_initial_exercises(self):
        exercise = Exercise.objects.create(name="Curl")
        plan = [{"exercise": exercise.pk, "order": n + 1} for n in range(201)]
        response = self.client.post(
            "/api/v1/workouts/",
            {"name": "Huge", "initial_exercises": plan},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_personalization_training_types(self):
        response = self.client.patch(
            "/api/v1/me/personalization/",
            {"training_types": [f"type-{n}" for n in range(21)]},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_ordinary_batches_still_work(self):
        days = [{"day": "2026-01-01", "steps": 9000}, {"day": "2026-01-02", "steps": 12000}]
        response = self.client.post("/api/v1/step-counts/record/", {"days": days}, format="json")
        self.assertEqual(response.status_code, 204)


class NumberRangeTests(RepbaseAPITestMixin, APITestCase):
    """Hand-declared integer fields had no ceiling, so a number past the
    database's integer range reached PostgreSQL and came back as a 500."""

    def setUp(self):
        self.user, self.profile, self.token = self.create_account("counter")
        self.authenticate(self.token)

    def test_manual_reps(self):
        response = self.client.put(
            "/api/v1/me/highlights/",
            {"highlights": [{"lift": "bench", "manual_weight_kg": "100", "manual_reps": 10**12}]},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_steps(self):
        response = self.client.post(
            "/api/v1/step-counts/record/",
            {"days": [{"day": "2026-01-01", "steps": 10**12}]},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_cardio_seconds(self):
        session = self.client.post("/api/v1/sessions/", {}, format="json").data
        response = self.client.post(
            f"/api/v1/sessions/{session['id']}/cardio/",
            {"machine": "treadmill", "seconds": 10**12},
            format="json",
        )
        self.assertEqual(response.status_code, 400)


class FoodSearchTermTests(RepbaseAPITestMixin, APITestCase):
    """A search term longer than the cache column (200) reached PostgreSQL as
    a 500, after spending two calls of the shared FoodData Central allowance."""

    def setUp(self):
        self.user, self.profile, self.token = self.create_account("hungry")
        self.authenticate(self.token)

    def test_a_long_term_is_cut_to_what_fits(self):
        with mock.patch("core.food_sources.search", return_value=[]) as search:
            response = self.client.get("/api/v1/food/search/", {"q": "oats " * 200})
        self.assertEqual(response.status_code, 200)
        term = search.call_args.args[0]
        self.assertLessEqual(len(term), 100)
        self.assertTrue(core_models.FoodSearchCache.objects.filter(term=term).exists())


class GunicornBindTests(APITestCase):
    """gunicorn listened on every interface. If the port was not firewalled a
    client could skip nginx -- its TLS, its rate limits -- and write its own
    X-Forwarded-For, which the throttles then believe."""

    CONFIG = Path(settings.BASE_DIR) / "gunicorn.conf.py"

    def load(self, **env):
        clean = {key: value for key, value in os.environ.items()
                 if key not in {"GUNICORN_BIND_HOST", "PORT"}}
        with mock.patch.dict(os.environ, {**clean, **env}, clear=True):
            spec = importlib.util.spec_from_file_location("gunicorn_conf_under_test", self.CONFIG)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        return module

    def test_loopback_by_default(self):
        self.assertEqual(self.load(PORT="8001").bind, "127.0.0.1:8001")

    def test_an_explicit_host_is_honoured(self):
        self.assertEqual(self.load(PORT="8001", GUNICORN_BIND_HOST="0.0.0.0").bind, "0.0.0.0:8001")

    def test_a_malformed_host_is_refused(self):
        from django.core.exceptions import ImproperlyConfigured

        with self.assertRaises(ImproperlyConfigured):
            self.load(GUNICORN_BIND_HOST="0.0.0.0:9999 --reload")
