from datetime import timedelta
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.db.models.deletion import ProtectedError
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.records.models import (
    CollectionTask,
    ExemptionCitation,
    FootagePreservation,
    PublicRecordsRequest,
    RecordItem,
    Redaction,
    Requester,
    StatusEvent,
)
from apps.records.tasks import stall_detector
from apps.records.triage import triage_footage


class FeatureAuditRegressionTests(TestCase):
    def make_request(self, *, description="Emails about transportation", days_old=0):
        requester = Requester.objects.create(
            email=f"requester-{Requester.objects.count()}@example.com"
        )
        return PublicRecordsRequest.objects.create(
            requester=requester,
            description=description,
            channel="email",
            received_at=timezone.now() - timedelta(days=days_old),
        )

    @override_settings(STALL_THRESHOLD_DAYS=14)
    @patch("apps.records.tasks.notifications.notify_stalled")
    def test_recent_collection_activity_prevents_false_stalled_alert(
        self,
        notify_stalled,
    ):
        request = self.make_request(days_old=30)
        CollectionTask.objects.create(
            request=request,
            state=CollectionTask.State.COMPLETE,
            completed_at=timezone.now(),
            instructions="Search the transportation archive.",
        )

        stall_detector()

        notify_stalled.assert_not_called()

    @override_settings(STALL_THRESHOLD_DAYS=14)
    @patch("apps.records.tasks.notifications.notify_stalled")
    def test_recent_collection_task_update_prevents_false_stalled_alert(
        self,
        notify_stalled,
    ):
        old_time = timezone.now() - timedelta(days=30)
        request = self.make_request(days_old=30)
        with patch("django.utils.timezone.now", return_value=old_time):
            task = CollectionTask.objects.create(
                request=request,
                state=CollectionTask.State.PENDING,
                instructions="Search the transportation archive.",
            )
        task.state = CollectionTask.State.RUNNING
        task.save()

        stall_detector()

        notify_stalled.assert_not_called()

    def test_stall_activity_query_avoids_reverse_relation_cross_product(self):
        from apps.records import tasks

        sql = str(tasks._stall_activity_queryset().query).upper()

        self.assertNotIn("LEFT OUTER JOIN", sql)

    @override_settings(CAMERA_RETENTION_DAYS=10)
    def test_expired_footage_routes_to_adequate_search_without_denial_language(
        self,
    ):
        request = self.make_request(
            description="Bus camera footage from January 1, 2024"
        )

        preservation = triage_footage(request)

        self.assertEqual(preservation.state, FootagePreservation.State.EXPIRED)
        self.assertNotIn(
            "denial",
            FootagePreservation.State.EXPIRED.label.lower(),
        )
        self.assertTrue(
            request.events.filter(note__icontains="HUMAN REVIEW REQUIRED").exists()
        )
        self.assertTrue(
            AuditLog.objects.filter(
                action="footage.expired_window",
                target=request.number,
            ).exists()
        )

    def test_request_description_cannot_change_after_intake(self):
        request = self.make_request()
        request.description = "Different request wording"

        with self.assertRaises(ValidationError):
            request.save()

    def test_request_description_cannot_be_bulk_updated(self):
        request = self.make_request()

        with self.assertRaises(ValidationError):
            PublicRecordsRequest.objects.filter(pk=request.pk).update(
                description="Different request wording"
            )

    def test_database_rejects_request_description_rewrite(self):
        request = self.make_request()
        bare_queryset = models.QuerySet(
            model=PublicRecordsRequest,
            using="default",
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            bare_queryset.filter(pk=request.pk).update(
                description="Different request wording"
            )

    def test_status_event_is_append_only_through_model_and_queryset(self):
        request = self.make_request()
        event = StatusEvent.objects.create(request=request, note="Original evidence")

        event.note = "Rewritten evidence"
        with self.assertRaises(ValidationError):
            event.save()
        with self.assertRaises(ValidationError):
            StatusEvent.objects.filter(pk=event.pk).update(note="Rewritten evidence")
        with self.assertRaises(ValidationError):
            StatusEvent.objects.filter(pk=event.pk).delete()
        with self.assertRaises(ValidationError):
            event.delete()
        bare_queryset = models.QuerySet(model=StatusEvent, using="default")
        with self.assertRaises(IntegrityError), transaction.atomic():
            bare_queryset.filter(pk=event.pk).update(
                note="Raw-query rewrite"
            )

    def test_status_event_protects_request_from_cascading_delete(self):
        request = self.make_request()
        StatusEvent.objects.create(request=request, note="Retained evidence")

        with self.assertRaises(ProtectedError):
            request.delete()

    def test_audit_log_is_append_only_through_model_and_queryset(self):
        row = AuditLog.objects.create(action="audit.test", target="case-1")

        row.target = "rewritten"
        with self.assertRaises(ValidationError):
            row.save()
        with self.assertRaises(ValidationError):
            AuditLog.objects.filter(pk=row.pk).update(target="rewritten")
        with self.assertRaises(ValidationError):
            AuditLog.objects.filter(pk=row.pk).delete()
        with self.assertRaises(ValidationError):
            row.delete()
        conflicting_row = AuditLog(
            id=row.pk,
            action="audit.rewritten_by_upsert",
            target=row.target,
        )
        with self.assertRaises(ValidationError):
            AuditLog.objects.bulk_create(
                [conflicting_row],
                update_conflicts=True,
                update_fields=["action"],
                unique_fields=["id"],
            )
        bare_queryset = models.QuerySet(model=AuditLog, using="default")
        with self.assertRaises(IntegrityError), transaction.atomic():
            bare_queryset.filter(pk=row.pk).update(
                target="Raw-query rewrite"
            )

    def test_withheld_record_requires_at_least_one_exemption_basis(self):
        request = self.make_request()
        item = RecordItem(
            request=request,
            storage_key="records/source.pdf",
            sha256="a" * 64,
            review_state=RecordItem.ReviewState.WITHHELD,
        )

        with self.assertRaises(ValidationError):
            item.full_clean()

    def test_database_rejects_withheld_record_without_exemption_basis(self):
        request = self.make_request()
        item = RecordItem.objects.create(
            request=request,
            storage_key="records/source.pdf",
            sha256="c" * 64,
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            RecordItem.objects.filter(pk=item.pk).update(
                review_state=RecordItem.ReviewState.WITHHELD,
            )

    def test_redaction_coordinates_must_be_normalized_and_ordered(self):
        request = self.make_request()
        item = RecordItem.objects.create(
            request=request,
            storage_key="records/source.pdf",
            sha256="b" * 64,
        )
        exemption = ExemptionCitation.objects.create(
            citation="RCW 42.56.230",
            short_name="Personal information",
            description="Protected personal information.",
        )
        redaction = Redaction(
            record_item=item,
            page_number=1,
            x0=0.9,
            y0=-0.1,
            x1=0.2,
            y1=1.2,
            exemption=exemption,
        )

        with self.assertRaises(ValidationError):
            redaction.full_clean()

        with self.assertRaises(IntegrityError), transaction.atomic():
            Redaction.objects.create(
                record_item=item,
                page_number=1,
                x0=0.9,
                y0=0.1,
                x1=0.2,
                y1=0.8,
                exemption=exemption,
            )

        with self.assertRaises(IntegrityError), transaction.atomic():
            Redaction.objects.create(
                record_item=item,
                page_number=0,
                x0=0.1,
                y0=0.1,
                x1=0.2,
                y1=0.2,
                exemption=exemption,
            )

    def test_review_and_production_evidence_is_read_only_in_admin(self):
        user = get_user_model().objects.create_superuser(
            username="feature-audit-admin",
            email="feature-audit-admin@example.com",
            password="not-used",
        )
        admin_request = RequestFactory().get("/admin/")
        admin_request.user = user

        for model in (RecordItem, Redaction):
            model_admin = admin.site._registry[model]
            with self.subTest(model=model.__name__):
                self.assertFalse(model_admin.has_add_permission(admin_request))
                self.assertFalse(model_admin.has_change_permission(admin_request))
                self.assertFalse(model_admin.has_delete_permission(admin_request))
