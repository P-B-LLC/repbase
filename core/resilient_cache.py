"""A Redis cache that a Redis outage cannot turn into an application outage.

Django's backend raises whatever the client raises. DRF's throttles read the
cache on every request, before the view runs, so an unreachable Redis used to
make every API request fail -- a workout save included, with nothing saved -- and
a Redis that had stopped answering held each worker thread for a full socket
timeout first.

Here a failed call is a cache miss. And after a few failures in a row the
breaker opens: calls stop going to the network at all, so an outage costs a
handful of short timeouts per process rather than one on every request. One
call is let through each cooldown to find out whether Redis is back.

What a miss means is each caller's decision. Rate limits that protect accounts
refuse instead of proceeding (`core.throttles`), and the For You page sheds
itself rather than rebuilding shared aggregates from the database on every
request (`core.for_you`).
"""

import logging
import threading
import time

from django.conf import settings
from django.core.cache import caches
from django.core.cache.backends.base import DEFAULT_TIMEOUT
from django.core.cache.backends.redis import RedisCache

logger = logging.getLogger(__name__)

#: Consecutive failures before calls stop going to the network.
BREAKER_FAILURES = 3
#: How long calls stay off the network before one is let through to probe.
BREAKER_COOLDOWN_SECONDS = 5


class CircuitBreaker:
    def __init__(self, failures=BREAKER_FAILURES, cooldown=BREAKER_COOLDOWN_SECONDS, clock=time.monotonic):
        self.failures = failures
        self.cooldown = cooldown
        self.clock = clock
        self._lock = threading.Lock()
        self._consecutive = 0
        self._opened_at = None

    def allow(self):
        """Whether this call may go to the network."""
        with self._lock:
            if self._opened_at is None:
                return True
            if self.clock() - self._opened_at >= self.cooldown:
                # This caller is the probe. Everybody else waits out another
                # cooldown, so a recovery test costs one slow call, not many.
                self._opened_at = self.clock()
                return True
            return False

    def succeeded(self):
        # Read without the lock on the path nearly every call takes.
        if not self._consecutive and self._opened_at is None:
            return
        with self._lock:
            recovered = self._opened_at is not None
            self._consecutive = 0
            self._opened_at = None
        if recovered:
            logger.warning("Redis is answering again; cache calls have resumed.")

    def reset(self):
        with self._lock:
            self._consecutive = 0
            self._opened_at = None

    def failed(self):
        with self._lock:
            self._consecutive += 1
            if self._consecutive < self.failures:
                return
            opening = self._opened_at is None
            self._opened_at = self.clock()
        if opening:
            logger.warning("Redis is not answering; cache calls are off the network for now.")


_breakers = {}
_breakers_lock = threading.Lock()


def _breaker_for(location):
    """One breaker per Redis per process. Django makes a backend per thread,
    and an outage is something the whole process should learn at once."""
    with _breakers_lock:
        if location not in _breakers:
            _breakers[location] = CircuitBreaker(
                getattr(settings, "CACHE_BREAKER_FAILURES", BREAKER_FAILURES),
                getattr(settings, "CACHE_BREAKER_COOLDOWN_SECONDS", BREAKER_COOLDOWN_SECONDS),
            )
        return _breakers[location]


def reset_breakers():
    """Close every breaker. For tests: backends outlive them and keep theirs."""
    with _breakers_lock:
        for breaker in _breakers.values():
            breaker.reset()


def _redis_errors():
    """What an unreachable or unhealthy Redis raises. `redis` is only installed
    where it is used, which is not the SQLite test job."""
    try:
        from redis.exceptions import RedisError
    except ImportError:
        return (OSError,)
    return (RedisError, OSError)


class ResilientRedisCache(RedisCache):
    def __init__(self, server, params):
        super().__init__(server, params)
        self._breaker = _breaker_for(str(server))
        self._errors = _redis_errors()

    def _guarded(self, miss, operation, *args, **kwargs):
        if not self._breaker.allow():
            return miss
        try:
            result = operation(*args, **kwargs)
        except self._errors:
            self._breaker.failed()
            return miss
        self._breaker.succeeded()
        return result

    def is_available(self):
        """Whether Redis answers right now. Costs a round trip, or nothing
        while the breaker is open."""
        def ping():
            self._cache.get_client(write=False).ping()
            return True

        return self._guarded(False, ping)

    def run(self, operation, miss=None):
        """Call `operation(client)` on a raw Redis client, under the same
        protection as everything else. For what the cache API cannot say."""
        return self._guarded(miss, lambda: operation(self._cache.get_client(write=True)))

    def add(self, key, value, timeout=DEFAULT_TIMEOUT, version=None):
        return self._guarded(False, super().add, key, value, timeout=timeout, version=version)

    def get(self, key, default=None, version=None):
        return self._guarded(default, super().get, key, default=default, version=version)

    def set(self, key, value, timeout=DEFAULT_TIMEOUT, version=None):
        self._guarded(None, super().set, key, value, timeout=timeout, version=version)

    def touch(self, key, timeout=DEFAULT_TIMEOUT, version=None):
        return self._guarded(False, super().touch, key, timeout=timeout, version=version)

    def delete(self, key, version=None):
        return self._guarded(False, super().delete, key, version=version)

    def get_many(self, keys, version=None):
        return self._guarded({}, super().get_many, keys, version=version)

    def has_key(self, key, version=None):
        return self._guarded(False, super().has_key, key, version=version)

    def incr(self, key, delta=1, version=None):
        # Backends raise ValueError for a key that does not exist, so callers
        # already handle it. That is exactly what an outage looks like here.
        unavailable = object()
        result = self._guarded(unavailable, super().incr, key, delta=delta, version=version)
        if result is unavailable:
            raise ValueError(f"Key '{key}' not found.")
        return result

    def set_many(self, data, timeout=DEFAULT_TIMEOUT, version=None):
        return self._guarded(list(data), super().set_many, data, timeout=timeout, version=version)

    def delete_many(self, keys, version=None):
        self._guarded(None, super().delete_many, keys, version=version)

    def clear(self):
        return self._guarded(False, super().clear)


def cache_is_available():
    """Whether the default cache can answer. Always true of a local-memory one."""
    check = getattr(caches["default"], "is_available", None)
    return True if check is None else check()
