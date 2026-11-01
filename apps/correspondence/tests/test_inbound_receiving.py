# ABOUTME: Contract tests for durable inbound RFC822 receipt orchestration.
# ABOUTME: Raw evidence is retained before parsing, deduplicated, and audited safely.
import datetime as dt
import hashlib
import json
from io import BytesIO
from unittest.mock import ANY, patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token

from apps.audit.models import AuditLog
from apps.correspondence import services
from apps.correspondence.models import (
    Correspondence,
    CorrespondenceAttachment,
    InboundCorrespondenceReceipt,
    InboundQuarantineWriteReservation,
)

PLAIN_MESSAGE = (
    b"From: Jane Requester <jane.requester@example.org>\r\n"
    b"To: records@prr.psd401.net\r\n"
    b"Date: Thu, 23 Jul 2026 12:20:30 -0700\r\n"
    b"Message-ID: <requester-reply-1@example.org>\r\n"
    b"Subject: Re: records request\r\n"
    b"MIME-Version: 1.0\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"The responsive date range is 2024 through 2026.\r\n"
)

ATTACHMENT_MESSAGE = (
    b"From: Jane Requester <jane.requester@example.org>\r\n"
    b"To: records@prr.psd401.net\r\n"
    b"Date: Thu, 23 Jul 2026 12:20:30 -0700\r\n"
    b"Message-ID: <requester-reply-attachment@example.org>\r\n"
    b"Subject: Supporting document\r\n"
    b"MIME-Version: 1.0\r\n"
    b'Content-Type: multipart/mixed; boundary="evidence-boundary"\r\n'
    b"\r\n"
    b"--evidence-boundary\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"Please see the attached text file.\r\n"
    b"--evidence-boundary\r\n"
    b"Content-Type: text/plain; name=\"evidence.txt\"\r\n"
    b"Content-Disposition: attachment; filename=\"../evidence.txt\"\r\n"
    b"Content-Transfer-Encoding: base64\r\n"
    b"\r\n"
    b"c2FmZSByZXF1ZXN0ZXIgZXZpZGVuY2UK\r\n"
    b"--evidence-boundary--\r\n"
)

EMPTY_ATTACHMENT_MESSAGE = (
    b"From: Jane Requester <jane.requester@example.org>\r\n"
    b"To: records@prr.psd401.net\r\n"
    b"Date: Thu, 23 Jul 2026 12:20:30 -0700\r\n"
    b"Message-ID: <requester-empty-attachment@example.org>\r\n"
    b"Subject: Empty evidence placeholder\r\n"
    b"MIME-Version: 1.0\r\n"
    b'Content-Type: multipart/mixed; boundary="empty-boundary"\r\n'
    b"\r\n"
    b"--empty-boundary\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"An intentionally empty attachment follows.\r\n"
    b"--empty-boundary\r\n"
    b'Content-Type: application/octet-stream; name="empty.bin"\r\n'
    b'Content-Disposition: attachment; filename="empty.bin"\r\n'
    b"Content-Transfer-Encoding: base64\r\n"
    b"\r\n"
    b"\r\n"
    b"--empty-boundary--\r\n"
)

TWO_ATTACHMENT_MESSAGE = ATTACHMENT_MESSAGE.replace(
    b"--evidence-boundary--\r\n",
    (
        b"--evidence-boundary\r\n"
        b'Content-Type: text/plain; name="second.txt"\r\n'
        b'Content-Disposition: attachment; filename="second.txt"\r\n'
        b"Content-Transfer-Encoding: base64\r\n"
        b"\r\n"
        b"c2Vjb25kIGF0dGFjaG1lbnQK\r\n"
        b"--evidence-boundary--\r\n"
    ),
)


def permission(app_label, codename):
    return Permission.objects.get(
        content_type__app_label=app_label,
        codename=codename,
    )


