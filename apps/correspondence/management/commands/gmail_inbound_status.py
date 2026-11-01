# ABOUTME: Emits PII-free Gmail inbound poller state for AWS-native diagnostics.
# ABOUTME: Never prints mailbox addresses, message IDs, history IDs, or content.
import json

from django.core.management.base import BaseCommand, CommandError

from apps.correspondence.operations import gmail_inbound_status_snapshot


class Command(BaseCommand):
    help = "Report PII-free Gmail inbound poller configuration and freshness."

    def add_arguments(self, parser):
        parser.add_argument(
            "--json",
            action="store_true",
            help="Emit the fixed-shape status as JSON.",
        )

    def handle(self, *args, **options):
        snapshot = gmail_inbound_status_snapshot()
        if options["json"]:
            self.stdout.write(
                json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
            )
        else:
            self.stdout.write(
                f"gmail_inbound: {snapshot['status']} "
                f"(healthy={str(snapshot['healthy']).lower()})"
            )
        if not snapshot["healthy"]:
            raise CommandError(f"Gmail inbound poller is {snapshot['status']}")
