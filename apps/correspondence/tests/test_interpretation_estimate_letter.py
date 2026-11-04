"""The interpretation-and-estimate letter (issue #42).

The system drafts it from the request-intent summary and a computed
estimate; it never approves, queues, or sends it (rule 6). A Records Officer
approves and sends it in one action, and delivery records the stated date as
an estimate revision that names the estimate it came from.
"""

import datetime as dt
import hashlib

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.test import override_settings

from apps.audit.models import AuditLog
from apps.correspondence import services
from apps.correspondence.automatic_templates import _date
from apps.correspondence.models import Correspondence, CorrespondenceDelivery
from apps.correspondence.rendering import RenderedMessage
from apps.correspondence.tests.test_phase_four_letters import Phase4LetterTestCase
from apps.records.estimate_letters import prepare_estimate_letters
from apps.records.estimates import estimate_request
from apps.records.models import (
    AdvisoryRun,
    EstimateRevision,
    MatterKind,
    PublicRecordsRequest,
    RecordItem,
    RecordsEstimate,
)

INTENT = "Bus video from route 12 on March 3, 2026, between 3 and 4 p.m."


class _LetterFixture(Phase4LetterTestCase):
    def setUp(self):
        super().setUp()
        self._n = 0

    def _intent(self, prr=None, summary=INTENT):
        prr = prr or self.prr
        return AdvisoryRun.objects.create(
            kind=AdvisoryRun.Kind.REQUEST_INTENT,
            request=prr,
            model_identifier="synthetic-intent-model",
            input_sha256=hashlib.sha256(f"intent{prr.pk}".encode()).hexdigest(),
            state=AdvisoryRun.State.COMPLETE,
            requested_by=self.officer,
            proposal={"summary": summary},
        )

    def _record(self, prr=None, state=RecordItem.ReviewState.UNREVIEWED, *, suggested=True):
        prr = prr or self.prr
        self._n += 1
        item = RecordItem.objects.create(
            request=prr,
            original_filename=f"r{self._n}.pdf",
            storage_key=f"records/{prr.pk}/r{self._n}",
            content_length=1_000_000,
            content_type="application/pdf",
            sha256=hashlib.sha256(f"{prr.pk}-{self._n}".encode()).hexdigest(),
            review_state=state,
        )
        if suggested:
            AdvisoryRun.objects.create(
                kind=AdvisoryRun.Kind.RECORD_RESPONSIVENESS,
                request=prr,
                record_item=item,
                model_identifier="synthetic-review-model",
                input_sha256=hashlib.sha256(str(item.pk).encode()).hexdigest(),
                state=AdvisoryRun.State.COMPLETE,
                requested_by=self.officer,
                proposal={"suggested_state": "responsive", "confidence": 0.9,
                          "matched_terms": [], "rationale": "Synthetic."},
            )
        return item

    def _throughput(self, reviews=10):
        for _ in range(reviews):
            AuditLog.objects.create(action="record.reviewed", target="synthetic")

    def _ready(self, prr=None):
        self._intent(prr)
        self._record(prr, RecordItem.ReviewState.RESPONSIVE, suggested=False)
        self._record(prr)
        self._record(prr)
        self._throughput()

    def _estimate(self):
        self._ready()
        return estimate_request(self.prr)


