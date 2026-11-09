"""API metrics on their way from a request to the database.

Every API request used to update the same two counter rows for its route, in a
transaction, before its response went out. Under load that is a queue, and the
thing waiting in it is somebody's workout save.

So a request now adds to counters in Redis, one network round trip, and
`flush_api_metrics` moves the totals to the database every 30 to 60 seconds.

## Flushing exactly once

The counters accumulate under fixed "open" keys. A flush renames them, in one
transaction, to keys carrying a new batch id. A request arriving after that
simply starts new open keys, so nothing can be lost between reading and clearing.
Each batch is applied in one database transaction that also records a receipt
for its id, and is deleted from Redis only after that commits. A flush that
stops at any point leaves the batch where the next run finds it: if the receipt
exists the counts are already in, and it only deletes the leftovers.

A counter carries the five-minute bucket of the request that made it, so a late
flush still lands in the right bucket and the right day.

## When it cannot

Metrics are approximate and are allowed to be lost. They are not allowed to
affect the request that produced them, so `record` cannot raise, and it does not
fall back to SQL: that would add the old write load at the worst possible moment.
Each observation lost is counted, logged, and reported with the next batch.
"""

import logging
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone as datetime_timezone

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import caches
from django.db import connection, transaction

from .analytics_models import DailyActiveAccount, DailyApiMetric, MetricFlushReceipt, RecentApiMetric

logger = logging.getLogger(__name__)

#: A counter left in Redis this long with no flush is given up on. Refreshed by
#: every write, so this only matters when nothing is writing either.
BUFFER_TTL_SECONDS = 48 * 3600
#: How long a receipt is kept. Longer than a batch can survive in Redis, so a
#: retried batch can never outlive the record that it was applied.
RECEIPT_RETENTION_DAYS = 7
#: Rows per INSERT when applying a batch.
UPSERT_BATCH = 500
#: How often a process says it is losing observations, at most.
LOSS_LOG_SECONDS = 30
#: Accounts remembered per process as already marked active today.
MARKED_LIMIT = 200_000

_LOST_FIELD = "__lost__"
_KINDS = {"r": "requests", "c": "client_errors", "s": "server_errors", "t": "total_ms"}
_PARTS = ("counters", "max", "active")


def _namespace():
    return f"{settings.CACHES['default'].get('KEY_PREFIX') or 'rytivo'}:api-metrics"


def _open_key(part):
    return f"{_namespace()}:open:{part}"


def _batch_key(batch_id, part):
    return f"{_namespace()}:batch:{batch_id}:{part}"


def _registry_key():
    return f"{_namespace()}:batches"


_unavailable = object()


def _run(operation):
    """`operation(client)` under the cache's own protection, or `_unavailable`."""
    run = getattr(caches["default"], "run", None)
    if run is None:
        return _unavailable
    return run(operation, miss=_unavailable)


class _ProcessState:
    """What one web process remembers between requests."""

    def __init__(self):
        self.lock = threading.Lock()
        self.lost = 0
        self.last_loss_log = 0.0
        self.day = None
        self.marked = set()

    def needs_mark(self, day, user_id):
        with self.lock:
            if day != self.day:
                self.day, self.marked = day, set()
            return user_id not in self.marked

    def remember_mark(self, user_id):
        with self.lock:
            if len(self.marked) >= MARKED_LIMIT:
                self.marked = set()
            self.marked.add(user_id)

    def lose_one(self):
        with self.lock:
            self.lost += 1
            now = time.monotonic()
            due = now - self.last_loss_log >= LOSS_LOG_SECONDS
            if due:
                self.last_loss_log = now
            count = self.lost
        if due:
            logger.warning("API metrics could not be buffered; %d observations lost in this process so far.", count)

    def take_lost(self):
        with self.lock:
            lost, self.lost = self.lost, 0
            return lost

    def give_back_lost(self, count):
        with self.lock:
            self.lost += count


_state = _ProcessState()


def reset_process_state():
    """Forget per-process memory. For tests."""
    global _state
    _state = _ProcessState()


