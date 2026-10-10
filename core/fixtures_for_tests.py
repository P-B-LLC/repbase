"""Builders shared by the test modules. Not itself a test module.

Named so the test runner's `test*.py` pattern passes it by: it holds no tests,
only the things several suites need to make -- finished sessions, posts of
each kind, photos with metadata in them, real video files.
"""

import base64
import io
import os
import tempfile
from datetime import timedelta
from decimal import Decimal

from django.test import override_settings
from django.utils import timezone

from .models import (
    Exercise,
    FoodEntry,
    FoodMeal,
    PlannerEntry,
    Post,
    PostMeal,
    PostMealEntry,
    PostPlannerEntry,
    PostWorkout,
    PostWorkoutExercise,
    SessionExercise,
    SetEntry,
    WorkoutSession,
)

VIDEO_FIXTURES = os.path.join(os.path.dirname(__file__), "test_fixtures", "video")


def video_fixture(name):
    with open(os.path.join(VIDEO_FIXTURES, name), "rb") as handle:
        return handle.read()


class TemporaryMediaMixin:
    """Uploaded files go to a throwaway directory, not the dev server's.

    The test database is discarded after a run; files written through the
    real settings are not, which is how a suite once left megabytes of test
    photos among the real ones.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._media = tempfile.TemporaryDirectory(prefix="repbase-test-media-", ignore_cleanup_errors=True)
        cls._media_override = override_settings(MEDIA_ROOT=cls._media.name)
        cls._media_override.enable()

    @classmethod
    def tearDownClass(cls):
        try:
            cls._media_override.disable()
        finally:
            try:
                cls._media.cleanup()
            finally:
                super().tearDownClass()


#: For suites that make many accounts and test nothing about passwords.
#: PBKDF2 at Django's default work factor is deliberately slow, and a suite
#: making a dozen accounts per test spends most of its time hashing.
FAST_PASSWORDS = override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"]
)


def make_account(username):
    """An account, its profile and a token, for use in setUpTestData."""
    from django.contrib.auth import get_user_model
    from rest_framework.authtoken.models import Token

    from .models import RepbaseUser

    user = get_user_model().objects.create_user(
        username=username, email=f"{username}@example.com", password="StrongPass!234",
        first_name=username.title(), last_name="Tester",
    )
    profile = RepbaseUser.objects.create(user=user)
    return user, profile, Token.objects.create(user=user)


# ------------------------------------------------------------------ sources

def finished_session(owner, exercise_name="Back Squat", weight="100.00", reps=5):
    started = timezone.now() - timedelta(hours=1)
    session = WorkoutSession.objects.create(
        repbase_user=owner,
        status=WorkoutSession.Status.COMPLETED,
        started_at=started,
        ended_at=started + timedelta(minutes=45),
    )
    exercise, _ = Exercise.objects.get_or_create(name=exercise_name, created_by=None)
    line = SessionExercise.objects.create(session=session, exercise=exercise, order=1)
    for number in (1, 2, 3):
        SetEntry.objects.create(
            session_exercise=line,
            set_number=number,
            weight_kg=Decimal(weight),
            reps=reps,
            completed_at=timezone.now(),
        )
    return session


def meal_with_foods(owner, foods=(("Oats", "300.00"), ("Banana", "100.00"))):
    meal = FoodMeal.objects.create(owner=owner, date=timezone.now().date(), name="Breakfast", position=1)
    for position, (name, calories) in enumerate(foods, start=1):
        FoodEntry.objects.create(meal=meal, name=name, calories=Decimal(calories), position=position)
    return meal


def planner_entry(owner, title="Deload week", category="workout"):
    return PlannerEntry.objects.create(
        owner=owner,
        kind=PlannerEntry.Kind.TASK,
        title=title,
        category=category,
        scheduled_date=timezone.now().date(),
    )


# -------------------------------------------------------------------- posts

def aged(post, hours=0, minutes=0):
    """Backdate a post. created_at is auto_now_add, so it is set afterwards."""
    stamp = timezone.now() - timedelta(hours=hours, minutes=minutes)
    Post.objects.filter(pk=post.pk).update(created_at=stamp)
    post.created_at = stamp
    return post


def workout_post(author, workout_type="lifting", caption="", hours_ago=1, visibility="public",
                 exercise="Bench Press"):
    post = Post.objects.create(
        author=author, kind=Post.Kind.WORKOUT, caption=caption or f"{workout_type} day",
        visibility=visibility,
    )
    snapshot = PostWorkout.objects.create(
        post=post, title=f"{workout_type.title()} session", workout_type=workout_type,
        performed_at=timezone.now(),
    )
    PostWorkoutExercise.objects.create(post_workout=snapshot, name=exercise, order=1, set_count=3)
    return aged(post, hours=hours_ago)


def meal_post(author, caption="", hours_ago=1, visibility="public"):
    post = Post.objects.create(
        author=author, kind=Post.Kind.MEAL, caption=caption or "lunch", visibility=visibility
    )
    snapshot = PostMeal.objects.create(post=post, name="Lunch", date=timezone.now().date())
    PostMealEntry.objects.create(post_meal=snapshot, name="Rice", calories=Decimal("200.00"))
    return aged(post, hours=hours_ago)


def planner_post(author, category="study", hours_ago=1):
    post = Post.objects.create(author=author, kind=Post.Kind.PLANNER, caption="planning")
    PostPlannerEntry.objects.create(
        post=post, kind="task", title="Plan", category=category,
        scheduled_date=timezone.now().date(),
    )
    return aged(post, hours=hours_ago)


# ------------------------------------------------------------------- photos

def gps_exif(orientation=1):
    from PIL import Image

    exif = Image.Exif()
    exif[0x0112] = orientation
    exif[0x010F] = "PhoneMaker"
    exif[0x0110] = "Model-With-Serial"
    exif[0x8825] = {1: "N", 2: (40.0, 26.0, 46.0), 3: "W", 4: (79.0, 58.0, 56.0)}
    return exif


def photo_bytes(fmt="JPEG", size=(64, 48), exif=None, color=(200, 30, 30)):
    from PIL import Image

    image = Image.new("RGB", size, color)
    buffer = io.BytesIO()
    options = {"format": fmt}
    if exif is not None:
        options["exif"] = exif
    image.save(buffer, **options)
    return buffer.getvalue()


def as_base64(data):
    return base64.b64encode(data).decode("ascii")
