# ABOUTME: End-to-end contracts for inbound RFC 3464 delivery-status evidence.
# ABOUTME: DSNs match only stable outbound Message-IDs and never rewrite legal state.
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.correspondence import inbound, services
from apps.correspondence.models import (
    ClarificationCycle,
    Correspondence,
    CorrespondenceDelivery,
    CorrespondenceDeliveryNotification,
    CorrespondenceRecipient,
    InboundCorrespondenceReceipt,
)
from apps.records.models import (
    PublicRecordsRequest,
    Requester,
    RequestStatus,
    StatusEvent,
)


def delivery_status_message(
    original_message_id,
    *,
    outer_subject="Delivery Status Notification",
    outer_in_reply_to="",
):
    in_reply_to = (
        f"In-Reply-To: {outer_in_reply_to}\r\n"
        if outer_in_reply_to
        else ""
    )
    return (
        "From: Mail Delivery Subsystem <mailer-daemon@example.org>\r\n"
        "To: records@prr.psd401.net\r\n"
        "Date: Fri, 24 Jul 2026 12:20:30 -0700\r\n"
        "Message-ID: <dsn-evidence@example.org>\r\n"
        f"Subject: {outer_subject}\r\n"
        f"{in_reply_to}"
        "MIME-Version: 1.0\r\n"
        'Content-Type: multipart/report; report-type=delivery-status; '
        'boundary="dsn-boundary"\r\n'
        "\r\n"
        "--dsn-boundary\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        "The provider supplied human-readable delivery details.\r\n"
        "--dsn-boundary\r\n"
        "Content-Type: message/delivery-status\r\n"
        "\r\n"
        "Reporting-MTA: dns; mx.example.org\r\n"
        "\r\n"
        "Final-Recipient: rfc822; sensitive.requester@example.org\r\n"
        "Action: failed\r\n"
        "Status: 5.1.1\r\n"
        "Diagnostic-Code: smtp; 550 private provider diagnostic prose\r\n"
        "\r\n"
        "--dsn-boundary\r\n"
        "Content-Type: text/rfc822-headers\r\n"
        "\r\n"
        f"Message-ID: {original_message_id}\r\n"
        "From: records@prr.psd401.net\r\n"
        "To: sensitive.requester@example.org\r\n"
        "Subject: Original requester correspondence\r\n"
        "\r\n"
        "--dsn-boundary--\r\n"
    ).encode("ascii")


