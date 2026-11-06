"""REST parity for secure preview and human record-review outcomes."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.records.models import (
    ExemptionCitation,
    PublicRecordsRequest,
    RecordItem,
    Redaction,
    Requester,
)
from apps.records.preview import PdfPreviewPage
from apps.records.review import review_record


class RecordReviewApiTests(TestCase):
    def setUp(self):
        self.reviewer = get_user_model().objects.create_user(
            "review-api-one",
            "review-api-one@example.org",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.reviewer, [StaffRole.REVIEWER])
        self.client.login(username=self.reviewer.username, password="pw")
        self.request = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="requester@example.org"),
            description="Review the retained records.",
        )
        self.item = RecordItem.objects.create(
            request=self.request,
            original_filename="student-record.pdf",
            storage_key=f"records/{self.request.pk}/source.pdf",
            content_length=123,
            content_type="application/pdf",
            sha256="a" * 64,
        )
        self.exemption = ExemptionCitation.objects.create(
            citation="20 U.S.C. § 1232g",
            short_name="FERPA education records",
            requires_second_review=True,
        )

    def test_record_summary_exposes_review_gates_without_storage_or_ocr(self):
        response = self.client.get(f"/api/staff/record-items/{self.item.pk}/")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["second_review_required"])
        self.assertFalse(body["redacted_artifact_ready"])
        self.assertIsNone(body["reviewed_at"])
        self.assertIsNone(body["second_reviewed_by_username"])
        for private_field in (
            "storage_key",
            "redacted_storage_key",
            "redacted_sha256",
            "redacted_manifest_sha256",
            "ocr_text",
        ):
            self.assertNotIn(private_field, body)

    def test_reviewer_reads_a_records_text_without_its_markup_and_it_is_audited(self):
        self.item.ocr_text = "<p>Board minutes&nbsp;attached</p><!-- tracking -->"
        self.item.email_body_sha256 = "b" * 64
        self.item.save(update_fields=["ocr_text", "email_body_sha256"])

        response = self.client.get(f"/api/staff/record-items/{self.item.pk}/text/")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), {"text": "Board minutes attached", "truncated": False})
        self.assertTrue(
            AuditLog.objects.filter(
                action="record.text_viewed", target=str(self.item.pk),
            ).exists()
        )
        self.item.refresh_from_db()
        self.assertEqual(
            self.item.ocr_text, "<p>Board minutes&nbsp;attached</p><!-- tracking -->",
        )
        # Identical copies are folded in review by their body fingerprint.
        listed = self.client.get(f"/api/staff/record-items/{self.item.pk}/").json()
        self.assertEqual(listed["email_body_sha256"], "b" * 64)

    def test_a_role_that_cannot_preview_cannot_read_record_text(self):
        liaison = get_user_model().objects.create(
            username="review-api-liaison", email="liaison@example.org", is_staff=True,
        )
        apply_staff_roles(liaison, [StaffRole.LIAISON])
        self.client.force_login(liaison)

        response = self.client.get(f"/api/staff/record-items/{self.item.pk}/text/")

        self.assertIn(response.status_code, {403, 404})

    def test_review_action_records_human_withholding_decision(self):
        response = self.client.post(
            f"/api/staff/record-items/{self.item.pk}/review/",
            {
                "state": RecordItem.ReviewState.WITHHELD,
                "withholding_exemption_ids": [self.exemption.pk],
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.item.refresh_from_db()
        self.assertEqual(self.item.review_state, RecordItem.ReviewState.WITHHELD)
        self.assertEqual(self.item.reviewed_by, self.reviewer)
        self.assertEqual(response.json()["reviewed_by_username"], self.reviewer.username)
        self.assertTrue(response.json()["second_review_required"])

    def test_review_lock_query_does_not_join_nullable_withholding_exemption(self):
        """PostgreSQL rejects FOR UPDATE on the nullable side of a join."""
        with CaptureQueriesContext(connection) as queries:
            reviewed = review_record(
                self.item.pk,
                state=RecordItem.ReviewState.RESPONSIVE,
                actor=self.reviewer,
            )

        self.assertEqual(reviewed.review_state, RecordItem.ReviewState.RESPONSIVE)
        lock_query = next(
            query["sql"]
            for query in queries.captured_queries
            if 'FROM "records_recorditem"' in query["sql"]
        )
        self.assertNotIn(
            'LEFT OUTER JOIN "records_exemptioncitation"',
            lock_query,
        )

    @patch("apps.api.resources.records_misc.preview_pdf_metadata")
    def test_preview_metadata_uses_verified_raster_service(self, metadata):
        metadata.return_value = {"page_count": 4}

        response = self.client.get(
            f"/api/staff/record-items/{self.item.pk}/preview-metadata/"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"page_count": 4})
        metadata.assert_called_once_with(self.item.pk, actor=self.reviewer)

    @patch("apps.api.resources.records_misc.render_pdf_preview_page")
    def test_preview_page_is_no_store_raster_not_original_pdf(self, render):
        render.return_value = PdfPreviewPage(
            content=b"\x89PNG\r\npreview",
            page_number=2,
            page_count=4,
            width=800,
            height=1000,
        )

        response = self.client.get(
            f"/api/staff/record-items/{self.item.pk}/preview-page/?page=2"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response["X-Page-Count"], "4")
        self.assertEqual(response.content, b"\x89PNG\r\npreview")
        render.assert_called_once_with(
            self.item.pk,
            page_number="2",
            actor=self.reviewer,
        )

    @patch("apps.api.resources.records_misc.create_redaction")
    def test_redaction_create_uses_domain_service_and_returns_legal_basis(self, create):
        redaction = Redaction.objects.create(
            record_item=self.item,
            page_number=1,
            x0=0.1,
            y0=0.2,
            x1=0.3,
            y1=0.4,
            exemption=self.exemption,
            explanation="Student identifier.",
            created_by=self.reviewer,
        )
        create.return_value = redaction

        response = self.client.post(
            "/api/staff/redactions/",
            {
                "record_item": str(self.item.pk),
                "page_number": 1,
                "x0": 0.1,
                "y0": 0.2,
                "x1": 0.3,
                "y1": 0.4,
                "exemption": self.exemption.pk,
                "explanation": "Student identifier.",
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["citation"], self.exemption.citation)
        self.assertTrue(response.json()["requires_second_review"])
        create.assert_called_once_with(
            self.item.pk,
            actor=self.reviewer,
            page_number=1,
            x0=0.1,
            y0=0.2,
            x1=0.3,
            y1=0.4,
            exemption_id=self.exemption.pk,
            explanation="Student identifier.",
        )

    def test_redaction_void_retains_row_and_reason(self):
        self.item.review_state = RecordItem.ReviewState.RESPONSIVE
        self.item.reviewed_by = self.reviewer
        self.item.reviewed_at = timezone.now()
        self.item.save(update_fields=["review_state", "reviewed_by", "reviewed_at"])
        redaction = Redaction.objects.create(
            record_item=self.item,
            page_number=1,
            x0=0.1,
            y0=0.2,
            x1=0.3,
            y1=0.4,
            exemption=self.exemption,
            created_by=self.reviewer,
        )

        response = self.client.post(
            f"/api/staff/redactions/{redaction.pk}/void/",
            {"reason": "The box covered the wrong content."},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        redaction.refresh_from_db()
        self.assertIsNotNone(redaction.voided_at)
        self.assertEqual(response.json()["void_reason"], redaction.void_reason)

    def test_distinct_reviewer_can_approve_required_second_review(self):
        self.item.review_state = RecordItem.ReviewState.WITHHELD
        self.item.reviewed_by = self.reviewer
        self.item.reviewed_at = timezone.now()
        self.item.withholding_exemption = self.exemption
        self.item.save(update_fields=[
            "review_state",
            "reviewed_by",
            "reviewed_at",
            "withholding_exemption",
        ])
        second = get_user_model().objects.create_user(
            "review-api-two",
            "review-api-two@example.org",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(second, [StaffRole.REVIEWER])
        self.client.logout()
        self.client.login(username=second.username, password="pw")

        response = self.client.post(
            f"/api/staff/record-items/{self.item.pk}/second-review/",
            {},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.item.refresh_from_db()
        self.assertEqual(self.item.second_reviewed_by, second)
        self.assertEqual(response.json()["second_reviewed_by_username"], second.username)

    def test_same_reviewer_cannot_approve_required_second_review(self):
        self.item.review_state = RecordItem.ReviewState.WITHHELD
        self.item.reviewed_by = self.reviewer
        self.item.reviewed_at = timezone.now()
        self.item.withholding_exemption = self.exemption
        self.item.save(update_fields=[
            "review_state",
            "reviewed_by",
            "reviewed_at",
            "withholding_exemption",
        ])

        response = self.client.post(
            f"/api/staff/record-items/{self.item.pk}/second-review/",
            {},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.item.refresh_from_db()
        self.assertIsNone(self.item.second_reviewed_by)
        self.assertIsNone(self.item.second_reviewed_at)

    @patch("apps.api.resources.records_misc.render_redacted_record")
    def test_render_redactions_action_uses_human_actor(self, render):
        self.item.review_state = RecordItem.ReviewState.RESPONSIVE
        self.item.reviewed_by = self.reviewer
        self.item.reviewed_at = timezone.now()
        self.item.redacted_storage_key = f"records/{self.request.pk}/redacted.pdf"
        self.item.redacted_sha256 = "b" * 64
        self.item.redacted_content_length = 456
        self.item.redacted_content_type = "application/pdf"
        self.item.redacted_manifest_sha256 = "c" * 64
        render.return_value = self.item

        response = self.client.post(
            f"/api/staff/record-items/{self.item.pk}/render-redactions/",
            {},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["redacted_artifact_ready"])
        render.assert_called_once_with(self.item.pk, actor=self.reviewer)
