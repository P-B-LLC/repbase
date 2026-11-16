"""EXPLAIN the statements real requests actually issue.

A hand-written list of queries drifts away from the code. So this makes the
real request, records every statement the view ran with its parameters, and
asks PostgreSQL for the plan of the slowest few of each shape. SELECTs are
EXPLAIN ANALYZEd; writes are planned only, and each journey is rolled back.
"""

import json
import re
import time
from datetime import date, timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.test import Client
from rest_framework.authtoken.models import Token

from core.models import Exercise, Post, RepbaseUser
from core.recommendations import _count_engagement

SHAPE = re.compile(r"\b\d+(\.\d+)?\b|'[^']*'")


def shape_of(sql):
    return re.sub(r"\s+", " ", SHAPE.sub("?", sql))[:400]


class Recorder:
    def __init__(self):
        self.rows = []

    def __call__(self, execute, sql, params, many, context):
        started = time.perf_counter()
        try:
            return execute(sql, params, many, context)
        finally:
            if not many:
                self.rows.append((sql, params, (time.perf_counter() - started) * 1000))


class Command(BaseCommand):
    help = "Capture plans for the important feed, workout, food and planner requests (config.loadtest only)."

    def add_arguments(self, parser):
        parser.add_argument("--output", required=True)
        parser.add_argument("--per-journey", type=int, default=6)

    def handle(self, *args, **options):
        if not getattr(settings, "LOADTEST", False):
            raise CommandError("Only under config.loadtest.")
        out = Path(options["output"])
        out.mkdir(parents=True, exist_ok=True)
        token = Token.objects.select_related("user").order_by("user_id").first()
        if token is None:
            raise CommandError("No seeded virtual users.")
        client = Client(HTTP_HOST="127.0.0.1", HTTP_AUTHORIZATION=f"Token {token.key}")
        profile = RepbaseUser.objects.get(user=token.user)
        today = date.today()
        exercise = Exercise.objects.filter(created_by__isnull=True).order_by("pk").first()

        def post(path, body):
            return client.post(path, json.dumps(body), content_type="application/json")

        def for_you_later():
            first = client.get("/api/v1/social/for-you/?page_size=20").json()
            path = first["next"].split("127.0.0.1", 1)[-1]
            return client.get(path)

        def set_save():
            session = post("/api/v1/sessions/", {}).json()
            line = post("/api/v1/session-exercises/", {"session": session["id"], "exercise": exercise.pk, "order": 1}).json()
            return post("/api/v1/set-entries/", {
                "session_exercise": line["id"], "set_number": 1, "weight_kg": "80.00", "reps": 8,
            })

        def planner_complete():
            entry = post("/api/v1/planner/", {
                "kind": "task", "title": "Plan", "category": "habit", "scheduled_date": str(today),
            }).json()
            return client.patch(f"/api/v1/planner/{entry['id']}/", json.dumps({"is_complete": True}),
                                content_type="application/json")

        def meal_log():
            meal = post("/api/v1/food/meals/", {"name": "Explain meal", "date": str(today)}).json()
            return post("/api/v1/food/entries/", {
                "meal": meal["id"], "name": "Rice", "servings": "1", "calories": "200", "protein_grams": "4",
                "carbohydrate_grams": "44", "fat_grams": "1",
            })

        newest = list(Post.objects.exclude(kind="repost").order_by("-published_at").values_list("pk", flat=True)[:500])
        journeys = {
            "feed-for-you-first-page": lambda: client.get("/api/v1/social/for-you/?page_size=20"),
            "feed-for-you-later-page": for_you_later,
            "feed-following": lambda: client.get("/api/v1/social/feed/"),
            "feed-impressions": lambda: post("/api/v1/social/impressions/", {
                "events": [{"post": pk, "dwell_ms": 2000, "surface": "for_you"} for pk in newest[:50]],
            }),
            "feed-stats-recount-500-posts": lambda: _count_engagement(newest),
            "workout-set-save": set_save,
            "workout-history": lambda: client.get("/api/v1/sessions/"),
            "planner-week": lambda: client.get(f"/api/v1/planner/?start={today}&end={today + timedelta(days=6)}"),
            "planner-complete": planner_complete,
            "food-search": lambda: client.get("/api/v1/food/search/?q=oats"),
            "food-day": lambda: client.get(f"/api/v1/food/meals/?date={today}"),
            "food-log-meal": meal_log,
        }
        summary = {}
        for name, journey in journeys.items():
            summary[name] = self.capture(name, journey, out, options["per_journey"])
            self.stdout.write(f"  {name}: {len(summary[name])} plans")
        (out / "summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True))

    def capture(self, name, journey, out, keep):
        recorder = Recorder()
        with transaction.atomic():
            with connection.execute_wrapper(recorder):
                journey()
            slowest = {}
            for sql, params, ms in recorder.rows:
                if re.match(r"\s*(SAVEPOINT|RELEASE|ROLLBACK)", sql):
                    continue
                shape = shape_of(sql)
                if shape not in slowest or ms > slowest[shape][2]:
                    slowest[shape] = (sql, params, ms)
            chosen = sorted(slowest.values(), key=lambda row: row[2], reverse=True)[:keep]
            found, text = [], []
            with connection.cursor() as cursor:
                for sql, params, ms in chosen:
                    analyze = "ANALYZE, BUFFERS, " if sql.lstrip().upper().startswith("SELECT") else ""
                    cursor.execute(f"EXPLAIN ({analyze}FORMAT TEXT) {sql}", params)
                    plan = "\n".join(row[0] for row in cursor.fetchall())
                    text.append(f"-- {ms:.1f} ms when captured\n-- {sql[:600]}\n{plan}\n")
                    found.append({
                        "captured_ms": round(ms, 2),
                        "statement": shape_of(sql)[:200],
                        "sequential_scans": sorted(set(re.findall(r"Seq Scan on (\w+)", plan))),
                        "execution_ms": _execution_ms(plan),
                    })
            transaction.set_rollback(True)
        (out / f"{name}.txt").write_text("\n".join(text))
        return found


def _execution_ms(plan):
    match = re.search(r"Execution Time: ([\d.]+) ms", plan)
    return float(match.group(1)) if match else None
