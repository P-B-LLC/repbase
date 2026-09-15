"""Bounded WSGI workers; size against the hosted database connection budget."""
import os
from config.environment import integer

bind = '0.0.0.0:' + str(integer(os.environ, 'PORT', 8000, 1, 65535))
workers = integer(os.environ, 'WEB_CONCURRENCY', 2, 1, 32)
worker_class = 'sync'
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
