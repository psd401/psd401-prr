from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.correspondence import inbound
from apps.correspondence.management.commands.seed_casework_test_fixture import (
    FIXTURE_MESSAGE_COUNT,
)
from apps.correspondence.models import (
    Correspondence,
    CorrespondenceDelivery,
    CorrespondenceDeliveryNotification,
    CorrespondenceRecipient,
    InboundCorrespondenceReceipt,
)
from apps.records.models import PublicRecordsRequest, Requester
from apps.work_queue.models import WorkItem, WorkItemEvent
from apps.work_queue.reconciliation import (
    reconcile_inbound_receipt,
    reconcile_inbound_work_items,
)
from apps.work_queue.services import materialize_work_item
from apps.work_queue.tasks import reconcile_inbound_receipt_work_item


class InboundWorkItemReconciliationTests(TestCase):
    def setUp(self):
        self.integration = get_user_model().objects.create_user(
            "work-queue-inbound-integration",
            password=None,
        )
        self.officer = get_user_model().objects.create_user(
            "work-queue-reconciliation-officer",
            "work-queue-reconciliation-officer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.request = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="requester@example.org"),
            description="Inbound work-item routing contract.",
        )
        self.sequence = 0

    def make_correspondence(self, *, request=None, kind=None):
        self.sequence += 1
        matched = request is not None
        return Correspondence.objects.create(
            request=request,
            direction=Correspondence.Direction.INBOUND,
            kind=kind or Correspondence.Kind.GENERAL,
            status=(
                Correspondence.Status.RECEIVED
                if matched
                else Correspondence.Status.UNMATCHED
            ),
            received_subject=f"Inbound {self.sequence}",
            received_body="Retained inbound evidence.",
            received_at=timezone.now(),
            matched_at=timezone.now() if matched else None,
            provider="gmail",
            provider_account="records-inbox",
            provider_message_id=f"inbound-correspondence-{self.sequence}",
        )

    def make_receipt(self, *, matching_status="unmatched", match_method=""):
        self.sequence += 1
        matched = matching_status == InboundCorrespondenceReceipt.MatchingStatus.MATCHED
        candidate = matching_status == InboundCorrespondenceReceipt.MatchingStatus.CANDIDATE
        quarantined = matching_status == InboundCorrespondenceReceipt.MatchingStatus.QUARANTINED
        correspondence = None if quarantined else self.make_correspondence(
            request=self.request if matched else None,
        )
        values = {
            "provider": "gmail",
            "provider_account": "records-inbox",
            "provider_message_id": f"inbound-receipt-{self.sequence}",
            "received_at": timezone.now(),
            "envelope_sender": "requester@example.org",
            "envelope_recipients": ["records@psd401.net"],
            "parsing_status": (
                InboundCorrespondenceReceipt.ParsingStatus.QUARANTINED
                if quarantined
                else InboundCorrespondenceReceipt.ParsingStatus.PARSED
            ),
            "matching_status": matching_status,
            "match_method": match_method,
            "candidate_request": self.request if candidate else None,
            "matched_request": self.request if matched else None,
            "matched_correspondence": correspondence,
            "matched_at": timezone.now() if matched else None,
            "quarantined": quarantined,
            "quarantine_reason": "malformed_message" if quarantined else "",
            "raw_message_size": 512,
            "raw_message_sha256": f"{self.sequence:064x}",
            "submitted_by": self.integration,
            "idempotency_key": f"gmail:work-item:{self.sequence}",
            "payload_sha256": f"{self.sequence + 1000:064x}",
            "completed_at": timezone.now(),
        }
        return InboundCorrespondenceReceipt.objects.create(**values)

    def make_dsn(self, action):
        self.sequence += 1
        outbound = Correspondence.objects.create(
            request=self.request,
            direction=Correspondence.Direction.OUTBOUND,
            kind=Correspondence.Kind.GENERAL,
            status=Correspondence.Status.DRAFT,
            draft_subject="Requester response",
            draft_body="Requester response body.",
            created_by=self.integration,
            provider="gmail",
            provider_account="records-inbox",
        )
        recipient = CorrespondenceRecipient.objects.create(
            correspondence=outbound,
            recipient_type=CorrespondenceRecipient.RecipientType.TO,
            email="requester@example.org",
        )
        delivery = CorrespondenceDelivery.objects.create(
            correspondence=outbound,
            provider="gmail",
            provider_account="records-inbox",
            idempotency_key=f"dsn-delivery:{self.sequence}",
        )
        inbound = self.make_correspondence(
            request=self.request,
            kind=Correspondence.Kind.DELIVERY_STATUS_NOTIFICATION,
        )
        receipt = InboundCorrespondenceReceipt.objects.create(
            provider="gmail",
            provider_account="records-inbox",
            provider_message_id=f"dsn-receipt-{self.sequence}",
            received_at=timezone.now(),
            envelope_sender="mailer-daemon@example.org",
            envelope_recipients=["records@psd401.net"],
            parsing_status=InboundCorrespondenceReceipt.ParsingStatus.PARSED,
            matching_status=InboundCorrespondenceReceipt.MatchingStatus.MATCHED,
            match_method=InboundCorrespondenceReceipt.MatchMethod.DELIVERY_STATUS,
            delivery_status_original_message_id=outbound.rfc_message_id,
            matched_request=self.request,
            matched_delivery=delivery,
            matched_correspondence=inbound,
            matched_at=timezone.now(),
            quarantined=False,
            raw_message_size=512,
            raw_message_sha256=f"{self.sequence:064x}",
            submitted_by=self.integration,
            idempotency_key=f"gmail:dsn-work-item:{self.sequence}",
            payload_sha256=f"{self.sequence + 1000:064x}",
            completed_at=timezone.now(),
        )
        CorrespondenceDeliveryNotification.objects.create(
            inbound_receipt=receipt,
            delivery=delivery,
            recipient_index=1,
            recipient=recipient,
            recipient_match_status=(
                CorrespondenceDeliveryNotification.RecipientMatchStatus.EXACT
            ),
            action=action,
            enhanced_status=(
                "5.1.1"
                if action == "failed"
                else "4.2.0"
                if action == "delayed"
                else "2.0.0"
            ),
        )
        return receipt

    def assert_route(self, receipt, expected_kind, *, request=None):
        result = reconcile_inbound_receipt(receipt.pk)

        self.assertIsNotNone(result.work_item)
        self.assertEqual(result.work_item.work_kind, expected_kind)
        self.assertEqual(result.work_item.request, request)
        self.assertEqual(
            result.work_item.disposition,
            WorkItem.Disposition.NEEDS_ATTENTION,
        )
        self.assertEqual(
            WorkItem.objects.filter(
                source_type=WorkItem.SourceType.INBOUND_RECEIPT,
                source_id=str(receipt.pk),
            ).count(),
            1,
        )
        self.assertEqual(
            WorkItemEvent.objects.filter(
                work_item=result.work_item,
                event_type=WorkItemEvent.EventType.CREATED,
            ).count(),
            1,
        )
        return result.work_item

    def test_nonduplicate_nondsn_inbound_states_route_exactly_once(self):
        cases = (
            (
                self.make_receipt(
                    matching_status=InboundCorrespondenceReceipt.MatchingStatus.MATCHED,
                    match_method=InboundCorrespondenceReceipt.MatchMethod.REPLY_TOKEN,
                ),
                WorkItem.WorkKind.INBOUND_REPLY,
                self.request,
            ),
            (
                self.make_receipt(
                    matching_status=InboundCorrespondenceReceipt.MatchingStatus.MATCHED,
                    match_method=InboundCorrespondenceReceipt.MatchMethod.EMAIL_INTAKE,
                ),
                WorkItem.WorkKind.EMAIL_INTAKE,
                self.request,
            ),
            (
                self.make_receipt(),
                WorkItem.WorkKind.INBOUND_UNMATCHED,
                None,
            ),
            (
                self.make_receipt(
                    matching_status=InboundCorrespondenceReceipt.MatchingStatus.CANDIDATE,
                    match_method=InboundCorrespondenceReceipt.MatchMethod.SUBJECT,
                ),
                WorkItem.WorkKind.INBOUND_CANDIDATE,
                self.request,
            ),
            (
                self.make_receipt(
                    matching_status=InboundCorrespondenceReceipt.MatchingStatus.AMBIGUOUS,
                ),
                WorkItem.WorkKind.INBOUND_AMBIGUOUS,
                None,
            ),
            (
                self.make_receipt(
                    matching_status=InboundCorrespondenceReceipt.MatchingStatus.QUARANTINED,
                ),
                WorkItem.WorkKind.INBOUND_QUARANTINED,
                None,
            ),
        )

        for receipt, expected_kind, request in cases:
            with self.subTest(expected_kind=expected_kind):
                first = self.assert_route(receipt, expected_kind, request=request)
                second = self.assert_route(receipt, expected_kind, request=request)
                self.assertEqual(first, second)

    def test_failed_dsn_routes_but_successful_dsn_does_not(self):
        failed = self.make_dsn(CorrespondenceDeliveryNotification.Action.FAILED)
        delayed = self.make_dsn(
            CorrespondenceDeliveryNotification.Action.DELAYED
        )
        delivered = self.make_dsn(
            CorrespondenceDeliveryNotification.Action.DELIVERED
        )

        self.assert_route(
            failed,
            WorkItem.WorkKind.DELIVERY_EXCEPTION,
            request=self.request,
        )
        self.assert_route(
            delayed,
            WorkItem.WorkKind.DELIVERY_EXCEPTION,
            request=self.request,
        )
        delivered_result = reconcile_inbound_receipt(delivered.pk)
        self.assertIsNone(delivered_result.work_item)
        self.assertFalse(
            WorkItem.objects.filter(source_id=str(delivered.pk)).exists()
        )

    def test_duplicate_receipt_does_not_create_normal_inbound_work(self):
        original = self.make_receipt()
        self.sequence += 1
        duplicate = InboundCorrespondenceReceipt.objects.create(
            provider="gmail",
            provider_account="records-inbox",
            provider_message_id=f"duplicate-receipt-{self.sequence}",
            received_at=timezone.now(),
            envelope_sender="requester@example.org",
            envelope_recipients=["records@psd401.net"],
            parsing_status=InboundCorrespondenceReceipt.ParsingStatus.DUPLICATE,
            matching_status=InboundCorrespondenceReceipt.MatchingStatus.DUPLICATE,
            duplicate_of=original,
            quarantined=True,
            raw_message_size=512,
            raw_message_sha256=f"{self.sequence:064x}",
            submitted_by=self.integration,
            idempotency_key=f"gmail:duplicate-work-item:{self.sequence}",
            payload_sha256=f"{self.sequence + 1000:064x}",
            completed_at=timezone.now(),
        )

        result = reconcile_inbound_receipt(duplicate.pk)

        self.assertIsNone(result.work_item)
        self.assertTrue(result.excluded)
        self.assertFalse(
            WorkItem.objects.filter(source_id=str(duplicate.pk)).exists()
        )

    def test_stale_duplicate_projection_is_source_excluded_not_human_handled(self):
        original = self.make_receipt()
        self.sequence += 1
        duplicate = InboundCorrespondenceReceipt.objects.create(
            provider="gmail",
            provider_account="records-inbox",
            provider_message_id=f"stale-duplicate-receipt-{self.sequence}",
            received_at=timezone.now(),
            envelope_sender="requester@example.org",
            envelope_recipients=["records@psd401.net"],
            parsing_status=InboundCorrespondenceReceipt.ParsingStatus.DUPLICATE,
            matching_status=InboundCorrespondenceReceipt.MatchingStatus.DUPLICATE,
            duplicate_of=original,
            quarantined=True,
            quarantine_reason="Duplicate inbound receipt.",
            raw_message_size=512,
            raw_message_sha256=f"{self.sequence:064x}",
            submitted_by=self.integration,
            idempotency_key=f"gmail:stale-duplicate:{self.sequence}",
            payload_sha256=f"{self.sequence + 1000:064x}",
            completed_at=timezone.now(),
        )
        stale = materialize_work_item(
            source_type=WorkItem.SourceType.INBOUND_RECEIPT,
            source_id=duplicate.pk,
            work_kind=WorkItem.WorkKind.INBOUND_UNMATCHED,
        ).work_item

        result = reconcile_inbound_receipt(duplicate.pk)

        stale.refresh_from_db()
        self.assertTrue(result.changed)
        self.assertTrue(result.excluded)
        self.assertEqual(
            stale.disposition,
            WorkItem.Disposition.SOURCE_EXCLUDED,
        )
        self.assertIsNone(stale.disposed_by)
        self.assertFalse(
            stale.events.filter(
                event_type=WorkItemEvent.EventType.DISPOSITIONED,
            ).exists()
        )
        self.assertEqual(
            stale.events.get(
                event_type=WorkItemEvent.EventType.RECONCILED,
            ).metadata["routing_code"],
            "source_excluded",
        )

    def test_stale_successful_dsn_projection_is_source_excluded(self):
        delivered = self.make_dsn(
            CorrespondenceDeliveryNotification.Action.DELIVERED
        )
        stale = materialize_work_item(
            source_type=WorkItem.SourceType.INBOUND_RECEIPT,
            source_id=delivered.pk,
            work_kind=WorkItem.WorkKind.DELIVERY_EXCEPTION,
            request=self.request,
        ).work_item

        result = reconcile_inbound_receipt(delivered.pk)

        stale.refresh_from_db()
        self.assertTrue(result.changed)
        self.assertTrue(result.excluded)
        self.assertEqual(
            stale.disposition,
            WorkItem.Disposition.SOURCE_EXCLUDED,
        )
        self.assertIsNone(stale.disposed_by)

    def test_human_match_reconciles_the_same_projection_after_source_commit(self):
        receipt = self.make_receipt()
        item = self.assert_route(
            receipt,
            WorkItem.WorkKind.INBOUND_UNMATCHED,
            request=None,
        )

        with patch(
            "apps.work_queue.tasks.reconcile_inbound_receipt_work_item.delay"
        ) as enqueue:
            with self.captureOnCommitCallbacks(execute=True):
                matched = inbound.match_inbound_receipt(
                    receipt,
                    request=self.request,
                    actor=self.officer,
                    reason="The retained message belongs to this request.",
                    expected_version=receipt.version,
                )

        enqueue.assert_called_once_with(str(receipt.pk))
        item.refresh_from_db()
        self.assertEqual(item.work_kind, WorkItem.WorkKind.INBOUND_UNMATCHED)

        reconcile_inbound_receipt_work_item.run(str(receipt.pk))

        item.refresh_from_db()
        self.assertEqual(item.work_kind, WorkItem.WorkKind.INBOUND_REPLY)
        self.assertEqual(item.request, self.request)
        self.assertEqual(
            WorkItem.objects.filter(source_id=str(receipt.pk)).count(),
            1,
        )
        self.assertEqual(
            WorkItemEvent.objects.filter(
                work_item=item,
                event_type=WorkItemEvent.EventType.RECONCILED,
            ).count(),
            1,
        )
        matched.refresh_from_db()
        self.assertEqual(
            matched.matching_status,
            InboundCorrespondenceReceipt.MatchingStatus.MATCHED,
        )

    def test_reconciliation_backfills_missing_items_idempotently_without_rewriting_source(self):
        receipts = [
            self.make_receipt(),
            self.make_receipt(
                matching_status=InboundCorrespondenceReceipt.MatchingStatus.AMBIGUOUS,
            ),
            self.make_receipt(
                matching_status=InboundCorrespondenceReceipt.MatchingStatus.QUARANTINED,
            ),
        ]
        source_snapshots = {
            receipt.pk: (
                receipt.parsing_status,
                receipt.matching_status,
                receipt.version,
                receipt.matched_request_id,
                receipt.matched_correspondence_id,
            )
            for receipt in receipts
        }

        first = reconcile_inbound_work_items(limit=100)
        second = reconcile_inbound_work_items(limit=100)

        self.assertEqual(first.created, 3)
        self.assertEqual(second.created, 0)
        self.assertEqual(WorkItem.objects.count(), 3)
        self.assertEqual(
            WorkItemEvent.objects.filter(
                event_type=WorkItemEvent.EventType.CREATED
            ).count(),
            3,
        )
        self.assertEqual(
            AuditLog.objects.filter(action="work_queue.item_created").count(),
            3,
        )
        for receipt in receipts:
            receipt.refresh_from_db()
            self.assertEqual(
                (
                    receipt.parsing_status,
                    receipt.matching_status,
                    receipt.version,
                    receipt.matched_request_id,
                    receipt.matched_correspondence_id,
                ),
                source_snapshots[receipt.pk],
            )


