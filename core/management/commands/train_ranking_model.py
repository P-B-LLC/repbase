"""Train the For You ranking model on what people did with what it showed
them, and make it active if it beat the rules on readers it did not learn
from (`core.ranking_model`).

Meant for a daily timer (deploy/repbase-ranking-model.timer). Prints the
model's figures -- sample counts, held-out AUC, the rules' AUC, and why it
was or was not made active -- as one JSON object. Counts only: nothing
names a reader or a post.
"""

import json

from django.core.management.base import BaseCommand

from core.ranking_model import train


class Command(BaseCommand):
    help = "Train the learned For You ranking model; activate it if it beat the rules."

    def add_arguments(self, parser):
        parser.add_argument(
            "--no-activate", action="store_true",
            help="Train and report, but leave the active model as it is.",
        )

    def handle(self, *args, **options):
        model = train(activate=not options["no_activate"])
        if model is None:
            self.stdout.write(json.dumps({"trained": False, "reason": "nothing to learn from yet"}))
            return
        self.stdout.write(json.dumps({
            "trained": True, "model": model.pk, "active": model.is_active, **model.metrics,
        }, sort_keys=True))