class SystemPreparedDraftTests(_LetterFixture):
    def test_the_system_drafts_but_never_approves_or_queues(self):
        estimate = self._estimate()

        draft = services.create_interpretation_estimate_draft(self.prr, estimate=estimate)

        self.assertEqual(draft.kind, Correspondence.Kind.INTERPRETATION_ESTIMATE)
        self.assertEqual(draft.status, Correspondence.Status.DRAFT)
        self.assertTrue(draft.system_prepared)
        self.assertFalse(draft.automated)
        self.assertIsNone(draft.created_by)
        self.assertIn(INTENT, draft.draft_body)
        self.assertIn("The District expects about", draft.draft_body)
        self.assertIn("RCW 42.56.520", draft.draft_body)
        self.assertIn("misunderstood", draft.draft_body)
        completion = dt.date.fromisoformat(estimate.result["completion_date"])
        self.assertIn(_date(completion), draft.draft_body)
        self.assertIn("1 has already been reviewed", draft.draft_body)
        self.assertEqual(draft.draft_metadata["estimate_id"], str(estimate.pk))
        self.assertFalse(CorrespondenceDelivery.objects.filter(correspondence=draft).exists())

    def test_no_other_kind_may_be_system_prepared(self):
        with self.assertRaises(PermissionDenied):
            services._create_draft(
                self.prr,
                kind=Correspondence.Kind.FEE_ESTIMATE,
                rendered=RenderedMessage(subject="Subject", body="Body"),
                metadata={},
                actor=None,
                system_prepared=True,
            )
        with self.assertRaises(PermissionDenied):
            services._create_draft(
                self.prr,
                kind=Correspondence.Kind.INTERPRETATION_ESTIMATE,
                rendered=RenderedMessage(subject="Subject", body="Body"),
                metadata={},
                actor=None,
                automated=True,
                system_prepared=True,
            )

    def test_the_database_refuses_a_system_prepared_letter_of_another_kind(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Correspondence.objects.create(
                request=self.prr,
                direction=Correspondence.Direction.OUTBOUND,
                kind=Correspondence.Kind.GENERAL,
                status=Correspondence.Status.DRAFT,
                draft_subject="Subject",
                draft_body="Body\n",
                system_prepared=True,
            )

    def test_it_needs_an_intent_summary_a_date_and_no_letter_outstanding(self):
        self._record(state=RecordItem.ReviewState.RESPONSIVE, suggested=False)
        no_date = estimate_request(self.prr)  # no throughput, so no date
        self._intent()
        with self.assertRaises(ValidationError):
            services.create_interpretation_estimate_draft(self.prr, estimate=no_date)

        self._throughput()
        dated = estimate_request(self.prr)
        services.create_interpretation_estimate_draft(self.prr, estimate=dated)
        with self.assertRaises(ValidationError):
            services.create_interpretation_estimate_draft(self.prr, estimate=dated)

    def test_without_an_intent_summary_there_is_nothing_to_interpret(self):
        self._record(state=RecordItem.ReviewState.RESPONSIVE, suggested=False)
        self._throughput()
        with self.assertRaises(ValidationError):
            services.create_interpretation_estimate_draft(
                self.prr, estimate=estimate_request(self.prr),
            )


class ApproveAndDeliverTests(_LetterFixture):
    def test_an_officer_may_reword_only_the_interpretation(self):
        draft = services.create_interpretation_estimate_draft(
            self.prr, estimate=self._estimate(),
        )
        edited = services.edit_structured_draft(
            draft,
            actor=self.officer,
            expected_version=draft.version,
            structured_data={"interpretation": "Route 12 bus video, March 3, 3-4 p.m."},
        )
        self.assertIn("Route 12 bus video, March 3, 3-4 p.m.", edited.draft_body)
        for key in ("estimate_id", "record_estimate", "charge_paragraph", "completion_date"):
            self.assertEqual(edited.draft_metadata[key], draft.draft_metadata[key])
        # A valid interpretation does not carry a date change with it.
        with self.assertRaises(ValidationError):
            services.edit_structured_draft(
                edited,
                actor=self.officer,
                expected_version=edited.version,
                structured_data={
                    "interpretation": "Route 12 bus video, March 3, 3-4 p.m.",
                    "completion_date": "2030-01-01",
                },
            )
        edited.refresh_from_db()
        self.assertEqual(edited.draft_metadata["completion_date"], draft.draft_metadata["completion_date"])
        self.assertNotIn("2030", edited.draft_body)

    def test_no_charge_is_stated_only_when_even_the_high_case_bills_nothing(self):
        def fee(expected, high):
            return {"low": {"total": "0.00"}, "expected": {"total": expected},
                    "high": {"total": high}}

        self.assertEqual(
            services._charge_paragraph(fee("0.00", "0.00")),
            "There is no copying charge for these records.",
        )
        self.assertIn("between $0.00 and $0.05", services._charge_paragraph(fee("0.00", "0.05")))

    def test_delivery_records_the_stated_date_as_estimate_evidence(self):
        estimate = self._estimate()
        draft = services.create_interpretation_estimate_draft(self.prr, estimate=estimate)

        sent = self.deliver(draft)

        self.assertEqual(sent.approved_by, self.officer)
        completion = dt.date.fromisoformat(estimate.result["completion_date"])
        self.assertEqual(self.prr.estimated_completion, completion)
        revision = EstimateRevision.objects.get(correspondence=sent)
        self.assertEqual(revision.new_date, completion)
        self.assertEqual(revision.revised_by, self.officer)
        self.assertIn(str(estimate.pk), revision.reason)
        row = AuditLog.objects.get(action="request.estimate_revised")
        self.assertEqual(row.detail["estimate_id"], str(estimate.pk))


@override_settings(PRR_AI_ADVISORY_ENABLED=True, PRR_AI_AUTOMATIC_ESTIMATE_LETTER_ENABLED=True)
class AutomaticDraftingTests(_LetterFixture):
    def test_a_ready_case_gets_one_draft_and_only_one(self):
        self._ready()
        self.assertEqual(prepare_estimate_letters(), 1)
        self.assertEqual(prepare_estimate_letters(), 0)
        draft = Correspondence.objects.get(kind=Correspondence.Kind.INTERPRETATION_ESTIMATE)
        self.assertTrue(draft.system_prepared)
        self.assertEqual(draft.status, Correspondence.Status.DRAFT)

    def test_unclassified_records_or_another_matter_kind_are_not_ready(self):
        self._ready()
        self._record(suggested=False)
        litigation = PublicRecordsRequest.objects.create(
            requester=self.prr.requester,
            description="Synthetic litigation pull.",
            matter_kind=MatterKind.LITIGATION_DISCOVERY,
        )
        self._intent(litigation)
        self._record(litigation)
        self.assertEqual(prepare_estimate_letters(), 0)

    def test_an_undated_estimate_is_kept_and_the_case_waits_for_tomorrow(self):
        self._intent()
        self._record()
        self.assertEqual(prepare_estimate_letters(), 0)
        self.assertEqual(RecordsEstimate.objects.count(), 1)
        self.assertEqual(prepare_estimate_letters(), 0)
        self.assertEqual(RecordsEstimate.objects.count(), 1)

    @override_settings(PRR_AI_AUTOMATIC_ESTIMATE_LETTER_ENABLED=False)
    def test_off_drafts_nothing(self):
        self._ready()
        self.assertEqual(prepare_estimate_letters(), 0)
        self.assertFalse(RecordsEstimate.objects.exists())


class EstimateLetterApiTests(_LetterFixture):
    def test_an_officer_reads_the_waiting_letter_and_a_reviewer_may_not(self):
        from rest_framework.test import APIClient

        from apps.api.identity import apply_staff_roles
        from apps.api.models import StaffRole

        api = APIClient()
        api.force_authenticate(self.officer)
        url = f"/api/staff/requests/{self.prr.number}/estimate-letter/"
        self.assertEqual(api.get(url).json(), {"correspondence": None, "estimate": None})

        estimate = self._estimate()
        draft = services.create_interpretation_estimate_draft(self.prr, estimate=estimate)
        body = api.get(url).json()
        self.assertEqual(body["correspondence"]["id"], str(draft.pk))
        self.assertTrue(body["correspondence"]["system_prepared"])
        self.assertIn(INTENT, body["correspondence"]["body"])
        self.assertEqual(body["estimate"]["id"], str(estimate.pk))

        reviewer = type(self.officer).objects.create_user(
            "letter-reviewer", "letter-reviewer@psd401.net", "pw", is_staff=True,
        )
        apply_staff_roles(reviewer, [StaffRole.REVIEWER])
        api.force_authenticate(reviewer)
        self.assertEqual(api.get(url).status_code, 403)
