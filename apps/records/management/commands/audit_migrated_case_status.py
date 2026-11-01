"""Report migrated cases whose status or response record needs a human look.

The district imported open NextRequest cases only. A migrated case sitting in
a terminal status is therefore an anomaly worth a person's attention, not a
settled outcome -- and a terminal case with no first response on record is a
live RCW 42.56.520 exposure that no automated path will ever reach, because
the safety-net worker and the awaiting-statutory queue both skip terminal
requests.

This command only reads. It changes no status, writes no StatusEvent, and
sends nothing to a requester. Correcting a case found here is a per-case,
reasoned, human decision through ``apps.records.services.transition``.
"""

import json

from django.core.management.base import BaseCommand

from apps.records.models import LegacyRequestImport, RequestStatus
from apps.records.services import CLOSED_STATUSES

#: A terminal status that never owed a five-day response. Closing a case as
#: "not a records request" is a written staff decision, so it is the one
#: terminal state this command does not treat as an anomaly on its own.
_RESPONSE_NEVER_OWED = {
    RequestStatus.CLOSED_NON_REQUEST,
    # A merged duplicate owes nothing itself; its case of record does.
    RequestStatus.CLOSED_DUPLICATE,
}


class Command(BaseCommand):
    help = "List migrated cases with an anomalous status or no first response on record."

    def add_arguments(self, parser):
        parser.add_argument(
            "--json",
            action="store_true",
            help="Emit machine-readable output instead of a staff-readable table.",
        )

    def handle(self, *args, **options):
        imports = (
            LegacyRequestImport.objects
            .select_related("request")
            .order_by("source_number")
        )

        terminal_cases = []
        unanswered_cases = []
        for item in imports:
            prr = item.request
            row = {
                "number": prr.number,
                "source_number": item.source_number,
                "source_id": item.source_id,
                "status": prr.status,
                "legacy_initial_response_at": (
                    item.legacy_initial_response_at.isoformat()
                    if item.legacy_initial_response_at
                    else None
                ),
                "source_attachment_count": item.source_attachment_count,
            }
            if prr.status in CLOSED_STATUSES:
                # Only open cases were migrated, so any terminal status here
                # came from somewhere other than the intended import.
                terminal_cases.append(row)
                if (
                    prr.status not in _RESPONSE_NEVER_OWED
                    and not prr.initial_response_delivered
                ):
                    unanswered_cases.append(row)

        payload = {
            "migrated_cases": imports.count(),
            "terminal_status_cases": terminal_cases,
            "terminal_without_first_response": unanswered_cases,
        }

        if options["json"]:
            self.stdout.write(json.dumps(payload, sort_keys=True, indent=2))
            return

        self.stdout.write(f"Migrated cases: {payload['migrated_cases']}")
        self.stdout.write(
            f"Terminal status (expected 0, only open cases were imported): "
            f"{len(terminal_cases)}"
        )
        for row in terminal_cases:
            self.stdout.write(
                f"  {row['number']}  source {row['source_number']}  "
                f"status={row['status']}"
            )
        self.stdout.write(
            f"Terminal with no five-day response on record: "
            f"{len(unanswered_cases)}"
        )
        for row in unanswered_cases:
            self.stdout.write(
                f"  {row['number']}  source {row['source_number']}  "
                f"status={row['status']}  NO FIRST RESPONSE"
            )
        if not terminal_cases:
            self.stdout.write(
                "No migrated case carries a terminal status. Nothing to correct."
            )
