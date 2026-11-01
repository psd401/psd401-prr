# ABOUTME: Validates every activated message template against the current spec.
# ABOUTME: Catches code rule changes that silently orphan a district template.
"""Report activated templates that the current rules would now reject.

An activated ``AutomaticMessageTemplate`` is district data, validated once
against the rules that existed when a Records Officer published it. The specs
in ``automatic_templates`` are code, and tightening one -- adding a required
token or a required phrase -- retroactively invalidates every stored template
that predates the change. Nothing notices until an officer tries to send that
letter, at which point drafting fails and the officer, who changed nothing,
gets an authoring error.

That is exactly how the acknowledgment broke: PR #113 dropped the
``estimated_completion`` token requirement so the district could publish its
counsel-approved wording, the district published it, and PR #186 reinstated
the requirement without looking at the row. Every acknowledgment draft failed
from then until someone tried to send one.

Run this after any change to a ``TemplateSpec``, and against production after
a deploy that touches one.
"""
import json

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.correspondence.automatic_templates import (
    template_guardrail_warnings,
    template_spec,
    validate_template_syntax,
)
from apps.correspondence.models import AutomaticMessageTemplate


class Command(BaseCommand):
    help = "Check that activated message templates still satisfy their spec."

    def add_arguments(self, parser):
        parser.add_argument(
            "--json",
            action="store_true",
            help="Emit the result as JSON.",
        )

    def handle(self, *args, **options):
        results = []
        templates = AutomaticMessageTemplate.objects.exclude(
            state=AutomaticMessageTemplate.State.DRAFT,
        ).order_by("kind", "template_version")
        for template in templates:
            try:
                spec = template_spec(template.kind)
                subject, body = validate_template_syntax(
                    template.kind,
                    template.subject_template,
                    template.body_template,
                )
            except ValidationError as exc:
                results.append(
                    {
                        "id": str(template.pk),
                        "kind": template.kind,
                        "template_version": template.template_version,
                        "state": template.state,
                        "ok": False,
                        "errors": list(exc.messages),
                        "unacknowledged_guardrails": [],
                    }
                )
                continue
            # A guardrail an officer knowingly published past is not drift.
            # Only one that appeared *since* they decided is worth reporting,
            # which is what makes this command useful after a spec change.
            acknowledged = set(template.guardrail_overrides or ())
            unacknowledged = [
                warning.as_dict()
                for warning in template_guardrail_warnings(
                    template.kind,
                    subject,
                    body,
                )
                if warning.code not in acknowledged
            ]
            results.append(
                {
                    "id": str(template.pk),
                    "kind": template.kind,
                    "label": spec.label,
                    "template_version": template.template_version,
                    "state": template.state,
                    "ok": not unacknowledged,
                    "errors": [],
                    "unacknowledged_guardrails": unacknowledged,
                }
            )

        broken = [result for result in results if not result["ok"]]
        if options["json"]:
            self.stdout.write(
                json.dumps(
                    {"checked": len(results), "broken": broken},
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        else:
            for result in results:
                if result["ok"]:
                    self.stdout.write(
                        f"{result['kind']} v{result['template_version']} "
                        f"({result['state']}): ok"
                    )
                else:
                    detail = " ".join(
                        result["errors"]
                        + [
                            f"{item['message']} {item['consequence']}"
                            for item in result["unacknowledged_guardrails"]
                        ]
                    )
                    self.stdout.write(
                        f"{result['kind']} v{result['template_version']} "
                        f"({result['state']}): {detail}"
                    )
            if not results:
                self.stdout.write("No activated message templates to check.")

        if broken:
            raise CommandError(
                f"{len(broken)} activated message template(s) trip a guardrail "
                "nobody has acknowledged; review each before that letter is "
                "needed. Delivery is not blocked -- a published template "
                "always renders -- so this is a review queue, not an outage."
            )
