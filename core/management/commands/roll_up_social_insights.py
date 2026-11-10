"""Work out the private social figures for the days just ended.

Once a day is enough: the page at /insights/social/ reads what this writes
and nothing else. Safe to run twice (it replaces the day's snapshot), safe
on an empty database, and it prints counts only.
"""

import json
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from core import social_insights


class Command(BaseCommand):
    help = "Write the 7- and 30-day social insight snapshots for the last whole UTC day."

    def add_arguments(self, parser):
        parser.add_argument("--day", help="The last day of the window, YYYY-MM-DD. Default: yesterday (UTC).")

    def handle(self, *args, **options):
        day = None
        if options["day"]:
            try:
                day = date.fromisoformat(options["day"])
            except ValueError:
                raise CommandError("--day takes a date as YYYY-MM-DD.") from None
        written = social_insights.roll_up(day=day)
        social_insights.prune()
        self.stdout.write(json.dumps({
            "day": written[0].day.isoformat(),
            "windows": [snapshot.window_days for snapshot in written],
            "withheld": {
                str(snapshot.window_days): sorted(
                    name for name, entry in snapshot.figures["product"].items() if entry.get("withheld")
                )
                for snapshot in written
            },
        }))
