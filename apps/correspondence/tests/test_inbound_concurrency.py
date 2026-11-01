# ABOUTME: PostgreSQL concurrency coverage for inbound transport idempotency.
# ABOUTME: Separate connections prove one durable receipt wins each race.
import datetime as dt
import threading
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import OperationalError, close_old_connections, connection
from django.test import TransactionTestCase, override_settings
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
    InboundQuarantineWriteReservation,
    InboundQuarantineWriteReservationQuerySet,
)
from apps.records.models import PublicRecordsRequest, Requester, RequestStatus

PLAIN_MESSAGE = (
    b"From: Concurrent Requester <concurrent@example.org>\r\n"
    b"To: records@prr.psd401.net\r\n"
    b"Date: Thu, 23 Jul 2026 12:20:30 -0700\r\n"
    b"Message-ID: <concurrent-requester-reply@example.org>\r\n"
    b"Subject: Concurrent inbound evidence\r\n"
    b"MIME-Version: 1.0\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"One immutable inbound message.\r\n"
)


def delivery_status_message(original_message_id):
    return (
        b"From: Mailer Daemon <mailer-daemon@example.org>\r\n"
        b"To: records@prr.psd401.net\r\n"
        b"Date: Thu, 23 Jul 2026 12:20:30 -0700\r\n"
        b"Message-ID: <concurrent-dsn@example.org>\r\n"
        b"Subject: Delivery Status Notification\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: multipart/report; report-type=delivery-status; "
        b'boundary="concurrent-dsn"\r\n\r\n'
        b"--concurrent-dsn\r\n"
        b"Content-Type: text/plain\r\n\r\n"
        b"Provider delivery report.\r\n"
        b"--concurrent-dsn\r\n"
        b"Content-Type: message/delivery-status\r\n\r\n"
        b"Reporting-MTA: dns; mx.example.org\r\n\r\n"
        b"Final-Recipient: rfc822; concurrent@example.org\r\n"
        b"Action: failed\r\n"
        b"Status: 5.1.1\r\n"
        b"Diagnostic-Code: smtp; private diagnostic prose\r\n\r\n"
        b"--concurrent-dsn\r\n"
        b"Content-Type: text/rfc822-headers\r\n\r\n"
        b"Message-ID: "
        + original_message_id.encode("ascii")
        + b"\r\n\r\n"
        b"--concurrent-dsn--\r\n"
    )


