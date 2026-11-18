"""Show or change which accounts get each part of the new Social.

    social_rollout show
    social_rollout gates
    social_rollout set takes cohort --backend-revision a71fa89 --client-build "1.2 (580)"
    social_rollout invite some_username --note "beta tester"
    social_rollout uninvite some_username

`set` to a wider stage checks the release gates first and refuses if one
fails; `--force` widens anyway and records that it was forced. Narrowing a
stage, including to `off`, is never refused: pulling something back must
always work. Widening needs the revisions it was tested at, because the
record of a release is written here and nowhere else.
"""

import json

from django.core.management.base import BaseCommand, CommandError

from core import rollout
from core.models import RepbaseUser, SocialCohortMember, SocialRolloutEvent


class Command(BaseCommand):
    help = "Show or change the staged release of the new Social."

    def add_arguments(self, parser):
        actions = parser.add_subparsers(dest="action", required=True)
        actions.add_parser("show")
        actions.add_parser("gates")
        change = actions.add_parser("set")
        change.add_argument("flag", choices=sorted(rollout.FLAGS))
        change.add_argument("stage", choices=rollout.STAGES)
        change.add_argument("--backend-revision", default="")
        change.add_argument("--client-build", default="")
        change.add_argument("--by", default="")
        change.add_argument("--force", action="store_true")
        for name in ("invite", "uninvite"):
            person = actions.add_parser(name)
            person.add_argument("username")
            person.add_argument("--note", default="")

    def handle(self, *args, **options):
        getattr(self, "_" + options["action"])(options)

    def _show(self, options):
        stages = rollout.stages()
        last = {}
        for event in SocialRolloutEvent.objects.all()[:200]:
            last.setdefault(event.flag, event)
        self.stdout.write(json.dumps({
            "default": rollout.default_stage("takes"),
            "cohort_members": SocialCohortMember.objects.count(),
            "flags": {
                name: {
                    "stage": stages[name],
                    "what": rollout.FLAGS[name][1],
                    "last_change": last[name].created_at.isoformat() if name in last else None,
                    "backend_revision": last[name].backend_revision if name in last else None,
                    "client_build": last[name].client_build if name in last else None,
                }
                for name in sorted(rollout.FLAGS)
            },
        }, indent=2))

    def _gates(self, options):
        results = rollout.gates()
        for name, passed, detail in results:
            self.stdout.write(f"{'pass' if passed else 'FAIL'}  {name}: {detail}")
        if not all(passed for _, passed, _ in results):
            raise CommandError("A release gate is failing. Do not widen any stage.")

    def _set(self, options):
        before = rollout.stages()[options["flag"]]
        widening = rollout.wider(options["stage"], before)
        if widening and not (options["backend_revision"] and options["client_build"]):
            raise CommandError(
                "Widening a stage needs --backend-revision and --client-build: the revisions it was tested at."
            )
        try:
            event = rollout.set_stage(
                options["flag"], options["stage"], by=options["by"],
                backend_revision=options["backend_revision"], client_build=options["client_build"],
                force=options["force"],
            )
        except rollout.GatesFailed as refusal:
            for name, _, detail in refusal.failed:
                self.stderr.write(f"FAIL  {name}: {detail}")
            raise CommandError(
                f"{options['flag']} stays at {before}. Fix what is failing, or pass --force to widen anyway."
            ) from None
        self.stdout.write(
            f"{event.flag}: {event.from_stage} -> {event.to_stage}"
            + (" (forced past failing gates)" if event.forced else "")
        )

    def _profile(self, username):
        profile = RepbaseUser.objects.filter(user__username=username).first()
        if profile is None:
            raise CommandError("No account with that username.")
        return profile

    def _invite(self, options):
        _, created = SocialCohortMember.objects.get_or_create(
            user=self._profile(options["username"]), defaults={"note": options["note"][:200]}
        )
        self.stdout.write("invited" if created else "already invited")

    def _uninvite(self, options):
        deleted, _ = SocialCohortMember.objects.filter(user=self._profile(options["username"])).delete()
        self.stdout.write("removed" if deleted else "was not invited")
