# ABOUTME: PII-free correspondence operational metrics and dependency readiness.
# ABOUTME: Proves fixed-code aggregation, bounded probes, and CLI-safe output.
import datetime as dt
import json
from io import StringIO
from unittest.mock import Mock, call, patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from apps import secure_files
from apps.correspondence.models import (
    Correspondence,
    CorrespondenceAttachment,
    CorrespondenceDelivery,
    CorrespondenceDeliveryAttempt,
    CorrespondenceDeliveryNotification,
    GmailSyncState,
    InboundCorrespondenceReceipt,
)
from apps.records.models import PublicRecordsRequest, Requester


@override_settings(PRR_CORRESPONDENCE_STUCK_SECONDS=3600)
class CorrespondenceOperationsSnapshotTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_user(
            "operations-officer",
            "operations-officer@psd401.net",
            "pw",
            is_staff=True,
        )
        self.request = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(
                name="Sensitive Requester",
                email="sensitive.requester@example.org",
            ),
            description="Sensitive correspondence operations fixture.",
        )
        self.now = timezone.make_aware(
            dt.datetime(2026, 7, 24, 12, 0),
            dt.UTC,
        )
        self.sequence = 0

    def make_delivery(
        self,
        *,
        status,
        queued_age_seconds,
        failure_category="",
        claim_age_seconds=None,
        claim_expires_at=None,
        recovery_not_before=None,
    ):
        self.sequence += 1
        queued_at = self.now - dt.timedelta(seconds=queued_age_seconds)
        correspondence_status = (
            Correspondence.Status.FAILED
            if status
            in {
                CorrespondenceDelivery.Status.FAILED,
                CorrespondenceDelivery.Status.RETRYABLE,
            }
            else Correspondence.Status.QUEUED
        )
        correspondence = Correspondence.objects.create(
            request=self.request,
            direction=Correspondence.Direction.OUTBOUND,
            kind=Correspondence.Kind.GENERAL,
            status=correspondence_status,
            draft_subject="Sensitive subject",
            draft_body="Sensitive body",
            approved_subject="Sensitive subject",
            approved_body="Sensitive body",
            created_by=self.actor,
            approved_by=self.actor,
            approved_at=queued_at,
            queued_by=self.actor,
            queued_at=queued_at,
            failed_at=(
                self.now
                if correspondence_status == Correspondence.Status.FAILED
                else None
            ),
            failure_category=failure_category,
            failure_description="Raw provider detail must never escape.",
        )
        delivery = CorrespondenceDelivery.objects.create(
            correspondence=correspondence,
            status=status,
            idempotency_key=f"operations-delivery-{self.sequence}",
            claim_owner=("automation-outbound" if claim_age_seconds is not None else ""),
            claim_expires_at=claim_expires_at,
            attempt_count=1 if claim_age_seconds is not None else 0,
            last_failure_category=failure_category,
            last_failure_description=(
                "sensitive.requester@example.org at secret.internal"
                if failure_category
                else ""
            ),
            queued_at=queued_at,
            completed_at=(
                self.now
                if status
                in {
                    CorrespondenceDelivery.Status.FAILED,
                    CorrespondenceDelivery.Status.RETRYABLE,
                }
                else None
            ),
            recovery_not_before=recovery_not_before,
        )
        if claim_age_seconds is not None:
            CorrespondenceDeliveryAttempt.objects.create(
                delivery=delivery,
                attempt_number=1,
                claim_owner="automation-outbound",
                claimed_at=(
                    self.now - dt.timedelta(seconds=claim_age_seconds)
                ),
            )
        return delivery

    def make_pending_inbound_receipt(self):
        self.sequence += 1
        return InboundCorrespondenceReceipt.objects.create(
            provider="gmail",
            provider_account="records",
            provider_message_id=f"inbound-{self.sequence}",
            received_at=self.now,
            envelope_sender="sensitive.requester@example.org",
            envelope_recipients=["records@psd401.net"],
            raw_message_size=100,
            raw_message_sha256=f"{self.sequence:064x}",
            payload_sha256=f"{self.sequence + 1000:064x}",
            idempotency_key=f"operations:inbound:{self.sequence}",
            submitted_by=self.actor,
        )

    def make_delivery_status_receipt(self, delivery):
        self.sequence += 1
        inbound = Correspondence.objects.create(
            request=self.request,
            direction=Correspondence.Direction.INBOUND,
            kind=Correspondence.Kind.DELIVERY_STATUS_NOTIFICATION,
            status=Correspondence.Status.RECEIVED,
            received_subject="Delivery Status Notification",
            received_body="",
            received_at=self.now,
            matched_at=self.now,
            provider="gmail",
            provider_account="records",
            provider_message_id=f"dsn-inbound-{self.sequence}",
        )
        return InboundCorrespondenceReceipt.objects.create(
            provider="gmail",
            provider_account="records",
            provider_message_id=f"dsn-receipt-{self.sequence}",
            received_at=self.now,
            parsing_status=InboundCorrespondenceReceipt.ParsingStatus.PARSED,
            matching_status=InboundCorrespondenceReceipt.MatchingStatus.MATCHED,
            match_method=InboundCorrespondenceReceipt.MatchMethod.DELIVERY_STATUS,
            delivery_status_original_message_id=(
                delivery.correspondence.rfc_message_id
            ),
            matched_request=self.request,
            matched_delivery=delivery,
            matched_correspondence=inbound,
            matched_at=self.now,
            quarantined=False,
            raw_message_size=100,
            raw_message_sha256=f"{self.sequence:064x}",
            payload_sha256=f"{self.sequence + 1000:064x}",
            idempotency_key=f"operations:dsn:{self.sequence}",
            submitted_by=self.actor,
            completed_at=self.now,
        )

    def test_snapshot_has_fixed_counts_ages_and_sanitized_failure_categories(self):
        pending_delivery = self.make_delivery(
            status=CorrespondenceDelivery.Status.PENDING,
            queued_age_seconds=7200,
        )
        self.make_delivery(
            status=CorrespondenceDelivery.Status.CLAIMED,
            queued_age_seconds=900,
            claim_age_seconds=600,
            claim_expires_at=self.now + dt.timedelta(seconds=60),
        )
        self.make_delivery(
            status=CorrespondenceDelivery.Status.RETRYABLE,
            queued_age_seconds=300,
            failure_category="provider_unavailable",
        )
        self.make_delivery(
            status=CorrespondenceDelivery.Status.FAILED,
            queued_age_seconds=120,
            failure_category=(
                "requester-sensitive.requester@example.org-secret.internal"
            ),
        )
        self.make_pending_inbound_receipt()
        inbound_receipt = self.make_delivery_status_receipt(
            pending_delivery
        )
        CorrespondenceDeliveryNotification.objects.create(
            inbound_receipt=inbound_receipt,
            delivery=pending_delivery,
            recipient_index=1,
            recipient_match_status=(
                CorrespondenceDeliveryNotification
                .RecipientMatchStatus.UNMATCHED
            ),
            action=CorrespondenceDeliveryNotification.Action.FAILED,
            enhanced_status="5.1.1",
        )
        draft = Correspondence.objects.create(
            request=self.request,
            direction=Correspondence.Direction.OUTBOUND,
            kind=Correspondence.Kind.GENERAL,
            status=Correspondence.Status.DRAFT,
            draft_subject="Attachment",
            draft_body="Attachment body",
            created_by=self.actor,
        )
        CorrespondenceAttachment.objects.create(
            correspondence=draft,
            original_filename="student-record.pdf",
            content_type="application/pdf",
            size=100,
            sha256="f" * 64,
            uploaded_by=self.actor,
        )

        from apps.correspondence.operations import operations_snapshot

        snapshot = operations_snapshot(now=self.now)

        self.assertEqual(
            snapshot["counts"],
            {
                "queued": 1,
                "claimed": 1,
                "awaiting_reconciliation": 0,
                "retryable": 1,
                "failed": 1,
                "sent": 0,
                "cancelled": 0,
                "stuck": 1,
                "inbound": 2,
                "unmatched": 1,
                "quarantined_messages": 1,
                "quarantined_attachments": 1,
                "delivery_exceptions": 1,
            },
        )
        self.assertEqual(
            snapshot["ages_seconds"],
            {
                "oldest_delivery": 7200,
                "oldest_claim": 600,
                "oldest_stuck": 7200,
            },
        )
        self.assertEqual(
            snapshot["failure_categories"],
            {"other": 1, "provider_unavailable": 1},
        )
        serialized = json.dumps(snapshot)
        for sensitive in (
            "sensitive.requester@example.org",
            "secret.internal",
            "Sensitive subject",
            "Sensitive body",
            "student-record.pdf",
            "Raw provider detail",
        ):
            self.assertNotIn(sensitive, serialized)

    def test_expired_claim_is_stuck_even_before_queue_age_threshold(self):
        self.make_delivery(
            status=CorrespondenceDelivery.Status.CLAIMED,
            queued_age_seconds=30,
            claim_age_seconds=20,
            claim_expires_at=self.now - dt.timedelta(seconds=1),
        )

        from apps.correspondence.operations import operations_snapshot

        snapshot = operations_snapshot(now=self.now)

        self.assertEqual(snapshot["counts"]["stuck"], 1)
        self.assertEqual(snapshot["ages_seconds"]["oldest_stuck"], 30)

    def test_expired_reconciliation_lease_is_immediately_stuck(self):
        self.make_delivery(
            status=(
                CorrespondenceDelivery.Status.AWAITING_RECONCILIATION
            ),
            queued_age_seconds=30,
            claim_age_seconds=20,
            claim_expires_at=self.now - dt.timedelta(seconds=1),
        )

        from apps.correspondence.operations import operations_snapshot

        snapshot = operations_snapshot(now=self.now)

        self.assertEqual(snapshot["counts"]["stuck"], 1)
        self.assertEqual(snapshot["ages_seconds"]["oldest_stuck"], 30)

    def test_old_unexpired_send_and_reconciliation_leases_are_not_stuck(self):
        self.make_delivery(
            status=CorrespondenceDelivery.Status.CLAIMED,
            queued_age_seconds=7200,
            claim_age_seconds=60,
            claim_expires_at=self.now + dt.timedelta(seconds=60),
        )
        self.make_delivery(
            status=(
                CorrespondenceDelivery.Status.AWAITING_RECONCILIATION
            ),
            queued_age_seconds=7200,
            claim_age_seconds=60,
            claim_expires_at=self.now + dt.timedelta(seconds=60),
        )

        from apps.correspondence.operations import operations_snapshot

        snapshot = operations_snapshot(now=self.now)

        self.assertEqual(snapshot["counts"]["stuck"], 0)
        self.assertIsNone(snapshot["ages_seconds"]["oldest_stuck"])

    def test_future_recovery_clock_defers_old_unclaimed_delivery(self):
        self.make_delivery(
            status=CorrespondenceDelivery.Status.PENDING,
            queued_age_seconds=7200,
            recovery_not_before=self.now + dt.timedelta(seconds=60),
        )

        from apps.correspondence.operations import operations_snapshot

        snapshot = operations_snapshot(now=self.now)

        self.assertEqual(snapshot["counts"]["stuck"], 0)
        self.assertIsNone(snapshot["ages_seconds"]["oldest_stuck"])

    def test_recent_result_defers_old_due_reconciliation_delivery(self):
        delivery = self.make_delivery(
            status=(
                CorrespondenceDelivery.Status.AWAITING_RECONCILIATION
            ),
            queued_age_seconds=7200,
            recovery_not_before=self.now - dt.timedelta(seconds=1),
        )
        delivery.attempt_count = 1
        delivery.save(update_fields=["attempt_count"])
        CorrespondenceDeliveryAttempt.objects.create(
            delivery=delivery,
            attempt_number=1,
            action=CorrespondenceDeliveryAttempt.Action.SEND,
            claim_owner="automation-outbound",
            claimed_at=self.now - dt.timedelta(seconds=60),
            result_at=self.now - dt.timedelta(seconds=30),
            result_category=(
                CorrespondenceDeliveryAttempt.ResultCategory.UNKNOWN
            ),
            error_category="outcome_unknown",
            result_fingerprint="a" * 64,
        )

        from apps.correspondence.operations import operations_snapshot

        snapshot = operations_snapshot(now=self.now)

        self.assertEqual(snapshot["counts"]["stuck"], 0)
        self.assertIsNone(snapshot["ages_seconds"]["oldest_stuck"])

    def test_cancelled_delivery_failure_is_not_a_current_failure(self):
        delivery = self.make_delivery(
            status=CorrespondenceDelivery.Status.RETRYABLE,
            queued_age_seconds=300,
            failure_category="provider_unavailable",
        )
        delivery.status = CorrespondenceDelivery.Status.CANCELLED
        delivery.save(update_fields=["status"])

        from apps.correspondence.operations import operations_snapshot

        snapshot = operations_snapshot(now=self.now)

        self.assertEqual(snapshot["counts"]["cancelled"], 1)
        self.assertEqual(snapshot["failure_categories"], {})


