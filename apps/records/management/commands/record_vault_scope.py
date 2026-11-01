# ABOUTME: Records the Vault search scope recovered from a retired source
# ABOUTME: system onto each migrated case, before that evidence disappears.
"""Write recovered Vault collection scope onto migrated cases.

When a source system is retired, the queries that produced its exported files
usually die with it. NextRequest stored the resulting files, not the searches,
and no matching Vault matters survive - so the only trace of what was actually
searched was the naming of the export bundles themselves
(``<search label>--<custodian>-<export id>.pst``).

That naming is evidence about an adequate search, and it belongs on the case
rather than in a spreadsheet someone has to remember exists. This command
writes it to the timeline, append-only and audited.

It deliberately records only what the source proves: which named searches ran
and whose mailboxes they covered. Search terms and date bounds are NOT
recoverable from a filename, and the note says so, because a scope that looks
complete is worse than one that is honestly partial - a reviewer might
otherwise re-run it believing nothing more was needed.
"""
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.audit.models import audit
from apps.records.models import LegacyRequestImport, StatusEvent

#: Enough of the sentence to recognise the note on a re-run without pinning
#: its whole wording.
_MARKER = "Recovered Google Vault collection scope"


def scope_note(*, source_number, searches, custodians, unmigrated, source_system):
    listed = ", ".join(sorted(custodians))
    named = "; ".join(searches)
    return (
        f"Recovered Google Vault collection scope for source request "
        f"{source_number}, read from the export bundle names in "
        f"{source_system} before access to it ended.\n\n"
        f"Named searches ({len(searches)}): {named}\n\n"
        f"Custodian mailboxes ({len(custodians)}): {listed}\n\n"
        f"These searches produced {unmigrated} file(s) that were NOT migrated. "
        "This scope is partial by nature: an export filename records the "
        "search name and the custodian, never the query terms or the date "
        "bounds. Confirm the custodian list, then supply terms and dates and "
        "review the resulting Vault count before collecting. Do not treat "
        "this as a complete, re-runnable scope."
    )


class Command(BaseCommand):
    help = "Record recovered Vault search scope on migrated cases."

    def add_arguments(self, parser):
        parser.add_argument("scope_json", help="JSON of {source_number: {...}}.")
        parser.add_argument("--operator", required=True)
        parser.add_argument(
            "--source-system",
            default="NextRequest",
            help="Name of the retired system, as it should read on the timeline.",
        )
        parser.add_argument("--commit", action="store_true")

    def handle(self, *args, **options):
        operator = options["operator"].strip()
        if not operator:
            raise CommandError("An operator is required.")
        path = Path(options["scope_json"])
        if not path.is_file():
            raise CommandError(f"{path} does not exist.")
        try:
            scope = json.loads(path.read_text())
        except ValueError as exc:
            raise CommandError(f"{path} is not valid JSON: {exc}") from exc
        if not isinstance(scope, dict) or not scope:
            raise CommandError("The scope file must be a non-empty object.")

        planned, blocked, already = [], [], 0
        for source_number in sorted(scope):
            entry = scope[source_number]
            searches = entry.get("searches") or []
            custodians = entry.get("custodians") or []
            if not searches or not custodians:
                blocked.append(
                    f"{source_number}: needs both searches and custodians."
                )
                continue
            imported = (
                LegacyRequestImport.objects
                .select_related("request")
                .filter(source_number=source_number)
                .first()
            )
            if imported is None:
                blocked.append(f"{source_number}: not an imported request.")
                continue
            if StatusEvent.objects.filter(
                request=imported.request, note__contains=_MARKER
            ).exists():
                already += 1
                continue
            planned.append((imported, searches, custodians,
                            int(entry.get("unmigrated") or 0)))

        self.stdout.write(f"Requests in scope file : {len(scope)}")
        self.stdout.write(f"  already recorded     : {already}")
        self.stdout.write(f"  to record            : {len(planned)}")
        for imported, searches, custodians, unmigrated in planned:
            self.stdout.write(
                f"    {imported.source_number:8} {imported.request.number:16}"
                f" searches={len(searches):<3} custodians={len(custodians):<4}"
                f" unmigrated={unmigrated}"
            )
        for problem in blocked:
            self.stdout.write(self.style.ERROR(f"  blocked: {problem}"))
        if blocked:
            raise CommandError(
                f"{len(blocked)} blocking issue(s). Nothing was written."
            )
        if not options["commit"]:
            self.stdout.write(self.style.SUCCESS("Dry run only. Nothing was written."))
            return
        if not planned:
            self.stdout.write(self.style.SUCCESS("Nothing to do."))
            return

        for imported, searches, custodians, unmigrated in planned:
            with transaction.atomic():
                StatusEvent.objects.create(
                    request=imported.request,
                    occurred_at=imported.request.received_at,
                    note=scope_note(
                        source_number=imported.source_number,
                        searches=searches,
                        custodians=custodians,
                        unmigrated=unmigrated,
                        source_system=options["source_system"],
                    ),
                    visible_to_requester=False,
                )
                audit(
                    "request.vault_scope_recovered",
                    target=imported.request.number,
                    operator=operator,
                    source_number=imported.source_number,
                    search_count=len(searches),
                    custodian_count=len(custodians),
                    unmigrated_files=unmigrated,
                )
        self.stdout.write(
            self.style.SUCCESS(f"Recorded scope on {len(planned)} case(s).")
        )
