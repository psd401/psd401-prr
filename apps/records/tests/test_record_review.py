"""Human review, second-review, preview, and irreversible redaction contracts."""

import hashlib
import io

import pymupdf as fitz
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.records.models import (
    ExemptionCitation,
    PublicRecordsRequest,
    RecordItem,
    Requester,
)
from apps.records.preview import preview_pdf_metadata, render_pdf_preview_page
from apps.records.review import (
    approve_second_review,
    create_redaction,
    render_redacted_record,
    review_record,
    void_redaction,
)


class _Body(io.BytesIO):
    def iter_chunks(self, chunk_size):
        while chunk := self.read(chunk_size):
            yield chunk


class _ObjectClient:
    def __init__(self):
        self.objects = {}

    def get_object(self, *, Bucket, Key):
        return {"Body": _Body(self.objects[Key])}

    def put_object(self, **kwargs):
        body = kwargs["Body"]
        position = body.tell()
        payload = body.read()
        body.seek(position)
        if kwargs["Key"] in self.objects:
            raise AssertionError("immutable object was overwritten")
        self.objects[kwargs["Key"]] = payload


def _pdf_with_secret():
    document = fitz.open()
    page = document.new_page(width=300, height=200)
    page.insert_text((60, 105), "SECRET STUDENT VALUE", fontsize=14)
    payload = document.tobytes(garbage=4, deflate=True)
    document.close()
    return payload


