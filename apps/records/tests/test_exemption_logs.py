"""RCW 42.56.210(3) exemption logs derive from protected review evidence."""

import hashlib

import pymupdf as fitz
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.records.exemption_logs import generate_exemption_log
from apps.records.models import (
    ExemptionCitation,
    Installment,
    PublicRecordsRequest,
    RecordItem,
    Redaction,
    Requester,
)


class _ObjectClient:
    def __init__(self):
        self.objects = {}
        self.put_calls = []

    def put_object(self, **kwargs):
        source = kwargs["Body"]
        position = source.tell()
        payload = source.read()
        source.seek(position)
        if kwargs["Key"] in self.objects:
            raise AssertionError("immutable object was overwritten")
        self.objects[kwargs["Key"]] = payload
        self.put_calls.append({**kwargs, "Body": payload})


@override_settings(OBJECT_STORE={"bucket": "records"})
class ExemptionLogTests(TestCase):
    def setUp(self):
        self.officer = get_user_model().objects.create_user(
            "log-officer",
            "log-officer@example.org",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.officer, [StaffRole.RECORDS_OFFICER])
        self.request = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(
                name="Private Requester",
                email="private-requester@example.org",
            ),
            description="Student and employee records.",
        )
        self.exemption = ExemptionCitation.objects.create(
            citation="RCW 42.56.230(3)",
            short_name="Personal information",
        )
        self.item = RecordItem.objects.create(
            request=self.request,
            original_filename="employee-record.pdf",
            storage_key=f"records/{self.request.pk}/secret-storage-key.pdf",
            content_length=100,
            content_type="application/pdf",
            sha256="a" * 64,
            review_state=RecordItem.ReviewState.RESPONSIVE,
            reviewed_by=self.officer,
            reviewed_at=timezone.now(),
        )
        self.redaction = Redaction.objects.create(
            record_item=self.item,
            page_number=2,
            x0=0.1,
            y0=0.2,
            x1=0.3,
            y1=0.4,
            exemption=self.exemption,
            explanation="Employee home address.",
            created_by=self.officer,
        )
        self.installment = Installment.objects.create(
            request=self.request,
            sequence=1,
        )
        self.installment.items.add(self.item)
        self.client = _ObjectClient()

    def test_generates_searchable_immutable_log_without_requester_pii_or_storage_keys(self):
        result = generate_exemption_log(
            self.installment.pk,
            actor=self.officer,
            _client=self.client,
        )

        self.assertTrue(result.exemption_log_storage_key.startswith(
            f"exemption-logs/{self.request.pk}/{self.installment.pk}/"
        ))
        payload = self.client.objects[result.exemption_log_storage_key]
        self.assertEqual(hashlib.sha256(payload).hexdigest(), result.exemption_log_sha256)
        self.assertEqual(len(payload), result.exemption_log_size)
        self.assertRegex(result.exemption_log_manifest_sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(result.exemption_log_prepared_by, self.officer)
        self.assertIsNotNone(result.exemption_log_prepared_at)
        self.assertEqual(self.client.put_calls[0]["IfNoneMatch"], "*")

        document = fitz.open(stream=payload, filetype="pdf")
        try:
            text = "\n".join(page.get_text() for page in document)
        finally:
            document.close()
        self.assertIn(self.request.number, text)
        self.assertIn("employee-record.pdf", text)
        self.assertIn("Page 2", text)
        self.assertIn(self.exemption.citation, text)
        self.assertIn("Employee home address", text)
        self.assertNotIn("Private Requester", text)
        self.assertNotIn("private-requester@example.org", text)
        self.assertNotIn("secret-storage-key", text)
        self.assertTrue(AuditLog.objects.filter(
            action="installment.exemption_log.generated",
            target=self.request.number,
            actor=self.officer,
            detail__manifest_sha256=result.exemption_log_manifest_sha256,
        ).exists())

    def test_manifest_lock_does_not_join_nullable_review_or_exemption_fields(self):
        with CaptureQueriesContext(connection) as queries:
            generate_exemption_log(
                self.installment.pk,
                actor=self.officer,
                _client=self.client,
            )

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

    def test_unchanged_retry_is_idempotent_but_changed_evidence_is_refused(self):
        first = generate_exemption_log(
            self.installment.pk,
            actor=self.officer,
            _client=self.client,
        )
        second = generate_exemption_log(
            self.installment.pk,
            actor=self.officer,
            _client=self.client,
        )

        self.assertEqual(second.exemption_log_storage_key, first.exemption_log_storage_key)
        self.assertEqual(len(self.client.put_calls), 1)

        self.redaction.explanation = "A materially different explanation."
        self.redaction.save(update_fields=["explanation"])
        with self.assertRaisesMessage(ValidationError, "changed after generation"):
            generate_exemption_log(
                self.installment.pk,
                actor=self.officer,
                _client=self.client,
            )
        self.assertEqual(len(self.client.put_calls), 1)

    def test_installment_without_exemptions_does_not_generate_an_empty_log(self):
        self.redaction.voided_at = timezone.now()
        self.redaction.voided_by = self.officer
        self.redaction.void_reason = "The proposed box was unnecessary."
        self.redaction.save(update_fields=["voided_at", "voided_by", "void_reason"])

        with self.assertRaisesMessage(ValidationError, "does not contain"):
            generate_exemption_log(
                self.installment.pk,
                actor=self.officer,
                _client=self.client,
            )

        self.assertEqual(self.client.put_calls, [])
