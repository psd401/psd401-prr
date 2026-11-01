# ABOUTME: Staff API tests for the requesters, record-items, installments,
# ABOUTME: and time-entries resources (apps/api/resources/records_misc.py).
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.records.installments import InstallmentNoticeUnsent
from apps.records.models import (
    ExemptionCitation,
    FeeAssessment,
    FeePayment,
    FeeSchedule,
    Installment,
    PublicRecordsRequest,
    RecordItem,
    Redaction,
    Requester,
    TimeEntry,
)


def _login_staff(client, username="officer"):
    user = get_user_model().objects.create_user(
        username, f"{username}@psd401.net", "pw", is_staff=True)
    client.login(username=username, password="pw")
    return user


def _rows(response):
    payload = response.json()
    return payload["results"] if isinstance(payload, dict) else payload


def _make_request(description="board meeting minutes", email="req@example.com"):
    requester = Requester.objects.create(name="Req Person", email=email)
    return PublicRecordsRequest.objects.create(requester=requester, description=description)


class StaffGateTests(TestCase):
    """Anonymous users are refused on every prefix in this module."""

    def test_requesters_refuses_anonymous(self):
        r = self.client.get("/api/staff/requesters/")
        self.assertEqual(r.status_code, 403)

    def test_record_items_refuses_anonymous(self):
        r = self.client.get("/api/staff/record-items/")
        self.assertEqual(r.status_code, 403)

    def test_installments_refuses_anonymous(self):
        r = self.client.get("/api/staff/installments/")
        self.assertEqual(r.status_code, 403)

    def test_time_entries_refuses_anonymous(self):
        r = self.client.get("/api/staff/time-entries/")
        self.assertEqual(r.status_code, 403)

    def test_fee_schedules_refuses_anonymous(self):
        r = self.client.get("/api/staff/fee-schedules/")
        self.assertEqual(r.status_code, 403)


