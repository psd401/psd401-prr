# ABOUTME: Staff API tests for the exemption-citation resource (exemplar
# ABOUTME: vertical — CRUD conventions all other resources follow).
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase

from apps.audit.models import AuditLog
from apps.records.models import (
    ExemptionCitation,
    PublicRecordsRequest,
    RecordItem,
    Redaction,
    Requester,
)


class ExemptionApiTests(TestCase):
    def setUp(self):
        get_user_model().objects.create_user(
            "officer", "officer@psd401.net", "pw", is_staff=True)
        self.client.login(username="officer", password="pw")
        self.ex = ExemptionCitation.objects.create(
            citation="RCW 42.56.230(1)", short_name="Personal info – students")

    def test_list(self):
        r = self.client.get("/api/staff/exemptions/")
        self.assertEqual(r.status_code, 200)
        payload = r.json()
        rows = payload["results"] if isinstance(payload, dict) else payload
        self.assertTrue(any(x["citation"] == "RCW 42.56.230(1)" for x in rows))

    def test_create(self):
        r = self.client.post("/api/staff/exemptions/", {
            "citation": "RCW 42.56.250(3)",
            "short_name": "Employee personal info",
            "requires_second_review": True,
        }, content_type="application/json")
        self.assertEqual(r.status_code, 201)
        self.assertTrue(ExemptionCitation.objects.filter(
            citation="RCW 42.56.250(3)").exists())

    def test_update(self):
        r = self.client.patch(f"/api/staff/exemptions/{self.ex.id}/",
                              {"active": False}, content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.ex.refresh_from_db()
        self.assertFalse(self.ex.active)

    def test_duplicate_citation_rejected(self):
        r = self.client.post("/api/staff/exemptions/", {
            "citation": "RCW 42.56.230(1)", "short_name": "dupe",
        }, content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def _use_for_withholding(self):
        prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="evidence@example.org"),
            description="Records whose exemption basis must remain stable.",
        )
        item = RecordItem.objects.create(
            request=prr,
            storage_key="records/evidence.pdf",
            sha256="a" * 64,
            review_state=RecordItem.ReviewState.WITHHELD,
            withholding_exemption=self.ex,
        )
        item.withholding_exemptions.add(self.ex)
        return item

    def test_used_citation_meaning_cannot_be_rewritten_through_api(self):
        self._use_for_withholding()

        response = self.client.patch(
            f"/api/staff/exemptions/{self.ex.id}/",
            {
                "citation": "RCW 42.56.999",
                "short_name": "Retroactively substituted authority",
                "description": "Changed after the withholding decision.",
                "requires_second_review": True,
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.ex.refresh_from_db()
        self.assertEqual(self.ex.citation, "RCW 42.56.230(1)")
        self.assertEqual(self.ex.short_name, "Personal info – students")
        self.assertEqual(self.ex.description, "")
        self.assertFalse(self.ex.requires_second_review)
        self.assertFalse(
            AuditLog.objects.filter(
                action="exemption.updated",
                target=str(self.ex.pk),
            ).exists()
        )

    def test_used_citation_meaning_cannot_be_rewritten_through_model_save(self):
        self._use_for_withholding()
        self.ex.short_name = "Retroactive rewrite"

        with self.assertRaises(ValidationError):
            self.ex.save(update_fields=["short_name"])

        self.ex.refresh_from_db()
        self.assertEqual(self.ex.short_name, "Personal info – students")

    def test_used_citation_meaning_cannot_be_rewritten_through_queryset_update(self):
        self._use_for_withholding()

        with self.assertRaises(ValidationError):
            ExemptionCitation.objects.filter(pk=self.ex.pk).update(
                short_name="Bulk retroactive rewrite"
            )

        self.ex.refresh_from_db()
        self.assertEqual(self.ex.short_name, "Personal info – students")

    def test_database_rejects_bare_queryset_rewrite_of_used_citation(self):
        self._use_for_withholding()
        bare_queryset = models.QuerySet(
            model=ExemptionCitation,
            using="default",
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            bare_queryset.filter(pk=self.ex.pk).update(
                short_name="Bypassed manager rewrite"
            )

        self.ex.refresh_from_db()
        self.assertEqual(self.ex.short_name, "Personal info – students")

    def test_redaction_also_freezes_citation_meaning(self):
        item = self._use_for_withholding()
        item.withholding_exemptions.clear()
        Redaction.objects.create(
            record_item=item,
            page_number=1,
            x0=0.1,
            y0=0.1,
            x1=0.2,
            y1=0.2,
            exemption=self.ex,
        )

        response = self.client.patch(
            f"/api/staff/exemptions/{self.ex.id}/",
            {"description": "Retroactively changed redaction authority."},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.ex.refresh_from_db()
        self.assertEqual(self.ex.description, "")

    def test_used_withholding_citation_cannot_be_deleted_at_api_or_model_layer(self):
        item = self._use_for_withholding()

        response = self.client.delete(
            f"/api/staff/exemptions/{self.ex.id}/"
        )

        self.assertEqual(response.status_code, 400)
        self.assertTrue(ExemptionCitation.objects.filter(pk=self.ex.pk).exists())
        self.assertTrue(item.withholding_exemptions.filter(pk=self.ex.pk).exists())
        with self.assertRaises(ProtectedError):
            ExemptionCitation.objects.filter(pk=self.ex.pk).delete()

    def test_active_state_change_remains_available_and_is_audited(self):
        self._use_for_withholding()

        response = self.client.patch(
            f"/api/staff/exemptions/{self.ex.id}/",
            {"active": False},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.ex.refresh_from_db()
        self.assertFalse(self.ex.active)
        log = AuditLog.objects.get(
            action="exemption.updated",
            target=str(self.ex.pk),
        )
        self.assertEqual(log.detail["before"]["active"], True)
        self.assertEqual(log.detail["after"]["active"], False)

    def test_unused_citation_meaning_change_is_audited(self):
        response = self.client.patch(
            f"/api/staff/exemptions/{self.ex.id}/",
            {"short_name": "Counsel-approved corrected label"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        log = AuditLog.objects.get(
            action="exemption.updated",
            target=str(self.ex.pk),
        )
        self.assertEqual(
            log.detail["before"]["short_name"],
            "Personal info – students",
        )
        self.assertEqual(
            log.detail["after"]["short_name"],
            "Counsel-approved corrected label",
        )