@override_settings(
    PRR_CORRESPONDENCE_OUTBOUND_ENABLED=True,
    PRR_GMAIL_ENABLED=True,
    PRR_CORRESPONDENCE_STUCK_SECONDS=3600,
)
class CorrespondenceReadinessTests(TestCase):
    def test_readiness_returns_only_fixed_codes_and_checks_gmail_transport(self):
        from apps.correspondence.operations import readiness_snapshot
        with (
            patch(
                "apps.correspondence.operations.secure_files."
                "probe_object_storage",
                side_effect=RuntimeError(
                    "bucket prr-private and access key SECRET failed"
                ),
            ),
            patch(
                "apps.correspondence.operations.secure_files.probe_clamav",
                return_value=True,
            ),
            patch(
                "apps.correspondence.operations.probe_redis",
                return_value=True,
            ),
        ):
            snapshot = readiness_snapshot()

        self.assertFalse(snapshot["ready"])
        self.assertEqual(
            snapshot["checks"],
            [
                {
                    "component": "outbound_transport",
                    "status": "ready",
                    "code": "enabled",
                },
                {
                    "component": "object_storage",
                    "status": "not_ready",
                    "code": "unavailable",
                },
                {
                    "component": "malware_scanner",
                    "status": "ready",
                    "code": "available",
                },
                {
                    "component": "task_broker",
                    "status": "ready",
                    "code": "available",
                },
                {
                    "component": "gmail_transport",
                    "status": "ready",
                    "code": "configured",
                },
            ],
        )
        serialized = json.dumps(snapshot)
        for sensitive in (
            "prr-private",
            "SECRET",
        ):
            self.assertNotIn(sensitive, serialized)

    def test_readiness_is_ready_with_all_probes(self):
        from apps.correspondence.operations import readiness_snapshot
        with (
            patch(
                "apps.correspondence.operations.secure_files."
                "probe_object_storage",
                return_value=True,
            ),
            patch(
                "apps.correspondence.operations.secure_files.probe_clamav",
                return_value=True,
            ),
            patch(
                "apps.correspondence.operations.probe_redis",
                return_value=True,
            ),
        ):
            snapshot = readiness_snapshot()

        self.assertTrue(snapshot["ready"])
        self.assertTrue(
            all(check["status"] == "ready" for check in snapshot["checks"])
        )

    def test_readiness_fails_closed_for_unsafe_inbound_system_principal(self):
        from rest_framework.authtoken.models import Token

        from apps.correspondence.gmail_sync import system_inbound_principal
        from apps.correspondence.operations import readiness_snapshot

        Token.objects.create(user=system_inbound_principal())
        with (
            patch(
                "apps.correspondence.operations.secure_files."
                "probe_object_storage",
                return_value=True,
            ),
            patch(
                "apps.correspondence.operations.secure_files.probe_clamav",
                return_value=True,
            ),
            patch(
                "apps.correspondence.operations.probe_redis",
                return_value=True,
            ),
        ):
            snapshot = readiness_snapshot()

        self.assertFalse(snapshot["ready"])
        gmail = next(
            check
            for check in snapshot["checks"]
            if check["component"] == "gmail_transport"
        )
        self.assertEqual(gmail["status"], "not_ready")
        self.assertEqual(gmail["code"], "unavailable")

    @override_settings(PRR_GMAIL_ENABLED=False)
    def test_readiness_fails_closed_when_gmail_is_disabled(self):
        from apps.correspondence.operations import readiness_snapshot
        with (
            patch(
                "apps.correspondence.operations.secure_files."
                "probe_object_storage",
                return_value=True,
            ),
            patch(
                "apps.correspondence.operations.secure_files.probe_clamav",
                return_value=True,
            ),
            patch(
                "apps.correspondence.operations.probe_redis",
                return_value=True,
            ),
        ):
            snapshot = readiness_snapshot()

        self.assertFalse(snapshot["ready"])
        gmail = next(
            check
            for check in snapshot["checks"]
            if check["component"] == "gmail_transport"
        )
        self.assertEqual(gmail["status"], "not_ready")
        self.assertEqual(gmail["code"], "unavailable")


