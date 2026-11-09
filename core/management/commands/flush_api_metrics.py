"""Move buffered API metrics from Redis to the database (`core.metric_buffer`).

Meant for a timer every 30 to 60 seconds (deploy/repbase-metrics-flush.timer).
Always exits 0: a batch that failed stays in Redis for the next run, and a
non-zero exit would only stop the timer from coming round again.
"""

import json

from django.core.management.base import BaseCommand

from core.metric_buffer import flush


class Command(BaseCommand):
    help = "Flush buffered API metrics from Redis to the database."

    def handle(self, *args, **options):
        self.stdout.write(json.dumps(flush(), sort_keys=True))