@override_settings(
    OBJECT_STORE={"bucket": "records"},
    PRR_RECORD_PREVIEW_DPI=96,
    PRR_REDACTION_DPI=150,
)
class RecordReviewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.reviewer = User.objects.create_user(
            "reviewer-one", "reviewer-one@example.org", "pw", is_staff=True
        )
        self.second_reviewer = User.objects.create_user(
            "reviewer-two", "reviewer-two@example.org", "pw", is_staff=True
        )
        apply_staff_roles(self.reviewer, [StaffRole.REVIEWER])
        apply_staff_roles(self.second_reviewer, [StaffRole.REVIEWER])
        self.request = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="requester@example.org"),
            description="Student records.",
        )
        self.source = _pdf_with_secret()
        self.source_key = f"records/{self.request.pk}/source.pdf"
        self.client = _ObjectClient()
        self.client.objects[self.source_key] = self.source
        self.item = RecordItem.objects.create(
            request=self.request,
            original_filename="student-record.pdf",
            storage_key=self.source_key,
            content_length=len(self.source),
            content_type="application/pdf",
            sha256=hashlib.sha256(self.source).hexdigest(),
        )
        self.general = ExemptionCitation.objects.create(
            citation="RCW 42.56.230(3)",
            short_name="Personal information",
        )
        self.ferpa = ExemptionCitation.objects.create(
            citation="20 U.S.C. § 1232g",
            short_name="FERPA education records",
            requires_second_review=True,
        )

    def test_secure_preview_is_raster_only_and_integrity_checked(self):
        metadata = preview_pdf_metadata(self.item.pk, _client=self.client)
        preview = render_pdf_preview_page(
            self.item.pk,
            page_number=1,
            _client=self.client,
        )

        self.assertEqual(metadata, {"page_count": 1})
        self.assertTrue(preview.content.startswith(b"\x89PNG"))
        self.assertEqual(preview.page_count, 1)
        self.assertEqual(preview.page_number, 1)
        self.assertGreater(preview.width, 0)
        self.assertGreater(preview.height, 0)

        self.client.objects[self.source_key] += b"tampered"
        with self.assertRaisesMessage(RuntimeError, "integrity"):
            preview_pdf_metadata(self.item.pk, _client=self.client)

    def test_fully_withheld_review_requires_an_active_exemption(self):
        with self.assertRaisesMessage(ValidationError, "exemption"):
            review_record(
                self.item.pk,
                state=RecordItem.ReviewState.WITHHELD,
                actor=self.reviewer,
                withholding_exemption_ids=[],
            )

        reviewed = review_record(
            self.item.pk,
            state=RecordItem.ReviewState.WITHHELD,
            actor=self.reviewer,
            withholding_exemption_ids=[self.general.pk],
        )

        self.assertEqual(reviewed.review_state, RecordItem.ReviewState.WITHHELD)
        self.assertEqual(reviewed.reviewed_by, self.reviewer)
        self.assertIsNotNone(reviewed.reviewed_at)
        self.assertEqual(reviewed.withholding_exemption, self.general)
        self.assertEqual(
            list(reviewed.withholding_exemptions.values_list("pk", flat=True)),
            [self.general.pk],
        )
        self.assertTrue(AuditLog.objects.filter(
            action="record.reviewed",
            target=str(self.item.pk),
            actor=self.reviewer,
        ).exists())

    def test_ferpa_redaction_requires_distinct_second_review_before_burn_in(self):
        review_record(
            self.item.pk,
            state=RecordItem.ReviewState.RESPONSIVE,
            actor=self.reviewer,
        )
        redaction = create_redaction(
            self.item.pk,
            actor=self.reviewer,
            page_number=1,
            x0=0.15,
            y0=0.35,
            x1=0.85,
            y1=0.65,
            exemption_id=self.ferpa.pk,
            explanation="Student identifying information.",
            _client=self.client,
        )
        self.item.refresh_from_db()
        self.assertTrue(self.item.second_review_required)

        with self.assertRaisesMessage(ValidationError, "second review"):
            render_redacted_record(
                self.item.pk,
                actor=self.reviewer,
                _client=self.client,
            )
        with self.assertRaisesMessage(ValidationError, "different"):
            approve_second_review(self.item.pk, actor=self.reviewer)

        approve_second_review(self.item.pk, actor=self.second_reviewer)
        rendered = render_redacted_record(
            self.item.pk,
            actor=self.second_reviewer,
            _client=self.client,
        )

        self.assertTrue(rendered.redacted_storage_key)
        self.assertTrue(rendered.redacted_manifest_sha256)
        output = self.client.objects[rendered.redacted_storage_key]
        self.assertEqual(hashlib.sha256(output).hexdigest(), rendered.redacted_sha256)
        self.assertNotIn(b"SECRET STUDENT VALUE", output)
        document = fitz.open(stream=output, filetype="pdf")
        try:
            self.assertEqual("".join(page.get_text() for page in document), "")
            pixels = document[0].get_pixmap(dpi=96, alpha=False)
            center = pixels.pixel(pixels.width // 2, pixels.height // 2)
            self.assertLess(max(center), 30)
        finally:
            document.close()
        self.assertTrue(AuditLog.objects.filter(
            action="record.redaction.rendered",
            target=str(self.item.pk),
            detail__manifest_sha256=rendered.redacted_manifest_sha256,
        ).exists())
        self.assertEqual(redaction.record_item_id, self.item.pk)

    def test_second_review_lock_does_not_join_nullable_review_relations(self):
        review_record(
            self.item.pk,
            state=RecordItem.ReviewState.RESPONSIVE,
            actor=self.reviewer,
        )
        create_redaction(
            self.item.pk,
            actor=self.reviewer,
            page_number=1,
            x0=0.1,
            y0=0.1,
            x1=0.2,
            y1=0.2,
            exemption_id=self.ferpa.pk,
            _client=self.client,
        )

        with CaptureQueriesContext(connection) as queries:
            approve_second_review(self.item.pk, actor=self.second_reviewer)

        lock_query = next(
            query["sql"]
            for query in queries.captured_queries
            if 'FROM "records_recorditem"' in query["sql"]
        )
        self.assertNotIn('LEFT OUTER JOIN "auth_user"', lock_query)
        self.assertNotIn(
            'LEFT OUTER JOIN "records_exemptioncitation"',
            lock_query,
        )

    def test_redaction_render_lock_does_not_join_nullable_reviewers(self):
        review_record(
            self.item.pk,
            state=RecordItem.ReviewState.RESPONSIVE,
            actor=self.reviewer,
        )
        create_redaction(
            self.item.pk,
            actor=self.reviewer,
            page_number=1,
            x0=0.1,
            y0=0.1,
            x1=0.2,
            y1=0.2,
            exemption_id=self.general.pk,
            _client=self.client,
        )

        with CaptureQueriesContext(connection) as queries:
            render_redacted_record(
                self.item.pk,
                actor=self.reviewer,
                _client=self.client,
            )

        lock_query = next(
            query["sql"]
            for query in queries.captured_queries
            if 'FROM "records_recorditem"' in query["sql"]
        )
        self.assertNotIn('LEFT OUTER JOIN "auth_user"', lock_query)

    def test_new_or_voided_redaction_invalidates_prior_approval_and_artifact(self):
        review_record(
            self.item.pk,
            state=RecordItem.ReviewState.RESPONSIVE,
            actor=self.reviewer,
        )
        redaction = create_redaction(
            self.item.pk,
            actor=self.reviewer,
            page_number=1,
            x0=0.1,
            y0=0.1,
            x1=0.2,
            y1=0.2,
            exemption_id=self.ferpa.pk,
            _client=self.client,
        )
        approve_second_review(self.item.pk, actor=self.second_reviewer)
        render_redacted_record(
            self.item.pk,
            actor=self.second_reviewer,
            _client=self.client,
        )

        void_redaction(
            redaction.pk,
            actor=self.reviewer,
            reason="The box was placed over the wrong content.",
        )

        self.item.refresh_from_db()
        redaction.refresh_from_db()
        self.assertIsNotNone(redaction.voided_at)
        self.assertEqual(redaction.voided_by, self.reviewer)
        self.assertFalse(self.item.second_review_required)
        self.assertIsNone(self.item.second_reviewed_at)
        self.assertEqual(self.item.redacted_storage_key, "")
        self.assertEqual(self.item.redacted_manifest_sha256, "")