@override_settings(
    PRR_LOCAL_IMPLICIT_STAFF_ROLES=(),
    PRR_CORRESPONDENCE_MESSAGE_ID_DOMAIN="mail.prr.test",
    PRR_CORRESPONDENCE_INBOUND_MESSAGE_MAX_BYTES=1024 * 1024,
    PRR_CORRESPONDENCE_INBOUND_BODY_MAX_BYTES=64 * 1024,
    PRR_CORRESPONDENCE_INBOUND_MAX_HEADERS=50,
    PRR_CORRESPONDENCE_INBOUND_MAX_HEADER_LINE_BYTES=998,
    PRR_CORRESPONDENCE_INBOUND_MAX_MIME_PARTS=25,
    PRR_CORRESPONDENCE_INBOUND_MAX_MIME_DEPTH=10,
    PRR_CORRESPONDENCE_ATTACHMENT_MAX_COUNT=5,
    PRR_CORRESPONDENCE_ATTACHMENT_MAX_BYTES=256 * 1024,
    PRR_CORRESPONDENCE_ATTACHMENT_TOTAL_MAX_BYTES=512 * 1024,
)
class DeliveryStatusReceivingTests(TestCase):
    def setUp(self):
        self.integration = get_user_model().objects.create_user(
            "automation-correspondence-inbound",
            password=None,
        )
        self.integration.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="correspondence",
                codename="submit_inbound_correspondence",
            )
        )
        self.officer = get_user_model().objects.create_user(
            "dsn-records-officer",
            "dsn-records-officer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.requester = Requester.objects.create(
            name="Sensitive Requester",
            email="sensitive.requester@example.org",
        )
        self.request = PublicRecordsRequest.objects.create(
            requester=self.requester,
            description="Delivery-status evidence contract.",
        )
        self.received_at = timezone.now().replace(microsecond=0)
        self.sequence = 0

    def open_clarification_fixture(self, correspondence):
        PublicRecordsRequest.objects.filter(pk=self.request.pk).update(
            status=RequestStatus.CLARIFICATION,
        )
        self.request.refresh_from_db()
        opened_event = StatusEvent.objects.create(
            request=self.request,
            actor=self.officer,
            from_status=RequestStatus.RECEIVED,
            to_status=RequestStatus.CLARIFICATION,
            note="Clarification request delivered.",
            visible_to_requester=True,
            occurred_at=correspondence.sent_at,
        )
        return ClarificationCycle.objects.create(
            request=self.request,
            outbound_correspondence=correspondence,
            opened_at=correspondence.sent_at,
            opened_status_event=opened_event,
        )

    def make_pending_delivery(self, *, provider_thread_id=""):
        self.sequence += 1
        correspondence = Correspondence.objects.create(
            request=self.request,
            direction=Correspondence.Direction.OUTBOUND,
            kind=Correspondence.Kind.GENERAL,
            status=Correspondence.Status.DRAFT,
            draft_subject="Approved requester response",
            draft_body="Approved requester response body.",
            created_by=self.integration,
            provider="gmail",
            provider_account="records-inbox",
        )
        CorrespondenceRecipient.objects.create(
            correspondence=correspondence,
            recipient_type=CorrespondenceRecipient.RecipientType.TO,
            email=self.requester.email,
            display_name=self.requester.name,
            requester=self.requester,
        )
        correspondence.status = Correspondence.Status.QUEUED
        correspondence.approved_subject = correspondence.draft_subject
        correspondence.approved_body = correspondence.draft_body
        correspondence.approved_by = self.integration
        correspondence.approved_at = self.received_at
        correspondence.queued_by = self.integration
        correspondence.queued_at = self.received_at
        correspondence.provider_thread_id = provider_thread_id
        correspondence.save()
        delivery = CorrespondenceDelivery.objects.create(
            correspondence=correspondence,
            status=CorrespondenceDelivery.Status.PENDING,
            provider="gmail",
            provider_account="records-inbox",
            idempotency_key=f"dsn-delivery:{self.sequence}",
            queued_at=self.received_at,
        )
        return correspondence, delivery

    def receive(self, payload, **overrides):
        self.sequence += 1
        values = {
            "raw_message": payload,
            "provider": "gmail",
            "provider_account": "records-inbox",
            "provider_message_id": f"gmail-dsn-{self.sequence}",
            "provider_thread_id": f"gmail-dsn-thread-{self.sequence}",
            "envelope_sender": "mailer-daemon@example.org",
            "envelope_recipients": ["records@prr.psd401.net"],
            "received_at": self.received_at,
            "idempotency_key": f"gmail:dsn:{self.sequence}",
            "actor": self.integration,
            "ip_address": "192.0.2.60",
        }
        values.update(overrides)
        if "credential" not in overrides:
            values["credential"] = Token.objects.get_or_create(
                user=values["actor"]
            )[0]
        return services.receive_inbound_message(**values)

    @patch("apps.secure_files.store_quarantined_object")
    def test_exact_dsn_is_append_only_evidence_without_legal_state_mutation(
        self,
        _store,
    ):
        outbound, delivery = self.make_pending_delivery()
        request_status = self.request.status
        outbound_version = outbound.version
        delivery_version = delivery.version
        event_count = StatusEvent.objects.filter(request=self.request).count()

        result = self.receive(
            delivery_status_message(outbound.rfc_message_id),
        )

        receipt = result.receipt
        inbound = receipt.matched_correspondence
        self.assertEqual(result.outcome, "matched")
        self.assertEqual(
            receipt.matching_status,
            InboundCorrespondenceReceipt.MatchingStatus.MATCHED,
        )
        self.assertEqual(
            receipt.match_method,
            InboundCorrespondenceReceipt.MatchMethod.DELIVERY_STATUS,
        )
        self.assertEqual(receipt.matched_request, self.request)
        self.assertEqual(receipt.matched_delivery, delivery)
        self.assertEqual(
            receipt.delivery_status_original_message_id,
            outbound.rfc_message_id,
        )
        self.assertEqual(
            inbound.kind,
            Correspondence.Kind.DELIVERY_STATUS_NOTIFICATION,
        )
        self.assertEqual(inbound.status, Correspondence.Status.RECEIVED)
        self.assertEqual(inbound.request, self.request)
        self.assertEqual(inbound.received_body, "")
        self.assertFalse(inbound.attachments.exists())

        notification = CorrespondenceDeliveryNotification.objects.get(
            inbound_receipt=receipt
        )
        self.assertEqual(notification.delivery, delivery)
        self.assertEqual(notification.recipient_index, 1)
        self.assertEqual(
            notification.recipient.correspondence_id,
            outbound.pk,
        )
        self.assertEqual(
            notification.recipient.email,
            self.requester.email,
        )
        self.assertEqual(
            notification.recipient_match_status,
            CorrespondenceDeliveryNotification.RecipientMatchStatus.EXACT,
        )
        self.assertEqual(
            notification.action,
            CorrespondenceDeliveryNotification.Action.FAILED,
        )
        self.assertEqual(notification.enhanced_status, "5.1.1")

        self.request.refresh_from_db()
        outbound.refresh_from_db()
        delivery.refresh_from_db()
        self.assertEqual(self.request.status, request_status)
        self.assertEqual(outbound.status, Correspondence.Status.QUEUED)
        self.assertEqual(outbound.version, outbound_version)
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.PENDING)
        self.assertEqual(delivery.version, delivery_version)
        self.assertEqual(delivery.attempts.count(), 0)
        self.assertEqual(
            StatusEvent.objects.filter(request=self.request).count(),
            event_count,
        )

        audit_payload = json.dumps(
            list(
                AuditLog.objects.filter(
                    action__startswith="correspondence."
                ).values("action", "target", "detail")
            ),
            default=str,
        )
        self.assertNotIn("sensitive.requester@example.org", audit_payload)
        self.assertNotIn("private provider diagnostic prose", audit_payload)

    @patch("apps.secure_files.store_quarantined_object")
    def test_mislabeled_automated_report_is_quarantined_not_a_clarification(
        self,
        _store,
    ):
        clarification = Correspondence.objects.create(
            request=self.request,
            direction=Correspondence.Direction.OUTBOUND,
            kind=Correspondence.Kind.CLARIFICATION_REQUEST,
            status=Correspondence.Status.SENT,
            draft_subject="Clarification",
            draft_body="Please clarify the request.",
            approved_subject="Clarification",
            approved_body="Please clarify the request.",
            created_by=self.officer,
            approved_by=self.officer,
            queued_by=self.officer,
            approved_at=self.received_at,
            queued_at=self.received_at,
            sent_at=self.received_at,
        )
        cycle = self.open_clarification_fixture(clarification)
        payload = delivery_status_message(
            clarification.rfc_message_id,
            outer_in_reply_to=clarification.rfc_message_id,
        ).replace(
            b"report-type=delivery-status",
            b"report-type=disposition-notification",
        )

        result = self.receive(payload)

        result.receipt.refresh_from_db()
        cycle.refresh_from_db()
        self.assertEqual(result.outcome, "quarantined")
        self.assertEqual(
            result.receipt.parsing_status,
            InboundCorrespondenceReceipt.ParsingStatus.QUARANTINED,
        )
        self.assertEqual(
            result.receipt.quarantine_reason,
            "malformed_message",
        )
        self.assertIsNone(result.receipt.matched_correspondence_id)
        self.assertIsNone(cycle.inbound_response_correspondence_id)
        self.assertIsNone(cycle.responded_at)

    @patch("apps.secure_files.store_quarantined_object")
    def test_recognized_dsn_never_falls_back_to_generic_matching(
        self,
        _store,
    ):
        outbound, delivery = self.make_pending_delivery(
            provider_thread_id="conflicting-thread"
        )
        payload = delivery_status_message(
            "<unknown-application-message@mail.prr.test>",
            outer_subject=f"Re: {self.request.number}",
            outer_in_reply_to=outbound.rfc_message_id,
        )

        result = self.receive(
            payload,
            provider_thread_id="conflicting-thread",
            envelope_recipients=[
                (
                    "records+"
                    f"{outbound.reply_token}"
                    "@mail.prr.test"
                )
            ],
        )

        receipt = result.receipt
        inbound = receipt.matched_correspondence
        self.assertEqual(result.outcome, "unmatched")
        self.assertEqual(
            receipt.matching_status,
            InboundCorrespondenceReceipt.MatchingStatus.UNMATCHED,
        )
        self.assertEqual(receipt.match_method, "")
        self.assertIsNone(receipt.matched_request)
        self.assertIsNone(receipt.matched_delivery)
        self.assertEqual(
            receipt.delivery_status_original_message_id,
            "<unknown-application-message@mail.prr.test>",
        )
        self.assertEqual(
            inbound.kind,
            Correspondence.Kind.DELIVERY_STATUS_NOTIFICATION,
        )
        self.assertEqual(inbound.status, Correspondence.Status.UNMATCHED)
        self.assertIsNone(inbound.request)
        self.assertFalse(
            CorrespondenceDeliveryNotification.objects.filter(
                inbound_receipt=receipt
            ).exists()
        )
        delivery.refresh_from_db()
        outbound.refresh_from_db()
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.PENDING)
        self.assertEqual(outbound.status, Correspondence.Status.QUEUED)

    @patch("apps.secure_files.store_quarantined_object")
    def test_exact_replay_does_not_duplicate_delivery_notifications(
        self,
        _store,
    ):
        outbound, _delivery = self.make_pending_delivery()
        payload = delivery_status_message(outbound.rfc_message_id)
        transport = {
            "provider_message_id": "gmail-dsn-exact-replay",
            "provider_thread_id": "gmail-dsn-replay-thread",
            "idempotency_key": "gmail:dsn:exact-replay",
        }

        first = self.receive(payload, **transport)
        second = self.receive(payload, **transport)

        self.assertFalse(first.deduplicated)
        self.assertTrue(second.deduplicated)
        self.assertEqual(first.receipt_id, second.receipt_id)
        self.assertEqual(
            CorrespondenceDeliveryNotification.objects.filter(
                inbound_receipt_id=first.receipt_id
            ).count(),
            1,
        )

    @patch("apps.secure_files.store_quarantined_object")
    def test_dsn_cannot_use_generic_human_matching_or_clarification_workflows(
        self,
        _store,
    ):
        outbound, _delivery = self.make_pending_delivery()
        unmatched = self.receive(
            delivery_status_message(
                "<unknown-human-match@mail.prr.test>"
            )
        ).receipt
        with self.assertRaises(ValidationError):
            inbound.match_inbound_receipt(
                unmatched,
                request=self.request,
                actor=self.officer,
                reason="The officer reviewed this delivery-status evidence.",
                expected_version=unmatched.version,
            )

        matched = self.receive(
            delivery_status_message(outbound.rfc_message_id)
        ).receipt
        other_request = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(
                email="other-requester@example.org"
            ),
            description="Unrelated rematch destination.",
        )
        with self.assertRaises(ValidationError):
            inbound.rematch_inbound_receipt(
                matched,
                request=other_request,
                actor=self.officer,
                reason="The officer reviewed this delivery-status evidence.",
                expected_version=matched.version,
            )

        clarification = Correspondence.objects.create(
            request=self.request,
            direction=Correspondence.Direction.OUTBOUND,
            kind=Correspondence.Kind.CLARIFICATION_REQUEST,
            status=Correspondence.Status.SENT,
            draft_subject="Clarification",
            draft_body="Please clarify the request.",
            approved_subject="Clarification",
            approved_body="Please clarify the request.",
            created_by=self.officer,
            approved_by=self.officer,
            queued_by=self.officer,
            approved_at=self.received_at,
            queued_at=self.received_at,
            sent_at=self.received_at,
        )
        cycle = self.open_clarification_fixture(clarification)
        with self.assertRaises(ValidationError):
            inbound.associate_clarification_response(
                matched,
                clarification_cycle=cycle,
                actor=self.officer,
                reason="A DSN is transport evidence, not a requester response.",
                expected_version=matched.version,
            )

        matched.matched_correspondence.refresh_from_db()
        cycle.refresh_from_db()
        self.assertEqual(
            matched.matched_correspondence.kind,
            Correspondence.Kind.DELIVERY_STATUS_NOTIFICATION,
        )
        self.assertIsNone(cycle.inbound_response_correspondence_id)
        self.assertIsNone(cycle.responded_at)
