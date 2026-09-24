"""Bounded WSGI workers; size against the hosted database connection budget."""
import os
from config.environment import integer

bind = '0.0.0.0:' + str(integer(os.environ, 'PORT', 8000, 1, 65535))
workers = integer(os.environ, 'WEB_CONCURRENCY', 2, 1, 32)
# CHANGED (perf): 'sync' workers serve exactly one request at a time. The
# moderation check in core/moderation.py makes a blocking HTTPS call to
# OpenAI (up to MODERATION_TIMEOUT_SECONDS, default 8s) on 9 endpoints --
# registration, the four profile edits, gym creation, comment creation, and
# post create/edit. Under 'sync' workers, one in-flight moderation call
# fully occupies its worker for that whole window; at WEB_CONCURRENCY=2,
# two people posting or commenting at once stalls the entire server, because
# there is no free worker left to answer anything else (feed reads, workout
# saves, everything). HOSTED_CONFIGURATION.md names this as a known,
# unmeasured risk and calls threaded workers "the real fix for blocking
# I/O." Switched to 'gthread' so a worker blocked on the OpenAI call can
# still serve other requests on its other threads.
# Old value, kept for reference: worker_class = 'sync'
worker_class = 'gthread'
# CHANGED (perf): threads-per-worker for the 'gthread' worker_class above.
# Each active thread that touches the database opens its own connection
# (Django's DB connections are thread-local), so this raises the connection
# budget by roughly workers * threads, not just workers -- see
# HOSTED_CONFIGURATION.md's "size WEB_CONCURRENCY against moderation, not
# just the database." A default of 4 keeps the worst case at
# WEB_CONCURRENCY * 4 connections; tune WEB_THREADS alongside the hosting
# provider's connection budget before raising it. This setting did not
# exist before this change -- there is no prior value to preserve.
threads = integer(os.environ, 'WEB_THREADS', 4, 1, 32)
timeout = 60
graceful_timeout = 30
max_requests = 1000
max_requests_jitter = 100
errorlog = '-'
# Avoid query strings (signed media URLs), auth headers and request bodies.
accesslog = '-'
access_log_format = '%(m)s %(U)s %(s)s %(L)s'
# Django's explicitly opted-in proxy setting is the single trust policy.
forwarded_allow_ips = ''
preload_app = False
