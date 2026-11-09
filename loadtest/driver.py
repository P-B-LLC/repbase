"""Virtual users: one person scrolling Social while others train, plan and eat.

Closed loop on purpose. Each virtual user waits for an answer, thinks, and goes
again, which is what a phone does; when the server slows down the offered load
falls with it, and the slowdown shows up as latency rather than an ever-growing
queue inside this script.

Standard library only, so the driver can run anywhere Python does. Each user has
its own seeded random stream, so a run makes the same choices as the last one.
"""

import http.client
import json
import random
import threading
import time
from datetime import date, timedelta
from urllib.parse import urlsplit

CORE_GROUPS = ("workout", "planner", "food")
SOCIAL_GROUPS = ("first_page", "later_page", "impressions", "like", "following")
GROUPS = SOCIAL_GROUPS + CORE_GROUPS

#: How long someone spends on a 20-card page, and how often the app reports
#: what was on screen while they do: 30 posts a minute and the app's own
#: 10-second flush. Faster than the plan's illustrative 12 posts a minute.
PAGE_SECONDS = (25, 55)
IMPRESSION_SECONDS = 10
#: Later pages in one sitting. Zero is opening the app, reading a page, leaving.
LATER_PAGES = (0, 4)

FOOD_TERMS = ("oats", "banana", "chicken breast", "rice", "egg", "greek yogurt", "salmon", "apple")


class Run:
    def __init__(self, host, port, measure_after, deadline):
        self.host = host
        self.port = port
        self.measure_after = measure_after
        self.deadline = deadline
        self.stop = threading.Event()
        self.samples = []

    def running(self):
        return not self.stop.is_set() and time.monotonic() < self.deadline

    def think(self, seconds):
        self.stop.wait(max(0.0, min(seconds, self.deadline - time.monotonic())))


class Client:
    """A new connection per request, which is how nginx talks to gunicorn."""

    def __init__(self, run, token):
        self.run = run
        self.headers = {
            "Authorization": f"Token {token}", "Content-Type": "application/json", "Connection": "close",
        }

    def call(self, group, method, path, body=None):
        """(status, parsed JSON or None). Status 0 means no response arrived."""
        # Every virtual user wakes at the deadline. Letting them all fire then
        # is a burst no real traffic makes, and it overflows the accept queue.
        if not self.run.running():
            return 0, None
        payload = None if body is None else json.dumps(body)
        started = time.monotonic()
        status, headers, data = 0, {}, None
        connection = http.client.HTTPConnection(self.run.host, self.run.port, timeout=30)
        try:
            connection.request(method, path, payload, self.headers)
            response = connection.getresponse()
            raw = response.read()
            status, headers = response.status, response.headers
            if raw and headers.get("Content-Type", "").startswith("application/json"):
                data = json.loads(raw)
        except (OSError, http.client.HTTPException, ValueError):
            pass
        finally:
            connection.close()
        elapsed = (time.monotonic() - started) * 1000
        if started >= self.run.measure_after:
            self.run.samples.append((
                group, status, elapsed,
                int(headers.get("X-Loadtest-Sql-Count", 0) or 0) if headers else 0,
                float(headers.get("X-Loadtest-Sql-Ms", 0) or 0) if headers else 0.0,
                int(headers.get("X-Loadtest-Metric-Sql", 0) or 0) if headers else 0,
            ))
        return status, data


def _results(data):
    return data.get("results", []) if isinstance(data, dict) else []


def _next_path(data):
    link = data.get("next") if isinstance(data, dict) else None
    if not link:
        return None
    parts = urlsplit(link)
    return f"{parts.path}?{parts.query}"


def social_user(run, client, rng):
    """Open For You, read a few pages, report what was on screen."""
    while run.running():
        _, data = client.call("first_page", "GET", "/api/v1/social/for-you/?page_size=20")
        later_pages = rng.randint(*LATER_PAGES)
        for page in range(later_pages + 1):
            posts = [post["id"] for post in _results(data) if isinstance(post, dict) and "id" in post]
            elapsed, reading = 0, rng.uniform(*PAGE_SECONDS)
            while elapsed < reading and run.running():
                run.think(IMPRESSION_SECONDS)
                elapsed += IMPRESSION_SECONDS
                shown = rng.sample(posts, min(5, len(posts)))
                if shown:
                    client.call("impressions", "POST", "/api/v1/social/impressions/", {"events": [
                        {"post": post, "dwell_ms": rng.randint(300, 9000), "surface": "for_you"} for post in shown
                    ]})
                if shown and rng.random() < 0.05:
                    client.call("like", "POST", f"/api/v1/social/posts/{shown[0]}/like/")
            if not run.running():
                return
            if rng.random() < 0.1:
                client.call("following", "GET", "/api/v1/social/feed/")
            path = _next_path(data)
            if path is None or page == later_pages:
                break
            _, data = client.call("later_page", "GET", path)


