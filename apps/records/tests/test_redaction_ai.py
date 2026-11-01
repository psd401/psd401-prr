"""AI redaction candidates are grounded proposals until a human decides."""

import hashlib
import io
from types import SimpleNamespace
from unittest.mock import patch

import pymupdf as fitz
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.records.advisory_ai import (
    advisory_input_sha256,
    execute_advisory_run,
    request_record_redaction_advisory,
)
from apps.records.models import (
    AdvisoryRun,
    ExemptionCitation,
    PublicRecordsRequest,
    RecordItem,
    Redaction,
    RedactionProposal,
    RedactionProposalDecision,
    Requester,
)
from apps.records.redaction_ai import (
    _page_spans,
    accept_redaction_proposal,
    reject_redaction_proposal,
)
from apps.records.review import _redaction_source_rectangle


def _pdf_with_text(*pages):
    document = fitz.open()
    for text in pages:
        page = document.new_page(width=612, height=792)
        page.insert_text((72, 96), text, fontsize=12)
    payload = document.tobytes()
    document.close()
    return payload


@override_settings(
    PRR_AI_ADVISORY_ENABLED=True,
    PRR_AI_AWS_REGION="us-west-2",
    PRR_AI_MODEL="district-approved-model-v1",
    PRR_AI_MAX_INPUT_CHARACTERS=50_000,
)
class RedactionAITests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.reviewer = User.objects.create_user(
            "redaction-ai-reviewer",
            "reviewer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.reviewer, {StaffRole.REVIEWER})
        self.liaison = User.objects.create_user(
            "redaction-ai-liaison",
            "liaison@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.liaison, {StaffRole.LIAISON})
        self.request = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="requester@example.org"),
            description="Records about student Alex Example.",
        )
        self.pdf = _pdf_with_text(
            "Student Alex Example date of birth January 2 2010.",
            "General district schedule with no student details.",
        )
        self.item = RecordItem.objects.create(
            request=self.request,
            original_filename="student-record.pdf",
            storage_key="records/student-record.pdf",
            content_length=len(self.pdf),
            content_type="application/pdf",
            sha256=hashlib.sha256(self.pdf).hexdigest(),
            ocr_text="Student Alex Example date of birth January 2 2010.",
            review_state=RecordItem.ReviewState.RESPONSIVE,
            reviewed_by=self.reviewer,
        )
        self.exemption = ExemptionCitation.objects.create(
            citation="FERPA 20 U.S.C. §1232g",
            short_name="Education records — personally identifiable student info",
            requires_second_review=True,
        )

    def test_grounded_boxes_align_with_rotated_preview_and_source_page(self):
        document = fitz.open()
        page = document.new_page(width=612, height=792)
        page.insert_text((72, 96), "Rotated student identifier", fontsize=12)
        page.set_rotation(90)

        proposal = _page_spans(page, 1)[0]
        source_rectangle = _redaction_source_rectangle(
            page,
            SimpleNamespace(
                x0=proposal.x0,
                y0=proposal.y0,
                x1=proposal.x1,
                y1=proposal.y1,
            ),
        )
        word_rectangle = fitz.Rect(page.get_text("words", sort=True)[0][:4])

        self.assertTrue(source_rectangle.contains(word_rectangle))
        self.assertGreater(proposal.x0, 0.5)
        document.close()

    @patch("apps.records.tasks.execute_advisory_analysis.delay")
    def test_queue_requires_responsive_record_and_accountable_reviewer(self, delay):
        with self.assertRaises(PermissionDenied):
            request_record_redaction_advisory(self.item.pk, actor=self.liaison)

        run = request_record_redaction_advisory(self.item.pk, actor=self.reviewer)

        self.assertEqual(run.kind, AdvisoryRun.Kind.RECORD_REDACTIONS)
        self.assertEqual(run.record_item, self.item)
        self.assertEqual(run.state, AdvisoryRun.State.PENDING)
        self.assertFalse(run.applied)
        delay.assert_called_once_with(str(run.pk))
        self.assertEqual(Redaction.objects.count(), 0)

        self.item.review_state = RecordItem.ReviewState.UNREVIEWED
        self.item.save(update_fields=["review_state"])
        with self.assertRaisesMessage(ValidationError, "human-confirmed responsive"):
            request_record_redaction_advisory(self.item.pk, actor=self.reviewer)

    @patch("apps.records.tasks.execute_advisory_analysis.delay")
    def test_new_pass_requires_disposition_of_existing_candidates(self, delay):
        self._proposal()

        with self.assertRaisesMessage(ValidationError, "Review every existing"):
            request_record_redaction_advisory(self.item.pk, actor=self.reviewer)

        delay.assert_not_called()

    @patch("apps.records.redaction_ai.invoke_advisory_model")
    @patch("apps.records.preview.load_verified_object")
    def test_worker_grounds_span_ids_and_never_creates_legal_redactions(
        self,
        load_object,
        invoke,
    ):
        load_object.return_value = io.BytesIO(self.pdf)
        invoke.side_effect = [
            {
                "candidates": [
                    {
                        "span_ids": ["p1s1"],
                        "exemption_id": self.exemption.pk,
                        "confidence": 0.91,
                        "rationale": "The span contains a student identifier.",
                    }
                ]
            },
            {"candidates": []},
        ]
        run = AdvisoryRun.objects.create(
            kind=AdvisoryRun.Kind.RECORD_REDACTIONS,
            request=self.request,
            record_item=self.item,
            requested_by=self.reviewer,
            model_identifier="district-approved-model-v1",
            input_sha256="placeholder",
        )
        run.input_sha256 = advisory_input_sha256(run)
        run.save(update_fields=["input_sha256"])

        execute_advisory_run(run.pk)

        run.refresh_from_db()
        proposal = RedactionProposal.objects.get(advisory_run=run)
        self.assertEqual(run.state, AdvisoryRun.State.COMPLETE)
        self.assertEqual(run.proposal, {"candidate_count": 1, "page_count": 2})
        self.assertEqual(proposal.page_number, 1)
        self.assertEqual(proposal.source_span_ids, ["p1s1"])
        self.assertGreater(proposal.x1, proposal.x0)
        self.assertGreater(proposal.y1, proposal.y0)
        self.assertEqual(proposal.exemption, self.exemption)
        self.assertEqual(Redaction.objects.count(), 0)
        self.item.refresh_from_db()
        self.assertEqual(self.item.review_state, RecordItem.ReviewState.RESPONSIVE)
        payload = invoke.call_args_list[0].args[1]
        self.assertIn("text_spans", payload)
        self.assertNotIn("x0", str(payload))
        self.assertNotIn("storage_key", str(payload))

    @patch("apps.records.redaction_ai.invoke_advisory_model")
    @patch("apps.records.preview.load_verified_object")
    def test_worker_rejects_invented_span_or_exemption(self, load_object, invoke):
        load_object.return_value = io.BytesIO(self.pdf)
        invoke.return_value = {
            "candidates": [
                {
                    "span_ids": ["p1s999"],
                    "exemption_id": self.exemption.pk + 999,
                    "confidence": 1,
                    "rationale": "Invented output.",
                }
            ]
        }
        run = AdvisoryRun.objects.create(
            kind=AdvisoryRun.Kind.RECORD_REDACTIONS,
            request=self.request,
            record_item=self.item,
            requested_by=self.reviewer,
            model_identifier="district-approved-model-v1",
            input_sha256="placeholder",
        )
        run.input_sha256 = advisory_input_sha256(run)
        run.save(update_fields=["input_sha256"])

        execute_advisory_run(run.pk)

        run.refresh_from_db()
        self.assertEqual(run.state, AdvisoryRun.State.FAILED)
        self.assertEqual(run.failure_code, "invalid_model_output")
        self.assertEqual(
            run.failure_detail,
            "The advisory result failed local validation: "
            "The model proposed an unknown source span.",
        )
        self.assertEqual(RedactionProposal.objects.count(), 0)

    @patch("apps.records.redaction_ai.invoke_advisory_model")
    @patch("apps.records.preview.load_verified_object")
    def test_worker_discards_candidates_if_source_changes_during_inference(
        self,
        load_object,
        invoke,
    ):
        load_object.return_value = io.BytesIO(self.pdf)

        def model_result(kind, payload, **kwargs):
            if payload["page_number"] == 1:
                RecordItem.objects.filter(pk=self.item.pk).update(sha256="d" * 64)
                return {
                    "candidates": [
                        {
                            "span_ids": ["p1s1"],
                            "exemption_id": self.exemption.pk,
                            "confidence": 0.91,
                            "rationale": "Possible student identifier.",
                        }
                    ]
                }
            return {"candidates": []}

        invoke.side_effect = model_result
        run = AdvisoryRun.objects.create(
            kind=AdvisoryRun.Kind.RECORD_REDACTIONS,
            request=self.request,
            record_item=self.item,
            requested_by=self.reviewer,
            model_identifier="district-approved-model-v1",
            input_sha256="placeholder",
        )
        run.input_sha256 = advisory_input_sha256(run)
        run.save(update_fields=["input_sha256"])

        execute_advisory_run(run.pk)

        run.refresh_from_db()
        self.assertEqual(run.state, AdvisoryRun.State.FAILED)
        self.assertEqual(run.failure_code, "source_changed")
        self.assertEqual(RedactionProposal.objects.count(), 0)

    @patch("apps.records.redaction_ai.invoke_advisory_model")
    @patch("apps.records.preview.load_verified_object")
    def test_worker_fails_legibly_when_pdf_has_no_coordinate_text(
        self,
        load_object,
        invoke,
    ):
        blank_pdf = _pdf_with_text("")
        self.item.content_length = len(blank_pdf)
        self.item.sha256 = hashlib.sha256(blank_pdf).hexdigest()
        self.item.save(update_fields=["content_length", "sha256"])
        load_object.return_value = io.BytesIO(blank_pdf)
        run = AdvisoryRun.objects.create(
            kind=AdvisoryRun.Kind.RECORD_REDACTIONS,
            request=self.request,
            record_item=self.item,
            requested_by=self.reviewer,
            model_identifier="district-approved-model-v1",
            input_sha256="placeholder",
        )
        run.input_sha256 = advisory_input_sha256(run)
        run.save(update_fields=["input_sha256"])

        execute_advisory_run(run.pk)

        run.refresh_from_db()
        self.assertEqual(run.state, AdvisoryRun.State.FAILED)
        self.assertEqual(run.failure_code, "unsearchable_record")
        self.assertIn("coordinate-bearing searchable text", run.failure_detail)
        invoke.assert_not_called()

    def _proposal(self):
        run = AdvisoryRun.objects.create(
            kind=AdvisoryRun.Kind.RECORD_REDACTIONS,
            state=AdvisoryRun.State.COMPLETE,
            request=self.request,
            record_item=self.item,
            requested_by=self.reviewer,
            model_identifier="district-approved-model-v1",
            input_sha256="placeholder",
            proposal={"candidate_count": 1, "page_count": 2},
        )
        run.input_sha256 = advisory_input_sha256(run)
        run.save(update_fields=["input_sha256"])
        return RedactionProposal.objects.create(
            advisory_run=run,
            record_item=self.item,
            page_number=1,
            x0=0.1,
            y0=0.1,
            x1=0.3,
            y1=0.2,
            exemption=self.exemption,
            confidence=0.91,
            rationale="The span contains a student identifier.",
            source_span_ids=["p1s1"],
            source_text_sha256="b" * 64,
        )

    @patch("apps.records.review._open_verified_pdf")
    def test_human_can_edit_and_accept_proposal_once(self, open_pdf):
        open_pdf.return_value = fitz.open(stream=self.pdf, filetype="pdf")
        proposal = self._proposal()

        decision = accept_redaction_proposal(
            proposal.pk,
            actor=self.reviewer,
            page_number=1,
            x0=0.12,
            y0=0.11,
            x1=0.34,
            y1=0.23,
            exemption_id=self.exemption.pk,
            explanation="Human-confirmed student identifier.",
        )

        self.assertEqual(decision.outcome, RedactionProposalDecision.Outcome.ACCEPTED)
        self.assertEqual(decision.decided_by, self.reviewer)
        self.assertIsNotNone(decision.redaction)
        self.assertEqual(decision.redaction.x0, 0.12)
        self.assertEqual(decision.redaction.explanation, "Human-confirmed student identifier.")
        self.assertTrue(AuditLog.objects.filter(
            action="advisory.redaction_proposal.accepted",
            target=str(proposal.pk),
        ).exists())
        with self.assertRaisesMessage(ValidationError, "already been decided"):
            reject_redaction_proposal(
                proposal.pk,
                actor=self.reviewer,
                reason="A later conflicting decision is not allowed.",
            )

    def test_human_rejection_requires_reason_and_has_no_legal_effect(self):
        proposal = self._proposal()
        with self.assertRaisesMessage(ValidationError, "substantive reason"):
            reject_redaction_proposal(proposal.pk, actor=self.reviewer, reason="No")

        decision = reject_redaction_proposal(
            proposal.pk,
            actor=self.reviewer,
            reason="The proposed span is not exempt information.",
        )

        self.assertEqual(decision.outcome, RedactionProposalDecision.Outcome.REJECTED)
        self.assertIsNone(decision.redaction)
        self.assertEqual(Redaction.objects.count(), 0)
        self.item.refresh_from_db()
        self.assertEqual(self.item.review_state, RecordItem.ReviewState.RESPONSIVE)