@override_settings(
    PRR_ENVIRONMENT="production",
    PRR_GMAIL_MAILBOX="publicrecords@psd401.net",
    PRR_GMAIL_ENABLED=True,
    PRR_GMAIL_INBOUND_POLL_ENABLED=True,
    PRR_GMAIL_INBOUND_STALE_SECONDS=300,
)
class GmailInboundStatusTests(TestCase):
    def setUp(self):
        self.now = dt.datetime(2026, 8, 21, 21, 0, tzinfo=dt.UTC)

    def snapshot(self):
        from apps.correspondence.operations import gmail_inbound_status_snapshot

        return gmail_inbound_status_snapshot(now=self.now)

    def test_never_synced_enabled_poller_is_unhealthy_without_exposing_mailbox(self):
        snapshot = self.snapshot()

        self.assertEqual(snapshot["status"], "never_synced")
        self.assertFalse(snapshot["healthy"])
        self.assertEqual(
            snapshot["configuration"],
            {
                "environment": "production",
                "gmail_enabled": True,
                "inbound_poll_enabled": True,
                "mailbox_configured": True,
            },
        )
        self.assertEqual(
            snapshot["checkpoint"],
            {
                "present": False,
                "history_present": False,
                "last_synced_at": None,
                "age_seconds": None,
                "stale_after_seconds": 300,
            },
        )
        self.assertNotIn("publicrecords@psd401.net", json.dumps(snapshot))

    def test_recent_checkpoint_is_healthy_and_stale_checkpoint_is_not(self):
        state = GmailSyncState.objects.create(
            mailbox="publicrecords@psd401.net",
            environment="production",
            history_id="123456",
        )
        GmailSyncState.objects.filter(pk=state.pk).update(
            last_synced_at=self.now - dt.timedelta(seconds=299)
        )

        recent = self.snapshot()

        self.assertEqual(recent["status"], "healthy")
        self.assertTrue(recent["healthy"])
        self.assertEqual(recent["checkpoint"]["age_seconds"], 299)
        self.assertNotIn("123456", json.dumps(recent))

        GmailSyncState.objects.filter(pk=state.pk).update(
            last_synced_at=self.now - dt.timedelta(seconds=301)
        )

        stale = self.snapshot()

        self.assertEqual(stale["status"], "stale")
        self.assertFalse(stale["healthy"])
        self.assertEqual(stale["checkpoint"]["age_seconds"], 301)

    @override_settings(PRR_GMAIL_INBOUND_POLL_ENABLED=False)
    def test_disabled_poller_reports_disabled_and_unhealthy(self):
        snapshot = self.snapshot()

        self.assertEqual(snapshot["status"], "disabled")
        self.assertFalse(snapshot["healthy"])

    @override_settings(PRR_GMAIL_ENABLED=False)
    def test_inbound_enabled_under_disabled_global_switch_is_unhealthy(self):
        snapshot = self.snapshot()

        self.assertEqual(snapshot["status"], "gmail_disabled")
        self.assertFalse(snapshot["healthy"])

    @patch(
        "apps.correspondence.management.commands.gmail_inbound_status."
        "gmail_inbound_status_snapshot"
    )
    def test_management_command_emits_machine_readable_healthy_status(self, snapshot):
        snapshot.return_value = {
            "observed_at": self.now.isoformat(),
            "status": "healthy",
            "healthy": True,
            "configuration": {},
            "checkpoint": {},
        }
        output = StringIO()

        call_command("gmail_inbound_status", "--json", stdout=output)

        self.assertEqual(json.loads(output.getvalue()), snapshot.return_value)

    @patch(
        "apps.correspondence.management.commands.gmail_inbound_status."
        "gmail_inbound_status_snapshot"
    )
    def test_management_command_preserves_unhealthy_json_and_fails(self, snapshot):
        snapshot.return_value = {
            "observed_at": self.now.isoformat(),
            "status": "stale",
            "healthy": False,
            "configuration": {},
            "checkpoint": {},
        }
        output = StringIO()

        with self.assertRaisesMessage(CommandError, "Gmail inbound poller is stale"):
            call_command("gmail_inbound_status", "--json", stdout=output)

        self.assertEqual(json.loads(output.getvalue()), snapshot.return_value)


