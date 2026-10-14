"""Bring the For You page's per-post counts up to date (`core.feed_stats`).

Meant for a timer every few minutes (deploy/repbase-feed-stats.timer). Each
run counts what was acted on or seen since the last one, and refreshes the
oldest of the rest; `--full` recounts every post that can still be
recommended, which is also what the first run does.
"""

import json

from django.core.management.base import BaseCommand, CommandError

from core.feed_stats import BATCH, REFRESH_LIMIT, refresh


class Command(BaseCommand):
    help = "Refresh the For You page's per-post engagement counts."

    def add_arguments(self, parser):
        parser.add_argument("--full", action="store_true", help="Recount every post in the window.")
        parser.add_argument("--limit", type=int, default=REFRESH_LIMIT,
                            help="At most this many rows refreshed for age in one run.")
        parser.add_argument("--batch", type=int, default=BATCH, help="Posts counted per batch.")

    def handle(self, *args, **options):
        if options["limit"] < 1 or not 1 <= options["batch"] <= 5000:
            raise CommandError("Use --limit of at least 1 and --batch between 1 and 5000.")
        summary = refresh(full=options["full"], limit=options["limit"], batch=options["batch"])
        self.stdout.write(json.dumps(summary, sort_keys=True))
