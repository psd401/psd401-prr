"""Automatic AI preparation stops before every human legal judgment."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.records.advisory_ai import (
    execute_advisory_run,
    request_automatic_record_advisory,
)
from apps.records.models import (
    AdvisoryRun,
    ExemptionCitation,
    PublicRecordsRequest,
    RecordItem,
    Redaction,
    Requester,
)
from apps.records.review import review_record


@override_settings(
    PRR_AI_ADVISORY_ENABLED=True,
    PRR_AI_AWS_REGION="us-west-2",
    PRR_AI_AUTOMATIC_RECORD_REVIEW_ENABLED=True,
    PRR_AI_MODEL="district-approved-model-v1",
    PRR_AI_MAX_INPUT_CHARACTERS=50_000,
)
class AutomaticRecordAdvisoryTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.reviewer = User.objects.create_user(
            "automatic-reviewer",
            "reviewer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.reviewer, {StaffRole.REVIEWER})
        self.request = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="requester@example.org"),
            description="Emails about the named student's counseling records.",
        )
        self.item = RecordItem.objects.create(
            request=self.request,
            source_system="google_vault_gmail",
            original_filename="message.pdf",
            storage_key="records/message.pdf",
            content_length=100,
            content_type="application/pdf",
            sha256="a" * 64,
            ocr_text="Counseling notes for the named student.",
        )
        ExemptionCitation.objects.create(
            citation="20 U.S.C. § 1232g",
            short_name="FERPA education records",
        )

    @patch("apps.records.tasks.execute_advisory_analysis.delay")
    def test_system_queues_attributed_automatic_responsiveness_without_legal_effect(
        self,
        delay,
    ):
        run = request_automatic_record_advisory(
            self.item.pk,
            kind=AdvisoryRun.Kind.RECORD_RESPONSIVENESS,
        )

        self.assertEqual(run.trigger, AdvisoryRun.Trigger.AUTOMATIC)
        self.assertIsNone(run.requested_by)
        self.assertEqual(run.state, AdvisoryRun.State.PENDING)
        self.assertFalse(run.applied)
        delay.assert_called_once_with(str(run.pk))
        self.item.refresh_from_db()
        self.assertEqual(self.item.review_state, RecordItem.ReviewState.UNREVIEWED)
        self.assertIsNone(self.item.reviewed_by)

    @override_settings(PRR_AI_AUTOMATIC_RECORD_REVIEW_ENABLED=False)
    @patch("apps.records.advisory_ai.invoke_advisory_model")
    def test_queued_automatic_run_rechecks_kill_switch_before_provider_call(
        self,
        invoke,
    ):
        run = AdvisoryRun.objects.create(
            kind=AdvisoryRun.Kind.RECORD_RESPONSIVENESS,
            trigger=AdvisoryRun.Trigger.AUTOMATIC,
            request=self.request,
            record_item=self.item,
            requested_by=None,
            model_identifier="district-approved-model-v1",
            input_sha256="placeholder",
        )
        from apps.records.advisory_ai import advisory_input_sha256

        run.input_sha256 = advisory_input_sha256(run)
        run.save(update_fields=["input_sha256"])

        execute_advisory_run(run.pk)

        invoke.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.state, AdvisoryRun.State.FAILED)
        self.assertEqual(run.failure_code, "automatic_disabled")
        self.assertIsNotNone(run.completed_at)
        failure = AuditLog.objects.get(
            action="advisory.failed",
            target=str(run.pk),
        )
        self.assertEqual(failure.detail["failure_code"], "automatic_disabled")
        self.assertEqual(failure.detail["trigger"], AdvisoryRun.Trigger.AUTOMATIC)

    @override_settings(PRR_AI_ADVISORY_ENABLED=False)
    @patch("apps.records.advisory_ai.invoke_advisory_model")
    def test_queued_run_records_global_pause_without_provider_call(self, invoke):
        run = AdvisoryRun.objects.create(
            kind=AdvisoryRun.Kind.RECORD_RESPONSIVENESS,
            trigger=AdvisoryRun.Trigger.AUTOMATIC,
            request=self.request,
            record_item=self.item,
            requested_by=None,
            model_identifier="district-approved-model-v1",
            input_sha256="placeholder",
        )
        from apps.records.advisory_ai import advisory_input_sha256

        run.input_sha256 = advisory_input_sha256(run)
        run.save(update_fields=["input_sha256"])

        execute_advisory_run(run.pk)

        invoke.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.state, AdvisoryRun.State.FAILED)
        self.assertEqual(run.failure_code, "advisory_disabled")
        failure = AuditLog.objects.get(
            action="advisory.failed",
            target=str(run.pk),
        )
        self.assertEqual(failure.detail["failure_code"], "advisory_disabled")

    @override_settings(PRR_AI_AUTOMATIC_RECORD_REVIEW_ENABLED=False)
    @patch("apps.records.advisory_ai.invoke_advisory_model")
    def test_manual_run_remains_available_when_only_automatic_gate_is_off(
        self,
        invoke,
    ):
        invoke.return_value = {
            "suggested_state": "non_responsive",
            "confidence": 0.91,
            "matched_terms": [],
            "rationale": "The record concerns a different subject.",
        }
        run = AdvisoryRun.objects.create(
            kind=AdvisoryRun.Kind.RECORD_RESPONSIVENESS,
            trigger=AdvisoryRun.Trigger.MANUAL,
            request=self.request,
            record_item=self.item,
            requested_by=self.reviewer,
            model_identifier="district-approved-model-v1",
            input_sha256="placeholder",
        )
        from apps.records.advisory_ai import advisory_input_sha256

        run.input_sha256 = advisory_input_sha256(run)
        run.save(update_fields=["input_sha256"])

        execute_advisory_run(run.pk)

        invoke.assert_called_once()
        run.refresh_from_db()
        self.assertEqual(run.state, AdvisoryRun.State.COMPLETE)
        self.assertEqual(run.failure_code, "")

    def test_manual_runs_cannot_lose_accountable_requester(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            AdvisoryRun.objects.create(
                kind=AdvisoryRun.Kind.RECORD_RESPONSIVENESS,
                trigger=AdvisoryRun.Trigger.MANUAL,
                request=self.request,
                record_item=self.item,
                requested_by=None,
                model_identifier="district-approved-model-v1",
                input_sha256="b" * 64,
            )

    def test_only_the_automatic_trigger_may_omit_the_requester(self):
        """Intake may start an intent run; a Run button may not lose its actor.

        The intake-time intent proposal is deliberately unattributed -- no
        human asked for it -- so the constraint admits it. What it must still
        refuse is a run that claims to be someone's manual action while
        recording nobody as having taken it.
        """
        unattributed = AdvisoryRun.objects.create(
            kind=AdvisoryRun.Kind.REQUEST_INTENT,
            trigger=AdvisoryRun.Trigger.AUTOMATIC,
            request=self.request,
            record_item=None,
            requested_by=None,
            model_identifier="district-approved-model-v1",
            input_sha256="b" * 64,
        )
        self.assertIsNone(unattributed.requested_by_id)

        with self.assertRaises(IntegrityError), transaction.atomic():
            AdvisoryRun.objects.create(
                kind=AdvisoryRun.Kind.REQUEST_INTENT,
                trigger=AdvisoryRun.Trigger.MANUAL,
                request=self.request,
                record_item=None,
                requested_by=None,
                model_identifier="district-approved-model-v1",
                input_sha256="c" * 64,
            )

    @patch("apps.records.advisory_ai.defer_automatic_record_advisory")
    @patch("apps.records.advisory_ai.invoke_advisory_model")
    def test_responsive_or_uncertain_recommendation_precomputes_redactions(
        self,
        invoke,
        defer,
    ):
        invoke.return_value = {
            "suggested_state": "responsive",
            "confidence": 0.88,
            "matched_terms": ["counseling"],
            "rationale": "The record matches the requested subject.",
        }
        run = AdvisoryRun.objects.create(
            kind=AdvisoryRun.Kind.RECORD_RESPONSIVENESS,
            trigger=AdvisoryRun.Trigger.AUTOMATIC,
            request=self.request,
            record_item=self.item,
            requested_by=None,
            model_identifier="district-approved-model-v1",
            input_sha256="placeholder",
        )
        from apps.records.advisory_ai import advisory_input_sha256

        run.input_sha256 = advisory_input_sha256(run)
        run.save(update_fields=["input_sha256"])

        execute_advisory_run(run.pk)

        defer.assert_called_once_with(
            self.item.pk,
            kind=AdvisoryRun.Kind.RECORD_REDACTIONS,
        )
        self.item.refresh_from_db()
        self.assertEqual(self.item.review_state, RecordItem.ReviewState.UNREVIEWED)
        self.assertEqual(Redaction.objects.count(), 0)

    @patch("apps.records.advisory_ai.defer_automatic_record_advisory")
    @patch("apps.records.advisory_ai.invoke_advisory_model")
    def test_nonresponsive_recommendation_does_not_precompute_redactions(
        self,
        invoke,
        defer,
    ):
        invoke.return_value = {
            "suggested_state": "non_responsive",
            "confidence": 0.91,
            "matched_terms": [],
            "rationale": "The record concerns a different subject.",
        }
        run = AdvisoryRun.objects.create(
            kind=AdvisoryRun.Kind.RECORD_RESPONSIVENESS,
            trigger=AdvisoryRun.Trigger.AUTOMATIC,
            request=self.request,
            record_item=self.item,
            requested_by=None,
            model_identifier="district-approved-model-v1",
            input_sha256="placeholder",
        )
        from apps.records.advisory_ai import advisory_input_sha256

        run.input_sha256 = advisory_input_sha256(run)
        run.save(update_fields=["input_sha256"])

        execute_advisory_run(run.pk)

        defer.assert_not_called()

    @patch("apps.records.advisory_ai.request_automatic_record_advisory")
    def test_human_responsive_confirmation_queues_redactions_after_commit(
        self,
        request_automatic,
    ):
        with self.captureOnCommitCallbacks(execute=True):
            reviewed = review_record(
                self.item.pk,
                state=RecordItem.ReviewState.RESPONSIVE,
                actor=self.reviewer,
            )

        self.assertEqual(reviewed.review_state, RecordItem.ReviewState.RESPONSIVE)
        request_automatic.assert_called_once_with(
            self.item.pk,
            kind=AdvisoryRun.Kind.RECORD_REDACTIONS,
            actor=self.reviewer,
        )
        self.assertEqual(Redaction.objects.count(), 0)

    @patch("apps.records.tasks.execute_advisory_analysis.delay")
    def test_automatic_redaction_precompute_reuses_same_completed_input(self, delay):
        first = request_automatic_record_advisory(
            self.item.pk,
            kind=AdvisoryRun.Kind.RECORD_REDACTIONS,
        )
        first.state = AdvisoryRun.State.COMPLETE
        first.proposal = {"candidate_count": 0, "page_count": 1}
        first.save(update_fields=["state", "proposal"])
        delay.reset_mock()

        repeated = request_automatic_record_advisory(
            self.item.pk,
            kind=AdvisoryRun.Kind.RECORD_REDACTIONS,
        )

        self.assertEqual(repeated, first)
        delay.assert_not_called()

    @patch("apps.records.tasks.execute_advisory_analysis.delay")
    def test_periodic_supervisor_backfills_missing_automatic_work(self, delay):
        responsive = RecordItem.objects.create(
            request=self.request,
            source_system="google_vault_gmail",
            original_filename="already-responsive.pdf",
            storage_key="records/already-responsive.pdf",
            content_length=200,
            content_type="application/pdf",
            sha256="c" * 64,
            ocr_text="Responsive student record.",
            review_state=RecordItem.ReviewState.RESPONSIVE,
            reviewed_by=self.reviewer,
        )
        from apps.records import tasks

        queued = tasks.prepare_record_review_advisories(limit=25)

        self.assertEqual(queued, 2)
        self.assertTrue(AdvisoryRun.objects.filter(
            record_item=self.item,
            kind=AdvisoryRun.Kind.RECORD_RESPONSIVENESS,
            trigger=AdvisoryRun.Trigger.AUTOMATIC,
        ).exists())
        self.assertTrue(AdvisoryRun.objects.filter(
            record_item=responsive,
            kind=AdvisoryRun.Kind.RECORD_REDACTIONS,
            trigger=AdvisoryRun.Trigger.AUTOMATIC,
        ).exists())
        self.assertEqual(delay.call_count, 2)

    @patch("apps.records.tasks.execute_advisory_analysis.delay")
    def test_planned_pause_does_not_exhaust_supervisor_retry_budget(self, delay):
        for failure_code in (
            "automatic_disabled",
            "advisory_disabled",
            "automatic_disabled",
            "advisory_disabled",
        ):
            AdvisoryRun.objects.create(
                kind=AdvisoryRun.Kind.RECORD_RESPONSIVENESS,
                state=AdvisoryRun.State.FAILED,
                trigger=AdvisoryRun.Trigger.AUTOMATIC,
                request=self.request,
                record_item=self.item,
                requested_by=None,
                model_identifier="district-approved-model-v1",
                input_sha256="d" * 64,
                failure_code=failure_code,
            )
        from apps.records import tasks

        queued = tasks.prepare_record_review_advisories(limit=25)

        self.assertEqual(queued, 1)
        self.assertTrue(
            AdvisoryRun.objects.filter(
                record_item=self.item,
                kind=AdvisoryRun.Kind.RECORD_RESPONSIVENESS,
                state=AdvisoryRun.State.PENDING,
            ).exists()
        )
        delay.assert_called_once()

    @patch("apps.records.tasks.execute_advisory_analysis.delay")
    def test_periodic_supervisor_backfills_precomputed_responsive_signal(self, delay):
        AdvisoryRun.objects.create(
            kind=AdvisoryRun.Kind.RECORD_RESPONSIVENESS,
            state=AdvisoryRun.State.COMPLETE,
            trigger=AdvisoryRun.Trigger.AUTOMATIC,
            request=self.request,
            record_item=self.item,
            requested_by=None,
            model_identifier="district-approved-model-v1",
            input_sha256="d" * 64,
            proposal={
                "suggested_state": "uncertain",
                "confidence": 0.5,
                "matched_terms": [],
                "rationale": "Human review is needed.",
            },
        )
        from apps.records import tasks

        queued = tasks.prepare_record_review_advisories(limit=25)

        self.assertEqual(queued, 1)
        self.assertTrue(AdvisoryRun.objects.filter(
            record_item=self.item,
            kind=AdvisoryRun.Kind.RECORD_REDACTIONS,
            trigger=AdvisoryRun.Trigger.AUTOMATIC,
            state=AdvisoryRun.State.PENDING,
        ).exists())
        delay.assert_called_once()
