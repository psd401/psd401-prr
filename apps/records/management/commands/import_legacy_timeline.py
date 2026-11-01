# ABOUTME: Dry-run-by-default CLI that imports a retired system's case history
# ABOUTME: onto migrated requests as append-only timeline evidence.
"""Import correspondence and notes from a retired source system.

Dry run by default. The importer records history only; it never changes a
request's status, because whether a migrated acknowledgment satisfies
RCW 42.56.520 is a records officer's call made through the lifecycle service.
"""
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.records.legacy_timeline import (
    commit_corrections,
    commit_entries,
    load_payload,
    plan_corrections,
    plan_entries,
)


class Command(BaseCommand):
    help = "Import legacy case correspondence and notes onto migrated requests."

    def add_arguments(self, parser):
        parser.add_argument("timeline_json")
        parser.add_argument("--operator", required=True)
        parser.add_argument("--timezone", default="America/Los_Angeles")
        parser.add_argument(
            "--source-system",
            default="NextRequest",
            help="Name of the retired system, as it reads on the timeline.",
        )
        parser.add_argument("--commit", action="store_true")

    def handle(self, *args, **options):
        operator = options["operator"].strip()
        if not operator:
            raise CommandError("An operator is required.")
        try:
            payload = load_payload(options["timeline_json"])
            planned, blockers, undated = plan_entries(
                payload, zone=options["timezone"]
            )
        except ValidationError as exc:
            raise CommandError(exc.messages[0]) from exc

        by_request = {}
        by_kind = {}
        for item in planned:
            by_request[item["source_number"]] = by_request.get(item["source_number"], 0) + 1
            by_kind[item["title"]] = by_kind.get(item["title"], 0) + 1

        self.stdout.write(f"Cases in file        : {len(payload)}")
        self.stdout.write(f"Entries to import    : {len(planned)}")
        self.stdout.write(f"  requester-visible  : {sum(1 for i in planned if i['visible'])}")
        self.stdout.write(f"  undated (fell back): {undated}")
        self.stdout.write("By kind:")
        for kind, count in sorted(by_kind.items(), key=lambda kv: -kv[1]):
            self.stdout.write(f"  {count:>4}  {kind}")
        self.stdout.write("By case:")
        for source_number, count in sorted(by_request.items()):
            self.stdout.write(f"  {source_number:8} {count}")
        for problem in blockers:
            self.stdout.write(self.style.ERROR(f"  blocked: {problem}"))
        if blockers:
            raise CommandError(
                f"{len(blockers)} blocking issue(s). Nothing was written."
            )
        if not options["commit"]:
            self.stdout.write(self.style.SUCCESS("Dry run only. Nothing was written."))
            return

        written, per_request = commit_entries(planned, operator=operator)
        corrections = plan_corrections(
            payload, source_system=options["source_system"]
        )
        corrected = commit_corrections(corrections, operator=operator)
        if corrected:
            self.stdout.write(self.style.WARNING(
                f"Superseded {corrected} earlier no-response note(s) the "
                "imported timeline disproves."
            ))
        self.stdout.write(self.style.SUCCESS(
            f"Imported {written} timeline entries across {len(per_request)} case(s)."
        ))
        if written < len(planned):
            self.stdout.write(
                f"{len(planned) - written} already present and were skipped."
            )
