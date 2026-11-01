import datetime as dt

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from apps.correspondence.rendering import render_versioned_template
from apps.records.models import Installment, PublicRecordsRequest, Requester


class VersionedRenderingTests(TestCase):
    def setUp(self):
        requester = Requester.objects.create(
            name="Jane Requester",
            email="jane@example.org",
        )
        self.prr = PublicRecordsRequest.objects.create(
            requester=requester,
            description="All transportation-plan records.",
            received_at=timezone.make_aware(dt.datetime(2026, 7, 1, 10, 30)),
        )
        self.base_context = {
            "request": self.prr,
            "status_url": f"https://prr.psd401.net/requests/{self.prr.number}",
            "pra_officer_email": "publicrecords@psd401.net",
        }

    def test_acknowledgment_v1_is_deterministic_and_contains_required_content(self):
        context = self.base_context | {
            "estimated_completion": dt.date(2026, 8, 3),
        }

        first = render_versioned_template("acknowledgment", "1", context)
        second = render_versioned_template("acknowledgment", "1", context)

        self.assertEqual(first, second)
        self.assertEqual(
            first.subject,
            f"Public Records Request {self.prr.number} — Acknowledgment",
        )
        self.assertIn("RCW 42.56.520", first.body)
        self.assertIn("August 3, 2026", first.body)
        self.assertIn(self.prr.description, first.body)
        self.assertIn(context["status_url"], first.body)
        self.assertTrue(first.body.endswith("\n"))

    def test_v1_staff_status_links_remain_reproducible_for_historical_snapshots(self):
        acknowledgment = render_versioned_template(
            "acknowledgment",
            "1",
            self.base_context
            | {"estimated_completion": dt.date(2026, 8, 3)},
        )
        estimate_revision = render_versioned_template(
            "estimate_revision",
            "1",
            self.base_context
            | {
                "previous_date": dt.date(2026, 8, 3),
                "new_date": dt.date(2026, 8, 17),
                "reason": "The search located an additional archive.",
            },
        )

        self.assertIn(self.base_context["status_url"], acknowledgment.body)
        self.assertIn(
            "You may track the status of your request at any time:",
            acknowledgment.body,
        )
        self.assertIn(self.base_context["status_url"], estimate_revision.body)
        self.assertIn("You may track status at:", estimate_revision.body)

    def test_v2_requester_templates_do_not_advertise_staff_status_route(self):
        acknowledgment = render_versioned_template(
            "acknowledgment",
            "2",
            self.base_context
            | {"estimated_completion": dt.date(2026, 8, 3)},
        )
        estimate_revision = render_versioned_template(
            "estimate_revision",
            "2",
            self.base_context
            | {
                "previous_date": dt.date(2026, 8, 3),
                "new_date": dt.date(2026, 8, 17),
                "reason": "The search located an additional archive.",
            },
        )

        for rendered in (acknowledgment, estimate_revision):
            with self.subTest(subject=rendered.subject):
                self.assertNotIn(self.base_context["status_url"], rendered.body)
                self.assertNotIn("status", rendered.body.lower())
                self.assertNotIn("track the status", rendered.body.lower())
                self.assertNotIn("track status", rendered.body.lower())

    def test_submission_receipt_is_plainly_nonstatutory(self):
        rendered = render_versioned_template(
            "submission_receipt",
            "1",
            self.base_context,
        )

        self.assertIn(self.prr.number, rendered.subject)
        self.assertIn(self.prr.number, rendered.body)
        self.assertIn(self.prr.description, rendered.body)
        self.assertIn(
            "This confirmation is not the District's five-business-day "
            "response required by RCW 42.56.520.",
            rendered.body,
        )
        self.assertIn(self.base_context["pra_officer_email"], rendered.body)

    def test_plain_text_rendering_does_not_emit_html_entities(self):
        special_request = PublicRecordsRequest.objects.create(
            requester=self.prr.requester,
            description="Buses & routes <2026>",
            received_at=self.prr.received_at,
        )

        rendered = render_versioned_template(
            "acknowledgment",
            "1",
            self.base_context
            | {
                "request": special_request,
                "estimated_completion": dt.date(2026, 8, 3),
            },
        )

        self.assertIn("Buses & routes <2026>", rendered.body)
        self.assertNotIn("&amp;", rendered.body)
        self.assertNotIn("&lt;", rendered.body)

    def test_clarification_and_estimate_templates_render_structured_inputs(self):
        clarification = render_versioned_template(
            "clarification_request",
            "1",
            self.base_context
            | {
                "clarification_questions": "Which school years are in scope?",
                "clarification_due": dt.date(2026, 7, 31),
            },
        )
        estimate = render_versioned_template(
            "estimate_revision",
            "1",
            self.base_context
            | {
                "previous_date": dt.date(2026, 8, 3),
                "new_date": dt.date(2026, 8, 17),
                "reason": "The search located an additional archive.",
            },
        )

        self.assertIn("Which school years are in scope?", clarification.body)
        self.assertIn("July 31, 2026", clarification.body)
        self.assertIn("August 3, 2026", estimate.body)
        self.assertIn("August 17, 2026", estimate.body)
        self.assertIn("additional archive", estimate.body)

    def test_installment_v2_is_explicitly_a_non_delivery_non_closure_draft(self):
        installment = Installment.objects.create(
            request=self.prr,
            sequence=1,
            download_expires_at=timezone.now() + dt.timedelta(days=7),
        )

        rendered = render_versioned_template(
            "installment_notice",
            "2",
            self.base_context
            | {
                "installment": installment,
                "download_url": (
                    f"https://prr.psd401.net/requests/{self.prr.number}"
                    "/installments/1"
                ),
                "item_count": 0,
                "has_exemptions": False,
                "is_final": True,
            },
        )

        self.assertIn("Delivery Draft", rendered.subject)
        self.assertIn("cannot yet be sent", rendered.body)
        self.assertIn("does not close the request", rendered.body)
        self.assertNotIn("now available", rendered.body)
        self.assertNotIn("now closed", rendered.body)

    def test_unknown_or_malformed_template_output_fails_closed(self):
        with self.assertRaises(ValidationError):
            render_versioned_template("acknowledgment", "999", self.base_context)

        with self.assertRaises(ValidationError):
            render_versioned_template(
                "test_header_injection",
                "1",
                self.base_context,
            )
