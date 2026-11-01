# ABOUTME: Durable recovery tests for quarantine writes made before domain commit.
# ABOUTME: Stale unadopted objects are leased, cross-checked, deleted, and audited.
import datetime as dt
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import QuerySet
from django.test import TestCase
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.correspondence.models import (
    InboundCorrespondenceReceipt,
    InboundQuarantineReservationConflict,
    InboundQuarantineTransportAlias,
    InboundQuarantineWriteReservation,
)
from apps.correspondence.tasks import (
    cleanup_inbound_quarantine_write,
    requeue_stale_inbound_quarantine_writes,
)


class InboundQuarantineWriteReservationTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_user(
            "reservation-recovery",
            password=None,
        )

    def reserve(self, *, logical_identity="a" * 64):
        return InboundQuarantineWriteReservation.objects.reserve(
            logical_identity=logical_identity,
            kind=InboundQuarantineWriteReservation.Kind.RAW_MESSAGE,
            size=128,
            sha256="b" * 64,
            transport_idempotency_identity=logical_identity,
            transport_provider_identity=logical_identity,
        )

    def test_exact_replay_reuses_generated_storage_key_and_immutable_evidence(self):
        first = self.reserve()
        replay = self.reserve()

        self.assertEqual(replay.pk, first.pk)
        self.assertEqual(replay.storage_key, first.storage_key)
        with self.assertRaises(ValidationError):
            InboundQuarantineWriteReservation.objects.filter(
                pk=first.pk
            ).update(size=129)
        with self.assertRaises(ValidationError):
            first.delete()

    def test_transport_aliases_are_unique_immutable_and_transactional(self):
        alias = InboundQuarantineTransportAlias.objects.claim(
            kind=InboundQuarantineTransportAlias.Kind.IDEMPOTENCY,
            identity="1" * 64,
            logical_identity="2" * 64,
        )
        replay = InboundQuarantineTransportAlias.objects.claim(
            kind=InboundQuarantineTransportAlias.Kind.IDEMPOTENCY,
            identity="1" * 64,
            logical_identity="2" * 64,
        )
        self.assertEqual(replay.pk, alias.pk)
        with self.assertRaises(InboundQuarantineReservationConflict):
            InboundQuarantineTransportAlias.objects.claim(
                kind=InboundQuarantineTransportAlias.Kind.IDEMPOTENCY,
                identity="1" * 64,
                logical_identity="3" * 64,
            )
        with self.assertRaises(ValidationError):
            InboundQuarantineTransportAlias.objects.filter(pk=alias.pk).update(
                logical_identity="4" * 64
            )
        with self.assertRaises(ValidationError):
            alias.delete()
        with self.assertRaises(ValidationError):
            InboundQuarantineTransportAlias.objects.bulk_create([])

        with self.assertRaises(IntegrityError), transaction.atomic():
            QuerySet.update(
                InboundQuarantineTransportAlias.objects.filter(pk=alias.pk),
                identity="not-a-hash",
            )

        existing = self.reserve(logical_identity="5" * 64)
        with (
            patch(
                "apps.correspondence.models._reservation_storage_key",
                return_value=existing.storage_key,
            ),
            self.assertRaises(
                InboundQuarantineWriteReservation.DoesNotExist
            ),
        ):
            InboundQuarantineWriteReservation.objects.reserve(
                logical_identity="6" * 64,
                kind=InboundQuarantineWriteReservation.Kind.RAW_MESSAGE,
                size=128,
                sha256="7" * 64,
                transport_idempotency_identity="8" * 64,
                transport_provider_identity="9" * 64,
            )
        self.assertFalse(
            InboundQuarantineTransportAlias.objects.filter(
                identity__in=["8" * 64, "9" * 64]
            ).exists()
        )

    @patch(
        "apps.correspondence.tasks.cleanup_inbound_quarantine_write.delay"
    )
    def test_stale_recovery_is_bounded_and_leased(self, delay):
        stale = self.reserve(logical_identity="1" * 64)
        second = self.reserve(logical_identity="2" * 64)

        with patch(
            "apps.correspondence.tasks.timezone.now",
            return_value=timezone.now() + dt.timedelta(hours=2),
        ):
            queued = requeue_stale_inbound_quarantine_writes(limit=1)

        self.assertEqual(queued, 1)
        delay.assert_called_once()
        stale.refresh_from_db()
        second.refresh_from_db()
        leased = [row for row in (stale, second) if row.lease_token]
        self.assertEqual(len(leased), 1)
        self.assertIsNone(
            next(row for row in (stale, second) if not row.lease_token).lease_expires_at
        )

    @patch("apps.secure_files.delete_quarantined_object")
    def test_restart_cleanup_deletes_unreferenced_object_and_audits_without_key(
        self,
        delete_object,
    ):
        reservation = self.reserve()
        claimed = InboundQuarantineWriteReservation.objects.claim_due(
            reservation_id=reservation.pk,
            now=timezone.now() + dt.timedelta(hours=2),
            stale_before=timezone.now() + dt.timedelta(hours=1),
            lease_expires_at=timezone.now() + dt.timedelta(hours=2, minutes=15),
        )

        result = cleanup_inbound_quarantine_write.run(
            str(claimed.pk),
            str(claimed.lease_token),
        )

        self.assertEqual(result, "deleted")
        delete_object.assert_called_once_with(reservation.storage_key)
        reservation.refresh_from_db()
        self.assertIsNotNone(reservation.completed_at)
        self.assertEqual(reservation.attempt_count, 1)
        event = AuditLog.objects.get(
            action="correspondence.inbound_quarantine_orphan_deleted"
        )
        self.assertNotIn(reservation.storage_key, str(event.detail))

        with self.assertRaises(
            InboundQuarantineReservationConflict
        ) as idempotency_conflict:
            InboundQuarantineWriteReservation.objects.reserve(
                logical_identity="9" * 64,
                kind=InboundQuarantineWriteReservation.Kind.RAW_MESSAGE,
                size=129,
                sha256="8" * 64,
                transport_idempotency_identity=(
                    reservation.transport_idempotency_identity
                ),
                transport_provider_identity="7" * 64,
            )
        self.assertEqual(
            idempotency_conflict.exception.conflict_kind,
            "idempotency",
        )
        with self.assertRaises(
            InboundQuarantineReservationConflict
        ) as provider_conflict:
            InboundQuarantineWriteReservation.objects.reserve(
                logical_identity="6" * 64,
                kind=InboundQuarantineWriteReservation.Kind.RAW_MESSAGE,
                size=129,
                sha256="5" * 64,
                transport_idempotency_identity="4" * 64,
                transport_provider_identity=(
                    reservation.transport_provider_identity
                ),
            )
        self.assertEqual(provider_conflict.exception.conflict_kind, "provider")

        retry = self.reserve()
        self.assertNotEqual(retry.pk, reservation.pk)
        self.assertNotEqual(retry.storage_key, reservation.storage_key)
        self.assertEqual(retry.logical_identity, reservation.logical_identity)
        self.assertEqual(retry.generation, reservation.generation + 1)
        reservation.refresh_from_db()
        self.assertIsNotNone(reservation.completed_at)

    @patch("apps.secure_files.delete_quarantined_object")
    def test_started_cleanup_cannot_reuse_key_after_lease_rotation(
        self,
        delete_object,
    ):
        reservation = self.reserve()
        with transaction.atomic():
            reservation = (
                InboundQuarantineWriteReservation.objects.mark_stored_locked(
                    reservation_id=reservation.pk,
                    stored_at=timezone.now(),
                )
            )
        claimed_at = timezone.now() + dt.timedelta(hours=2)
        claimed = InboundQuarantineWriteReservation.objects.claim_due(
            reservation_id=reservation.pk,
            now=claimed_at,
            stale_before=claimed_at - dt.timedelta(hours=1),
            lease_expires_at=claimed_at + dt.timedelta(minutes=15),
        )

        def rotate_lease_before_old_worker_returns(_storage_key):
            from apps.correspondence import receiving
            from apps.correspondence.services import InboundSubmissionInProgress

            rotated_at = claimed_at + dt.timedelta(hours=1)
            rotated = InboundQuarantineWriteReservation.objects.claim_due(
                reservation_id=reservation.pk,
                now=rotated_at,
                stale_before=rotated_at - dt.timedelta(hours=1),
                lease_expires_at=rotated_at + dt.timedelta(minutes=15),
            )
            self.assertIsNotNone(rotated)
            self.assertTrue(
                InboundQuarantineWriteReservation.objects.release_claim(
                    reservation_id=rotated.pk,
                    lease_token=rotated.lease_token,
                    next_attempt_at=rotated_at + dt.timedelta(minutes=1),
                )
            )
            with self.assertRaises(InboundSubmissionInProgress):
                receiving._reserve_and_store(
                    logical_identity=reservation.logical_identity,
                    kind=reservation.kind,
                    size=reservation.size,
                    sha256=reservation.sha256,
                    content=b"x" * reservation.size,
                    transport_idempotency_identity=(
                        reservation.transport_idempotency_identity
                    ),
                    transport_provider_identity=(
                        reservation.transport_provider_identity
                    ),
                )

        delete_object.side_effect = rotate_lease_before_old_worker_returns

        result = cleanup_inbound_quarantine_write.run(
            str(claimed.pk),
            str(claimed.lease_token),
        )

        self.assertEqual(result, "deferred")
        reservation.refresh_from_db()
        self.assertEqual(reservation.attempt_count, 1)
        self.assertIsNotNone(reservation.last_attempt_at)
        self.assertIsNone(reservation.adopted_at)
        self.assertIsNone(reservation.completed_at)

    @patch("apps.secure_files.delete_quarantined_object")
    def test_recovery_heals_an_exact_domain_reference_instead_of_deleting(
        self,
        delete_object,
    ):
        receipt = InboundCorrespondenceReceipt.objects.create(
            provider="gmail",
            provider_account="default",
            provider_message_id="healed-reference",
            provider_thread_id="healed-thread",
            received_at=timezone.now(),
            envelope_sender="requester@example.org",
            envelope_recipients=["records@example.org"],
            raw_message_size=128,
            raw_message_sha256="b" * 64,
            payload_sha256="c" * 64,
            idempotency_key="gmail:healed-reference",
            submitted_by=self.actor,
        )
        reservation = InboundQuarantineWriteReservation.objects.create(
            logical_identity="d" * 64,
            kind=InboundQuarantineWriteReservation.Kind.RAW_MESSAGE,
            size=receipt.raw_message_size,
            sha256=receipt.raw_message_sha256,
            storage_key=receipt.raw_storage_key,
            transport_idempotency_identity="e" * 64,
            transport_provider_identity="f" * 64,
        )
        claimed = InboundQuarantineWriteReservation.objects.claim_due(
            reservation_id=reservation.pk,
            now=timezone.now() + dt.timedelta(hours=2),
            stale_before=timezone.now() + dt.timedelta(hours=1),
            lease_expires_at=timezone.now() + dt.timedelta(hours=2, minutes=15),
        )

        result = cleanup_inbound_quarantine_write.run(
            str(claimed.pk),
            str(claimed.lease_token),
        )

        self.assertEqual(result, "retained")
        delete_object.assert_not_called()
        reservation.refresh_from_db()
        self.assertEqual(reservation.adopted_receipt, receipt)
        self.assertIsNotNone(reservation.adopted_at)
        self.assertIsNone(reservation.lease_token)
        self.assertTrue(
            AuditLog.objects.filter(
                action="correspondence.inbound_quarantine_reservation_healed"
            ).exists()
        )

    @patch("apps.secure_files.delete_quarantined_object")
    def test_reference_healing_rolls_back_if_its_audit_cannot_commit(
        self,
        delete_object,
    ):
        receipt = InboundCorrespondenceReceipt.objects.create(
            provider="gmail",
            provider_account="default",
            provider_message_id="healing-audit-failure",
            provider_thread_id="healing-audit-thread",
            received_at=timezone.now(),
            envelope_sender="requester@example.org",
            envelope_recipients=["records@example.org"],
            raw_message_size=128,
            raw_message_sha256="b" * 64,
            payload_sha256="c" * 64,
            idempotency_key="gmail:healing-audit-failure",
            submitted_by=self.actor,
        )
        reservation = InboundQuarantineWriteReservation.objects.create(
            logical_identity="1" * 64,
            kind=InboundQuarantineWriteReservation.Kind.RAW_MESSAGE,
            size=receipt.raw_message_size,
            sha256=receipt.raw_message_sha256,
            storage_key=receipt.raw_storage_key,
            transport_idempotency_identity="2" * 64,
            transport_provider_identity="3" * 64,
        )
        claimed = InboundQuarantineWriteReservation.objects.claim_due(
            reservation_id=reservation.pk,
            now=timezone.now() + dt.timedelta(hours=2),
            stale_before=timezone.now() + dt.timedelta(hours=1),
            lease_expires_at=timezone.now() + dt.timedelta(hours=2, minutes=15),
        )

        with (
            patch(
                "apps.audit.models.audit",
                side_effect=RuntimeError("audit unavailable"),
            ),
            self.assertRaisesRegex(RuntimeError, "audit unavailable"),
        ):
            cleanup_inbound_quarantine_write.run(
                str(claimed.pk),
                str(claimed.lease_token),
            )

        delete_object.assert_not_called()
        reservation.refresh_from_db()
        self.assertIsNone(reservation.adopted_at)
        self.assertIsNone(reservation.adopted_receipt)
        self.assertEqual(reservation.lease_token, claimed.lease_token)
        self.assertIsNotNone(reservation.lease_expires_at)
