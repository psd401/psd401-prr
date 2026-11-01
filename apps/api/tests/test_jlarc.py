"""Staff JLARC reporting APIs preserve human attribution and transfer safety."""

from datetime import datetime
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.records.models import (
    JlarcAnnualSupplement,
    JlarcRequestMetrics,
    PublicRecordsRequest,
    Requester,
    RequestStatus,
)


class JlarcApiTests(TestCase):
    def setUp(self):
        self.officer = get_user_model().objects.create_user(
            "officer", "officer@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.client.force_login(self.officer)
        requester = Requester.objects.create(email="requester@example.org")
        zone = ZoneInfo("America/Los_Angeles")
        self.prr = PublicRecordsRequest.objects.create(
            requester=requester,
            description="Board records.",
            received_at=datetime(2025, 1, 2, 10, tzinfo=zone),
            closed_at=datetime(2025, 1, 6, 10, tzinfo=zone),
            status=RequestStatus.FULFILLED,
        )

    def supplement_payload(self):
        return {
            "unallocated_staff_minutes": 30,
            "staff_compensation_cost": "100000.00",
            "legal_review_cost": "5000.00",
            "supplies_services_cost": "250.00",
            "claims_filed": 0,
            "claims_by_type": {},
            "claims_by_exemption": {},
            "litigation_agency_cost": "0.00",
            "litigation_settlements": "0.00",
            "litigation_penalties": "0.00",
            "litigation_requester_attorney_fees": "0.00",
            "litigation_requester_costs": "0.00",
            "records_management_staff_cost": "12000.00",
            "records_management_systems_cost": "8000.00",
            "records_management_other_cost": "0.00",
            "other_expenses_recovered": "0.00",
            "customized_service_expenses_recovered_external": "0.00",
            "methodology_notes": (
                "Finance, HR, risk management, and records office year-end ledgers."
            ),
        }

    def test_officer_records_supplement_classifies_request_and_reads_report(self):
        supplement = self.client.post(
            "/api/staff/jlarc-reports/2025/supplement/",
            self.supplement_payload(),
            content_type="application/json",
        )
        self.assertEqual(supplement.status_code, 201, supplement.content)
        self.assertEqual(supplement.json()["revision"], 1)

        classification = self.client.post(
            f"/api/staff/requests/{self.prr.number}/jlarc-classification/",
            {
                "requester_type": "unknown",
                "fulfillment_format": "electronic",
                "scanned_physical": False,
                "records_produced_description": "Responsive board minutes.",
                "records_withheld_description": "None.",
                "reason": "Final-disposition classification from the production record.",
            },
            content_type="application/json",
        )
        self.assertEqual(classification.status_code, 201, classification.content)
        self.assertEqual(classification.json()["revision"], 1)

        report = self.client.get("/api/staff/jlarc-reports/2025/")
        self.assertEqual(report.status_code, 200, report.content)
        self.assertEqual(report.json()["baseline"]["received_during_period"], 1)
        self.assertEqual(len(report.json()["metrics"]), 15)
        self.assertRegex(report.json()["data_sha256"], r"^[0-9a-f]{64}$")

    def test_csv_is_no_store_human_transfer_worksheet(self):
        JlarcAnnualSupplement.objects.create(
            reporting_year=2025,
            revision=1,
            methodology_notes="Finance and risk year-end ledgers were reconciled.",
            created_by=self.officer,
        )
        response = self.client.get("/api/staff/jlarc-reports/2025/export-csv/")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertIn("attachment", response["Content-Disposition"])
        text = response.content.decode("utf-8")
        self.assertIn("metric,field,value", text)
        self.assertIn("baseline,received_during_period,1", text)
        self.assertIn("15,total_expenses_recovered,0.00", text)

    def test_report_without_supplement_returns_validation_error(self):
        response = self.client.get("/api/staff/jlarc-reports/2025/")
        self.assertEqual(response.status_code, 400)
        self.assertIn("annual supplement", response.json()[0].lower())

    def test_reviewer_can_read_but_cannot_create_reporting_evidence(self):
        JlarcAnnualSupplement.objects.create(
            reporting_year=2025,
            revision=1,
            methodology_notes="Finance and risk year-end ledgers were reconciled.",
            created_by=self.officer,
        )
        reviewer = get_user_model().objects.create_user(
            "reviewer", "reviewer@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(reviewer, {StaffRole.REVIEWER})
        self.client.force_login(reviewer)

        self.assertEqual(self.client.get("/api/staff/jlarc-reports/2025/").status_code, 200)
        denied = self.client.post(
            "/api/staff/jlarc-reports/2025/supplement/",
            self.supplement_payload(),
            content_type="application/json",
        )
        self.assertEqual(denied.status_code, 403)
        classify = self.client.post(
            f"/api/staff/requests/{self.prr.number}/jlarc-classification/",
            {},
            content_type="application/json",
        )
        self.assertEqual(classify.status_code, 403)
        self.assertFalse(JlarcRequestMetrics.objects.exists())

    def test_anonymous_report_is_forbidden(self):
        self.client.logout()
        self.assertEqual(
            self.client.get("/api/staff/jlarc-reports/2025/").status_code,
            403,
        )
