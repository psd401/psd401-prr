# ABOUTME: Records, on each migrated case, a document-by-document account of
# ABOUTME: what came across from the retired system and what deliberately did not.
"""Write the source-vs-target document reconciliation onto each case.

A migration is only defensible if someone can later ask "did everything come
across?" and get an answer from the record rather than from whoever ran it.
This writes that answer per case: how many documents the source system held,
how many were loose uploads, how many of those were migrated, and how many
were export containers deliberately left behind.

The container count is the load-bearing number. Leaving archives out was the
right call - their contents are re-collected from the source of truth rather
than unpacked from a mailbox dump - but "we chose not to move 633 files" is a
statement a district has to be able to make deliberately, with the count on
the case, rather than discover later.
"""
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.audit.models import audit
from apps.records.models import LegacyRequestImport, StatusEvent

MARKER = "Document reconciliation against"


def reconciliation_note(*, source_number, data, source_system):
    total = data["documents_total"]
    folderless = data["folderless"]
    infolders = data["in_vault_folders"]
    migrated = data["folderless_migrated"]
    containers = data["vault_containers_not_migrated"]
    types = ", ".join(
        f"{n} .{ext}" for ext, n in sorted(data.get("container_types", {}).items())
    ) or "none"

    lines = [
        (
            f"{MARKER} {source_system} for source request {source_number}, "
            "counted document by document against the source system's own totals."
        ),
        "",
        f"Documents in the source system : {total}",
        f"  loose uploads                : {folderless}",
        f"  inside export folders        : {infolders}",
        "",
        f"Loose uploads migrated here    : {migrated} of {folderless}",
    ]
    if containers:
        lines += [
            f"Export containers NOT migrated : {containers} ({types})",
            "",
            (
                "Those containers are Google Vault mailbox and Drive exports, not "
                "individual records. They were deliberately left behind: their "
                "contents are re-collected from Vault directly, which extracts, "
                "de-duplicates and makes each message reviewable, rather than "
                "storing an archive nobody can open. The recovered search scope "
                "for that re-collection is recorded separately on this case."
            ),
        ]
    elif not infolders:
        lines += ["", "No export containers on this case; every document came across."]
    if infolders:
        lines += [
            "",
            (
                f"The {infolders} document(s) inside export folders are the "
                "extracted contents of those same containers and are covered by "
                "the same re-collection."
            ),
        ]
    return "\n".join(lines)


class Command(BaseCommand):
    help = "Record the source-vs-target document reconciliation on migrated cases."

    def add_arguments(self, parser):
        parser.add_argument("reconciliation_json")
        parser.add_argument("--operator", required=True)
        parser.add_argument("--source-system", default="NextRequest")
        parser.add_argument("--commit", action="store_true")

    def handle(self, *args, **options):
        operator = options["operator"].strip()
        if not operator:
            raise CommandError("An operator is required.")
        path = Path(options["reconciliation_json"])
        if not path.is_file():
            raise CommandError(f"{path} does not exist.")
        try:
            payload = json.loads(path.read_text())
        except ValueError as exc:
            raise CommandError(f"{path} is not valid JSON: {exc}") from exc

        imports = {
            item.source_number: item
            for item in LegacyRequestImport.objects.select_related("request")
            if item.source_number
        }
        planned, blockers, already = [], [], 0
        for source_number in sorted(payload):
            data = payload[source_number]
            imported = imports.get(source_number)
            if imported is None:
                blockers.append(f"{source_number}: not an imported request.")
                continue
            required = {
                "documents_total", "folderless", "in_vault_folders",
                "folderless_migrated", "vault_containers_not_migrated",
            }
            if not required.issubset(data):
                blockers.append(f"{source_number}: incomplete reconciliation data.")
                continue
            if StatusEvent.objects.filter(
                request=imported.request, note__contains=MARKER
            ).exists():
                already += 1
                continue
            planned.append((imported, data))

        self.stdout.write(f"Cases in file      : {len(payload)}")
        self.stdout.write(f"  already recorded : {already}")
        self.stdout.write(f"  to record        : {len(planned)}")
        for imported, data in planned:
            self.stdout.write(
                f"    {imported.source_number:8} total={data['documents_total']:<6}"
                f" migrated={data['folderless_migrated']:<5}"
                f" containersLeft={data['vault_containers_not_migrated']}"
            )
        for problem in blockers:
            self.stdout.write(self.style.ERROR(f"  blocked: {problem}"))
        if blockers:
            raise CommandError(
                f"{len(blockers)} blocking issue(s). Nothing was written."
            )
        if not options["commit"]:
            self.stdout.write(self.style.SUCCESS("Dry run only. Nothing was written."))
            return

        for imported, data in planned:
            with transaction.atomic():
                StatusEvent.objects.create(
                    request=imported.request,
                    occurred_at=imported.request.received_at,
                    note=reconciliation_note(
                        source_number=imported.source_number,
                        data=data,
                        source_system=options["source_system"],
                    ),
                    visible_to_requester=False,
                )
                audit(
                    "request.document_reconciliation_recorded",
                    target=imported.request.number,
                    operator=operator,
                    source_number=imported.source_number,
                    **{k: data[k] for k in sorted(data) if k != "container_types"},
                )
        self.stdout.write(
            self.style.SUCCESS(f"Recorded reconciliation on {len(planned)} case(s).")
        )
