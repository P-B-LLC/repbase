"""Refuse to point the load-test harness at anything that could be real.

The harness deletes and recreates its database and empties its Redis, so the
one thing it must never do is reach production because of a stray environment
variable. Two independent conditions have to hold: the database's own name says
it is a load-test database, and every host is either loopback or was named
explicitly for a staging run.

No Django here: `config.loadtest` runs this before settings exist.
"""

from urllib.parse import urlsplit

LOOPBACK = {"127.0.0.1", "localhost", "::1"}
STAGING_HOSTS_VARIABLE = "LOADTEST_ALLOW_STAGING_HOSTS"


class UnsafeTarget(Exception):
    pass


def check_target(env):
    """Raise UnsafeTarget unless DATABASE_URL and REDIS_URL are safe to wipe."""
    database = urlsplit(env.get("DATABASE_URL", ""))
    cache = urlsplit(env.get("REDIS_URL", ""))
    if "loadtest" not in database.path.strip("/"):
        raise UnsafeTarget('The load-test database must have "loadtest" in its name.')
    allowed = {host.strip() for host in env.get(STAGING_HOSTS_VARIABLE, "").split(",") if host.strip()}
    for label, host in (("DATABASE_URL", database.hostname), ("REDIS_URL", cache.hostname)):
        if not host:
            raise UnsafeTarget(f"{label} must name a host.")
        if host not in LOOPBACK and host not in allowed:
            raise UnsafeTarget(f"{label} points at {host}: not loopback, and not in {STAGING_HOSTS_VARIABLE}.")