@override_settings(PRR_TEST_FIXTURE_SEED_ENABLED=True)
class CaseworkFixtureWorkItemContractTests(TestCase):
    def test_every_eligible_receipt_in_two_thousand_message_fixture_has_one_item(self):
        call_command("seed_casework_test_fixture", stdout=StringIO())
        # The 2000 on the primary case, the single retained email that
        # opened PRR-E2E-0006 automatically, and the system-prepared
        # interpretation-and-estimate draft on PRR-E2E-0010.
        self.assertEqual(
            Correspondence.objects.count(),
            FIXTURE_MESSAGE_COUNT + 2,
        )
        self.assertEqual(
            WorkItem.objects.filter(
                source_type=WorkItem.SourceType.INBOUND_RECEIPT,
            ).count(),
            3,
        )
        # Five on the 2000-message case, the request-action items on the two
        # cases that owe a five-business-day response (PRR-E2E-0005 and the
        # forwarded-requester case PRR-E2E-0008), and the receipt-backed item
        # on the auto-created email case PRR-E2E-0006.
        self.assertEqual(WorkItem.objects.count(), 8)
        self.assertEqual(
            WorkItem.objects.filter(
                work_kind=WorkItem.WorkKind.REQUEST_ACTION,
            ).count(),
            2,
        )

        first = reconcile_inbound_work_items(limit=100)
        second = reconcile_inbound_work_items(limit=100)

        receipts = list(
            InboundCorrespondenceReceipt.objects.prefetch_related(
                "delivery_notifications"
            )
        )
        self.assertEqual(len(receipts), 4)
        self.assertEqual(first.created, 0)
        self.assertEqual(second.created, 0)
        for receipt in receipts:
            items = WorkItem.objects.filter(
                source_type=WorkItem.SourceType.INBOUND_RECEIPT,
                source_id=str(receipt.pk),
            )
            if (
                receipt.parsing_status
                == InboundCorrespondenceReceipt.ParsingStatus.DUPLICATE
            ):
                self.assertEqual(items.count(), 0)
                continue
            self.assertEqual(items.count(), 1)
            item = items.get()
            self.assertIn(
                item.work_kind,
                {
                    WorkItem.WorkKind.INBOUND_REPLY,
                    WorkItem.WorkKind.DELIVERY_EXCEPTION,
                    WorkItem.WorkKind.EMAIL_INTAKE,
                },
            )