def workout_user(run, client, rng, exercise_id):
    """Start a session, log sets between rests, finish."""
    while run.running():
        status, session = client.call("workout", "POST", "/api/v1/sessions/", {})
        if status != 201 or not isinstance(session, dict):
            run.think(5)
            continue
        client.call("workout", "POST", f"/api/v1/sessions/{session['id']}/start/", {})
        status, line = client.call("workout", "POST", "/api/v1/session-exercises/", {
            "session": session["id"], "exercise": exercise_id, "order": 1,
        })
        if status == 201 and isinstance(line, dict):
            for number in range(1, 5):
                if not run.running():
                    return
                run.think(rng.uniform(6, 12))
                client.call("workout", "POST", "/api/v1/set-entries/", {
                    "session_exercise": line["id"], "set_number": number, "weight_kg": "80.00", "reps": 8,
                })
        run.think(rng.uniform(2, 5))
        client.call("workout", "POST", f"/api/v1/sessions/{session['id']}/end/", {})
        run.think(rng.uniform(2, 5))


def planner_user(run, client, rng):
    """Look at the week, add a task, tick one off."""
    while run.running():
        today = date.today()
        client.call("planner", "GET", f"/api/v1/planner/?start={today}&end={today + timedelta(days=6)}")
        run.think(rng.uniform(3, 6))
        status, entry = client.call("planner", "POST", "/api/v1/planner/", {
            "kind": "task", "title": "Load test task", "category": "habit", "scheduled_date": str(today),
        })
        run.think(rng.uniform(3, 6))
        if status == 201 and isinstance(entry, dict):
            client.call("planner", "PATCH", f"/api/v1/planner/{entry['id']}/", {"is_complete": True})
        run.think(rng.uniform(3, 6))


def food_user(run, client, rng):
    """Search for a food, read today's meals, log one."""
    while run.running():
        today = date.today()
        term = rng.choice(FOOD_TERMS).replace(" ", "%20")
        client.call("food", "GET", f"/api/v1/food/search/?q={term}")
        run.think(rng.uniform(2, 5))
        client.call("food", "GET", f"/api/v1/food/meals/?date={today}")
        run.think(rng.uniform(2, 5))
        status, meal = client.call("food", "POST", "/api/v1/food/meals/", {
            "name": f"Meal {rng.randint(1, 1000000)}", "date": str(today),
        })
        if status == 201 and isinstance(meal, dict):
            client.call("food", "POST", "/api/v1/food/entries/", {
                "meal": meal["id"], "name": "Rice", "servings": "1", "calories": "200",
                "protein_grams": "4", "carbohydrate_grams": "44", "fat_grams": "1",
            })
        run.think(rng.uniform(3, 6))


def _start(run, tokens, target, seed, ramp, *extra):
    threads = []
    for index, token in enumerate(tokens):
        rng = random.Random(f"{seed}:{target.__name__}:{index}")

        def body(token=token, rng=rng):
            run.think(rng.uniform(0, ramp))
            target(run, Client(run, token), rng, *extra)

        thread = threading.Thread(target=body, daemon=True)
        thread.start()
        threads.append(thread)
    return threads


def apply_load(host, port, tokens, *, feed_users, core_users, seconds, ramp, seed, exercise_id):
    """Run the journeys and return (samples, measured seconds). The ramp is not measured."""
    now = time.monotonic()
    run = Run(host, port, measure_after=now + ramp, deadline=now + ramp + seconds)
    needed = feed_users + core_users
    if needed > len(tokens):
        raise ValueError(f"{needed} virtual users need {needed} tokens; the seed has {len(tokens)}.")
    third = core_users // 3
    workout = core_users - 2 * third
    groups = [
        (feed_users, social_user, ()),
        (workout, workout_user, (exercise_id,)),
        (third, planner_user, ()),
        (third, food_user, ()),
    ]
    threads, cursor = [], 0
    for count, target, extra in groups:
        threads += _start(run, tokens[cursor:cursor + count], target, seed, ramp, *extra)
        cursor += count
    for thread in threads:
        thread.join(timeout=ramp + seconds + 60)
    run.stop.set()
    return list(run.samples), seconds
