"""REST parity for grounded AI redaction proposals and human dispositions."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APITestCase

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.records.advisory_ai import advisory_input_sha256
from apps.records.models import (
    AdvisoryRun,
    ExemptionCitation,
    PublicRecordsRequest,
    RecordItem,
    RedactionProposal,
    RedactionProposalDecision,
    Requester,
)


@override_settings(
    PRR_AI_ADVISORY_ENABLED=True,
    PRR_AI_AWS_REGION="us-west-2",
    PRR_AI_MODEL="district-approved-model-v1",
    PRR_AI_MAX_INPUT_CHARACTERS=50_000,
)
class RedactionAIAPITests(APITestCase):
    def setUp(self):
        self.reviewer = get_user_model().objects.create_user(
            "redaction-api-reviewer",
            "reviewer@example.org",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.reviewer, {StaffRole.REVIEWER})
        self.client.force_authenticate(self.reviewer)
        self.request = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="requester@example.org"),
            description="Student records for Alex Example.",
        )
        self.item = RecordItem.objects.create(
            request=self.request,
            storage_key="records/item.pdf",
            content_length=12,
            content_type="application/pdf",
            sha256="a" * 64,
            review_state=RecordItem.ReviewState.RESPONSIVE,
            reviewed_by=self.reviewer,
        )
        self.exemption = ExemptionCitation.objects.create(
            citation="FERPA 20 U.S.C. §1232g",
            short_name="Education records",
            requires_second_review=True,
        )

    @patch("apps.records.tasks.execute_advisory_analysis.delay")
    def test_get_and_post_redaction_advisory_are_reviewer_scoped(self, delay):
        url = f"/api/staff/record-items/{self.item.pk}/advisory-redactions/"

        empty = self.client.get(url)
        self.assertEqual(empty.status_code, 200)
        self.assertTrue(empty.data["enabled"])
        self.assertIsNone(empty.data["latest"])
        self.assertEqual(empty.data["proposals"], [])

        queued = self.client.post(url, {})
        self.assertEqual(queued.status_code, 202, queued.content)
        self.assertEqual(queued.data["kind"], "record_redactions")
        self.assertFalse(queued.data["applied"])
        delay.assert_called_once_with(queued.data["id"])
        self.assertEqual(self.item.redactions.count(), 0)

    def _proposal(self):
        run = AdvisoryRun.objects.create(
            kind=AdvisoryRun.Kind.RECORD_REDACTIONS,
            state=AdvisoryRun.State.COMPLETE,
            request=self.request,
            record_item=self.item,
            requested_by=self.reviewer,
            model_identifier="district-approved-model-v1",
            input_sha256="placeholder",
            proposal={"candidate_count": 1, "page_count": 1},
        )
        run.input_sha256 = advisory_input_sha256(run)
        run.save(update_fields=["input_sha256"])
        return RedactionProposal.objects.create(
            advisory_run=run,
            record_item=self.item,
            page_number=1,
            x0=0.1,
            y0=0.2,
            x1=0.4,
            y1=0.3,
            exemption=self.exemption,
            confidence=0.9,
            rationale="Possible student identifier.",
            source_span_ids=["p1s1"],
            source_text_sha256="b" * 64,
        )

    def test_get_exposes_grounded_candidate_without_source_text(self):
        proposal = self._proposal()

        response = self.client.get(
            f"/api/staff/record-items/{self.item.pk}/advisory-redactions/"
        )

        self.assertEqual(response.status_code, 200)
        body = response.data["proposals"][0]
        self.assertEqual(body["id"], str(proposal.pk))
        self.assertEqual(body["citation"], self.exemption.citation)
        self.assertEqual(body["decision"], None)
        self.assertEqual(body["source_text_sha256"], "b" * 64)
        self.assertNotIn("Alex Example", str(body))

    @patch("apps.records.redaction_ai.create_redaction")
    def test_accept_endpoint_is_explicit_human_action_and_returns_decision(
        self,
        create,
    ):
        proposal = self._proposal()
        from apps.records.models import Redaction

        redaction = Redaction.objects.create(
            record_item=self.item,
            page_number=1,
            x0=0.12,
            y0=0.21,
            x1=0.41,
            y1=0.31,
            exemption=self.exemption,
            explanation="Human-confirmed student identifier.",
            created_by=self.reviewer,
        )
        create.return_value = redaction

        response = self.client.post(
            f"/api/staff/redaction-proposals/{proposal.pk}/accept/",
            {
                "page_number": 1,
                "x0": 0.12,
                "y0": 0.21,
                "x1": 0.41,
                "y1": 0.31,
                "exemption": self.exemption.pk,
                "explanation": "Human-confirmed student identifier.",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["decision"]["outcome"], "accepted")
        self.assertEqual(response.data["decision"]["redaction"], redaction.pk)
        create.assert_called_once_with(
            self.item.pk,
            actor=self.reviewer,
            page_number=1,
            x0=0.12,
            y0=0.21,
            x1=0.41,
            y1=0.31,
            exemption_id=self.exemption.pk,
            explanation="Human-confirmed student identifier.",
        )

    def test_reject_endpoint_requires_substantive_reason(self):
        proposal = self._proposal()
        url = f"/api/staff/redaction-proposals/{proposal.pk}/reject/"
        self.assertEqual(self.client.post(url, {"reason": "No"}, format="json").status_code, 400)

        response = self.client.post(
            url,
            {"reason": "This span is not exempt information."},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["decision"]["outcome"], "rejected")
        self.assertFalse(RedactionProposalDecision.objects.get(
            proposal=proposal
        ).redaction_id)
