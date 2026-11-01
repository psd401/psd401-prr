"""The district's own acknowledgment wording must keep working.

The five-day acknowledgment is the one letter the district had already
published in its own words before this system existed, pasted from
NextRequest and reviewed by counsel. It states the RCW 42.56.520 estimate as
a duration -- "60 business days" -- rather than as a rendered calendar date.

PR #113 made room for that on purpose by dropping ``estimated_completion``
from the acknowledgment's required tokens. The district activated its wording
the next day. PR #186 rebuilt the template system and reinstated the token
requirement without checking the row that was already active, so from then on
every acknowledgment draft failed at render time -- and failed with an
authoring error naming ``estimated_completion``, which reads like a complaint
about the date field in the compose dialog the officer had just filled in.

These tests pin the wording family, not one template: an acknowledgment may
express its estimate as a duration, and may not drop the statute.
"""

import datetime as dt
import json
from dataclasses import replace
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError

from apps.configuration import runtime
from apps.correspondence import services
from apps.correspondence.automatic_templates import (
    TEMPLATE_SPECS,
    validate_template_source,
)
from apps.correspondence.models import Correspondence

from .test_editable_templates import StructuredTemplateBase

#: The wording activated in production on 2026-08-14, trimmed to the parts
#: that carry legal or structural weight. Reproduced here so a future rule
#: change has to break a test before it can break the district's letter.
DISTRICT_SUBJECT = (
    "Public Records Request {{ request_number }} — Acknowledgment"
)
DISTRICT_BODY = """Dear {{ requester_name }},

Peninsula School District has received your Public Records Request.
We will begin to search for records responsive to your request. The District
processes all requests in the order in which they are received.

Pursuant to RCW 42.56.520, the Peninsula School District is providing you
with notice that it needs additional time to complete your request. The
additional time is necessary to locate and assemble the materials requested
and to determine whether any of the documents are exempt from disclosure or
require redaction, or to notify third persons or agencies affected by the
request. We will strive to provide you with the first installment of any
responsive records by 60 business days.

You will have 30 days to review or request copies of assembled records once
they are available. If you do not respond to such notice of availability,
your request will be closed.
"""


class DistrictAcknowledgmentWordingTests(StructuredTemplateBase):
    def test_estimate_may_be_stated_as_a_duration_not_a_date(self):
        """RCW 42.56.520 asks for the time required, not for a date."""
        validate_template_source(
            Correspondence.Kind.ACKNOWLEDGMENT,
            DISTRICT_SUBJECT,
            DISTRICT_BODY,
        )
        self.assertNotIn(
            "estimated_completion",
            TEMPLATE_SPECS[Correspondence.Kind.ACKNOWLEDGMENT].required_tokens,
        )

    def test_the_activated_district_wording_drafts_an_acknowledgment(self):
        self.activate(
            Correspondence.Kind.ACKNOWLEDGMENT,
            subject=DISTRICT_SUBJECT,
            body=DISTRICT_BODY,
        )
        draft = services.create_acknowledgment_draft(
            self.request,
            actor=self.officer,
        )
        self.assertEqual(draft.kind, Correspondence.Kind.ACKNOWLEDGMENT)
        self.assertIn("60 business days", draft.draft_body)
        self.assertIn("RCW 42.56.520", draft.draft_body)
        self.assertIn(self.request.number, draft.draft_subject)

    def test_a_blank_date_still_tracks_an_estimate_on_the_case(self):
        """The compose dialog promises a default when the field is blank."""
        self.request.estimated_completion = None
        self.request.save(update_fields=["estimated_completion"])
        self.activate(
            Correspondence.Kind.ACKNOWLEDGMENT,
            subject=DISTRICT_SUBJECT,
            body=DISTRICT_BODY,
        )
        draft = services.create_acknowledgment_draft(
            self.request,
            actor=self.officer,
        )
        estimate = dt.date.fromisoformat(
            draft.draft_metadata["estimated_completion"]
        )
        self.assertGreater(estimate, dt.date.today())

    def test_the_default_matches_the_horizon_the_letter_promises(self):
        """A 20-day tracked estimate under a 60-day letter is a lie."""
        self.assertEqual(runtime.get("DEFAULT_ESTIMATE_BUSINESS_DAYS"), 60)
        self.assertIn("60 business days", DISTRICT_BODY)

    def test_dropping_the_statute_warns_loudly(self):
        """Relaxing the token requirement must not silence the citation."""
        _, _, warnings = validate_template_source(
            Correspondence.Kind.ACKNOWLEDGMENT,
            DISTRICT_SUBJECT,
            DISTRICT_BODY.replace("RCW 42.56.520", "district policy"),
        )
        self.assertIn(
            "must cite the statute",
            " ".join(warning.consequence for warning in warnings),
        )

    def test_the_district_wording_needs_no_override_at_all(self):
        """The published district letter must publish on the ordinary path."""
        _, _, warnings = validate_template_source(
            Correspondence.Kind.ACKNOWLEDGMENT,
            DISTRICT_SUBJECT,
            DISTRICT_BODY,
        )
        self.assertEqual(warnings, ())

    def test_an_estimate_is_still_required_on_the_case_itself(self):
        """The letter may omit the date; the case may not omit the estimate."""
        self.assertIn(
            "estimated_completion",
            TEMPLATE_SPECS[Correspondence.Kind.ACKNOWLEDGMENT].required_values,
        )