@override_settings(
    PRR_LOCAL_IMPLICIT_STAFF_ROLES=(),
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
class InboundReceivingContractTests(TestCase):
    def setUp(self):
        self.integration = get_user_model().objects.create_user(
            "automation-correspondence-inbound",
            password=None,
        )
        self.integration.user_permissions.add(
            permission("correspondence", "submit_inbound_correspondence")
        )
        self.received_at = timezone.now()
        self._sequence = 0

    def raw_upload(self, payload=PLAIN_MESSAGE):
        return SimpleUploadedFile(
            "original.eml",
            payload,
            content_type="message/rfc822",
        )

    def receive(self, payload=PLAIN_MESSAGE, **overrides):
        self._sequence += 1
        values = {
            "raw_message": self.raw_upload(payload),
            "provider": "gmail",
            "provider_account": "default",
            "provider_message_id": f"gmail-inbound-{self._sequence}",
            "provider_thread_id": f"gmail-thread-{self._sequence}",
            "envelope_sender": "jane.requester@example.org",
            "envelope_recipients": ["records@prr.psd401.net"],
            "received_at": self.received_at,
            "idempotency_key": f"gmail:inbound:{self._sequence}",
            "actor": self.integration,
            "ip_address": "192.0.2.25",
        }
        values.update(overrides)
        if "credential" not in overrides:
            values["credential"] = Token.objects.get_or_create(
                user=values["actor"]
            )[0]
        return services.receive_inbound_message(**values)

    @patch("apps.secure_files.store_quarantined_object")
    def test_raw_evidence_is_stored_before_hostile_mime_is_parsed(self, store):
        observed = {}

        def assert_stored_first(*_args, **_kwargs):
            observed["stored"] = True

        def parse_after_store(*_args, **_kwargs):
            self.assertTrue(observed.get("stored"))
            from apps.correspondence.mime import parse_inbound_message

            return parse_inbound_message(PLAIN_MESSAGE)

        store.side_effect = assert_stored_first
        with patch(
            "apps.correspondence.receiving.parse_inbound_message",
            side_effect=parse_after_store,
        ):
            result = self.receive()

        receipt = result.receipt
        self.assertEqual(receipt.parsing_status, "parsed")
        self.assertFalse(receipt.quarantined)
        self.assertIsNotNone(receipt.completed_at)
        storage_key, source = store.call_args.args[:2]
        self.assertEqual(storage_key, receipt.raw_storage_key)
        self.assertIsInstance(source, (BytesIO, SimpleUploadedFile))
        self.assertEqual(
            store.call_args.kwargs["content_length"],
            receipt.raw_message_size,
        )
        self.assertEqual(store.call_args.kwargs["sha256"], receipt.raw_message_sha256)
        reservation = InboundQuarantineWriteReservation.objects.get(
            storage_key=receipt.raw_storage_key
        )
        self.assertEqual(reservation.kind, "raw_message")
        self.assertEqual(reservation.size, len(PLAIN_MESSAGE))
        self.assertEqual(reservation.sha256, receipt.raw_message_sha256)
        self.assertIsNotNone(reservation.stored_at)
        self.assertEqual(reservation.adopted_receipt, receipt)
        self.assertIsNotNone(reservation.adopted_at)

    @patch(
        "apps.correspondence.tasks.cleanup_inbound_quarantine_write.delay"
    )
    @patch("apps.secure_files.store_quarantined_object")
    def test_stale_sweep_cannot_claim_a_live_reservation_during_parse(
        self,
        store,
        cleanup_delay,
    ):
        from apps.correspondence import tasks
        from apps.correspondence.mime import parse_inbound_message

        observed = {}

        def parse_while_sweep_runs(*args, **kwargs):
            with patch(
                "apps.correspondence.tasks.timezone.now",
                return_value=timezone.now() + dt.timedelta(minutes=20),
            ):
                observed["queued"] = (
                    tasks.requeue_stale_inbound_quarantine_writes(limit=10)
                )
            return parse_inbound_message(*args, **kwargs)

        with patch(
            "apps.correspondence.receiving.parse_inbound_message",
            side_effect=parse_while_sweep_runs,
        ):
            result = self.receive(
                provider_message_id="gmail-live-parse",
                provider_thread_id="thread-live-parse",
                idempotency_key="gmail:live-parse",
            )

        self.assertEqual(observed["queued"], 0)
        cleanup_delay.assert_not_called()
        reservation = InboundQuarantineWriteReservation.objects.get(
            adopted_receipt=result.receipt
        )
        self.assertIsNone(reservation.processing_expires_at)

    @patch("apps.secure_files.delete_quarantined_object")
    @patch("apps.secure_files.store_quarantined_object")
    def test_cleanup_completion_during_parse_returns_retryable_in_progress(
        self,
        store,
        delete_object,
    ):
        from apps.correspondence.mime import parse_inbound_message
        from apps.correspondence.tasks import (
            cleanup_inbound_quarantine_write,
        )

        def parse_after_forced_expiry(*args, **kwargs):
            reservation = InboundQuarantineWriteReservation.objects.get(
                kind="raw_message"
            )
            claimed = InboundQuarantineWriteReservation.objects.claim_due(
                reservation_id=reservation.pk,
                now=timezone.now() + dt.timedelta(hours=2),
                stale_before=timezone.now() + dt.timedelta(hours=1),
                lease_expires_at=(
                    timezone.now() + dt.timedelta(hours=2, minutes=15)
                ),
            )
            self.assertIsNotNone(claimed)
            self.assertEqual(
                cleanup_inbound_quarantine_write.run(
                    str(claimed.pk),
                    str(claimed.lease_token),
                ),
                "deleted",
            )
            return parse_inbound_message(*args, **kwargs)

        with (
            patch(
                "apps.correspondence.receiving.parse_inbound_message",
                side_effect=parse_after_forced_expiry,
            ),
            self.assertRaises(services.InboundSubmissionInProgress),
        ):
            self.receive(
                provider_message_id="gmail-completed-during-parse",
                provider_thread_id="thread-completed-during-parse",
                idempotency_key="gmail:completed-during-parse",
            )

        store.assert_called_once()
        delete_object.assert_called_once()
        self.assertFalse(
            InboundCorrespondenceReceipt.objects.filter(
                provider_message_id="gmail-completed-during-parse"
            ).exists()
        )

    @patch("apps.secure_files.store_quarantined_object")
    def test_parsed_message_creates_immutable_correspondence_and_pii_free_audit(
        self,
        store,
    ):
        result = self.receive()

        receipt = result.receipt
        correspondence = Correspondence.objects.get(pk=result.correspondence_id)
        self.assertEqual(receipt.matched_correspondence, correspondence)
        self.assertEqual(correspondence.direction, Correspondence.Direction.INBOUND)
        self.assertEqual(correspondence.status, Correspondence.Status.UNMATCHED)
        self.assertEqual(correspondence.received_subject, "Re: records request")
        self.assertIn("2024 through 2026", correspondence.received_body)
        self.assertEqual(
            correspondence.received_sender,
            "Jane Requester <jane.requester@example.org>",
        )
        self.assertEqual(
            correspondence.received_recipients,
            ["records@prr.psd401.net"],
        )
        self.assertEqual(
            correspondence.received_headers["Message-ID"],
            "<requester-reply-1@example.org>",
        )
        self.assertEqual(receipt.ip_address, "192.0.2.25")

        audit_row = AuditLog.objects.get(action="correspondence.inbound_received")
        serialized = json.dumps(audit_row.detail, sort_keys=True)
        self.assertEqual(audit_row.actor, self.integration)
        self.assertEqual(audit_row.ip_address, "192.0.2.25")
        self.assertNotIn("jane.requester@example.org", serialized)
        self.assertNotIn("2024 through 2026", serialized)
        self.assertNotIn("Re: records request", serialized)
        self.assertNotIn(receipt.raw_storage_key, serialized)
        store.assert_called_once()

    @patch("apps.secure_files.store_quarantined_object")
    def test_envelope_recipient_uniqueness_is_exact_and_preserves_case(
        self,
        store,
    ):
        recipients = [
            "records@prr.psd401.net",
            "Records@prr.psd401.net",
        ]

        result = self.receive(envelope_recipients=recipients)

        self.assertEqual(result.receipt.envelope_recipients, recipients)
        store.assert_called_once()

    @patch("apps.secure_files.store_quarantined_object")
    def test_exact_idempotency_replay_returns_the_completed_receipt_without_io(
        self,
        store,
    ):
        first = self.receive(
            provider_message_id="gmail-replay-1",
            provider_thread_id="thread-replay-1",
            idempotency_key="gmail:replay:1",
        )
        second = self.receive(
            provider_message_id="gmail-replay-1",
            provider_thread_id="thread-replay-1",
            idempotency_key="gmail:replay:1",
        )

        self.assertFalse(first.deduplicated)
        self.assertTrue(second.deduplicated)
        self.assertEqual(second.receipt_id, first.receipt_id)
        self.assertEqual(second.correspondence_id, first.correspondence_id)
        self.assertEqual(store.call_count, 1)
        self.assertEqual(
            AuditLog.objects.filter(
                action="correspondence.inbound_received"
            ).count(),
            1,
        )
        self.assertEqual(
            InboundQuarantineWriteReservation.objects.filter(
                kind="raw_message"
            ).count(),
            1,
        )

    @patch("apps.secure_files.store_quarantined_object")
    def test_idempotency_key_reuse_with_different_evidence_is_rejected(
        self,
        store,
    ):
        self.receive(
            provider_message_id="gmail-key-conflict-original",
            provider_thread_id="thread-key-conflict",
            idempotency_key="gmail:key-conflict",
        )

        with self.assertRaises(services.InboundIdempotencyConflict):
            self.receive(
                PLAIN_MESSAGE.replace(b"2026", b"2025"),
                provider_message_id="gmail-key-conflict-changed",
                provider_thread_id="thread-key-conflict",
                idempotency_key="gmail:key-conflict",
            )

        self.assertEqual(store.call_count, 1)

    @patch("apps.secure_files.store_quarantined_object")
    def test_provider_message_reuse_with_different_evidence_is_rejected(
        self,
        store,
    ):
        self.receive(
            provider_message_id="gmail-provider-conflict",
            provider_thread_id="thread-provider-conflict",
            idempotency_key="gmail:provider-conflict:1",
        )

        with self.assertRaises(services.InboundProviderMessageConflict):
            self.receive(
                PLAIN_MESSAGE.replace(b"2026", b"2025"),
                provider_message_id="gmail-provider-conflict",
                provider_thread_id="thread-provider-conflict",
                idempotency_key="gmail:provider-conflict:2",
            )

        self.assertEqual(store.call_count, 1)

    @patch("apps.secure_files.store_quarantined_object")
    def test_same_provider_message_and_evidence_deduplicates_across_transport_keys(
        self,
        store,
    ):
        first = self.receive(
            provider_message_id="gmail-provider-replay",
            provider_thread_id="thread-provider-replay",
            idempotency_key="gmail:provider-replay:1",
        )
        replay = self.receive(
            provider_message_id="gmail-provider-replay",
            provider_thread_id="thread-provider-replay",
            idempotency_key="gmail:provider-replay:2",
        )

        self.assertTrue(replay.deduplicated)
        self.assertEqual(replay.receipt_id, first.receipt_id)
        self.assertEqual(store.call_count, 1)

    @patch("apps.secure_files.store_quarantined_object")
    def test_malformed_message_is_retained_and_quarantined_with_safe_reason(
        self,
        store,
    ):
        secret = b"requester-secret-that-must-not-be-stored-as-an-error"
        result = self.receive(
            b"not an RFC822 message\r\n" + secret,
            provider_message_id="gmail-malformed",
            provider_thread_id="thread-malformed",
            idempotency_key="gmail:malformed",
        )

        receipt = result.receipt
        self.assertEqual(result.outcome, "quarantined")
        self.assertEqual(receipt.parsing_status, "quarantined")
        self.assertEqual(receipt.matching_status, "quarantined")
        self.assertTrue(receipt.quarantined)
        self.assertIsNone(result.correspondence_id)
        self.assertNotIn(secret.decode(), receipt.parsing_error)
        self.assertLessEqual(len(receipt.parsing_error), 1000)
        self.assertIsNotNone(receipt.completed_at)
        store.assert_called_once()

    @patch(
        "apps.correspondence.tasks.scan_correspondence_attachment.delay"
    )
    @patch("apps.secure_files.store_quarantined_object")
    def test_attachments_are_persisted_quarantined_and_scanned_after_commit(
        self,
        store,
        scan_delay,
    ):
        with self.captureOnCommitCallbacks(execute=True):
            result = self.receive(
                ATTACHMENT_MESSAGE,
                provider_message_id="gmail-attachment",
                provider_thread_id="thread-attachment",
                idempotency_key="gmail:attachment",
            )

        attachment = CorrespondenceAttachment.objects.get(
            correspondence_id=result.correspondence_id
        )
        self.assertEqual(attachment.inbound_receipt, result.receipt)
        self.assertEqual(attachment.original_filename, "evidence.txt")
        self.assertEqual(attachment.content_type, "text/plain")
        self.assertTrue(attachment.quarantined)
        self.assertEqual(attachment.scan_status, "pending")
        self.assertEqual(store.call_count, 2)
        attachment_store = store.call_args_list[1]
        self.assertEqual(attachment_store.args[0], attachment.storage_key)
        self.assertEqual(
            attachment_store.kwargs["content_length"],
            attachment.size,
        )
        self.assertEqual(
            attachment_store.kwargs["sha256"],
            attachment.sha256,
        )
        reservation = InboundQuarantineWriteReservation.objects.get(
            storage_key=attachment.storage_key
        )
        self.assertEqual(reservation.kind, "attachment")
        self.assertEqual(reservation.size, attachment.size)
        self.assertEqual(reservation.sha256, attachment.sha256)
        self.assertEqual(reservation.adopted_attachment, attachment)
        self.assertIsNotNone(reservation.adopted_at)
        scan_delay.assert_called_once_with(str(attachment.id))

    @patch("apps.secure_files.store_quarantined_object")
    def test_valid_zero_byte_attachment_is_reserved_and_received(self, store):
        result = self.receive(
            EMPTY_ATTACHMENT_MESSAGE,
            provider_message_id="gmail-empty-attachment",
            provider_thread_id="thread-empty-attachment",
            idempotency_key="gmail:empty-attachment",
        )

        attachment = CorrespondenceAttachment.objects.get(
            correspondence_id=result.correspondence_id
        )
        reservation = InboundQuarantineWriteReservation.objects.get(
            adopted_attachment=attachment
        )
        self.assertEqual(attachment.size, 0)
        self.assertEqual(
            attachment.sha256,
            hashlib.sha256(b"").hexdigest(),
        )
        self.assertEqual(reservation.size, 0)
        self.assertEqual(store.call_count, 2)

    @patch("apps.secure_files.store_quarantined_object")
    def test_crash_reservation_is_reused_after_integration_identity_rotation(
        self,
        store,
    ):
        with patch(
            "apps.correspondence.receiving._receive_reserved_atomic",
            side_effect=SystemExit("simulated abrupt worker loss"),
        ):
            with self.assertRaises(SystemExit):
                self.receive(
                    provider_message_id="gmail-crash-reservation",
                    provider_thread_id="thread-crash-reservation",
                    idempotency_key="gmail:crash-reservation",
                )

        reservation = InboundQuarantineWriteReservation.objects.get(
            kind="raw_message"
        )
        self.assertIsNone(reservation.adopted_at)
        self.assertIsNone(reservation.completed_at)
        self.assertIsNotNone(reservation.stored_at)
        self.assertFalse(
            InboundCorrespondenceReceipt.objects.filter(
                provider_message_id="gmail-crash-reservation"
            ).exists()
        )
        store.assert_called_once_with(
            reservation.storage_key,
            ANY,
            content_length=reservation.size,
            sha256=reservation.sha256,
        )

        replacement = get_user_model().objects.create_user(
            "automation-correspondence-inbound-replacement",
            password=None,
        )
        replacement.user_permissions.add(
            permission("correspondence", "submit_inbound_correspondence")
        )
        result = self.receive(
            provider_message_id="gmail-crash-reservation",
            provider_thread_id="thread-crash-reservation",
            idempotency_key="gmail:crash-reservation",
            actor=replacement,
        )

        reservation.refresh_from_db()
        self.assertEqual(reservation.adopted_receipt, result.receipt)
        self.assertEqual(result.receipt.submitted_by, replacement)
        self.assertEqual(store.call_count, 1)

    @patch("apps.secure_files.store_quarantined_object")
    def test_replacement_identity_alias_survives_crash_and_rejects_key_reuse(
        self,
        store,
    ):
        replacement = get_user_model().objects.create_user(
            "automation-correspondence-alias-replacement",
            password=None,
        )
        replacement.user_permissions.add(
            permission("correspondence", "submit_inbound_correspondence")
        )

        with patch(
            "apps.correspondence.receiving._receive_reserved_atomic",
            side_effect=SystemExit("crash before receipt commit"),
        ):
            with self.assertRaises(SystemExit):
                self.receive(
                    provider_message_id="gmail-alias-crash",
                    provider_thread_id="thread-alias-crash",
                    idempotency_key="gmail:alias:original",
                )
            with self.assertRaises(SystemExit):
                self.receive(
                    provider_message_id="gmail-alias-crash",
                    provider_thread_id="thread-alias-crash",
                    idempotency_key="gmail:alias:replacement",
                    actor=replacement,
                )

        with self.assertRaises(services.InboundIdempotencyConflict):
            self.receive(
                PLAIN_MESSAGE + b"changed evidence\r\n",
                provider_message_id="gmail-alias-changed",
                provider_thread_id="thread-alias-changed",
                idempotency_key="gmail:alias:replacement",
                actor=replacement,
            )

        self.assertEqual(store.call_count, 1)

    @patch("apps.secure_files.store_quarantined_object")
    def test_successful_duplicate_claims_replacement_identity_alias(
        self,
        store,
    ):
        first = self.receive(
            provider_message_id="gmail-successful-alias",
            provider_thread_id="thread-successful-alias",
            idempotency_key="gmail:successful-alias:original",
        )
        replacement = get_user_model().objects.create_user(
            "automation-correspondence-successful-alias-replacement",
            password=None,
        )
        replacement.user_permissions.add(
            permission("correspondence", "submit_inbound_correspondence")
        )
        duplicate = self.receive(
            provider_message_id="gmail-successful-alias",
            provider_thread_id="thread-successful-alias",
            idempotency_key="gmail:successful-alias:replacement",
            actor=replacement,
        )

        self.assertTrue(duplicate.deduplicated)
        self.assertEqual(duplicate.receipt_id, first.receipt_id)
        with self.assertRaises(services.InboundIdempotencyConflict):
            self.receive(
                PLAIN_MESSAGE + b"changed after successful duplicate\r\n",
                provider_message_id="gmail-successful-alias-changed",
                provider_thread_id="thread-successful-alias-changed",
                idempotency_key="gmail:successful-alias:replacement",
                actor=replacement,
            )
        self.assertEqual(store.call_count, 1)

    @patch("apps.secure_files.store_quarantined_object")
    def test_exact_retry_defers_while_stale_cleanup_owns_raw_reservation(
        self,
        store,
    ):
        with patch(
            "apps.correspondence.receiving._receive_reserved_atomic",
            side_effect=SystemExit("crash before receipt commit"),
        ):
            with self.assertRaises(SystemExit):
                self.receive(
                    provider_message_id="gmail-cleanup-race-raw",
                    provider_thread_id="thread-cleanup-race-raw",
                    idempotency_key="gmail:cleanup-race:raw",
                )
        reservation = InboundQuarantineWriteReservation.objects.get(
            kind="raw_message"
        )
        claimed = InboundQuarantineWriteReservation.objects.claim_due(
            reservation_id=reservation.pk,
            now=timezone.now() + dt.timedelta(hours=2),
            stale_before=timezone.now() + dt.timedelta(hours=1),
            lease_expires_at=timezone.now() + dt.timedelta(hours=2, minutes=15),
        )
        self.assertIsNotNone(claimed)

        with self.assertRaises(services.InboundSubmissionInProgress):
            self.receive(
                provider_message_id="gmail-cleanup-race-raw",
                provider_thread_id="thread-cleanup-race-raw",
                idempotency_key="gmail:cleanup-race:raw",
            )
        self.assertEqual(store.call_count, 1)

    @patch(
        "apps.secure_files.store_quarantined_object",
        side_effect=RuntimeError("storage unavailable"),
    )
    def test_exact_retry_does_not_write_while_cleanup_owns_unstored_reservation(
        self,
        store,
    ):
        with self.assertRaises(services.InboundStorageUnavailable):
            self.receive(
                provider_message_id="gmail-cleanup-race-unstored",
                provider_thread_id="thread-cleanup-race-unstored",
                idempotency_key="gmail:cleanup-race:unstored",
            )
        reservation = InboundQuarantineWriteReservation.objects.get(
            kind="raw_message"
        )
        self.assertIsNone(reservation.stored_at)
        claimed = InboundQuarantineWriteReservation.objects.claim_due(
            reservation_id=reservation.pk,
            now=timezone.now() + dt.timedelta(hours=2),
            stale_before=timezone.now() + dt.timedelta(hours=1),
            lease_expires_at=timezone.now() + dt.timedelta(hours=2, minutes=15),
        )
        self.assertIsNotNone(claimed)
        store.side_effect = None

        with self.assertRaises(services.InboundSubmissionInProgress):
            self.receive(
                provider_message_id="gmail-cleanup-race-unstored",
                provider_thread_id="thread-cleanup-race-unstored",
                idempotency_key="gmail:cleanup-race:unstored",
            )
        self.assertEqual(store.call_count, 1)

    @patch("apps.secure_files.store_quarantined_object")
    def test_exact_retry_defers_if_cleanup_claimed_an_attachment_stage(
        self,
        store,
    ):
        with patch(
            "apps.correspondence.receiving._receive_reserved_atomic",
            side_effect=SystemExit("crash after attachment writes"),
        ):
            with self.assertRaises(SystemExit):
                self.receive(
                    TWO_ATTACHMENT_MESSAGE,
                    provider_message_id="gmail-cleanup-race-attachment",
                    provider_thread_id="thread-cleanup-race-attachment",
                    idempotency_key="gmail:cleanup-race:attachment",
                )
        reservation = (
            InboundQuarantineWriteReservation.objects.filter(
                kind="attachment"
            )
            .order_by("created_at")
            .first()
        )
        claimed = InboundQuarantineWriteReservation.objects.claim_due(
            reservation_id=reservation.pk,
            now=timezone.now() + dt.timedelta(hours=2),
            stale_before=timezone.now() + dt.timedelta(hours=1),
            lease_expires_at=timezone.now() + dt.timedelta(hours=2, minutes=15),
        )
        self.assertIsNotNone(claimed)

        with self.assertRaises(services.InboundSubmissionInProgress):
            self.receive(
                TWO_ATTACHMENT_MESSAGE,
                provider_message_id="gmail-cleanup-race-attachment",
                provider_thread_id="thread-cleanup-race-attachment",
                idempotency_key="gmail:cleanup-race:attachment",
            )
        self.assertEqual(store.call_count, 3)

    @patch("apps.secure_files.delete_quarantined_object")
    @patch(
        "apps.correspondence.tasks.scan_correspondence_attachment.delay",
        side_effect=RuntimeError("broker unavailable"),
    )
    @patch("apps.secure_files.store_quarantined_object")
    @override_settings(PRR_CORRESPONDENCE_ATTACHMENT_SCAN_STALE_SECONDS=60)
    def test_scan_enqueue_failure_is_recovered_from_committed_evidence(
        self,
        store,
        scan_delay,
        delete_object,
    ):
        with self.captureOnCommitCallbacks(execute=True):
            result = self.receive(
                ATTACHMENT_MESSAGE,
                provider_message_id="gmail-attachment-broker-outage",
                provider_thread_id="thread-attachment-broker-outage",
                idempotency_key="gmail:attachment-broker-outage",
            )

        self.assertTrue(
            InboundCorrespondenceReceipt.objects.filter(
                pk=result.receipt_id
            ).exists()
        )
        self.assertTrue(
            CorrespondenceAttachment.objects.filter(
                correspondence_id=result.correspondence_id,
                scan_status=CorrespondenceAttachment.ScanStatus.PENDING,
                quarantined=True,
            ).exists()
        )
        self.assertEqual(store.call_count, 2)
        scan_delay.assert_called_once()
        delete_object.assert_not_called()

        attachment = CorrespondenceAttachment.objects.get(
            correspondence_id=result.correspondence_id
        )
        scan_delay.side_effect = None

        from apps.correspondence.tasks import (
            requeue_stale_correspondence_attachment_scans,
        )

        with patch(
            "apps.correspondence.tasks.timezone.now",
            return_value=timezone.now() + dt.timedelta(minutes=2),
        ):
            recovered = requeue_stale_correspondence_attachment_scans()

        self.assertEqual(recovered, 1)
        self.assertEqual(scan_delay.call_count, 2)
        scan_delay.assert_called_with(str(attachment.id))

    @patch("apps.secure_files.delete_quarantined_object")
    @patch(
        "apps.secure_files.store_quarantined_object",
        side_effect=RuntimeError("provider secret: storage credentials"),
    )
    def test_storage_failure_rolls_back_evidence_and_raises_sanitized_error(
        self,
        store,
        delete_object,
    ):
        with self.assertRaises(services.InboundStorageUnavailable) as raised:
            self.receive(
                provider_message_id="gmail-storage-failure",
                provider_thread_id="thread-storage-failure",
                idempotency_key="gmail:storage-failure",
            )

        self.assertNotIn("provider secret", str(raised.exception))
        self.assertFalse(
            InboundCorrespondenceReceipt.objects.filter(
                provider_message_id="gmail-storage-failure"
            ).exists()
        )
        self.assertFalse(
            AuditLog.objects.filter(
                action="correspondence.inbound_received"
            ).exists()
        )
        store.assert_called_once()
        delete_object.assert_not_called()
        reservation = InboundQuarantineWriteReservation.objects.get(
            kind="raw_message"
        )
        self.assertIsNone(reservation.adopted_at)
        self.assertIsNone(reservation.completed_at)
        self.assertIsNone(reservation.stored_at)

    @patch("apps.secure_files.delete_quarantined_object")
    @patch("apps.secure_files.store_quarantined_object")
    def test_failing_exact_replay_cannot_delete_an_adopted_shared_key(
        self,
        store,
        delete_object,
    ):
        first = self.receive(
            provider_message_id="gmail-shared-reservation",
            provider_thread_id="thread-shared-reservation",
            idempotency_key="gmail:shared-reservation",
        )
        reservation = InboundQuarantineWriteReservation.objects.get(
            adopted_receipt=first.receipt
        )

        with (
            patch(
                "apps.correspondence.receiving._resolve_existing",
                return_value=None,
            ),
            patch(
                "apps.correspondence.receiving._receive_reserved_atomic",
                side_effect=RuntimeError("losing replay failed"),
            ),
            self.assertRaises(RuntimeError),
        ):
            self.receive(
                provider_message_id="gmail-shared-reservation",
                provider_thread_id="thread-shared-reservation",
                idempotency_key="gmail:shared-reservation",
            )

        delete_object.assert_not_called()
        reservation.refresh_from_db()
        self.assertEqual(reservation.adopted_receipt, first.receipt)
        self.assertIsNotNone(reservation.adopted_at)

    @patch("apps.secure_files.store_quarantined_object")
    def test_service_rechecks_exact_inbound_identity(self, store):
        self.integration.user_permissions.add(
            permission("correspondence", "deliver_outbound_correspondence")
        )

        with self.assertRaises(PermissionDenied):
            self.receive()

        self.assertFalse(InboundCorrespondenceReceipt.objects.exists())
        store.assert_not_called()

    @patch("apps.secure_files.store_quarantined_object")
    def test_identity_revoked_during_parse_cannot_adopt_domain_evidence(
        self,
        store,
    ):
        from apps.correspondence.mime import parse_inbound_message

        def revoke_before_domain_commit(*args, **kwargs):
            get_user_model().objects.filter(pk=self.integration.pk).update(
                is_active=False
            )
            return parse_inbound_message(*args, **kwargs)

        with (
            patch(
                "apps.correspondence.receiving.parse_inbound_message",
                side_effect=revoke_before_domain_commit,
            ),
            self.assertRaises(PermissionDenied),
        ):
            self.receive()

        store.assert_called_once()
        self.assertFalse(InboundCorrespondenceReceipt.objects.exists())
        self.assertFalse(Correspondence.objects.exists())
        self.assertFalse(
            AuditLog.objects.filter(
                action="correspondence.inbound_received"
            ).exists()
        )
        reservation = InboundQuarantineWriteReservation.objects.get(
            kind="raw_message"
        )
        self.assertIsNone(reservation.adopted_at)
        self.assertIsNone(reservation.completed_at)

    @patch("apps.secure_files.store_quarantined_object")
    def test_presented_token_revoked_before_replay_transaction_is_rejected(
        self,
        store,
    ):
        evidence = {
            "provider_message_id": "gmail-inbound-replay-token-boundary",
            "provider_thread_id": "gmail-thread-replay-token-boundary",
            "idempotency_key": "gmail:inbound:replay-token-boundary",
        }
        first = self.receive(**evidence)
        credential = Token.objects.get_or_create(user=self.integration)[0]
        from apps.correspondence import receiving

        real_validate = receiving._validate_transport_metadata

        def revoke_after_preflight(*args, **kwargs):
            result = real_validate(*args, **kwargs)
            Token.objects.filter(pk=credential.pk).delete()
            return result

        with (
            patch(
                "apps.correspondence.receiving._validate_transport_metadata",
                side_effect=revoke_after_preflight,
            ),
            self.assertRaises(PermissionDenied),
        ):
            self.receive(credential=credential, **evidence)

        self.assertEqual(store.call_count, 1)
        self.assertEqual(InboundCorrespondenceReceipt.objects.count(), 1)
        self.assertEqual(
            InboundCorrespondenceReceipt.objects.get().pk,
            first.receipt.pk,
        )

    @patch("apps.secure_files.store_quarantined_object")
    def test_presented_token_revoked_during_parse_cannot_adopt_domain_evidence(
        self,
        store,
    ):
        from apps.correspondence.mime import parse_inbound_message

        credential = Token.objects.get_or_create(user=self.integration)[0]

        def revoke_before_domain_commit(*args, **kwargs):
            Token.objects.filter(pk=credential.pk).delete()
            return parse_inbound_message(*args, **kwargs)

        with (
            patch(
                "apps.correspondence.receiving.parse_inbound_message",
                side_effect=revoke_before_domain_commit,
            ),
            self.assertRaises(PermissionDenied),
        ):
            self.receive(credential=credential)

        store.assert_called_once()
        self.assertFalse(InboundCorrespondenceReceipt.objects.exists())
        self.assertFalse(Correspondence.objects.exists())
        self.assertFalse(
            AuditLog.objects.filter(
                action="correspondence.inbound_received"
            ).exists()
        )
        reservation = InboundQuarantineWriteReservation.objects.get(
            kind="raw_message"
        )
        self.assertIsNone(reservation.adopted_at)
        self.assertIsNone(reservation.completed_at)
