# ABOUTME: Machine-checkable correspondence dependency readiness command.
# ABOUTME: Emits fixed codes only and exits nonzero whenever delivery is unsafe.
import json

from django.core.management.base import BaseCommand, CommandError

from apps.correspondence.operations import readiness_snapshot


class Command(BaseCommand):
    help = "Check correspondence storage, scanning, broker, and event readiness."

    def add_arguments(self, parser):
        parser.add_argument(
            "--json",
            action="store_true",
            help="Emit the fixed-code readiness result as JSON.",
        )

    def handle(self, *args, **options):
        snapshot = readiness_snapshot()
        if options["json"]:
            self.stdout.write(
                json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
            )
        else:
            for check in snapshot["checks"]:
                self.stdout.write(
                    f"{check['component']}: {check['status']} "
                    f"({check['code']})"
                )
        if not snapshot["ready"]:
            raise CommandError("Correspondence dependencies are not ready.")