class SecureFileReadinessProbeTests(TestCase):
    @override_settings(OBJECT_STORE={"bucket": "private-bucket"})
    def test_object_storage_probe_uses_one_read_only_bucket_request(self):
        client = Mock()
        client.get_bucket_versioning.return_value = {"Status": "Enabled"}

        self.assertTrue(secure_files.probe_object_storage(_client=client))

        client.get_bucket_versioning.assert_called_once_with(
            Bucket="private-bucket"
        )
        self.assertEqual(
            client.method_calls,
            [call.get_bucket_versioning(Bucket="private-bucket")],
        )

    @override_settings(
        CLAMAV_HOST="clamav.internal",
        CLAMAV_PORT=3310,
        CLAMAV_TIMEOUT_SECONDS=30,
    )
    def test_clamav_probe_is_bounded_and_requires_exact_pong(self):
        connection = Mock()
        connection.__enter__ = Mock(return_value=connection)
        connection.__exit__ = Mock(return_value=False)
        connection.recv.return_value = b"PONG\0"
        factory = Mock(return_value=connection)

        self.assertTrue(
            secure_files.probe_clamav(_connection_factory=factory)
        )

        factory.assert_called_once_with(
            ("clamav.internal", 3310),
            timeout=5,
        )
        connection.sendall.assert_called_once_with(b"zPING\0")
        connection.recv.assert_called_once_with(16)

    @override_settings(
        CLAMAV_HOST="clamav.internal",
        CLAMAV_PORT=3310,
        CLAMAV_TIMEOUT_SECONDS=30,
    )
    def test_clamav_probe_rejects_non_pong_without_echoing_response(self):
        connection = Mock()
        connection.__enter__ = Mock(return_value=connection)
        connection.__exit__ = Mock(return_value=False)
        connection.recv.return_value = b"requester@example.org ERROR\0"

        with self.assertRaisesRegex(
            secure_files.AttachmentScanUnavailable,
            r"^ClamAV readiness check failed\.$",
        ):
            secure_files.probe_clamav(
                _connection_factory=Mock(return_value=connection)
            )

    @override_settings(
        CLAMAV_HOST="clamav.internal",
        CLAMAV_PORT=3310,
        CLAMAV_TIMEOUT_SECONDS=30,
    )
    def test_clamav_probe_accepts_fragmented_exact_pong(self):
        connection = Mock()
        connection.__enter__ = Mock(return_value=connection)
        connection.__exit__ = Mock(return_value=False)
        connection.recv.side_effect = [b"PO", b"NG\0"]

        self.assertTrue(
            secure_files.probe_clamav(
                _connection_factory=Mock(return_value=connection)
            )
        )

        self.assertEqual(
            connection.recv.call_args_list,
            [call(16), call(14)],
        )


