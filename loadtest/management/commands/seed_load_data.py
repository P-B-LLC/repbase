"""Fill a load-test database with a community of a realistic shape.

Deterministic for a given --seed, so two runs start from the same data. Rows go
in through COPY: millions of them through the ORM would take hours.

The shape matters more than the totals. Engagement is heavily skewed, one
creator is followed by most accounts and one post has been seen by most of
them, because those are the rows that make a query plan fall over. Private
history (sessions, sets, planner, meals) is deep for the accounts that drive the
load and shallow for everyone else, so those tables have realistic depth without
the seed taking an hour.
"""

import hashlib
import json
import random
import uuid
from datetime import datetime, timedelta, timezone as datetime_timezone

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

POPULAR_CREATOR = 1
POPULAR_POST = 1
FIRST_VIRTUAL_USER = 101

KINDS = ("workout", "meal", "media", "planner", "repost")
KIND_WEIGHTS = (50, 25, 15, 5, 5)
WORKOUT_TYPES = ("lifting", "running", "biking", "swimming")
EXERCISES = (
    "Bench Press", "Back Squat", "Deadlift", "Overhead Press", "Barbell Row", "Pull Up", "Dip", "Lunge",
    "Romanian Deadlift", "Leg Press", "Lat Pulldown", "Bicep Curl", "Tricep Pushdown", "Face Pull",
    "Hip Thrust", "Calf Raise", "Plank", "Push Up", "Front Squat", "Incline Bench Press",
)
DISCIPLINES = ("powerlifting", "bodybuilding", "running", "cycling", "crossfit", "general_fitness")
#: The food search journey asks for these and nothing else.
FOOD_TERMS = (
    "oats", "banana", "chicken breast", "rice", "egg", "greek yogurt", "salmon", "apple", "almonds",
    "peanut butter", "broccoli", "sweet potato", "tuna", "cottage cheese", "avocado", "whey protein",
    "bread", "milk", "pasta", "beef",
)


