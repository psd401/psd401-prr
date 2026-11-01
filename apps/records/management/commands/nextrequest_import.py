"""Plan or commit an exact mapped NextRequest CSV export."""

import json
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.records.nextrequest_migration import (
    commit_nextrequest_plan,
    plan_nextrequest_import,
)


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def load_inputs(source_path, mapping_path):
    try:
        source = Path(source_path).read_bytes()
        mapping_text = Path(mapping_path).read_text(encoding="utf-8")
        mapping = json.loads(mapping_text, object_pairs_hook=_reject_duplicate_keys)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise CommandError(f"Unable to read migration input: {exc}") from exc
    return source, mapping


def find_operator(username):
    operator = get_user_model().objects.filter(
        username=username,
        is_active=True,
        is_staff=True,
    ).first()
    if operator is None:
        raise CommandError("--operator must identify an active staff user")
    return operator


class Command(BaseCommand):
    help = "Dry-run by default; commit only an exact reviewed NextRequest plan digest."

    def add_arguments(self, parser):
        parser.add_argument("source_csv")
        parser.add_argument("mapping_json")
        parser.add_argument("--operator", required=True)
        parser.add_argument("--commit", action="store_true")
        parser.add_argument("--expected-plan-sha256")

    def handle(self, *args, **options):
        source, mapping = load_inputs(options["source_csv"], options["mapping_json"])
        try:
            plan = plan_nextrequest_import(source, mapping)
        except ValidationError as exc:
            raise CommandError("; ".join(exc.messages)) from exc

        summary = {
            "mode": "commit" if options["commit"] else "dry-run",
            "ready": plan.ready,
            "source_sha256": plan.source_sha256,
            "mapping_sha256": plan.mapping_sha256,
            "plan_sha256": plan.plan_sha256,
            "valid_rows": len(plan.rows),
            "blocked_issues": [
                {
                    "row": issue.row_number,
                    "field": issue.field,
                    "message": issue.message,
                }
                for issue in plan.issues
            ],
        }
        if not options["commit"]:
            self.stdout.write(json.dumps(summary, sort_keys=True, indent=2))
            if not plan.ready:
                raise CommandError("Dry-run is blocked; correct every reported issue.")
            return

        expected = options.get("expected_plan_sha256")
        if not expected:
            raise CommandError(
                "--commit requires --expected-plan-sha256 from a separately reviewed dry-run."
            )
        operator = find_operator(options["operator"])
        try:
            batch = commit_nextrequest_plan(
                plan,
                actor=operator,
                expected_plan_sha256=expected,
            )
        except (PermissionDenied, ValidationError) as exc:
            messages = getattr(exc, "messages", [str(exc)])
            raise CommandError("; ".join(messages)) from exc
        summary["batch_id"] = str(batch.pk)
        summary["imported_rows"] = batch.row_count
        self.stdout.write(self.style.SUCCESS(json.dumps(summary, sort_keys=True, indent=2)))