@skipUnless(
    connection.vendor == "postgresql",
    "Inbound concurrency requires PostgreSQL row-level locks.",
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
    PRR_CORRESPONDENCE_MESSAGE_ID_DOMAIN="mail.prr.test",
    PRR_CORRESPONDENCE_REPLY_DOMAIN="mail.prr.test",
    PRR_CORRESPONDENCE_OUTBOUND_ENABLED=True,
)
class InboundReceivingConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.officer = get_user_model().objects.create_user(
            "concurrency-inbound-officer",
            "concurrency-inbound-officer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.integration = get_user_model().objects.create_user(
            "concurrency-inbound-transport",
            password=None,
        )
        self.integration.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="correspondence",
                codename="submit_inbound_correspondence",
            )
        )
        Token.objects.create(user=self.integration)
        self.outbound_integration = get_user_model().objects.create_user(
            "concurrency-outbound-transport",
            password=None,
        )
        self.outbound_integration.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="correspondence",
                codename="deliver_outbound_correspondence",
            )
        )
        self.outbound_token = Token.objects.create(
            user=self.outbound_integration,
        )
        self.received_at = timezone.now().replace(microsecond=0)

    def _queue_outbound(self):
        prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(
                name="Concurrent Requester",
                email="concurrent@example.org",
            ),
            description="Concurrent DSN evidence contract.",
        )
        draft = services.create_authored_draft(
            prr,
            kind=Correspondence.Kind.GENERAL,
            subject="Concurrent outbound correspondence",
            body="Approved content for the DSN concurrency contract.",
            actor=self.officer,
            recipients=[
                {
                    "recipient_type": (
                        CorrespondenceRecipient.RecipientType.TO
                    ),
                    "email": prr.requester.email,
                    "display_name": prr.requester.name,
                    "requester": prr.requester,
                }
            ],
        )
        draft.provider = "gmail"
        draft.provider_account = "records-inbox"
        draft.save(update_fields=["provider", "provider_account"])
        approved = services.approve_draft(
            draft,
            actor=self.officer,
            expected_version=draft.version,
        )
        with patch("apps.correspondence.tasks.enqueue_queued_delivery"):
            delivery = services.queue_approved(
                approved,
                actor=self.officer,
                expected_version=approved.version,
            )
        approved.refresh_from_db()
        return prr, approved, delivery

    def _callback(
        self,
        *,
        payload,
        provider_message_id,
        idempotency_key,
    ):
        def callback():
            actor = get_user_model().objects.get(pk=self.integration.pk)
            credential = Token.objects.get(user=actor)
            try:
                result = services.receive_inbound_message(
                    raw_message=BytesIO(payload),
                    provider="gmail",
                    provider_account="records-inbox",
                    provider_message_id=provider_message_id,
                    provider_thread_id="concurrent-thread",
                    envelope_sender="concurrent@example.org",
                    envelope_recipients=["records@prr.psd401.net"],
                    received_at=self.received_at,
                    idempotency_key=idempotency_key,
                    actor=actor,
                    credential=credential,
                    ip_address="192.0.2.44",
                )
            except services.InboundIdempotencyConflict:
                return ("idempotency_conflict", None)
            except services.InboundProviderMessageConflict:
                return ("provider_conflict", None)
            return ("recorded", result.deduplicated)

        return callback

    @staticmethod
    def _run_together(*callbacks):
        barrier = threading.Barrier(len(callbacks))

        def invoke(callback):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return callback()
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=len(callbacks)) as executor:
            futures = [executor.submit(invoke, callback) for callback in callbacks]
            return [future.result(timeout=20) for future in futures]

    def assert_one_durable_message(self, store):
        self.assertEqual(store.call_count, 1)
        self.assertEqual(InboundCorrespondenceReceipt.objects.count(), 1)
        self.assertEqual(
            Correspondence.objects.filter(
                direction=Correspondence.Direction.INBOUND
            ).count(),
            1,
        )
        self.assertEqual(
            AuditLog.objects.filter(
                action="correspondence.inbound_received"
            ).count(),
            1,
        )

    def _make_matched_clarification_candidate(self):
        source_request = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(
                name="Clarification Source Requester",
                email="clarification-source@example.org",
            ),
            description="Clarification source request for lock-order coverage.",
            status=RequestStatus.CLARIFICATION,
        )
        target_request = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(
                name="Clarification Target Requester",
                email="clarification-target@example.org",
            ),
            description="Clarification rematch target for lock-order coverage.",
        )
        now = timezone.now().replace(microsecond=0)
        opened_at = now - dt.timedelta(seconds=1)
        outbound = Correspondence.objects.create(
            request=source_request,
            direction=Correspondence.Direction.OUTBOUND,
            kind=Correspondence.Kind.CLARIFICATION_REQUEST,
            status=Correspondence.Status.SENT,
            draft_subject="Clarification requested",
            draft_body="Please clarify the requested scope.",
            approved_subject="Clarification requested",
            approved_body="Please clarify the requested scope.",
            created_by=self.officer,
            approved_by=self.officer,
            queued_by=self.officer,
            approved_at=opened_at,
            queued_at=opened_at,
            sent_at=opened_at,
            provider_message_id="clarification-lock-order-outbound",
        )
        cycle = ClarificationCycle.objects.create(
            request=source_request,
            outbound_correspondence=outbound,
            opened_at=opened_at,
        )
        correspondence = Correspondence.objects.create(
            request=source_request,
            direction=Correspondence.Direction.INBOUND,
            kind=Correspondence.Kind.GENERAL,
            status=Correspondence.Status.RECEIVED,
            received_subject="Clarification response candidate",
            received_body="This message provides the requested clarification.",
            received_at=now,
            matched_at=now,
            matched_by=self.officer,
            provider_message_id="clarification-lock-order-inbound",
        )
        receipt = InboundCorrespondenceReceipt.objects.create(
            provider="gmail",
            provider_account="records-inbox",
            provider_message_id="clarification-lock-order-receipt",
            received_at=now,
            envelope_sender="clarification-source@example.org",
            envelope_recipients=["records@prr.psd401.net"],
            parsing_status=InboundCorrespondenceReceipt.ParsingStatus.PARSED,
            matching_status=InboundCorrespondenceReceipt.MatchingStatus.MATCHED,
            match_method=InboundCorrespondenceReceipt.MatchMethod.MANUAL,
            matched_request=source_request,
            matched_correspondence=correspondence,
            matched_at=now,
            matched_by=self.officer,
            quarantined=False,
            raw_message_size=512,
            raw_message_sha256="5" * 64,
            payload_sha256="6" * 64,
            submitted_by=self.integration,
            idempotency_key="gmail:clarification:lock-order",
            completed_at=now,
        )
        return source_request, target_request, cycle, receipt

    def test_association_and_rematch_use_one_lock_order_without_deadlock(self):
        (
            _source_request,
            target_request,
            cycle,
            receipt,
        ) = self._make_matched_clarification_candidate()
        expected_version = receipt.version
        associate_reached_receipt = threading.Event()
        rematch_locked_receipt = threading.Event()
        operation_role = threading.local()
        original_locked_receipt = inbound._locked_receipt

        def synchronized_locked_receipt(candidate):
            if operation_role.name == "associate":
                associate_reached_receipt.set()
                rematch_locked_receipt.wait(timeout=0.5)
                return original_locked_receipt(candidate)
            self.assertTrue(associate_reached_receipt.wait(timeout=10))
            locked = original_locked_receipt(candidate)
            rematch_locked_receipt.set()
            return locked

        def associate():
            operation_role.name = "associate"
            actor = get_user_model().objects.get(pk=self.officer.pk)
            try:
                inbound.associate_clarification_response(
                    InboundCorrespondenceReceipt.objects.get(pk=receipt.pk),
                    clarification_cycle=ClarificationCycle.objects.get(pk=cycle.pk),
                    actor=actor,
                    reason=(
                        "The officer confirmed this message answers the open "
                        "clarification."
                    ),
                    expected_version=expected_version,
                )
            except OperationalError as exc:
                return ("database_error", str(exc))
            except services.VersionConflict:
                return ("version_conflict", None)
            return ("associated", None)

        def rematch():
            operation_role.name = "rematch"
            actor = get_user_model().objects.get(pk=self.officer.pk)
            try:
                inbound.rematch_inbound_receipt(
                    InboundCorrespondenceReceipt.objects.get(pk=receipt.pk),
                    request=PublicRecordsRequest.objects.get(pk=target_request.pk),
                    actor=actor,
                    reason=(
                        "The officer reviewed the message and selected a "
                        "different request."
                    ),
                    expected_version=expected_version,
                )
            except OperationalError as exc:
                return ("database_error", str(exc))
            except services.VersionConflict:
                return ("version_conflict", None)
            return ("rematched", None)

        with patch(
            "apps.correspondence.inbound._locked_receipt",
            side_effect=synchronized_locked_receipt,
        ):
            def invoke(callback):
                close_old_connections()
                try:
                    return callback()
                finally:
                    close_old_connections()

            with ThreadPoolExecutor(max_workers=2) as executor:
                associate_future = executor.submit(invoke, associate)
                self.assertTrue(associate_reached_receipt.wait(timeout=10))
                rematch_future = executor.submit(invoke, rematch)
                results = [
                    associate_future.result(timeout=20),
                    rematch_future.result(timeout=20),
                ]

        self.assertNotIn("database_error", [result[0] for result in results])
        self.assertCountEqual(
            [result[0] for result in results],
            ["associated", "version_conflict"],
        )

    @patch("apps.secure_files.store_quarantined_object")
    def test_simultaneous_exact_replay_stores_and_audits_once(self, store):
        callback = self._callback(
            payload=PLAIN_MESSAGE,
            provider_message_id="concurrent-exact-replay",
            idempotency_key="gmail:concurrent:exact-replay",
        )

        results = self._run_together(callback, callback)

        self.assertCountEqual(
            results,
            [("recorded", False), ("recorded", True)],
        )
        self.assert_one_durable_message(store)

    @patch("apps.secure_files.store_quarantined_object")
    def test_simultaneous_exact_dsn_replay_creates_one_notification(self, store):
        _prr, correspondence, _delivery = self._queue_outbound()
        callback = self._callback(
            payload=delivery_status_message(correspondence.rfc_message_id),
            provider_message_id="concurrent-dsn-exact-replay",
            idempotency_key="gmail:concurrent:dsn-exact-replay",
        )

        results = self._run_together(callback, callback)

        self.assertCountEqual(
            results,
            [("recorded", False), ("recorded", True)],
        )
        self.assert_one_durable_message(store)
        self.assertEqual(
            CorrespondenceDeliveryNotification.objects.count(),
            1,
        )

    @patch("apps.secure_files.store_quarantined_object")
    def test_dsn_and_success_callback_preserve_both_evidence_records(self, _store):
        prr, correspondence, delivery = self._queue_outbound()
        claim_now = timezone.now() + dt.timedelta(seconds=1)
        claim = services.claim_delivery(
            delivery,
            actor=self.outbound_integration,
            credential=self.outbound_token,
            now=claim_now,
        )
        dsn_callback = self._callback(
            payload=delivery_status_message(correspondence.rfc_message_id),
            provider_message_id="concurrent-dsn-success-race",
            idempotency_key="gmail:concurrent:dsn-success-race",
        )

        def success_callback():
            actor = get_user_model().objects.get(
                pk=self.outbound_integration.pk
            )
            result = services.report_delivery_result(
                CorrespondenceDelivery.objects.get(pk=delivery.pk),
                actor=actor,
                credential=Token.objects.get(user=actor),
                attempt_id=claim.attempt.pk,
                outcome="sent",
                provider_message_id="gmail-concurrent-success-race",
                provider_thread_id="gmail-concurrent-thread",
                provider_timestamp=claim_now + dt.timedelta(seconds=1),
                provider_metadata={"http_status": 200},
                now=claim_now + dt.timedelta(seconds=2),
            )
            return ("sent", result.status)

        results = self._run_together(dsn_callback, success_callback)

        self.assertEqual(results[0], ("recorded", False))
        self.assertEqual(results[1], ("sent", CorrespondenceDelivery.Status.SENT))
        delivery.refresh_from_db()
        correspondence.refresh_from_db()
        prr.refresh_from_db()
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.SENT)
        self.assertEqual(correspondence.status, Correspondence.Status.SENT)
        self.assertEqual(prr.status, "received")
        self.assertEqual(delivery.attempts.count(), 1)
        self.assertEqual(
            CorrespondenceDeliveryNotification.objects.filter(
                delivery=delivery
            ).count(),
            1,
        )

    @patch("apps.secure_files.store_quarantined_object")
    def test_simultaneous_idempotency_key_conflict_has_one_winner(self, store):
        results = self._run_together(
            self._callback(
                payload=PLAIN_MESSAGE,
                provider_message_id="concurrent-key-original",
                idempotency_key="gmail:concurrent:key-conflict",
            ),
            self._callback(
                payload=PLAIN_MESSAGE + b"Conflicting bytes.\r\n",
                provider_message_id="concurrent-key-conflict",
                idempotency_key="gmail:concurrent:key-conflict",
            ),
        )

        self.assertCountEqual(
            [result[0] for result in results],
            ["recorded", "idempotency_conflict"],
        )
        self.assert_one_durable_message(store)

    @patch("apps.secure_files.store_quarantined_object")
    def test_simultaneous_provider_message_conflict_has_one_winner(self, store):
        results = self._run_together(
            self._callback(
                payload=PLAIN_MESSAGE,
                provider_message_id="concurrent-provider-conflict",
                idempotency_key="gmail:concurrent:provider-original",
            ),
            self._callback(
                payload=PLAIN_MESSAGE + b"Conflicting bytes.\r\n",
                provider_message_id="concurrent-provider-conflict",
                idempotency_key="gmail:concurrent:provider-conflict",
            ),
        )

        self.assertCountEqual(
            [result[0] for result in results],
            ["recorded", "provider_conflict"],
        )
        self.assert_one_durable_message(store)

    def test_simultaneous_attachment_reservation_ignores_unrelated_nullable_aliases(
        self,
    ):
        InboundQuarantineWriteReservation.objects.reserve(
            logical_identity="1" * 64,
            kind=InboundQuarantineWriteReservation.Kind.ATTACHMENT,
            size=1,
            sha256="2" * 64,
        )
        logical_identity = "3" * 64
        loser_checked_for_active = threading.Event()
        winner_committed = threading.Event()
        role = threading.local()
        original_first = InboundQuarantineWriteReservationQuerySet.first

        def synchronized_first(queryset, *args, **kwargs):
            result = original_first(queryset, *args, **kwargs)
            if (
                getattr(role, "name", "") == "loser"
                and not getattr(role, "paused", False)
            ):
                role.paused = True
                loser_checked_for_active.set()
                self.assertTrue(winner_committed.wait(timeout=10))
            return result

        def reserve_attachment(role_name):
            role.name = role_name
            if role_name == "winner":
                self.assertTrue(loser_checked_for_active.wait(timeout=10))
            try:
                return InboundQuarantineWriteReservation.objects.reserve(
                    logical_identity=logical_identity,
                    kind=InboundQuarantineWriteReservation.Kind.ATTACHMENT,
                    size=0,
                    sha256="4" * 64,
                ).pk
            finally:
                if role_name == "winner":
                    winner_committed.set()

        with patch.object(
            InboundQuarantineWriteReservationQuerySet,
            "first",
            autospec=True,
            side_effect=synchronized_first,
        ):
            reservation_ids = self._run_together(
                lambda: reserve_attachment("loser"),
                lambda: reserve_attachment("winner"),
            )

        self.assertEqual(reservation_ids[0], reservation_ids[1])
        self.assertEqual(
            InboundQuarantineWriteReservation.objects.count(),
            2,
        )

    @patch("apps.secure_files.store_quarantined_object")
    def test_processing_lease_is_committed_before_object_storage_io(self, store):
        observed = {}

        def read_committed_processing_lease():
            close_old_connections()
            try:
                return (
                    InboundQuarantineWriteReservation.objects.get(
                        kind=(
                            InboundQuarantineWriteReservation.Kind.RAW_MESSAGE
                        )
                    ).processing_expires_at
                )
            finally:
                close_old_connections()

        def lose_process_during_put(*_args, **_kwargs):
            with ThreadPoolExecutor(max_workers=1) as executor:
                observed["processing_expires_at"] = executor.submit(
                    read_committed_processing_lease
                ).result(timeout=10)
            raise SystemExit("simulated process loss during PutObject")

        store.side_effect = lose_process_during_put
        with self.assertRaises(SystemExit):
            self._callback(
                payload=PLAIN_MESSAGE,
                provider_message_id="processing-lease-before-put",
                idempotency_key="gmail:processing-lease-before-put",
            )()

        reservation = InboundQuarantineWriteReservation.objects.get(
            kind=InboundQuarantineWriteReservation.Kind.RAW_MESSAGE
        )
        self.assertIsNotNone(observed["processing_expires_at"])
        self.assertEqual(
            reservation.processing_expires_at,
            observed["processing_expires_at"],
        )
        claimed_at = timezone.now() + dt.timedelta(minutes=20)
        self.assertIsNone(
            InboundQuarantineWriteReservation.objects.claim_due(
                reservation_id=reservation.pk,
                now=claimed_at,
                stale_before=claimed_at - dt.timedelta(minutes=15),
                lease_expires_at=claimed_at + dt.timedelta(minutes=15),
            )
        )