class RedisReadinessProbeTests(SimpleTestCase):
    @override_settings(
        CELERY_BROKER_URL=(
            "redis://localhost:6379/0"
            "?socket_timeout=600&socket_connect_timeout=601"
            "&retry_on_timeout=true&health_check_interval=999"
        )
    )
    def test_url_query_cannot_override_probe_timeout_bounds(self):
        from apps.correspondence.operations import _redis_probe_client

        client = _redis_probe_client()
        try:
            connection_kwargs = client.connection_pool.connection_kwargs
            self.assertEqual(connection_kwargs["socket_timeout"], 5)
            self.assertEqual(connection_kwargs["socket_connect_timeout"], 5)
            self.assertFalse(connection_kwargs["retry_on_timeout"])
            self.assertEqual(connection_kwargs["health_check_interval"], 0)
        finally:
            client.close()


class CorrespondenceReadinessCommandTests(SimpleTestCase):
    def test_json_mode_is_machine_readable_and_fails_nonzero_when_not_ready(self):
        payload = {
            "ready": False,
            "checks": [
                {
                    "component": "object_storage",
                    "status": "not_ready",
                    "code": "unavailable",
                }
            ],
        }
        output = StringIO()
        with (
            patch(
                "apps.correspondence.management.commands."
                "check_correspondence_readiness.readiness_snapshot",
                return_value=payload,
            ),
            self.assertRaisesRegex(
                CommandError,
                r"^Correspondence dependencies are not ready\.$",
            ),
        ):
            call_command(
                "check_correspondence_readiness",
                "--json",
                stdout=output,
            )

        self.assertEqual(json.loads(output.getvalue()), payload)