class Command(BaseCommand):
    help = "Seed a load-test database (config.loadtest only)."

    def add_arguments(self, parser):
        parser.add_argument("--seed", type=int, default=20261109)
        parser.add_argument("--accounts", type=int, default=20000)
        parser.add_argument("--follows", type=int, default=300000)
        parser.add_argument("--posts", type=int, default=60000)
        parser.add_argument("--views", type=int, default=3000000)
        parser.add_argument("--likes", type=int, default=600000)
        parser.add_argument("--comments", type=int, default=60000)
        parser.add_argument("--saves", type=int, default=60000)
        parser.add_argument("--popular-creator-followers", type=int, default=15000)
        parser.add_argument("--popular-post-viewers", type=int, default=18000)
        parser.add_argument("--popular-post-likes", type=int, default=8000)
        parser.add_argument("--virtual-users", type=int, default=400)

    def handle(self, *args, **options):
        if not getattr(settings, "LOADTEST", False):
            raise CommandError("Seeding is only allowed under config.loadtest.")
        self.options = options
        self.rng = random.Random(options["seed"])
        self.now = datetime.now(datetime_timezone.utc)
        self.accounts = options["accounts"]
        self.virtual_users = set(range(FIRST_VIRTUAL_USER, FIRST_VIRTUAL_USER + options["virtual_users"]))
        if self.accounts < FIRST_VIRTUAL_USER + options["virtual_users"]:
            raise CommandError("--accounts is too small for that many virtual users.")
        with connection.cursor() as cursor:
            self.cursor = cursor
            cursor.execute("SET synchronous_commit = off")
            cursor.execute("SELECT count(*) FROM auth_user")
            if cursor.fetchone()[0]:
                raise CommandError("This database already has accounts.")
            cursor.execute("SELECT id FROM core_exercise WHERE created_by_id IS NULL ORDER BY id LIMIT 200")
            self.exercise_ids = [row[0] for row in cursor.fetchall()]
            if not self.exercise_ids:
                raise CommandError("The exercise catalogue is empty; run migrate with the library sync on.")
            self.seed_accounts()
            self.seed_follows()
            self.seed_posts()
            self.seed_engagement()
            self.seed_views()
            self.seed_private_history()
            self.seed_food_search()
            for table in ("auth_user", "core_repbaseuser", "core_post", "core_postworkout", "core_postmeal",
                          "core_workoutsession", "core_sessionexercise", "core_foodmeal"):
                cursor.execute(
                    f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), (SELECT max(id) FROM {table}))"
                )
            cursor.execute("ANALYZE")
        self.stdout.write("Seeded.")

    def copy(self, table, columns, rows):
        quoted = ", ".join(f'"{name}"' for name in columns)
        written = 0
        with self.cursor.copy(f"COPY {table} ({quoted}) FROM STDIN") as copy:
            for row in rows:
                copy.write_row(row)
                written += 1
        self.stdout.write(f"  {table}: {written:,}")

    def ago(self, days):
        return self.now - timedelta(days=days)

    def skewed(self, count, power=3.0):
        """0 to count-1, with low numbers much more likely."""
        return int(count * self.rng.random() ** power)

    # ------------------------------------------------------------- accounts

    def seed_accounts(self):
        now, rng = self.now, self.rng
        joined = {pk: self.ago(rng.uniform(35, 365) if pk <= 200 or pk in self.virtual_users or rng.random() > 0.06
                               else rng.uniform(0.5, 30))
                  for pk in range(1, self.accounts + 1)}
        self.joined = joined
        self.copy("auth_user", (
            "id", "password", "is_superuser", "username", "first_name", "last_name", "email",
            "is_staff", "is_active", "date_joined",
        ), ((pk, "!", False, f"member{pk}", "Member", str(pk), f"member{pk}@loadtest.invalid",
             False, pk % 500 != 0, joined[pk]) for pk in joined))
        self.copy("core_repbaseuser", (
            "id", "user_id", "height_cm", "weight_kg", "created_at", "updated_at", "unit_preference", "bio",
            "shows_height", "shows_target_weight", "shows_weight", "daily_step_goal", "time_zone",
            "is_profile_public",
        ), ((pk, pk, 175, 78, joined[pk], joined[pk], "metric", "", False, False, False, 8000, "UTC", pk % 20 != 0)
            for pk in joined))
        tokens = ((hashlib.sha1(f"{self.options['seed']}:{pk}".encode()).hexdigest(), now, pk)
                  for pk in sorted(self.virtual_users))
        self.copy("authtoken_token", ("key", "created", "user_id"), tokens)
        self.copy("core_userdiscipline", ("discipline", "created_at", "profile_id"), (
            (discipline, now, pk) for pk in sorted(self.virtual_users)
            for discipline in rng.sample(DISCIPLINES, 2)
        ))

    def seed_follows(self):
        rng, accounts = self.rng, self.accounts
        pairs = {(follower, POPULAR_CREATOR) for follower in rng.sample(
            range(2, accounts + 1), min(self.options["popular_creator_followers"], accounts - 1))}
        for follower in sorted(self.virtual_users):
            for _ in range(150):
                following = 1 + self.skewed(accounts)
                if following != follower:
                    pairs.add((follower, following))
        while len(pairs) < self.options["follows"]:
            follower, following = rng.randint(1, accounts), 1 + self.skewed(accounts)
            if follower != following:
                pairs.add((follower, following))
        self.copy("core_follow", ("follower_id", "following_id", "created_at"), (
            (follower, following, self.ago(rng.uniform(1, 200))) for follower, following in sorted(pairs)
        ))

    # ---------------------------------------------------------------- posts

    def seed_posts(self):
        rng, accounts = self.rng, self.accounts
        count = self.options["posts"]
        self.post_author = {}
        self.post_kind = {}
        self.post_time = {}
        private = set()
        posts = []
        for pk in range(1, count + 1):
            if pk == POPULAR_POST:
                kind, author, published, visibility = "media", POPULAR_CREATOR, self.ago(2), "public"
            else:
                kind = rng.choices(KINDS, KIND_WEIGHTS)[0]
                author = POPULAR_CREATOR if rng.random() < 0.01 else 1 + self.skewed(accounts, 2.0)
                published = self.ago(rng.uniform(0, 90))
                roll = rng.random()
                visibility = "private" if roll < 0.02 else "followers" if roll < 0.10 else "public"
            self.post_author[pk], self.post_kind[pk], self.post_time[pk] = author, kind, published
            if visibility == "private":
                private.add(pk)
            posts.append((pk, kind, visibility, author, published))
        self.private = private
        # Which posts are popular has nothing to do with their ids.
        self.by_popularity = [POPULAR_POST] + rng.sample(
            [pk for pk in range(2, count + 1) if pk not in private and self.post_kind[pk] != "repost"],
            k=sum(1 for pk in range(2, count + 1) if pk not in private and self.post_kind[pk] != "repost"),
        )
        videos = {POPULAR_POST} | {pk for pk in self.post_kind if self.post_kind[pk] == "media" and rng.random() < 0.4}
        reposted = set()

        def post_rows():
            for pk, kind, visibility, author, published in posts:
                source = None
                if kind == "repost":
                    while source is None:
                        candidate = self.popular_post()
                        if self.post_author[candidate] != author and (author, candidate) not in reposted:
                            source = candidate
                    reposted.add((author, source))
                image = "seed/photo.jpg" if kind == "media" and pk not in videos else None
                yield (pk, kind, f"Post number {pk}", visibility, published, published, author, source, image, True,
                       False, False, uuid.UUID(int=rng.getrandbits(128)), "approved", published)

        self.copy("core_post", (
            "id", "kind", "caption", "visibility", "created_at", "updated_at", "author_id", "repost_of_id", "image",
            "shows_weights", "is_hidden", "moderation_child_hold", "moderation_generation", "moderation_status",
            "published_at",
        ), post_rows())
        workouts = [pk for pk in self.post_kind if self.post_kind[pk] == "workout"]
        meals = [pk for pk in self.post_kind if self.post_kind[pk] == "meal"]
        self.copy("core_postworkout", ("id", "title", "workout_type", "performed_at", "duration_seconds", "post_id"), (
            (pk, "Session", rng.choice(WORKOUT_TYPES), self.post_time[pk], 3600, pk) for pk in workouts
        ))
        self.copy("core_postworkoutexercise", ("name", "order", "set_count", "post_workout_id"), (
            (name, position, rng.randint(1, 6), pk) for pk in workouts
            for position, name in enumerate(rng.sample(EXERCISES, 4))
        ))
        self.copy("core_postmeal", ("id", "name", "date", "post_id", "cooking_instructions"), (
            (pk, "Lunch", self.post_time[pk].date(), pk, "") for pk in meals
        ))
        self.copy("core_postmealentry", (
            "name", "servings", "calories", "protein_grams", "carbohydrate_grams", "fat_grams", "position",
            "post_meal_id",
        ), (("Rice", 1, 200, 4, 44, 1, position, pk) for pk in meals for position in range(2)))
        self.copy("core_postplannerentry", (
            "kind", "title", "category", "scheduled_date", "is_complete", "post_id",
        ), (("task", "Morning run", "habit", self.post_time[pk].date(), True, pk)
            for pk in self.post_kind if self.post_kind[pk] == "planner"))
        self.copy("core_postvideo", (
            "file", "content_type", "codec", "duration_ms", "width", "height", "size_bytes", "has_audio", "status",
            "reviewed_at", "created_at", "updated_at", "post_id", "processing_error", "processing_state",
            "thumbnail_key", "variants",
        ), ((f"seed/clip-{pk}.mp4", "video/mp4", "h264", 15000, 1080, 1920, 4000000, True, "approved",
             self.post_time[pk], self.post_time[pk], self.post_time[pk], pk, "", "ready", "", "{}")
            for pk in sorted(videos)))

    def popular_post(self):
        return self.by_popularity[self.skewed(len(self.by_popularity), 2.5)]

    def when_after(self, published):
        return published + (self.now - published) * self.rng.random()

    # ----------------------------------------------------------- engagement

    def seed_engagement(self):
        rng, accounts = self.rng, self.accounts
        likes = {(user, POPULAR_POST) for user in rng.sample(
            range(2, accounts + 1), min(self.options["popular_post_likes"], accounts - 1))}
        for user in sorted(self.virtual_users):
            for _ in range(100):
                post = self.popular_post()
                if self.post_author[post] != user:
                    likes.add((user, post))
        while len(likes) < self.options["likes"]:
            user, post = 1 + self.skewed(accounts, 1.6), self.popular_post()
            if self.post_author[post] != user:
                likes.add((user, post))
        self.copy("core_postlike", ("post_id", "user_id", "created_at"), (
            (post, user, self.when_after(self.post_time[post])) for user, post in sorted(likes)
        ))

        def comments():
            for _ in range(self.options["comments"]):
                post = self.popular_post()
                when = self.when_after(self.post_time[post])
                yield ("Nice work", when, when, rng.randint(1, accounts), post, False)

        self.copy("core_postcomment", ("body", "created_at", "updated_at", "author_id", "post_id", "is_hidden"),
                  comments())
        feedback = set()
        while len(feedback) < self.options["comments"] // 3:
            feedback.add((rng.randint(1, accounts), self.popular_post()))
        self.copy("core_postfeedback", ("kind", "created_at", "post_id", "viewer_id"), (
            (rng.choice(("not_interested", "fewer_like_this", "more_like_this")),
             self.when_after(self.post_time[post]), post, viewer)
            for viewer, post in sorted(feedback)
        ))
        by_kind = {"workout": [], "meal": []}
        for post in self.by_popularity:
            if self.post_kind[post] in by_kind:
                by_kind[self.post_kind[post]].append(post)
        saves = self.options["saves"]

        def saved(kind, count):
            posts = by_kind[kind]
            seen = set()
            while len(seen) < min(count, len(posts) * 2):
                seen.add((1 + self.skewed(accounts, 1.4), posts[self.skewed(len(posts), 2.5)]))
            return sorted(seen)

        self.copy("core_workouttemplate", (
            "name", "description", "created_at", "updated_at", "owner_id", "workout_type", "source_post_id", "activity",
        ), ((f"Saved {post}", "", self.post_time[post], self.post_time[post], owner, "lifting", post, "lifting")
            for owner, post in saved("workout", saves * 2 // 3)))
        self.copy("core_savedfoodmeal", (
            "name", "created_at", "updated_at", "owner_id", "source_post_id", "cooking_instructions",
        ), ((f"Saved {post}", self.post_time[post], self.post_time[post], owner, post, "")
            for owner, post in saved("meal", saves // 3)))

    def seed_views(self):
        rng, accounts = self.rng, self.accounts
        target = self.options["views"]
        heavy = sorted(self.virtual_users)
        popular_viewers = set(rng.sample(range(2, accounts + 1), min(self.options["popular_post_viewers"], accounts - 1)))
        average = max(5, (target - 1000 * len(heavy)) / max(accounts - len(heavy), 1))
        written = 0

        def view_rows():
            nonlocal written
            for viewer in range(1, accounts + 1):
                if written >= target:
                    return
                wanted = 1000 if viewer in self.virtual_users else min(4000, max(3, int(rng.expovariate(1 / average))))
                posts = {POPULAR_POST} if viewer in popular_viewers else set()
                for _ in range(wanted):
                    posts.add(self.popular_post())
                for post in posts:
                    if self.post_author[post] == viewer:
                        continue
                    last_seen = self.when_after(self.post_time[post])
                    dwell = rng.randint(200, 30000)
                    written += 1
                    yield (rng.randint(1, 3), dwell, dwell >= 12000, dwell < 1500,
                           "for_you" if rng.random() < 0.7 else "following",
                           last_seen, last_seen, post, viewer, "")

        self.copy("core_postview", (
            "view_count", "total_dwell_ms", "completed", "skipped", "surface", "first_seen_at", "last_seen_at",
            "post_id", "viewer_id", "reason",
        ), view_rows())

    # ------------------------------------------------------ private history

    def seed_private_history(self):
        rng, now = self.rng, self.now
        sessions, session_lines, set_rows = [], [], []
        next_session = next_line = 1
        for owner in range(1, self.accounts + 1):
            deep = owner in self.virtual_users
            for _ in range(120 if deep else 10):
                started = self.ago(rng.uniform(1, 365))
                sessions.append((next_session, started, started + timedelta(hours=1), started, owner, "completed",
                                 started, "{}", False, ""))
                if deep:
                    for order in range(4):
                        session_lines.append((next_line, order, "", rng.choice(self.exercise_ids), next_session))
                        set_rows.extend(
                            (number, 60, 8, started, started, started, next_line, "normal") for number in range(1, 4)
                        )
                        next_line += 1
                next_session += 1
        self.copy("core_workoutsession", (
            "id", "started_at", "ended_at", "created_at", "repbase_user_id", "status", "updated_at", "activity_details",
            "is_manual_entry", "notes",
        ), sessions)
        self.copy("core_sessionexercise", ("id", "order", "notes", "exercise_id", "session_id"), session_lines)
        self.copy("core_setentry", (
            "set_number", "weight_kg", "reps", "completed_at", "created_at", "updated_at", "session_exercise_id",
            "set_type",
        ), set_rows)

        def planner_rows():
            for owner in range(1, self.accounts + 1):
                deep = owner in self.virtual_users
                for _ in range(500 if deep else 30):
                    offset = rng.uniform(-200, 30)
                    scheduled = (now + timedelta(days=offset)).date()
                    done = now + timedelta(days=offset) if offset < 0 and rng.random() < 0.7 else None
                    yield ("task", "Stretch", "habit", scheduled, done, "", now, now, owner, "normal", False)

        self.copy("core_plannerentry", (
            "kind", "title", "category", "scheduled_date", "completed_at", "notes", "created_at", "updated_at",
            "owner_id", "priority", "is_subtask",
        ), planner_rows())

        meal_rows, entry_rows = [], []
        next_meal = 1
        for owner in range(1, self.accounts + 1):
            deep = owner in self.virtual_users
            for day in range(1, (300 if deep else 20) + 1):
                date = (now - timedelta(days=day)).date()
                meal_rows.append((next_meal, date, "Meal 1", 0, now, now, owner))
                if deep:
                    entry_rows.extend(
                        ("Oats", 1, 300, 10, 54, 5, position, now, now, next_meal) for position in range(3)
                    )
                next_meal += 1
        self.copy("core_foodmeal", ("id", "date", "name", "position", "created_at", "updated_at", "owner_id"), meal_rows)
        self.copy("core_foodentry", (
            "name", "servings", "calories", "protein_grams", "carbohydrate_grams", "fat_grams", "position",
            "created_at", "updated_at", "meal_id",
        ), entry_rows)

    def seed_food_search(self):
        def result(term, index):
            return {"name": f"{term.title()} {index}", "brand": "Loadtest", "serving_description": "100 g",
                    "calories": 150, "protein_grams": 5, "carbohydrate_grams": 20, "fat_grams": 3}

        self.copy("core_foodsearchcache", ("term", "payload", "fetched_at"), (
            ("off:v1:search:" + term, json.dumps([result(term, index) for index in range(20)]), self.now)
            for term in FOOD_TERMS
        ))
