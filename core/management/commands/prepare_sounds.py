"""Extract the audio for sounds that are waiting for it.

Run on a timer beside the video worker. It does a few at a time and stops, so
a backlog is worked through over several runs rather than holding one process
for as long as it takes.
"""

from django.core.management.base import BaseCommand

from core.sounds import prepare_pending


class Command(BaseCommand):
    help = "Prepare the audio of published sounds that do not have it yet."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=10, help="How many to prepare this run.")

    def handle(self, *args, **options):
        prepared, failed = prepare_pending(limit=max(1, options["limit"]))
        self.stdout.write(f"prepared {prepared} sounds, {failed} could not be prepared")
