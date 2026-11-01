"""Advisory AI proposes; it never performs a PRA legal outcome."""

import hashlib
import io
import json
from datetime import UTC, datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models.query import QuerySet
from django.test import TestCase, override_settings

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.records.advisory_ai import (
    AdvisoryProviderError,
    _advisory_system_message,
    _validate_proposal,
    execute_advisory_run,
    invoke_advisory_model,
    request_record_responsiveness_advisory,
    request_request_intent_advisory,
)
from apps.records.models import (
    AdvisoryRun,
    IntakeAttachment,
    PublicRecordsRequest,
    RecordItem,
    Requester,
)
from apps.records.tasks import execute_advisory_analysis


# The region joins the model here because advisory AI is inert without an
# approved model to call, and "approved" means provider, region, and model.
@override_settings(
    PRR_AI_ADVISORY_ENABLED=True,
    PRR_AI_AWS_REGION="us-west-2",
    PRR_AI_MODEL="district-approved-model-v1",
    PRR_AI_MAX_INPUT_CHARACTERS=50_000,
)
class AdvisoryAITests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.officer = User.objects.create_user(
            "ai-officer", "officer@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.reviewer = User.objects.create_user(
            "ai-reviewer", "reviewer@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.reviewer, {StaffRole.REVIEWER})
        self.liaison = User.objects.create_user(
            "ai-liaison", "liaison@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.liaison, {StaffRole.LIAISON})
        requester = Requester.objects.create(email="requester@example.org")
        self.request = PublicRecordsRequest.objects.create(
            requester=requester,
            description="Emails about the 2025 field renovation contract.",
        )
        self.item = RecordItem.objects.create(
            request=self.request,
            source_system="gmail",
            original_filename="message.pdf",
            storage_key="records/source.pdf",
            content_length=100,
            content_type="application/pdf",
            sha256="a" * 64,
            ocr_text="Subject: Field project. Attached is the renovation contract.",
        )

    @patch("apps.records.tasks.execute_advisory_analysis.delay")
    def test_request_intent_queue_is_attributed_deduplicated_and_has_no_legal_effect(
        self,
        delay,
    ):
        with self.assertRaises(PermissionDenied):
            request_request_intent_advisory(self.request.pk, actor=self.liaison)

        run = request_request_intent_advisory(self.request.pk, actor=self.officer)
        repeated = request_request_intent_advisory(self.request.pk, actor=self.officer)

        self.assertEqual(run, repeated)
        self.assertEqual(run.kind, AdvisoryRun.Kind.REQUEST_INTENT)
        self.assertEqual(run.state, AdvisoryRun.State.PENDING)
        self.assertEqual(run.requested_by, self.officer)
        self.assertEqual(len(run.input_sha256), 64)
        delay.assert_called_once_with(str(run.pk))
        self.request.refresh_from_db()
        self.assertEqual(self.request.status, "received")
        self.assertIsNone(self.request.estimated_completion)

    def test_request_intent_input_carries_the_pacific_receipt_date(self):
        """G3: responsive records are those in existence at receipt.

        The model cannot propose a date range it was never told. The receipt
        date is a case fact, so it travels in the payload the input digest is
        taken over, not in the prompt text. It is the district's calendar
        day: a request logged at 22:30 Pacific is 05:30 UTC the next morning,
        and the UTC date would tell the model a later day's records are
        responsive.
        """
        from apps.records.advisory_ai import _build_advisory_input

        self.request.received_at = datetime(2026, 7, 2, 5, 30, tzinfo=UTC)
        self.request.save(update_fields=["received_at"])
        run = AdvisoryRun.objects.create(
            kind=AdvisoryRun.Kind.REQUEST_INTENT,
            request=self.request,
            requested_by=self.officer,
            model_identifier="district-approved-model-v1",
            input_sha256="placeholder",
        )

        payload = _build_advisory_input(run).payload

        self.assertEqual(payload["request_received_date"], "2026-07-01")

    def test_request_intent_instruction_bounds_responsiveness_at_receipt(self):
        system_message = _advisory_system_message(AdvisoryRun.Kind.REQUEST_INTENT)

        self.assertIn("request_received_date", system_message)
        self.assertIn("in existence", system_message)

    @patch("apps.records.tasks.execute_advisory_analysis.delay")
    def test_record_queue_requires_human_reviewer_and_ocr(self, delay):
        with self.assertRaises(PermissionDenied):
            request_record_responsiveness_advisory(self.item.pk, actor=self.liaison)

        run = request_record_responsiveness_advisory(
            self.item.pk,
            actor=self.reviewer,
        )
        self.assertEqual(run.record_item, self.item)
        delay.assert_called_once_with(str(run.pk))

        self.item.ocr_text = ""
        self.item.save(update_fields=["ocr_text"])
        with self.assertRaisesMessage(ValidationError, "searchable text"):
            request_record_responsiveness_advisory(self.item.pk, actor=self.reviewer)

    @patch("apps.records.advisory_ai.invoke_advisory_model")
    @patch("apps.records.advisory_ai.open_clean_attachment")
    def test_request_intent_sends_clean_pdf_attachment_to_bedrock(
        self,
        open_attachment,
        invoke,
    ):
        attachment_bytes = b"%PDF-1.7 synthetic request details"
        clean_attachment = IntakeAttachment.objects.create(
            request=self.request,
            original_filename="request details.pdf",
            content_type="application/pdf",
            size=len(attachment_bytes),
            sha256=hashlib.sha256(attachment_bytes).hexdigest(),
            storage_key="intake/request-details.pdf",
            scan_status=IntakeAttachment.ScanStatus.CLEAN,
        )
        IntakeAttachment.objects.create(
            request=self.request,
            original_filename="still-scanning.pdf",
            content_type="application/pdf",
            size=20,
            sha256="c" * 64,
            storage_key="quarantine/intake/still-scanning.pdf",
            scan_status=IntakeAttachment.ScanStatus.PENDING,
        )
        open_attachment.return_value = io.BytesIO(attachment_bytes)
        run = AdvisoryRun.objects.create(
            kind=AdvisoryRun.Kind.REQUEST_INTENT,
            request=self.request,
            requested_by=self.officer,
            model_identifier="district-approved-model-v1",
            input_sha256="placeholder",
        )
        from apps.records.advisory_ai import (
            _build_advisory_input,
            advisory_input_sha256,
        )

        advisory_input = _build_advisory_input(run)
        run.input_sha256 = advisory_input_sha256(run)
        run.input_truncated = advisory_input.truncated
        run.save(update_fields=["input_sha256", "input_truncated"])
        invoke.return_value = {
            "summary": "The attached request seeks former-student records.",
            "required_search_terms": ["former student"],
            "search_terms": ["former student"],
            "record_system_slugs": [],
            "clarification_questions": [],
            "complexity_flags": [],
            "date_range": {"start": None, "end": None},
            "narrowing_options": [],
        }

        execute_advisory_run(run.pk)

        run.refresh_from_db()
        self.assertEqual(run.state, AdvisoryRun.State.COMPLETE)
        self.assertTrue(run.input_truncated)
        open_attachment.assert_called_once()
        self.assertEqual(open_attachment.call_args.args[0].pk, clean_attachment.pk)
        attachment_blocks = invoke.call_args.kwargs["attachment_blocks"]
        self.assertEqual(
            attachment_blocks,
            [
                {
                    "document": {
                        "format": "pdf",
                        "name": "attachment 1",
                        "source": {"bytes": attachment_bytes},
                    }
                }
            ],
        )

    @patch("apps.records.advisory_ai.invoke_advisory_model")
    @patch("apps.records.advisory_ai.open_clean_attachment")
    def test_request_intent_attachment_integrity_mismatch_fails_closed(
        self,
        open_attachment,
        invoke,
    ):
        expected = b"%PDF-1.7 expected bytes"
        IntakeAttachment.objects.create(
            request=self.request,
            original_filename="request.pdf",
            content_type="application/pdf",
            size=len(expected),
            sha256=hashlib.sha256(expected).hexdigest(),
            storage_key="intake/request.pdf",
            scan_status=IntakeAttachment.ScanStatus.CLEAN,
        )
        open_attachment.return_value = io.BytesIO(b"%PDF-1.7 changed bytes")
        run = AdvisoryRun.objects.create(
            kind=AdvisoryRun.Kind.REQUEST_INTENT,
            request=self.request,
            requested_by=self.officer,
            model_identifier="district-approved-model-v1",
            input_sha256="placeholder",
        )
        from apps.records.advisory_ai import (
            _build_advisory_input,
            advisory_input_sha256,
        )

        advisory_input = _build_advisory_input(run)
        run.input_sha256 = advisory_input_sha256(run)
        run.input_truncated = advisory_input.truncated
        run.save(update_fields=["input_sha256", "input_truncated"])

        execute_advisory_run(run.pk)

        run.refresh_from_db()
        self.assertEqual(run.state, AdvisoryRun.State.FAILED)
        self.assertEqual(run.failure_code, "source_changed")
        invoke.assert_not_called()

    @patch("apps.records.advisory_ai.invoke_advisory_model")
    def test_responsiveness_result_is_bounded_and_never_applied(self, invoke):
        run = AdvisoryRun.objects.create(
            kind=AdvisoryRun.Kind.RECORD_RESPONSIVENESS,
            request=self.request,
            record_item=self.item,
            requested_by=self.reviewer,
            model_identifier="district-approved-model-v1",
            input_sha256="placeholder",
        )
        from apps.records.advisory_ai import advisory_input_sha256

        run.input_sha256 = advisory_input_sha256(run)
        run.save(update_fields=["input_sha256"])
        invoke.return_value = {
            "suggested_state": "responsive",
            "confidence": 0.82,
            "matched_terms": ["field renovation", "contract"],
            "rationale": "The document discusses the requested renovation contract.",
        }

        execute_advisory_run(run.pk)

        run.refresh_from_db()
        self.item.refresh_from_db()
        self.assertEqual(run.state, AdvisoryRun.State.COMPLETE)
        self.assertEqual(run.proposal["suggested_state"], "responsive")
        self.assertFalse(run.applied)
        self.assertEqual(self.item.review_state, RecordItem.ReviewState.UNREVIEWED)
        self.assertIsNone(self.item.reviewed_by)
        self.assertTrue(AuditLog.objects.filter(
            action="advisory.completed",
            target=str(run.pk),
            detail__input_sha256=run.input_sha256,
        ).exists())

    @patch("apps.records.advisory_ai.invoke_advisory_model")
    def test_worker_locks_only_the_advisory_row(self, invoke):
        """PostgreSQL rejects FOR UPDATE across the nullable record-item join."""
        run = AdvisoryRun.objects.create(
            kind=AdvisoryRun.Kind.REQUEST_INTENT,
            request=self.request,
            requested_by=self.officer,
            model_identifier="district-approved-model-v1",
            input_sha256="placeholder",
        )
        from apps.records.advisory_ai import advisory_input_sha256

        run.input_sha256 = advisory_input_sha256(run)
        run.save(update_fields=["input_sha256"])
        invoke.return_value = {
            "summary": "Records about the field renovation contract.",
            "required_search_terms": [],
            "search_terms": ["field renovation", "contract"],
            "record_system_slugs": [],
            "clarification_questions": [],
            "complexity_flags": [],
            "date_range": {"start": None, "end": None},
            "narrowing_options": [],
        }
        original_select_for_update = QuerySet.select_for_update
        lock_calls = []

        def record_lock_scope(queryset, *args, **kwargs):
            lock_calls.append(kwargs)
            return original_select_for_update(queryset, *args, **kwargs)

        with patch.object(QuerySet, "select_for_update", new=record_lock_scope):
            execute_advisory_run(run.pk)

        self.assertIn({"of": ("self",)}, lock_calls)

    @patch(
        "apps.records.advisory_ai.execute_advisory_run",
        side_effect=RuntimeError("synthetic worker defect"),
    )
    def test_unexpected_worker_failure_does_not_leave_run_in_progress(self, execute):
        run = AdvisoryRun.objects.create(
            kind=AdvisoryRun.Kind.REQUEST_INTENT,
            request=self.request,
            requested_by=self.officer,
            model_identifier="district-approved-model-v1",
            input_sha256="f" * 64,
        )

        with self.assertRaises(RuntimeError):
            execute_advisory_analysis(str(run.pk))

        run.refresh_from_db()
        self.assertEqual(run.state, AdvisoryRun.State.FAILED)
        self.assertEqual(run.failure_code, "worker_error")
        self.assertEqual(
            run.failure_detail,
            "The advisory worker could not complete the analysis.",
        )
        self.assertEqual(run.proposal, {})

    @patch("apps.records.advisory_ai.invoke_advisory_model")
    def test_model_cannot_suggest_withholding_or_smuggle_unbounded_output(self, invoke):
        run = AdvisoryRun.objects.create(
            kind=AdvisoryRun.Kind.RECORD_RESPONSIVENESS,
            request=self.request,
            record_item=self.item,
            requested_by=self.reviewer,
            model_identifier="district-approved-model-v1",
            input_sha256="placeholder",
        )
        from apps.records.advisory_ai import advisory_input_sha256

        run.input_sha256 = advisory_input_sha256(run)
        run.save(update_fields=["input_sha256"])
        invoke.return_value = {
            "suggested_state": "withheld",
            "confidence": 0.99,
            "matched_terms": [],
            "rationale": "Hide it.",
        }

        execute_advisory_run(run.pk)

        run.refresh_from_db()
        self.assertEqual(run.state, AdvisoryRun.State.FAILED)
        self.assertEqual(run.failure_code, "invalid_model_output")
        # The validator's own message names the failed check, never model text.
        self.assertEqual(
            run.failure_detail,
            "The advisory result failed local validation: "
            "The suggested state is not advisory-safe.",
        )
        self.assertNotIn("Hide it", run.failure_detail)
        failed = AuditLog.objects.get(action="advisory.failed", target=str(run.pk))
        self.assertEqual(
            failed.detail["failure_reason"],
            "The suggested state is not advisory-safe.",
        )
        self.assertEqual(run.proposal, {})
        self.item.refresh_from_db()
        self.assertEqual(self.item.review_state, RecordItem.ReviewState.UNREVIEWED)

    @patch("apps.records.advisory_ai._validate_proposal")
    @patch("apps.records.advisory_ai.invoke_advisory_model")
    def test_an_unexpected_value_error_keeps_the_generic_detail(self, invoke, validate):
        # Only the validators' own messages are recorded. Any other
        # ValueError (a library error can quote its input) stays generic.
        run = AdvisoryRun.objects.create(
            kind=AdvisoryRun.Kind.RECORD_RESPONSIVENESS,
            request=self.request,
            record_item=self.item,
            requested_by=self.reviewer,
            model_identifier="district-approved-model-v1",
            input_sha256="placeholder",
        )
        from apps.records.advisory_ai import advisory_input_sha256

        run.input_sha256 = advisory_input_sha256(run)
        run.save(update_fields=["input_sha256"])
        invoke.return_value = {}
        validate.side_effect = ValueError("invalid literal: secret requester text")

        execute_advisory_run(run.pk)

        run.refresh_from_db()
        self.assertEqual(run.failure_code, "invalid_model_output")
        self.assertEqual(
            run.failure_detail, "The advisory result failed local validation."
        )
        failed = AuditLog.objects.get(action="advisory.failed", target=str(run.pk))
        self.assertNotIn("failure_reason", failed.detail)

    def test_request_intent_rejects_twenty_or_alternatives(self):
        proposal = {
            "summary": "A bounded synthetic request.",
            "required_search_terms": ["Jordan Sample"],
            "search_terms": [f"alternative {index}" for index in range(20)],
            "record_system_slugs": [],
            "clarification_questions": [],
            "complexity_flags": [],
            "date_range": {"start": None, "end": None},
            "narrowing_options": [],
        }

        with self.assertRaisesMessage(ValueError, "search_terms must be a bounded list"):
            _validate_proposal(AdvisoryRun.Kind.REQUEST_INTENT, proposal)

    def _intent_proposal(self, **overrides):
        proposal = {
            "summary": "A bounded synthetic request.",
            "required_search_terms": ["Jordan Sample"],
            "search_terms": ["stadium"],
            "record_system_slugs": [],
            "clarification_questions": [],
            "complexity_flags": [],
            "date_range": {"start": None, "end": None},
            "narrowing_options": [],
        }
        proposal.update(overrides)
        return proposal

    def test_request_intent_accepts_a_short_title(self):
        validated = _validate_proposal(
            AdvisoryRun.Kind.REQUEST_INTENT,
            self._intent_proposal(short_title="  Stadium contract emails  "),
        )

        self.assertEqual(validated["short_title"], "Stadium contract emails")

    def test_request_intent_still_accepts_output_without_a_short_title(self):
        """The key is optional, so an older model answer stays valid.

        Runs stored before the key existed are read back through the same
        shape, and a provider that ignores the new instruction degrades to the
        description column rather than failing the run.
        """
        validated = _validate_proposal(
            AdvisoryRun.Kind.REQUEST_INTENT, self._intent_proposal()
        )

        self.assertNotIn("short_title", validated)

    def test_request_intent_rejects_an_unbounded_short_title(self):
        with self.assertRaisesMessage(ValueError, "short_title"):
            _validate_proposal(
                AdvisoryRun.Kind.REQUEST_INTENT,
                self._intent_proposal(short_title="x" * 81),
            )

    def test_request_intent_still_rejects_an_unknown_key(self):
        with self.assertRaisesMessage(ValueError, "unexpected fields"):
            _validate_proposal(
                AdvisoryRun.Kind.REQUEST_INTENT,
                self._intent_proposal(suggested_denial="deny it"),
            )

    def test_request_intent_still_requires_every_mandatory_key(self):
        proposal = self._intent_proposal(short_title="Stadium emails")
        del proposal["summary"]

        with self.assertRaisesMessage(ValueError, "unexpected fields"):
            _validate_proposal(AdvisoryRun.Kind.REQUEST_INTENT, proposal)

    def test_the_short_title_is_named_in_the_output_contract(self):
        from apps.records.advisory_ai import _advisory_system_message

        message = _advisory_system_message(AdvisoryRun.Kind.REQUEST_INTENT)

        self.assertIn("short_title", message)
        self.assertIn("80 characters", message)

    @patch("apps.records.advisory_ai.invoke_advisory_model")
    def test_provider_failure_retains_only_non_sensitive_failure_category(self, invoke):
        run = AdvisoryRun.objects.create(
            kind=AdvisoryRun.Kind.REQUEST_INTENT,
            request=self.request,
            requested_by=self.officer,
            model_identifier="district-approved-model-v1",
            input_sha256="placeholder",
        )
        from apps.records.advisory_ai import advisory_input_sha256

        run.input_sha256 = advisory_input_sha256(run)
        run.save(update_fields=["input_sha256"])
        invoke.side_effect = AdvisoryProviderError(
            "provider_timeout", "secret requester content must not be persisted"
        )

        execute_advisory_run(run.pk)

        run.refresh_from_db()
        self.assertEqual(run.state, AdvisoryRun.State.FAILED)
        self.assertEqual(run.failure_code, "provider_timeout")
        self.assertEqual(run.failure_detail, "The advisory provider did not return a usable result.")
        self.assertNotIn("secret requester", run.failure_detail)

    @override_settings(PRR_AI_ADVISORY_ENABLED=False)
    def test_feature_is_fail_closed_when_disabled(self):
        with self.assertRaisesMessage(
            ValidationError,
            "Advisory AI is not enabled in this environment.",
        ):
            request_request_intent_advisory(self.request.pk, actor=self.officer)

    @override_settings(PRR_AI_MODEL="")
    def test_feature_is_inert_without_an_approved_model(self):
        """The other half of the store's asymmetry.

        The settings store refuses to *enable* advisory AI without an
        approved model, and deliberately allows the model to be blanked
        afterwards -- the same way a parent switch may always be turned off.
        That only holds if the consumer goes inert rather than calling the
        provider with an empty model identifier.
        """
        with self.assertRaisesMessage(
            ValidationError,
            "Advisory AI has no approved model configured.",
        ):
            request_request_intent_advisory(self.request.pk, actor=self.officer)

    def test_database_rejects_marking_a_model_proposal_applied(self):
        run = AdvisoryRun.objects.create(
            kind=AdvisoryRun.Kind.REQUEST_INTENT,
            request=self.request,
            requested_by=self.officer,
            model_identifier="district-approved-model-v1",
            input_sha256="f" * 64,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            AdvisoryRun.objects.filter(pk=run.pk).update(applied=True)

    @override_settings(
        PRR_AI_PROVIDER="bedrock",
        PRR_AI_AWS_REGION="us-west-2",
        PRR_AI_MODEL="us.amazon.nova-2-lite-v1:0",
        PRR_AI_MAX_RESPONSE_BYTES=8,
    )
    def test_bedrock_response_is_rejected_at_the_configured_byte_limit(self):
        with patch("apps.records.advisory_ai.boto3.client") as client_factory:
            client_factory.return_value.converse.return_value = {
                "output": {"message": {"content": [{"text": "123456789"}]}}
            }
            with self.assertRaises(AdvisoryProviderError) as raised:
                invoke_advisory_model(
                    AdvisoryRun.Kind.REQUEST_INTENT,
                    {"request_description": "Test request"},
                )
        self.assertEqual(raised.exception.code, "invalid_provider_response")

    @override_settings(
        PRR_AI_PROVIDER="bedrock",
        PRR_AI_AWS_REGION="us-west-2",
        PRR_AI_MODEL="us.amazon.nova-2-lite-v1:0",
        PRR_AI_MAX_RESPONSE_BYTES=65_536,
    )
    def test_bedrock_converse_uses_keyless_bounded_advisory_contract(self):
        proposal = {
            "summary": "Records about the field renovation contract.",
            "required_search_terms": ["field renovation"],
            "search_terms": ["contract", "general contractor"],
            "record_system_slugs": [],
            "clarification_questions": [],
            "complexity_flags": [],
            "date_range": {"start": None, "end": None},
            "narrowing_options": [],
        }
        with patch("apps.records.advisory_ai.boto3.client") as client_factory:
            client_factory.return_value.converse.return_value = {
                "output": {
                    "message": {"content": [{"text": json.dumps(proposal)}]}
                }
            }

            result = invoke_advisory_model(
                AdvisoryRun.Kind.REQUEST_INTENT,
                {"request_description": "Test request"},
                attachment_blocks=[
                    {
                        "document": {
                            "format": "pdf",
                            "name": "attachment 1",
                            "source": {"bytes": b"%PDF-1.7 request details"},
                        }
                    }
                ],
            )

        self.assertEqual(result, proposal)
        client_factory.assert_called_once()
        service_name, = client_factory.call_args.args
        self.assertEqual(service_name, "bedrock-runtime")
        self.assertEqual(client_factory.call_args.kwargs["region_name"], "us-west-2")
        call = client_factory.return_value.converse.call_args.kwargs
        self.assertEqual(call["modelId"], "us.amazon.nova-2-lite-v1:0")
        self.assertEqual(call["inferenceConfig"], {"maxTokens": 1000, "temperature": 0})
        self.assertEqual(
            call["messages"][0]["content"][1]["document"]["name"],
            "attachment 1",
        )
        self.assertEqual(
            call["messages"][0]["content"][1]["document"]["source"]["bytes"],
            b"%PDF-1.7 request details",
        )
        self.assertIn(
            "no markdown fence or surrounding text",
            call["system"][0]["text"],
        )
        self.assertIn(
            "mandatory AND anchors",
            call["system"][0]["text"],
        )
        self.assertEqual(
            call["requestMetadata"],
            {
                "application": "psd401-prr",
                "feature": "advisory",
                "kind": "request_intent",
            },
        )
        self.assertNotIn("apiKey", client_factory.call_args.kwargs)

    @override_settings(
        PRR_AI_PROVIDER="bedrock",
        PRR_AI_AWS_REGION="us-west-2",
        PRR_AI_MODEL="us.amazon.nova-2-lite-v1:0",
        PRR_AI_MAX_RESPONSE_BYTES=65_536,
    )
    def test_bedrock_accepts_exactly_one_json_markdown_fence(self):
        proposal = {
            "summary": "Synthetic board minutes request.",
            "required_search_terms": [],
            "search_terms": ["board minutes"],
            "record_system_slugs": [],
            "clarification_questions": [],
            "complexity_flags": [],
            "date_range": {"start": None, "end": None},
            "narrowing_options": [],
        }
        fenced = f"```json\n{json.dumps(proposal)}\n```"
        with patch("apps.records.advisory_ai.boto3.client") as client_factory:
            client_factory.return_value.converse.return_value = {
                "output": {"message": {"content": [{"text": fenced}]}}
            }

            result = invoke_advisory_model(
                AdvisoryRun.Kind.REQUEST_INTENT,
                {"request_description": "Synthetic request"},
            )

        self.assertEqual(result, proposal)

    @override_settings(
        PRR_AI_PROVIDER="bedrock",
        PRR_AI_AWS_REGION="us-west-2",
        PRR_AI_MODEL="us.amazon.nova-2-lite-v1:0",
        PRR_AI_MAX_RESPONSE_BYTES=65_536,
    )
    def test_bedrock_rejects_fenced_json_with_surrounding_text(self):
        content = 'Here is the result:\n```json\n{"summary":"unsafe"}\n```'
        with patch("apps.records.advisory_ai.boto3.client") as client_factory:
            client_factory.return_value.converse.return_value = {
                "output": {"message": {"content": [{"text": content}]}}
            }

            with self.assertRaises(AdvisoryProviderError) as raised:
                invoke_advisory_model(
                    AdvisoryRun.Kind.REQUEST_INTENT,
                    {"request_description": "Synthetic request"},
                )

        self.assertEqual(raised.exception.code, "invalid_provider_response")