def record(endpoint, method, status, elapsed_ms, bucket, user_id=None):
    """Add one request to the shared counters. Never raises."""
    state = _state
    lost = 0
    try:
        stamp = int(bucket.timestamp())
        route = f"{stamp}|{method}|{endpoint}"
        day = bucket.astimezone(datetime_timezone.utc).date()
        mark = user_id is not None and state.needs_mark(day, user_id)
        lost = state.take_lost()

        def write(client):
            counters, maximum, active = (_open_key(part) for part in _PARTS)
            pipeline = client.pipeline(transaction=True)
            pipeline.hincrby(counters, f"r|{route}", 1)
            pipeline.hincrby(counters, f"t|{route}", elapsed_ms)
            if 400 <= status < 500:
                pipeline.hincrby(counters, f"c|{route}", 1)
            elif status >= 500:
                pipeline.hincrby(counters, f"s|{route}", 1)
            if lost:
                pipeline.hincrby(counters, _LOST_FIELD, lost)
            # GT: only ever raises the stored value, which is what a running
            # maximum needs and a plain write from another worker would break.
            pipeline.zadd(maximum, {route: elapsed_ms}, gt=True)
            pipeline.expire(counters, BUFFER_TTL_SECONDS)
            pipeline.expire(maximum, BUFFER_TTL_SECONDS)
            if mark:
                pipeline.sadd(active, f"{day.isoformat()}|{user_id}")
                pipeline.expire(active, BUFFER_TTL_SECONDS)
            pipeline.execute()
            return True

        if _run(write) is _unavailable:
            state.give_back_lost(lost)
            state.lose_one()
        elif mark:
            state.remember_mark(user_id)
    except Exception:  # noqa: BLE001 - observability must never change a response
        state.give_back_lost(lost)
        state.lose_one()


# ----------------------------------------------------------------- flushing


def _pending(client):
    return {value.decode() if isinstance(value, bytes) else value for value in client.smembers(_registry_key())}


def _claim(client):
    """Move the open counters into a new batch. Returns its id, or None if there
    was nothing to claim."""
    batch_id = uuid.uuid4().hex
    pipeline = client.pipeline(transaction=True)
    for part in _PARTS:
        pipeline.rename(_open_key(part), _batch_key(batch_id, part))
    pipeline.sadd(_registry_key(), batch_id)
    results = pipeline.execute(raise_on_error=False)
    renamed = results[:len(_PARTS)]
    for outcome in renamed:
        # RENAME of a key that does not exist is the expected way to find out
        # a part had nothing in it. Anything else is a real failure.
        if isinstance(outcome, Exception) and "no such key" not in str(outcome).lower():
            raise outcome
    if all(isinstance(outcome, Exception) for outcome in renamed):
        client.srem(_registry_key(), batch_id)
        return None
    return batch_id


def _text(value):
    return value.decode() if isinstance(value, bytes) else value


def _read(client, batch_id):
    pipeline = client.pipeline(transaction=False)
    pipeline.hgetall(_batch_key(batch_id, "counters"))
    pipeline.zrange(_batch_key(batch_id, "max"), 0, -1, withscores=True)
    pipeline.smembers(_batch_key(batch_id, "active"))
    counters, maxima, active = pipeline.execute()
    return parse_batch(
        {_text(key): int(value) for key, value in counters.items()},
        {_text(member): int(score) for member, score in maxima},
        {_text(member) for member in active},
    )


def parse_batch(counters, maxima, active):
    """{"routes": {(stamp, method, endpoint): counts}, "active": {(day, user id)},
    "lost": n} from the raw Redis contents."""
    routes = {}
    lost = counters.pop(_LOST_FIELD, 0)

    def route_of(text):
        stamp, method, endpoint = text.split("|", 2)
        return routes.setdefault((int(stamp), method, endpoint), {
            "requests": 0, "client_errors": 0, "server_errors": 0, "total_ms": 0, "max_ms": 0,
        })

    for field, value in counters.items():
        kind, route = field.split("|", 1)
        route_of(route)[_KINDS[kind]] += value
    for route, value in maxima.items():
        route_of(route)["max_ms"] = value
    marks = set()
    for member in active:
        day, user_id = member.split("|", 1)
        marks.add((day, int(user_id)))
    return {"routes": routes, "active": marks, "lost": lost}


def _discard(client, batch_id):
    pipeline = client.pipeline(transaction=True)
    pipeline.delete(*(_batch_key(batch_id, part) for part in _PARTS))
    pipeline.srem(_registry_key(), batch_id)
    pipeline.execute()
    return True


