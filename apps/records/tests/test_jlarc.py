"""JLARC annual metrics are reproducible and expose incomplete source data."""

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.utils import timezone

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.records.jlarc import (
    build_jlarc_report,
    classify_request_for_jlarc,
    record_jlarc_annual_supplement,
)
from apps.records.models import (
    ExemptionCitation,
    FeeAssessment,
    FeePayment,
    FeeSchedule,
    Installment,
    JlarcRequestMetrics,
    PublicRecordsRequest,
    RecordItem,
    Redaction,
    Requester,
    RequestStatus,
    StatusEvent,
    TimeEntry,
)

PACIFIC = ZoneInfo("America/Los_Angeles")


def at(year, month, day, hour=10):
    return datetime(year, month, day, hour, tzinfo=PACIFIC)


class JlarcReportingTests(TestCase):
    def setUp(self):
        self.officer = get_user_model().objects.create_user(
            "officer", "officer@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.reviewer = get_user_model().objects.create_user(
            "reviewer", "reviewer@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.reviewer, {StaffRole.REVIEWER})
        self.requester = Requester.objects.create(
            name="Voluntary Name",
            email="requester@example.org",
        )

    def request(self, *, received, closed=None, status=RequestStatus.RECEIVED, description="Records."):
        return PublicRecordsRequest.objects.create(
            requester=self.requester,
            description=description,
            received_at=received,
            closed_at=closed,
            status=status,
        )

    def test_request_classification_is_optional_append_only_human_evidence(self):
        prr = self.request(received=at(2025, 2, 1))
        with self.assertRaises(PermissionDenied):
            classify_request_for_jlarc(
                prr.pk,
                actor=self.reviewer,
                requester_type=JlarcRequestMetrics.RequesterType.INDIVIDUAL,
                fulfillment_format=JlarcRequestMetrics.FulfillmentFormat.ELECTRONIC,
                scanned_physical=False,
                records_produced_description="Responsive board minutes.",
                records_withheld_description="None.",
                reason="Unauthorized classification attempt.",
            )

        first = classify_request_for_jlarc(
            prr.pk,
            actor=self.officer,
            requester_type=JlarcRequestMetrics.RequesterType.INDIVIDUAL,
            fulfillment_format=JlarcRequestMetrics.FulfillmentFormat.ELECTRONIC,
            scanned_physical=False,
            records_produced_description="Responsive board minutes.",
            records_withheld_description="None.",
            reason="Classification based only on information volunteered by the requester.",
        )
        revised = classify_request_for_jlarc(
            prr.pk,
            actor=self.officer,
            requester_type=JlarcRequestMetrics.RequesterType.ORGANIZATION,
            fulfillment_format=JlarcRequestMetrics.FulfillmentFormat.BOTH,
            scanned_physical=True,
            records_produced_description="Electronic files and paper inspection copies.",
            records_withheld_description="None.",
            reason="Requester voluntarily identified the represented organization in later correspondence.",
        )

        self.assertEqual(revised.revision, 2)
        self.assertEqual(revised.supersedes, first)
        self.assertEqual(JlarcRequestMetrics.objects.count(), 2)
        first.reason = "rewritten"
        with self.assertRaisesMessage(ValidationError, "append-only"):
            first.save()

    def test_supplement_requires_officer_and_retains_methodology_revisions(self):
        with self.assertRaises(PermissionDenied):
            record_jlarc_annual_supplement(
                2025,
                actor=self.reviewer,
                methodology_notes="Finance and risk management year-end totals.",
            )

        first = record_jlarc_annual_supplement(
            2025,
            actor=self.officer,
            unallocated_staff_minutes=120,
            staff_compensation_cost=Decimal("100000.00"),
            legal_review_cost=Decimal("5000.00"),
            supplies_services_cost=Decimal("250.00"),
            claims_filed=2,
            claims_by_type={"PRA timeliness": 2},
            claims_by_exemption={"RCW 42.56.250": 1},
            litigation_agency_cost=Decimal("1000.00"),
            litigation_penalties=Decimal("100.00"),
            records_management_staff_cost=Decimal("12000.00"),
            records_management_systems_cost=Decimal("8000.00"),
            other_expenses_recovered=Decimal("12.00"),
            customized_service_expenses_recovered_external=Decimal("5.00"),
            methodology_notes="Finance, HR, risk management, and records office year-end ledgers.",
        )
        revised = record_jlarc_annual_supplement(
            2025,
            actor=self.officer,
            staff_compensation_cost=Decimal("100500.00"),
            methodology_notes="Corrected finance ledger after fiscal close; all omitted fields are zero.",
        )
        self.assertEqual(revised.revision, 2)
        self.assertEqual(revised.supersedes, first)
        self.assertEqual(first.staff_compensation_cost, Decimal("100000.00"))

    def test_builds_baseline_all_fifteen_metrics_log_and_quality_warnings(self):
        _open_at_start = self.request(
            received=at(2024, 12, 20),
            closed=at(2025, 1, 20),
            status=RequestStatus.FULFILLED,
            description="Prior-year carryover.",
        )
        fast = self.request(
            received=at(2025, 1, 2),
            closed=at(2025, 1, 6),
            status=RequestStatus.FULFILLED,
            description="Fast electronic records.",
        )
        denied = self.request(
            received=at(2025, 2, 3),
            closed=at(2025, 2, 20),
            status=RequestStatus.DENIED,
            description="Fully exempt records.",
        )
        partial = self.request(
            received=at(2025, 3, 3),
            closed=at(2025, 3, 20),
            status=RequestStatus.FULFILLED,
            description="Partially redacted records.",
        )
        _abandoned = self.request(
            received=at(2025, 4, 1),
            closed=at(2025, 4, 5),
            status=RequestStatus.CLOSED_ABANDONED,
            description="Unclaimed installment.",
        )
        still_open = self.request(
            received=at(2025, 5, 1),
            status=RequestStatus.GATHERING,
            description="Open request.",
        )

        classify_request_for_jlarc(
            fast.pk,
            actor=self.officer,
            requester_type=JlarcRequestMetrics.RequesterType.INDIVIDUAL,
            fulfillment_format=JlarcRequestMetrics.FulfillmentFormat.ELECTRONIC,
            scanned_physical=True,
            records_produced_description="Three scanned board packets.",
            records_withheld_description="None.",
            reason="Final-disposition classification from the production record.",
        )
        classify_request_for_jlarc(
            denied.pk,
            actor=self.officer,
            requester_type=JlarcRequestMetrics.RequesterType.UNKNOWN,
            fulfillment_format=JlarcRequestMetrics.FulfillmentFormat.NO_RECORDS_OR_OTHER,
            scanned_physical=False,
            records_produced_description="None.",
            records_withheld_description="All responsive records withheld.",
            reason="Final-disposition classification from approved denial evidence.",
        )
        classify_request_for_jlarc(
            partial.pk,
            actor=self.officer,
            requester_type=JlarcRequestMetrics.RequesterType.MEDIA,
            fulfillment_format=JlarcRequestMetrics.FulfillmentFormat.BOTH,
            scanned_physical=False,
            records_produced_description="Redacted PDF plus paper inspection.",
            records_withheld_description="Student identifiers redacted.",
            reason="Final-disposition classification from the production record.",
        )
        exemption = ExemptionCitation.objects.create(
            citation="RCW 42.56.230(1)",
            short_name="Personal information",
        )
        denied_item = RecordItem.objects.create(
            request=denied,
            storage_key="records/denied.pdf",
            sha256="a" * 64,
            review_state=RecordItem.ReviewState.WITHHELD,
            withholding_exemption=exemption,
        )
        partial_item = RecordItem.objects.create(
            request=partial,
            storage_key="records/partial.pdf",
            sha256="b" * 64,
            review_state=RecordItem.ReviewState.RESPONSIVE,
        )
        Redaction.objects.create(
            record_item=partial_item,
            page_number=1,
            x0=0.1,
            y0=0.1,
            x1=0.2,
            y1=0.2,
            exemption=exemption,
            created_by=self.officer,
        )
        StatusEvent.objects.create(
            request=still_open,
            from_status=RequestStatus.RECEIVED,
            to_status=RequestStatus.CLARIFICATION,
            occurred_at=at(2025, 5, 2),
            actor=self.officer,
        )
        TimeEntry.objects.create(
            request=fast,
            staff=self.officer,
            date=date(2025, 1, 5),
            minutes=60,
            activity="Search and review",
        )

        schedule = FeeSchedule.objects.create(
            name="Adopted",
            basis=FeeSchedule.Basis.STATUTORY_DEFAULT,
            effective_on=date(2025, 1, 1),
            counsel_approval_reference="Legal memo 1",
            adoption_reference="Board action 1",
            activated_by=self.officer,
        )
        installment = Installment.objects.create(
            request=fast,
            sequence=1,
            fee_amount=Decimal("1.00"),
            fee_paid_at=timezone.make_aware(datetime(2025, 1, 7, 10)),
        )
        assessment = FeeAssessment.objects.create(
            installment=installment,
            schedule=schedule,
            revision=1,
            itemized_amount=Decimal("1.00"),
            customized_service_cost=Decimal("0.25"),
            amount=Decimal("1.00"),
            deposit_amount=Decimal("0.00"),
            reason="Adopted electronic delivery charges.",
            created_by=self.officer,
        )
        FeePayment.objects.create(
            installment=installment,
            assessment=assessment,
            amount=Decimal("1.00"),
            method=FeePayment.Method.CHECK,
            reference="District receipt 1",
            received_on=date(2025, 1, 7),
            recorded_by=self.officer,
        )
        record_jlarc_annual_supplement(
            2025,
            actor=self.officer,
            unallocated_staff_minutes=30,
            staff_compensation_cost=Decimal("100000.00"),
            legal_review_cost=Decimal("5000.00"),
            supplies_services_cost=Decimal("250.00"),
            claims_filed=2,
            claims_by_type={"PRA timeliness": 2},
            claims_by_exemption={"RCW 42.56.250": 1},
            litigation_agency_cost=Decimal("1000.00"),
            litigation_penalties=Decimal("100.00"),
            records_management_staff_cost=Decimal("12000.00"),
            records_management_systems_cost=Decimal("8000.00"),
            other_expenses_recovered=Decimal("12.00"),
            customized_service_expenses_recovered_external=Decimal("5.00"),
            methodology_notes="Finance, HR, risk management, and records office year-end ledgers.",
        )

        report = build_jlarc_report(2025)

        self.assertEqual(report["baseline"], {
            "open_at_start": 1,
            "open_at_start_closed_during_period": 1,
            "received_during_period": 5,
            "received_during_period_closed_during_period": 4,
            "total_closed_during_period": 5,
        })
        self.assertEqual(set(report["metrics"]), {str(i) for i in range(1, 16)})
        self.assertEqual(report["metrics"]["1"]["requests_closed_within_five_business_days"], 1)
        self.assertEqual(report["metrics"]["4"]["requests_with_clarification"], 1)
        self.assertEqual(report["metrics"]["5"]["denied_in_full"], 1)
        self.assertEqual(report["metrics"]["5"]["partially_denied_or_redacted"], 1)
        self.assertEqual(report["metrics"]["6"]["abandoned"], 1)
        self.assertEqual(report["metrics"]["7"]["by_requester_type"]["individual"], 1)
        self.assertEqual(report["metrics"]["7"]["by_requester_type"]["unknown"], 3)
        self.assertEqual(report["metrics"]["8"]["electronic"], 1)
        self.assertEqual(report["metrics"]["8"]["both"], 1)
        self.assertEqual(report["metrics"]["9"]["requests_involving_physical_scanning"], 1)
        self.assertEqual(report["metrics"]["10"]["total_staff_minutes"], 90)
        self.assertEqual(report["metrics"]["11"]["total_fulfillment_cost"], "105250.00")
        self.assertEqual(report["metrics"]["12"]["claims_filed"], 2)
        self.assertEqual(report["metrics"]["13"]["total_litigation_cost"], "1100.00")
        self.assertEqual(report["metrics"]["14"]["total_records_management_cost"], "20000.00")
        self.assertEqual(report["metrics"]["15"]["total_expenses_recovered"], "13.00")
        self.assertEqual(report["metrics"]["15"]["customized_service_charges_recovered"], "5.25")
        self.assertEqual(len(report["request_log"]), 6)
        self.assertIn("requester@example.org", report["request_log"][0]["requester_identity"])
        self.assertRegex(report["data_sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(any("unclassified" in warning.lower() for warning in report["quality_warnings"]))
        self.assertTrue(AuditLog.objects.filter(action="jlarc.supplement_recorded").exists())
        self.assertIsNotNone(denied_item.pk)

    def test_report_without_annual_supplement_fails_closed_for_cost_metrics(self):
        with self.assertRaisesMessage(ValidationError, "annual supplement"):
            build_jlarc_report(2025)

    def test_no_records_closure_is_not_counted_as_denial_or_unclassified(self):
        self.request(
            received=at(2025, 6, 2),
            closed=at(2025, 6, 10),
            status=RequestStatus.CLOSED_NO_RECORDS,
            description="Records not located after a documented search.",
        )
        record_jlarc_annual_supplement(
            2025,
            actor=self.officer,
            methodology_notes=(
                "Year-end records-office ledger; omitted financial fields are zero."
            ),
        )

        report = build_jlarc_report(2025)

        self.assertEqual(report["metrics"]["5"]["denied_in_full"], 0)
        self.assertEqual(report["metrics"]["8"]["no_records_or_other"], 1)
        self.assertEqual(report["metrics"]["8"]["unclassified"], 0)
