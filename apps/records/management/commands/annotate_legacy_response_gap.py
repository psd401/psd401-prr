# ABOUTME: Backfills the "source system recorded no first response" note onto
# ABOUTME: legacy cases imported before the importer stated that explicitly.
"""Say on the case timeline which migrated requests were never answered.

The importer records a note when the source system reported an initial
response. It said nothing when the source reported none, so an unanswered case
and a case whose response simply was not carried across looked identical to
whoever opened them - a silent timeline either way. Those two call for
opposite actions, and one of them is a live statutory exposure.

This adds the missing statement to cases already imported. StatusEvent is
append-only, so nothing is rewritten: a new event is appended saying what the
source system recorded, dated to the request's receipt so it sits with the
other import notes rather than looking like something that happened today.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.audit.models import audit
from apps.records.models import LegacyRequestImport, StatusEvent
from apps.records.nextrequest_migration import legacy_response_gap_note

#: Enough of the sentence to recognise the note without matching its whole
#: text, so a later wording change cannot cause a duplicate.
_MARKER = "recorded NO initial response for source ID"


class Command(BaseCommand):
    help = "Record, on each migrated case, that its source system logged no first response."

    def add_arguments(self, parser):
        parser.add_argument("--operator", required=True)
        parser.add_argument(
            "--retaining-system",
            default="NextRequest",
            help="Name of the source system as it should read on the timeline.",
        )
        parser.add_argument("--commit", action="store_true")

    def handle(self, *args, **options):
        operator = options["operator"].strip()
        if not operator:
            raise CommandError("An operator is required.")
        system = options["retaining_system"].strip()

        imports = (
            LegacyRequestImport.objects
            .select_related("request")
            .filter(legacy_initial_response_at=None)
            .order_by("source_number")
        )

        pending, already = [], 0
        for item in imports:
            if StatusEvent.objects.filter(
                request=item.request, note__contains=_MARKER
            ).exists():
                already += 1
                continue
            pending.append(item)

        self.stdout.write(
            f"Migrated requests with no recorded first response: {imports.count()}"
        )
        self.stdout.write(f"  already annotated : {already}")
        self.stdout.write(f"  to annotate       : {len(pending)}")
        for item in pending:
            self.stdout.write(
                f"    {item.source_number:8} {item.request.number:16}"
                f" due {item.request.statutory_response_due}"
                f"  source status {item.source_status!r}"
            )

        if not options["commit"]:
            self.stdout.write(self.style.SUCCESS("Dry run only. Nothing was written."))
            return
        if not pending:
            self.stdout.write(self.style.SUCCESS("Nothing to do."))
            return

        for item in pending:
            with transaction.atomic():
                StatusEvent.objects.create(
                    request=item.request,
                    occurred_at=item.request.received_at,
                    note=legacy_response_gap_note(
                        source_id=item.source_id,
                        source_status=item.source_status,
                        retaining_system=system,
                    ),
                    visible_to_requester=False,
                )
                audit(
                    "request.legacy_response_gap_recorded",
                    target=item.request.number,
                    operator=operator,
                    source_id=item.source_id,
                    source_status=item.source_status,
                    statutory_response_due=str(item.request.statutory_response_due),
                )
        self.stdout.write(
            self.style.SUCCESS(f"Annotated {len(pending)} case(s).")
        )