def _upsert(model, time_field, rows):
    """Add {(time value, endpoint, method): counts} to `model` in key order."""
    if not rows:
        return
    ops = connection.ops
    table = ops.quote_name(model._meta.db_table)
    quoted_time = ops.quote_name(time_field)
    field = model._meta.get_field(time_field)
    columns = (time_field, "endpoint", "method", "requests", "client_errors", "server_errors", "total_ms", "max_ms")
    single = "(" + ", ".join(["%s"] * len(columns)) + ")"
    # CASE rather than GREATEST, which SQLite does not have.
    update = (
        "requests = {t}.requests + excluded.requests, "
        "client_errors = {t}.client_errors + excluded.client_errors, "
        "server_errors = {t}.server_errors + excluded.server_errors, "
        "total_ms = {t}.total_ms + excluded.total_ms, "
        "max_ms = CASE WHEN excluded.max_ms > {t}.max_ms THEN excluded.max_ms ELSE {t}.max_ms END"
    ).format(t=table)
    ordered = sorted(rows.items(), key=lambda item: (str(item[0][0]), item[0][1], item[0][2]))
    with connection.cursor() as cursor:
        for start in range(0, len(ordered), UPSERT_BATCH):
            chunk = ordered[start:start + UPSERT_BATCH]
            params = []
            for (moment, endpoint, method), counts in chunk:
                params += [field.get_db_prep_value(moment, connection), endpoint, method, counts["requests"],
                           counts["client_errors"], counts["server_errors"], counts["total_ms"], counts["max_ms"]]
            cursor.execute(
                f"INSERT INTO {table} ({', '.join(ops.quote_name(name) for name in columns)}) "
                f"VALUES {', '.join([single] * len(chunk))} "
                f"ON CONFLICT ({quoted_time}, {ops.quote_name('endpoint')}, {ops.quote_name('method')}) "
                f"DO UPDATE SET {update}",
                params,
            )


def _roll_up(routes):
    """The per-bucket counts, and the same counts summed per UTC day."""
    recent, daily = {}, {}
    for (stamp, method, endpoint), counts in routes.items():
        moment = datetime.fromtimestamp(stamp, tz=datetime_timezone.utc)
        recent[(moment, endpoint, method)] = counts
        total = daily.setdefault((moment.date(), endpoint, method), {
            "requests": 0, "client_errors": 0, "server_errors": 0, "total_ms": 0, "max_ms": 0,
        })
        for name in ("requests", "client_errors", "server_errors", "total_ms"):
            total[name] += counts[name]
        total["max_ms"] = max(total["max_ms"], counts["max_ms"])
    return recent, daily


def _mark_active(marks):
    """Accounts deleted since they were marked are left out. An account deleted
    in the instant between this check and the commit fails the transaction, and
    the retry finds it gone."""
    if not marks:
        return
    user_ids = sorted({user_id for _, user_id in marks})
    existing = set()
    for start in range(0, len(user_ids), UPSERT_BATCH):
        existing.update(
            get_user_model().objects.filter(pk__in=user_ids[start:start + UPSERT_BATCH]).values_list("pk", flat=True)
        )
    DailyActiveAccount.objects.bulk_create(
        [DailyActiveAccount(day=day, user_id=user_id) for day, user_id in sorted(marks) if user_id in existing],
        ignore_conflicts=True, batch_size=UPSERT_BATCH,
    )


def apply_batch(batch_id, batch):
    """Put a batch in the database, once. False if it had already been applied."""
    recent, daily = _roll_up(batch["routes"])
    with transaction.atomic():
        _, created = MetricFlushReceipt.objects.get_or_create(batch_id=batch_id, defaults={
            "requests": sum(counts["requests"] for counts in recent.values()),
            "lost_observations": batch["lost"],
        })
        if not created:
            return False
        _upsert(RecentApiMetric, "bucket", recent)
        _upsert(DailyApiMetric, "day", daily)
        _mark_active(batch["active"])
    return True


def flush():
    """Claim what has accumulated and apply every unfinished batch. Returns what
    happened, as counts. A failed batch stays in Redis for the next run."""
    summary = {"applied": 0, "already_applied": 0, "failed": 0, "requests": 0, "lost": 0, "redis_available": True}
    pending = _run(_pending)
    claimed = _run(_claim) if pending is not _unavailable else _unavailable
    if pending is _unavailable or claimed is _unavailable:
        summary["redis_available"] = False
        logger.warning("API metrics not flushed: Redis is unavailable.")
        return summary
    if claimed:
        pending.add(claimed)
    for batch_id in sorted(pending):
        try:
            batch = _run(lambda client: _read(client, batch_id))
            if batch is _unavailable:
                summary["failed"] += 1
                continue
            if apply_batch(batch_id, batch):
                summary["applied"] += 1
                summary["requests"] += sum(counts["requests"] for counts in batch["routes"].values())
                summary["lost"] += batch["lost"]
            else:
                summary["already_applied"] += 1
        except Exception:  # noqa: BLE001 - one bad batch must not stop the others
            logger.exception("API metrics batch %s could not be applied; it will be retried.", batch_id)
            summary["failed"] += 1
            continue
        # A failure here is harmless: the receipt makes the next run skip it.
        _run(lambda client: _discard(client, batch_id))
    if summary["lost"]:
        logger.warning("API metrics: %d observations were lost before this flush.", summary["lost"])
    return summary


def prune_receipts(now):
    MetricFlushReceipt.objects.filter(applied_at__lt=now - timedelta(days=RECEIPT_RETENTION_DAYS)).delete()
