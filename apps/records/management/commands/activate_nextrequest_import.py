"""Explicitly activate live triage for imported open requests."""

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.records.management.commands.nextrequest_import import find_operator
from apps.records.models import LegacyImportBatch, LegacyRequestImport
from apps.records.nextrequest_migration import activate_legacy_request
from apps.records.services import CLOSED_STATUSES


class Command(BaseCommand):
    help = "Run preservation and RecordSystem routing for imported open requests."

    def add_arguments(self, parser):
        parser.add_argument("batch_id")
        parser.add_argument("--operator", required=True)
        parser.add_argument("--source-id")
        parser.add_argument("--confirm-live-triage", action="store_true")

    def handle(self, *args, **options):
        if not options["confirm_live_triage"]:
            raise CommandError(
                "No activation performed. Re-run with --confirm-live-triage after "
                "verifying live hold and notification integrations."
            )
        operator = find_operator(options["operator"])
        try:
            batch = LegacyImportBatch.objects.get(pk=options["batch_id"])
        except (LegacyImportBatch.DoesNotExist, ValueError) as exc:
            raise CommandError("Legacy import batch not found") from exc
        imports = LegacyRequestImport.objects.filter(batch=batch).select_related("request")
        if options.get("source_id"):
            imports = imports.filter(source_id=options["source_id"])
        imports = imports.exclude(request__status__in=CLOSED_STATUSES).order_by("source_id")
        if not imports.exists():
            raise CommandError("No matching open imported requests require activation.")

        activated = 0
        already_active = 0
        for imported in imports:
            existed = hasattr(imported, "activation")
            try:
                activate_legacy_request(imported.pk, actor=operator)
            except (PermissionDenied, ValidationError) as exc:
                messages = getattr(exc, "messages", [str(exc)])
                raise CommandError(
                    f"Activation stopped at source ID {imported.source_id}: "
                    + "; ".join(messages)
                ) from exc
            if existed:
                already_active += 1
            else:
                activated += 1
        self.stdout.write(
            self.style.SUCCESS(
                f"Activated {activated} open request(s); {already_active} were already active."
            )
        )
