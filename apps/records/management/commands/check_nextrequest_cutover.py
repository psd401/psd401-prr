"""Fail a deployment gate unless one exact NextRequest plan is cutover-ready."""

import json

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.records.management.commands.nextrequest_import import load_inputs
from apps.records.nextrequest_migration import (
    assess_nextrequest_cutover,
    plan_nextrequest_import,
)


class Command(BaseCommand):
    help = "Verify exact import and open-request activation for a source/mapping pair."

    def add_arguments(self, parser):
        parser.add_argument("source_csv")
        parser.add_argument("mapping_json")

    def handle(self, *args, **options):
        source, mapping = load_inputs(options["source_csv"], options["mapping_json"])
        try:
            plan = plan_nextrequest_import(source, mapping)
        except ValidationError as exc:
            raise CommandError("; ".join(exc.messages)) from exc
        report = assess_nextrequest_cutover(plan)
        payload = {
            "ready": report.ready,
            "plan_sha256": report.plan_sha256,
            "source_rows": report.source_rows,
            "imported_rows": report.imported_rows,
            "activated_open_rows": report.activated_open_rows,
            "blockers": report.blockers,
            "warnings": report.warnings,
        }
        self.stdout.write(json.dumps(payload, sort_keys=True, indent=2))
        if not report.ready:
            raise CommandError("NextRequest cutover is blocked.")