class RequesterApiTests(TestCase):
    def setUp(self):
        _login_staff(self.client)
        self.req = Requester.objects.create(
            name="Original Name", email="original@example.com",
            phone="253-555-0100", mailing_address="123 Main St",
            city="Gig Harbor", state="WA", postal_code="98332",
            company="Original Company")

    def test_list(self):
        r = self.client.get("/api/staff/requesters/")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(any(x["email"] == "original@example.com" for x in _rows(r)))

    def test_retrieve(self):
        r = self.client.get(f"/api/staff/requesters/{self.req.id}/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["name"], "Original Name")

    def test_partial_update_editable_fields(self):
        r = self.client.patch(f"/api/staff/requesters/{self.req.id}/",
                              {"name": "Updated Name", "phone": "253-555-0199",
                               "mailing_address": "456 Oak Ave", "city": "Tacoma",
                               "state": "WA", "postal_code": "98402",
                               "company": "Updated Company"},
                              content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.req.refresh_from_db()
        self.assertEqual(self.req.name, "Updated Name")
        self.assertEqual(self.req.phone, "253-555-0199")
        self.assertEqual(self.req.mailing_address, "456 Oak Ave")
        self.assertEqual(self.req.city, "Tacoma")
        self.assertEqual(self.req.state, "WA")
        self.assertEqual(self.req.postal_code, "98402")
        self.assertEqual(self.req.company, "Updated Company")

    def test_email_is_read_only(self):
        r = self.client.patch(f"/api/staff/requesters/{self.req.id}/",
                              {"email": "changed@example.com"},
                              content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.req.refresh_from_db()
        self.assertEqual(self.req.email, "original@example.com")

    def test_search_by_name(self):
        Requester.objects.create(name="Alice Smith", email="alice@example.com")
        Requester.objects.create(name="Bob Jones", email="bob@example.com")
        r = self.client.get("/api/staff/requesters/?search=Alice")
        rows = _rows(r)
        self.assertTrue(any(x["name"] == "Alice Smith" for x in rows))
        self.assertFalse(any(x["name"] == "Bob Jones" for x in rows))

    def test_search_by_email(self):
        Requester.objects.create(name="Carol X", email="carol@psd401.net")
        r = self.client.get("/api/staff/requesters/?search=carol@psd401.net")
        rows = _rows(r)
        self.assertTrue(any(x["email"] == "carol@psd401.net" for x in rows))

    def test_no_create(self):
        r = self.client.post("/api/staff/requesters/",
                             {"name": "New", "email": "new@example.com"},
                             content_type="application/json")
        self.assertEqual(r.status_code, 405)

    def test_no_destroy(self):
        r = self.client.delete(f"/api/staff/requesters/{self.req.id}/")
        self.assertEqual(r.status_code, 405)


class RecordItemApiTests(TestCase):
    def setUp(self):
        self.staff = _login_staff(self.client)
        self.prr = _make_request()
        self.exemption = ExemptionCitation.objects.create(
            citation="RCW 42.56.230(1)", short_name="Personal info – students")
        self.item = RecordItem.objects.create(
            request=self.prr,
            source_system="gmail",
            custodian="Records Officer",
            original_filename="email1.pdf",
            storage_key="records/email1.pdf",
            sha256="a" * 64,
            review_state=RecordItem.ReviewState.RESPONSIVE,
            reviewed_by=self.staff,
        )
        self.item.withholding_exemptions.add(self.exemption)
        Redaction.objects.create(
            record_item=self.item, page_number=1,
            x0=0.1, y0=0.1, x1=0.2, y1=0.2,
            exemption=self.exemption,
        )

    def test_list_fields(self):
        r = self.client.get("/api/staff/record-items/")
        self.assertEqual(r.status_code, 200)
        row = next(x for x in _rows(r) if x["id"] == str(self.item.id))
        self.assertEqual(row["request_number"], self.prr.number)
        self.assertEqual(row["source_system"], "gmail")
        self.assertEqual(row["custodian"], "Records Officer")
        self.assertEqual(row["original_filename"], "email1.pdf")
        self.assertEqual(row["sha256"], "a" * 64)
        self.assertEqual(row["review_state"], RecordItem.ReviewState.RESPONSIVE)
        self.assertEqual(row["reviewed_by_username"], "officer")
        self.assertEqual(row["redaction_count"], 1)
        self.assertEqual(row["withholding_exemption_citations"], ["RCW 42.56.230(1)"])
        self.assertIn("created_at", row)

    def test_list_reports_the_review_path_and_size(self):
        row = next(x for x in _rows(self.client.get("/api/staff/record-items/"))
                   if x["id"] == str(self.item.id))
        self.assertEqual(row["handling"], "preview")
        self.assertEqual(row["handling_label"], "Ready to review")
        self.assertTrue(row["handling_detail"])
        self.assertIn("content_type", row)
        self.assertIn("content_length", row)

    def test_search_matches_inside_an_expanded_archive_path(self):
        RecordItem.objects.create(
            request=self.prr, source_system="nextrequest_legacy",
            original_filename="2018 Resources/policy.pdf",
            storage_key="records/x.pdf", sha256="b" * 64,
            content_type="application/pdf",
        )
        rows = _rows(self.client.get(
            "/api/staff/record-items/?search=2018 Resources"))
        self.assertEqual(
            [r["original_filename"] for r in rows],
            ["2018 Resources/policy.pdf"],
        )

    def test_ordering_is_whitelisted_and_ignores_unknown_keys(self):
        RecordItem.objects.create(
            request=self.prr, source_system="s", original_filename="aaa.pdf",
            storage_key="records/a.pdf", sha256="c" * 64,
            content_type="application/pdf",
        )
        rows = _rows(self.client.get("/api/staff/record-items/?ordering=filename"))
        self.assertEqual(rows[0]["original_filename"], "aaa.pdf")
        # An unknown key falls back to the default rather than erroring or
        # letting a caller order by an arbitrary column.
        fallback = _rows(self.client.get(
            "/api/staff/record-items/?ordering=storage_key"))
        self.assertEqual(len(fallback), 2)

    def test_handling_filter_selects_by_review_path(self):
        RecordItem.objects.create(
            request=self.prr, source_system="s",
            original_filename="bus-camera.mp4",
            storage_key="records/v.mp4", sha256="d" * 64,
            content_type="video/mp4",
        )
        rows = _rows(self.client.get("/api/staff/record-items/?handling=media"))
        self.assertEqual([r["original_filename"] for r in rows], ["bus-camera.mp4"])
        self.assertEqual(rows[0]["handling_label"], "Audio or video")

    def test_handling_filter_does_not_break_retrieve(self):
        # The filter runs in Python and returns a list, which cannot be
        # filtered further by primary key -- so it must apply to list alone.
        r = self.client.get(
            f"/api/staff/record-items/{self.item.id}/?handling=media")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["id"], str(self.item.id))

    def test_retrieve(self):
        r = self.client.get(f"/api/staff/record-items/{self.item.id}/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["custodian"], "Records Officer")

    def test_primary_withholding_basis_is_included_in_legal_summary(self):
        withheld = RecordItem.objects.create(
            request=self.prr,
            storage_key="records/withheld.pdf",
            sha256="e" * 64,
            review_state=RecordItem.ReviewState.WITHHELD,
            withholding_exemption=self.exemption,
        )

        response = self.client.get(
            f"/api/staff/record-items/{withheld.id}/"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["withholding_exemption_citations"],
            ["RCW 42.56.230(1)"],
        )

    def test_filter_by_request_number(self):
        other_prr = _make_request(email="other@example.com")
        RecordItem.objects.create(
            request=other_prr, storage_key="records/other.pdf", sha256="b" * 64)
        r = self.client.get(f"/api/staff/record-items/?request={self.prr.number}")
        rows = _rows(r)
        self.assertTrue(all(x["request_number"] == self.prr.number for x in rows))
        self.assertEqual(len(rows), 1)

    def test_filter_by_review_state(self):
        RecordItem.objects.create(
            request=self.prr, storage_key="records/other2.pdf", sha256="c" * 64,
            review_state=RecordItem.ReviewState.UNREVIEWED)
        r = self.client.get("/api/staff/record-items/?review_state=responsive")
        rows = _rows(r)
        self.assertTrue(all(x["review_state"] == "responsive" for x in rows))
        self.assertEqual(len(rows), 1)

    def test_no_create(self):
        r = self.client.post("/api/staff/record-items/", {
            "request": str(self.prr.id), "storage_key": "x", "sha256": "d" * 64,
        }, content_type="application/json")
        self.assertEqual(r.status_code, 405)

    def test_no_update(self):
        r = self.client.patch(f"/api/staff/record-items/{self.item.id}/",
                              {"custodian": "Someone Else"},
                              content_type="application/json")
        self.assertEqual(r.status_code, 405)

    def test_no_destroy(self):
        r = self.client.delete(f"/api/staff/record-items/{self.item.id}/")
        self.assertEqual(r.status_code, 405)

    def test_retrieve_missing_is_404(self):
        r = self.client.get(
            "/api/staff/record-items/00000000-0000-0000-0000-000000000000/")
        self.assertEqual(r.status_code, 404)


class InstallmentApiTests(TestCase):
    def setUp(self):
        _login_staff(self.client)
        self.prr = _make_request()
        self.installment = Installment.objects.create(
            request=self.prr, sequence=1, fee_amount="12.50",
            download_expires_at="2026-08-01T00:00:00Z",
        )

    def test_list_fields(self):
        r = self.client.get("/api/staff/installments/")
        self.assertEqual(r.status_code, 200)
        row = next(x for x in _rows(r) if x["id"] == self.installment.id)
        self.assertEqual(row["request_number"], self.prr.number)
        self.assertEqual(row["sequence"], 1)
        self.assertEqual(row["fee_amount"], "12.50")
        self.assertIn("download_expires_at", row)
        self.assertIn("fee_paid_at", row)
        self.assertIn("released_at", row)
        self.assertIn("item_count", row)
        self.assertIn("package_prepared_at", row)
        self.assertIn("package_prepared_by_username", row)
        self.assertIn("package_size", row)
        self.assertIn("exemption_log_prepared_at", row)
        self.assertIn("exemption_log_prepared_by_username", row)
        self.assertIn("exemption_log_ready", row)
        self.assertIn("has_exemptions", row)
        self.assertIn("fee_assessment", row)
        self.assertIn("fee_paid_amount", row)
        self.assertIn("fee_balance", row)
        self.assertNotIn("package_storage_key", row)
        self.assertNotIn("package_sha256", row)
        self.assertNotIn("exemption_log_storage_key", row)
        self.assertNotIn("exemption_log_sha256", row)
        self.assertNotIn("exemption_log_manifest_sha256", row)
        self.assertNotIn("download_token", row)

    def test_retrieve(self):
        r = self.client.get(f"/api/staff/installments/{self.installment.id}/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["sequence"], 1)

    @patch("apps.api.resources.records_misc.generate_exemption_log")
    def test_generate_exemption_log_action_uses_current_human_actor(self, generate):
        self.installment.exemption_log_storage_key = "exemption-logs/generated.pdf"
        self.installment.exemption_log_sha256 = "a" * 64
        self.installment.exemption_log_size = 321
        self.installment.exemption_log_manifest_sha256 = "b" * 64
        self.installment.exemption_log_prepared_at = "2026-08-03T12:00:00Z"
        generate.return_value = self.installment

        response = self.client.post(
            f"/api/staff/installments/{self.installment.pk}/generate-exemption-log/",
            {},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["exemption_log_ready"])
        generate.assert_called_once()
        args, kwargs = generate.call_args
        self.assertEqual(args, (self.installment.pk,))
        self.assertEqual(kwargs["actor"].username, "officer")

    def test_create_rejects_arbitrary_model_fields(self):
        """Creation takes a request number and record ids, nothing else."""
        r = self.client.post("/api/staff/installments/",
                             {"request": str(self.prr.id), "sequence": 2},
                             content_type="application/json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("request_number", r.json())
        self.assertIn("record_item_ids", r.json())
        self.assertEqual(
            Installment.objects.filter(request=self.prr).count(),
            1,
        )

    def test_no_update(self):
        r = self.client.patch(f"/api/staff/installments/{self.installment.id}/",
                              {"sequence": 9}, content_type="application/json")
        self.assertEqual(r.status_code, 405)

    def test_no_destroy(self):
        r = self.client.delete(f"/api/staff/installments/{self.installment.id}/")
        self.assertEqual(r.status_code, 405)

    def _activate_fee_schedule(self):
        response = self.client.post(
            "/api/staff/fee-schedules/",
            {
                "name": "PSD adopted PRA copy schedule",
                "basis": "statutory_default",
                "effective_on": "2026-08-01",
                "counsel_approval_reference": "Legal memo PRA-fees-2026-07-28",
                "adoption_reference": "Board policy 4040 resolution 2026-08-01",
                "photocopy_page_rate": "0.15",
                "scanned_page_rate": "0.10",
                "electronic_file_bundle_rate": "0.05",
                "electronic_gb_rate": "0.10",
                "flat_fee_amount": "2.00",
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()

    def test_officer_activates_immutable_counsel_referenced_schedule(self):
        row = self._activate_fee_schedule()

        self.assertEqual(row["basis"], "statutory_default")
        self.assertEqual(row["counsel_approval_reference"], "Legal memo PRA-fees-2026-07-28")
        self.assertTrue(row["is_current"])
        self.assertEqual(self.client.patch(
            f"/api/staff/fee-schedules/{row['id']}/",
            {"name": "rewrite"},
            content_type="application/json",
        ).status_code, 405)

    def test_reviewer_cannot_activate_fee_schedule(self):
        reviewer = get_user_model().objects.create_user(
            "fee-reviewer", "fee-reviewer@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(reviewer, {StaffRole.REVIEWER})
        self.client.force_login(reviewer)

        response = self.client.post(
            "/api/staff/fee-schedules/",
            {
                "name": "Unauthorized schedule",
                "basis": "statutory_default",
                "effective_on": "2026-08-01",
                "counsel_approval_reference": "Legal memo 2026-1",
                "adoption_reference": "Board policy 4040",
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(FeeSchedule.objects.exists())

    def test_assess_fee_and_record_payment_actions_use_domain_evidence(self):
        schedule = self._activate_fee_schedule()
        assessment_response = self.client.post(
            f"/api/staff/installments/{self.installment.pk}/assess-fee/",
            {
                "schedule_id": schedule["id"],
                "electronic_files": 8,
                "deposit_percent": "10.00",
                "reason": "Electronic delivery under the adopted district fee schedule.",
            },
            content_type="application/json",
        )
        self.assertEqual(assessment_response.status_code, 200, assessment_response.content)
        assessed = assessment_response.json()
        self.assertEqual(assessed["fee_amount"], "0.10")
        self.assertEqual(assessed["fee_assessment"]["revision"], 1)
        self.assertEqual(assessed["fee_assessment"]["deposit_amount"], "0.01")
        self.assertEqual(assessed["fee_paid_amount"], "0.00")
        self.assertEqual(assessed["fee_balance"], "0.10")

        paid_response = self.client.post(
            f"/api/staff/installments/{self.installment.pk}/record-payment/",
            {
                "amount": "0.10",
                "method": "check",
                "reference": "District receipt 1042",
                "received_on": timezone.localdate().isoformat(),
            },
            content_type="application/json",
        )
        self.assertEqual(paid_response.status_code, 200, paid_response.content)
        paid = paid_response.json()
        self.assertIsNotNone(paid["fee_paid_at"])
        self.assertEqual(paid["fee_paid_amount"], "0.10")
        self.assertEqual(paid["fee_balance"], "0.00")
        self.assertEqual(FeeAssessment.objects.count(), 1)
        self.assertEqual(FeePayment.objects.count(), 1)

    def test_waiver_floor_is_adopted_with_counsel_and_only_prompts(self):
        floor_response = self.client.post(
            "/api/staff/fee-waiver-floors/",
            {
                "amount": "1.00",
                "counsel_reference": "Legal memo PRA-waiver-floor-2026-09-21",
                "effective_on": "2026-08-01",
            },
            content_type="application/json",
        )
        self.assertEqual(floor_response.status_code, 201, floor_response.content)
        self.assertTrue(floor_response.json()["is_current"])

        schedule = self._activate_fee_schedule()
        assessed = self.client.post(
            f"/api/staff/installments/{self.installment.pk}/assess-fee/",
            {
                "schedule_id": schedule["id"],
                "scanned_pages": 3,
                "reason": "Three scanned pages under the adopted schedule.",
            },
            content_type="application/json",
        ).json()

        # The workspace is told to prompt. The fee is still assessed and still
        # owed: nothing waived it.
        self.assertEqual(assessed["fee_amount"], "0.30")
        self.assertTrue(assessed["fee_assessment"]["below_waiver_floor"])
        self.assertEqual(
            assessed["fee_assessment"]["waiver_floor_amount"],
            "1.00",
        )
        self.assertEqual(assessed["fee_assessment"]["waiver_amount"], "0.00")

    def test_the_adopted_floor_is_read_once_however_many_installments_list(self):
        """The effective floor is a property of today, not of a row."""
        schedule = self._activate_fee_schedule()
        self.client.post(
            "/api/staff/fee-waiver-floors/",
            {
                "amount": "1.00",
                "counsel_reference": "Legal memo PRA-waiver-floor-2026-09-21",
                "effective_on": "2026-08-01",
            },
            content_type="application/json",
        )
        for installment in (
            self.installment,
            Installment.objects.create(request=self.prr, sequence=2),
        ):
            self.client.post(
                f"/api/staff/installments/{installment.pk}/assess-fee/",
                {
                    "schedule_id": schedule["id"],
                    "scanned_pages": 3,
                    "reason": "Three scanned pages under the adopted schedule.",
                },
                content_type="application/json",
            )

        with CaptureQueriesContext(connection) as captured:
            listed = self.client.get("/api/staff/installments/")
        self.assertEqual(listed.status_code, 200)
        floor_queries = [
            query for query in captured.captured_queries
            if "records_feewaiverfloor" in query["sql"]
        ]
        self.assertEqual(len(floor_queries), 1, floor_queries)

    def test_reviewer_cannot_adopt_a_waiver_floor(self):
        reviewer = get_user_model().objects.create_user(
            "floor-reviewer", "floor-reviewer@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(reviewer, {StaffRole.REVIEWER})
        self.client.force_login(reviewer)

        response = self.client.post(
            "/api/staff/fee-waiver-floors/",
            {
                "amount": "1.00",
                "counsel_reference": "Legal memo 2026-1",
                "effective_on": "2026-08-01",
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)

    def _apply_deposit_gate(self, **overrides):
        payload = {
            "request_number": self.prr.number,
            "scope": "production",
            "amount": "36.72",
            "estimated_total": "367.25",
            "reason": "Ten percent deposit on the estimated copying charge.",
        }
        payload.update(overrides)
        return self.client.post(
            "/api/staff/fee-deposit-gates/",
            payload,
            content_type="application/json",
        )

    def test_deposit_gate_blocks_production_until_it_is_recorded(self):
        self._activate_fee_schedule()

        gate = self._apply_deposit_gate()
        self.assertEqual(gate.status_code, 201, gate.content)
        gate_row = gate.json()
        self.assertTrue(gate_row["is_open"])
        self.assertEqual(gate_row["request_number"], self.prr.number)

        blocked = self.client.post(
            f"/api/staff/installments/{self.installment.pk}/prepare/",
            {},
            content_type="application/json",
        )
        self.assertEqual(blocked.status_code, 400, blocked.content)
        self.assertIn(b"deposit", blocked.content)

        paid = self.client.post(
            f"/api/staff/fee-deposit-gates/{gate_row['id']}/record-payment/",
            {
                "amount": "36.72",
                "method": "check",
                "reference": "District receipt 2210",
                "received_on": timezone.localdate().isoformat(),
            },
            content_type="application/json",
        )
        self.assertEqual(paid.status_code, 200, paid.content)
        self.assertFalse(paid.json()["is_open"])

        unblocked = self.client.post(
            f"/api/staff/installments/{self.installment.pk}/prepare/",
            {},
            content_type="application/json",
        )
        # Preparation now fails, if at all, on its own merits rather than on
        # the deposit.
        self.assertNotIn(b"deposit", unblocked.content)

    def test_a_deposit_gate_is_capped_at_ten_percent_and_lifted_with_a_reason(self):
        self._activate_fee_schedule()

        over_cap = self._apply_deposit_gate(amount="36.73")
        self.assertEqual(over_cap.status_code, 400, over_cap.content)
        self.assertIn(b"ten percent", over_cap.content)

        gate_row = self._apply_deposit_gate().json()

        thin = self.client.post(
            f"/api/staff/fee-deposit-gates/{gate_row['id']}/lift/",
            {"reason": "short"},
            content_type="application/json",
        )
        self.assertEqual(thin.status_code, 400, thin.content)

        lifted = self.client.post(
            f"/api/staff/fee-deposit-gates/{gate_row['id']}/lift/",
            {"reason": "Requester narrowed the request below the threshold."},
            content_type="application/json",
        )
        self.assertEqual(lifted.status_code, 200, lifted.content)
        self.assertFalse(lifted.json()["is_open"])
        self.assertEqual(
            lifted.json()["lift_reason"],
            "Requester narrowed the request below the threshold.",
        )

    def test_a_deposit_gate_needs_an_adopted_schedule_and_a_real_case(self):
        no_schedule = self._apply_deposit_gate()
        self.assertEqual(no_schedule.status_code, 400, no_schedule.content)
        self.assertIn(b"adopted fee schedule", no_schedule.content)

        self._activate_fee_schedule()
        missing_case = self._apply_deposit_gate(request_number="PRR-NOPE-1")
        self.assertEqual(missing_case.status_code, 400, missing_case.content)
        self.assertIn("request_number", missing_case.json())

    @patch("apps.api.resources.records_misc.prepare_installment")
    def test_records_officer_can_prepare_package_through_domain_service(self, prepare):
        prepare.return_value = self.installment

        response = self.client.post(
            f"/api/staff/installments/{self.installment.pk}/prepare/",
            {},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        prepare.assert_called_once()
        self.assertEqual(prepare.call_args.args, (self.installment.pk,))
        self.assertEqual(prepare.call_args.kwargs["actor"].username, "officer")

    @patch("apps.api.resources.records_misc.prepare_installment")
    def test_reviewer_cannot_prepare_package(self, prepare):
        reviewer = get_user_model().objects.create_user(
            "package-reviewer",
            "package-reviewer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(reviewer, {StaffRole.REVIEWER})
        self.client.force_login(reviewer)

        response = self.client.post(
            f"/api/staff/installments/{self.installment.pk}/prepare/",
            {},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)
        prepare.assert_not_called()

    @patch("apps.api.resources.records_misc.release_installment_with_notice")
    def test_records_officer_can_release_and_receive_scoped_url(self, release):
        download_url = (
            "https://prr.psd401.net/requests/PRR-2026-0001/"
            "installments/1/download/opaque-token/"
        )
        release.return_value = (
            self.installment,
            download_url,
            SimpleNamespace(pk="11111111-1111-4111-8111-111111111111"),
        )

        response = self.client.post(
            f"/api/staff/installments/{self.installment.pk}/release/",
            {"is_final": False},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["download_url"], download_url)
        self.assertEqual(
            response.json()["correspondence_id"],
            "11111111-1111-4111-8111-111111111111",
        )
        release.assert_called_once()
        self.assertEqual(release.call_args.args, (self.installment.pk,))
        self.assertEqual(release.call_args.kwargs["actor"].username, "officer")
        self.assertIs(release.call_args.kwargs["is_final"], False)

    @patch("apps.api.resources.records_misc.release_installment_with_notice")
    def test_reviewer_cannot_release_package(self, release):
        reviewer = get_user_model().objects.create_user(
            "release-reviewer",
            "release-reviewer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(reviewer, {StaffRole.REVIEWER})
        self.client.force_login(reviewer)

        response = self.client.post(
            f"/api/staff/installments/{self.installment.pk}/release/",
            {},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)
        release.assert_not_called()


class TimeEntryApiTests(TestCase):
    def setUp(self):
        self.staff = _login_staff(self.client)
        self.prr = _make_request()

    def test_create_and_delete_round_trip(self):
        r = self.client.post("/api/staff/time-entries/", {
            "request": str(self.prr.id),
            "date": "2026-07-01",
            "minutes": 45,
            "activity": "Search and review",
        }, content_type="application/json")
        self.assertEqual(r.status_code, 201)
        body = r.json()
        self.assertEqual(body["minutes"], 45)
        self.assertEqual(body["staff_username"], "officer")
        self.assertEqual(body["request_number"], self.prr.number)
        entry_id = body["id"]
        self.assertTrue(TimeEntry.objects.filter(id=entry_id).exists())

        d = self.client.delete(f"/api/staff/time-entries/{entry_id}/")
        self.assertEqual(d.status_code, 204)
        self.assertFalse(TimeEntry.objects.filter(id=entry_id).exists())

    def test_list(self):
        TimeEntry.objects.create(request=self.prr, staff=self.staff, minutes=30)
        r = self.client.get("/api/staff/time-entries/")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(any(x["minutes"] == 30 for x in _rows(r)))

    def test_list_can_use_the_same_request_scope_as_the_case_overview(self):
        other = _make_request()
        scoped = TimeEntry.objects.create(
            request=self.prr,
            staff=self.staff,
            minutes=30,
        )
        TimeEntry.objects.create(request=other, staff=self.staff, minutes=45)

        response = self.client.get(
            "/api/staff/time-entries/",
            {"request": self.prr.number},
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual([row["id"] for row in _rows(response)], [scoped.pk])

    def test_update(self):
        entry = TimeEntry.objects.create(request=self.prr, staff=self.staff, minutes=15)
        r = self.client.patch(f"/api/staff/time-entries/{entry.id}/",
                              {"minutes": 20}, content_type="application/json")
        self.assertEqual(r.status_code, 200)
        entry.refresh_from_db()
        self.assertEqual(entry.minutes, 20)


class ReleasePathApiTests(TestCase):
    """The Releases tab's API surface: pick records, package, send, revoke."""

    def setUp(self):
        self.officer = _login_staff(self.client)
        self.prr = _make_request()
        self.ready = RecordItem.objects.create(
            request=self.prr,
            original_filename="ready.pdf",
            storage_key="records/ready.pdf",
            sha256="a" * 64,
            content_length=4,
            content_type="application/pdf",
            review_state=RecordItem.ReviewState.RESPONSIVE,
            reviewed_by=self.officer,
            reviewed_at=timezone.now(),
        )
        self.unreviewed = RecordItem.objects.create(
            request=self.prr,
            original_filename="unreviewed.pdf",
            storage_key="records/unreviewed.pdf",
            sha256="b" * 64,
            content_length=4,
            content_type="application/pdf",
        )

    def test_release_filters_narrow_the_record_list(self):
        response = self.client.get(
            "/api/staff/record-items/",
            {"request": self.prr.number, "release_ready": "true",
             "unreleased": "true"},
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            [row["id"] for row in _rows(response)],
            [str(self.ready.pk)],
        )

    def test_create_then_set_items_then_lock_on_prepare(self):
        second = RecordItem.objects.create(
            request=self.prr,
            original_filename="second.pdf",
            storage_key="records/second.pdf",
            sha256="c" * 64,
            content_length=4,
            content_type="application/pdf",
            review_state=RecordItem.ReviewState.RESPONSIVE,
            reviewed_by=self.officer,
            reviewed_at=timezone.now(),
        )

        created = self.client.post(
            "/api/staff/installments/",
            {"request_number": self.prr.number,
             "record_item_ids": [str(self.ready.pk)]},
            content_type="application/json",
        )

        self.assertEqual(created.status_code, 201, created.content)
        body = created.json()
        self.assertEqual(body["sequence"], 1)
        self.assertEqual(body["item_count"], 1)
        self.assertFalse(body["contents_locked"])
        self.assertFalse(body["requires_access_code"])
        self.assertEqual(body["download_count"], 0)
        self.assertNotIn("download_token", body)
        self.assertNotIn("access_code_hash", body)

        installment_id = body["id"]
        updated = self.client.post(
            f"/api/staff/installments/{installment_id}/set-items/",
            {"record_item_ids": [str(self.ready.pk), str(second.pk)]},
            content_type="application/json",
        )
        self.assertEqual(updated.status_code, 200, updated.content)
        self.assertEqual(updated.json()["item_count"], 2)

        # Filters now hide both records: a live installment has claimed them.
        listed = self.client.get(
            "/api/staff/record-items/",
            {"request": self.prr.number, "release_ready": "true",
             "unreleased": "true"},
        )
        self.assertEqual(_rows(listed), [])

    def test_create_refuses_records_that_are_not_release_ready(self):
        response = self.client.post(
            "/api/staff/installments/",
            {"request_number": self.prr.number,
             "record_item_ids": [str(self.unreviewed.pk)]},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("not ready for release", str(response.json()))
        self.assertFalse(Installment.objects.exists())

    def test_create_for_an_unknown_request_is_not_found(self):
        response = self.client.post(
            "/api/staff/installments/",
            {"request_number": "PRR-0000-0000",
             "record_item_ids": [str(self.ready.pk)]},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 404)

    @patch("apps.api.resources.records_misc.release_installment_and_send_notice")
    def test_release_and_send_returns_the_link_and_the_message(self, release):
        installment = Installment.objects.create(
            request=self.prr,
            sequence=1,
            released_at=timezone.now(),
            download_expires_at=timezone.now(),
        )
        notice = SimpleNamespace(pk="11111111-1111-4111-8111-111111111111",
                                 status="queued")
        release.return_value = (installment, "https://example.test/dl/", notice)

        response = self.client.post(
            f"/api/staff/installments/{installment.pk}/release-and-send/",
            {"is_final": True},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["download_url"], "https://example.test/dl/")
        self.assertEqual(body["correspondence_id"], str(notice.pk))
        self.assertEqual(body["correspondence_status"], "queued")
        self.assertEqual(release.call_args.kwargs["is_final"], True)
        self.assertEqual(
            release.call_args.kwargs["actor"].username,
            "officer",
        )

    @patch("apps.api.resources.records_misc.release_installment_and_send_notice")
    def test_release_and_send_reports_a_release_whose_notice_did_not_send(self, release):
        """A 400 would read as "nothing happened"; the release is real."""
        installment = Installment.objects.create(
            request=self.prr,
            sequence=1,
            released_at=timezone.now(),
            download_expires_at=timezone.now(),
        )
        release.side_effect = InstallmentNoticeUnsent(
            "The package was released and its notice drafted, but the notice "
            "was not sent: Outbound correspondence transport is disabled."
        )

        response = self.client.post(
            f"/api/staff/installments/{installment.pk}/release-and-send/",
            {"is_final": False},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 409, response.content)
        body = response.json()
        self.assertEqual(body["code"], "installment_notice_unsent")
        self.assertIn("released", body["detail"])
        self.assertEqual(body["sequence"], 1)
        self.assertNotIn("download_token", body)

    def test_opaque_disposition_rejects_an_unknown_exemption_citation(self):
        response = self.client.post(
            f"/api/staff/record-items/{self.ready.pk}/opaque-disposition/",
            {
                "withholding_exemption": 999999,
                "basis": "Vendor export is a proprietary container.",
                "reasoning": "Nothing renders it; withheld under citation.",
                "review_state": "withheld",
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("Unknown exemption citation", response.content.decode())

    def test_revoke_requires_a_reason_and_records_one(self):
        installment = Installment.objects.create(
            request=self.prr,
            sequence=1,
            released_at=timezone.now(),
            download_expires_at=timezone.now(),
        )

        too_short = self.client.post(
            f"/api/staff/installments/{installment.pk}/revoke/",
            {"reason": "oops"},
            content_type="application/json",
        )
        self.assertEqual(too_short.status_code, 400)

        revoked = self.client.post(
            f"/api/staff/installments/{installment.pk}/revoke/",
            {"reason": "The wrong custodian's mail was packaged."},
            content_type="application/json",
        )
        self.assertEqual(revoked.status_code, 200, revoked.content)
        self.assertIsNotNone(revoked.json()["revoked_at"])
        self.assertEqual(revoked.json()["revoked_by_username"], "officer")

    def test_access_code_is_set_and_cleared_without_ever_being_returned(self):
        installment = Installment.objects.create(
            request=self.prr,
            sequence=1,
        )

        set_response = self.client.post(
            f"/api/staff/installments/{installment.pk}/set-access-code/",
            {"access_code": "salmon-bay-42"},
            content_type="application/json",
        )

        self.assertEqual(set_response.status_code, 200, set_response.content)
        self.assertTrue(set_response.json()["requires_access_code"])
        self.assertNotIn("salmon-bay-42", set_response.content.decode())

        cleared = self.client.post(
            f"/api/staff/installments/{installment.pk}/clear-access-code/",
            {},
            content_type="application/json",
        )
        self.assertEqual(cleared.status_code, 200, cleared.content)
        self.assertFalse(cleared.json()["requires_access_code"])