class TemplateRuleDriftTests(StructuredTemplateBase):
    """A rule tightened in code must never break an already-published letter.

    Activated templates are district data, decided by a human against the
    rules in force when they published. Specs are code. When the two drift,
    the old design made the officer find out at render time, months later,
    while composing a letter -- and refused to produce it.

    That is now impossible: rendering checks only that substitution is
    possible. Drift becomes a review queue instead of an outage.
    """

    def _tighten(self):
        """Add a required token the stored district wording cannot satisfy."""
        spec = TEMPLATE_SPECS[Correspondence.Kind.ACKNOWLEDGMENT]
        return mock.patch.dict(
            TEMPLATE_SPECS,
            {
                Correspondence.Kind.ACKNOWLEDGMENT: replace(
                    spec,
                    required_tokens=spec.required_tokens
                    | {"estimated_completion"},
                )
            },
        )

    def test_a_tightened_rule_does_not_stop_the_letter_going_out(self):
        self.activate(
            Correspondence.Kind.ACKNOWLEDGMENT,
            subject=DISTRICT_SUBJECT,
            body=DISTRICT_BODY,
        )

        with self._tighten():
            draft = services.create_acknowledgment_draft(
                self.request,
                actor=self.officer,
            )

        self.assertIn("60 business days", draft.draft_body)

    def test_the_check_command_surfaces_the_drift_for_review(self):
        self.activate(
            Correspondence.Kind.ACKNOWLEDGMENT,
            subject=DISTRICT_SUBJECT,
            body=DISTRICT_BODY,
        )
        out = StringIO()
        call_command("check_message_templates", stdout=out)
        self.assertIn("ok", out.getvalue())

        with self._tighten(), self.assertRaises(CommandError):
            call_command("check_message_templates", stdout=StringIO())

    def test_the_check_command_names_the_consequence_not_the_token(self):
        self.activate(
            Correspondence.Kind.ACKNOWLEDGMENT,
            subject=DISTRICT_SUBJECT,
            body=DISTRICT_BODY,
        )
        out = StringIO()
        with self._tighten(), self.assertRaises(CommandError):
            call_command("check_message_templates", "--json", stdout=out)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["checked"], 1)
        self.assertEqual(len(payload["broken"]), 1)
        consequences = " ".join(
            item["consequence"]
            for item in payload["broken"][0]["unacknowledged_guardrails"]
        )
        self.assertIn("RCW 42.56.520", consequences)

    def test_an_acknowledged_guardrail_is_not_reported_as_drift(self):
        """What an officer knowingly published past is a decision, not a bug."""
        with self._tighten():
            template = services.create_automatic_message_template_draft(
                kind=Correspondence.Kind.ACKNOWLEDGMENT,
                subject_template=DISTRICT_SUBJECT,
                body_template=DISTRICT_BODY,
                change_reason="Publish the counsel-approved district wording.",
                actor=self.officer,
                guardrail_override_reason=(
                    "Counsel-approved wording states the estimate as a "
                    "duration rather than a date."
                ),
            )
            services.activate_automatic_message_template(
                template,
                actor=self.officer,
                expected_version=template.revision,
                confirmed=True,
                reason="Activate the counsel-reviewed district wording.",
            )
            out = StringIO()
            call_command("check_message_templates", stdout=out)

        self.assertIn("ok", out.getvalue())
        template.refresh_from_db()
        self.assertEqual(
            template.guardrail_overrides,
            ["missing_token:estimated_completion"],
        )
