# ABOUTME: PostgreSQL concurrency coverage for outbound delivery leases and callbacks.
# ABOUTME: Separate connections prove claims and callback idempotency serialize safely.
import datetime as dt
import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.db import IntegrityError, close_old_connections, connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.correspondence import services, tasks
from apps.correspondence.models import (
    Correspondence,
    CorrespondenceAttachment,
    CorrespondenceDelivery,
    CorrespondenceDeliveryAttempt,
    CorrespondenceDenialCitation,
    CorrespondenceRecipient,
)
from apps.integrations.gmail.client import SentMessage
from apps.records.models import (
    ExemptionCitation,
    PublicRecordsRequest,
    Requester,
    RequestStatus,
    StatusEvent,
)
from apps.records.service_identities import SYSTEM_OUTBOUND_TRANSPORT_USERNAME


@skipUnless(
    connection.vendor == "postgresql",
    "Outbound concurrency requires PostgreSQL row-level locks.",
)
@override_settings(
    PRR_LOCAL_IMPLICIT_STAFF_ROLES=(),
    PRR_CORRESPONDENCE_MESSAGE_ID_DOMAIN="mail.prr.test",
    PRR_CORRESPONDENCE_REPLY_DOMAIN="mail.prr.test",
    PRR_CORRESPONDENCE_AUTOMATED_OUTBOUND_ENABLED=True,
)
class OutboundDeliveryConcurrencyTests(TransactionTestCase):
    def setUp(self):
        user_model = get_user_model()
        self.officer = user_model.objects.create_user(
            "concurrency-records-officer",
            "officer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.integration = self._create_outbound_identity(
            "concurrency-outbound-primary"
        )
        self.other_integration = self._create_outbound_identity(
            "concurrency-outbound-secondary"
        )
        self.system_transport, _created = user_model.objects.get_or_create(
            username=SYSTEM_OUTBOUND_TRANSPORT_USERNAME,
        )
        self.system_transport.set_unusable_password()
        self.system_transport.is_active = True
        self.system_transport.is_staff = False
        self.system_transport.is_superuser = False
        self.system_transport.save()
        self.system_transport.groups.clear()
        Token.objects.filter(user=self.system_transport).delete()
        self.system_transport.user_permissions.set(
            [
                Permission.objects.get(
                    content_type__app_label="correspondence",
                    codename="deliver_outbound_correspondence",
                )
            ]
        )
        self.prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(
                name="Concurrent Requester",
                email="concurrent@example.org",
            ),
            description="Records needed to exercise concurrent delivery.",
            estimated_completion=dt.date(2026, 8, 3),
        )
        self.now = timezone.now().replace(microsecond=0)

    def _create_outbound_identity(self, username):
        user = get_user_model().objects.create_user(username, password=None)
        user.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="correspondence",
                codename="deliver_outbound_correspondence",
            )
        )
        Token.objects.create(user=user)
        return user

    def _queue_delivery(
        self,
        *,
        kind=Correspondence.Kind.GENERAL,
        with_attachment=False,
    ):
        if kind == Correspondence.Kind.ACKNOWLEDGMENT:
            draft = services.create_acknowledgment_draft(
                self.prr,
                actor=self.officer,
            )
        else:
            draft = services.create_authored_draft(
                self.prr,
                kind=kind,
                subject="Concurrent delivery",
                body="This approved content must be delivered exactly once.",
                actor=self.officer,
                recipients=[
                    {
                        "recipient_type": (
                            CorrespondenceRecipient.RecipientType.TO
                        ),
                        "email": self.prr.requester.email,
                        "display_name": self.prr.requester.name,
                        "requester": self.prr.requester,
                    }
                ],
            )
        attachment = None
        if with_attachment:
            attachment = CorrespondenceAttachment.objects.create(
                correspondence=draft,
                original_filename="concurrent-attachment.pdf",
                content_type="application/pdf",
                size=len(b"approved attachment bytes"),
                sha256=hashlib.sha256(
                    b"approved attachment bytes"
                ).hexdigest(),
                uploaded_by=self.officer,
            )
            attachment = CorrespondenceAttachment.objects.filter(
                pk=attachment.pk
            ).finalize_locked_scan(
                expected_storage_key=attachment.storage_key,
                scan_status=CorrespondenceAttachment.ScanStatus.CLEAN,
                scanned_at=timezone.now(),
                promoted_storage_key=(
                    f"correspondence/attachments/{draft.pk}/{attachment.pk}"
                ),
            )
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
        return approved, delivery, attachment

    def _run_together(self, *callbacks):
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

    def _wait_for_backend_lock(self, backend_pid, *, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT wait_event_type
                    FROM pg_stat_activity
                    WHERE pid = %s
                    """,
                    [backend_pid],
                )
                row = cursor.fetchone()
            if row and row[0] == "Lock":
                return True
            time.sleep(0.02)
        return False

    def _claim_callback(self, delivery_id, actor_id):
        def callback():
            actor = get_user_model().objects.get(pk=actor_id)
            try:
                claim = services.claim_delivery(
                    delivery_id,
                    actor=actor,
                    credential=Token.objects.get(user=actor),
                    now=self.now,
                )
            except services.DeliveryClaimConflict as exc:
                return ("conflict", exc.code)
            return ("claimed", claim.attempt.id)

        return callback

    def _result_callback(
        self,
        delivery_id,
        attempt_id,
        *,
        provider_message_id,
        provider_thread_id,
        provider_timestamp,
    ):
        def callback():
            close_old_connections()
            try:
                actor = get_user_model().objects.get(pk=self.integration.pk)
                try:
                    result = services.report_delivery_result(
                        delivery_id,
                        actor=actor,
                        credential=Token.objects.get(user=actor),
                        attempt_id=attempt_id,
                        outcome="sent",
                        provider_message_id=provider_message_id,
                        provider_thread_id=provider_thread_id,
                        provider_timestamp=provider_timestamp,
                        provider_metadata={"http_status": 200},
                        now=provider_timestamp + dt.timedelta(seconds=1),
                    )
                except services.DeliveryResultConflict as exc:
                    return ("conflict", exc.code, provider_message_id)
                return ("recorded", result.idempotent, provider_message_id)
            finally:
                close_old_connections()

        return callback

    def test_simultaneous_claims_create_one_attempt_and_one_conflict(self):
        _, delivery, _ = self._queue_delivery()

        results = self._run_together(
            self._claim_callback(delivery.id, self.integration.pk),
            self._claim_callback(delivery.id, self.other_integration.pk),
        )

        self.assertCountEqual(
            [result[0] for result in results],
            ["claimed", "conflict"],
        )
        conflict = next(result for result in results if result[0] == "conflict")
        self.assertEqual(conflict[1], "delivery_claim_in_progress")
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.CLAIMED)
        self.assertEqual(delivery.attempt_count, 1)
        self.assertIn(
            delivery.claim_owner,
            {str(self.integration.pk), str(self.other_integration.pk)},
        )
        self.assertEqual(
            CorrespondenceDeliveryAttempt.objects.filter(
                delivery=delivery
            ).count(),
            1,
        )

    def test_simultaneous_submission_receipt_queue_creates_one_delivery(self):
        def queue_receipt():
            prr = PublicRecordsRequest.objects.get(pk=self.prr.pk)
            return services.queue_submission_receipt(prr).created

        with patch("apps.correspondence.tasks.enqueue_queued_delivery"):
            results = self._run_together(queue_receipt, queue_receipt)

        self.assertCountEqual(results, [True, False])
        self.assertEqual(
            Correspondence.objects.filter(
                request=self.prr,
                kind=Correspondence.Kind.SUBMISSION_RECEIPT,
            ).count(),
            1,
        )
        self.assertEqual(
            CorrespondenceDelivery.objects.filter(
                correspondence__request=self.prr,
                correspondence__kind=Correspondence.Kind.SUBMISSION_RECEIPT,
            ).count(),
            1,
        )

    def test_simultaneous_officer_receipt_drafts_create_one_receipt(self):
        """Two officers clicking send must not mail the requester twice."""
        def draft_receipt():
            prr = PublicRecordsRequest.objects.get(pk=self.prr.pk)
            try:
                services.create_officer_submission_receipt_draft(
                    prr,
                    actor=self.officer,
                )
            except ValidationError:
                return "refused"
            return "created"

        results = self._run_together(draft_receipt, draft_receipt)

        self.assertCountEqual(results, ["created", "refused"])
        self.assertEqual(
            Correspondence.objects.filter(
                request=self.prr,
                kind=Correspondence.Kind.SUBMISSION_RECEIPT,
            ).count(),
            1,
        )

    def test_officer_receipt_races_the_automated_intake_receipt(self):
        """The human and automated paths take the same lock, so one wins."""
        def draft_receipt():
            prr = PublicRecordsRequest.objects.get(pk=self.prr.pk)
            try:
                services.create_officer_submission_receipt_draft(
                    prr,
                    actor=self.officer,
                )
            except ValidationError:
                return "refused"
            return "created"

        def queue_receipt():
            prr = PublicRecordsRequest.objects.get(pk=self.prr.pk)
            try:
                services.queue_submission_receipt(prr)
            except ValidationError:
                return "refused"
            return "created"

        with patch("apps.correspondence.tasks.enqueue_queued_delivery"):
            results = self._run_together(draft_receipt, queue_receipt)

        self.assertIn("created", results)
        self.assertEqual(
            Correspondence.objects.filter(
                request=self.prr,
                kind=Correspondence.Kind.SUBMISSION_RECEIPT,
            ).count(),
            1,
        )

    def test_the_database_refuses_a_second_receipt_outside_the_service(self):
        """The invariant survives a caller that skips the service check."""
        prr = PublicRecordsRequest.objects.get(pk=self.prr.pk)
        services.create_officer_submission_receipt_draft(
            prr,
            actor=self.officer,
        )
        existing = Correspondence.objects.get(
            request=prr,
            kind=Correspondence.Kind.SUBMISSION_RECEIPT,
        )

        with self.assertRaises(IntegrityError):
            Correspondence.objects.create(
                request=prr,
                kind=Correspondence.Kind.SUBMISSION_RECEIPT,
                direction=existing.direction,
                status=Correspondence.Status.DRAFT,
                draft_subject=existing.draft_subject,
                draft_body=existing.draft_body,
                template_version=existing.template_version,
                created_by=self.officer,
            )

    def test_token_revocation_waits_for_claim_boundary_to_commit(self):
        _, delivery, _ = self._queue_delivery()
        credential = Token.objects.get(user=self.integration)
        credential_key = credential.pk
        identity_guard_complete = threading.Event()
        allow_claim_to_finish = threading.Event()
        revocation_started = threading.Event()
        revocation_pid = []
        original_lock = services._lock_correspondence_and_delivery

        def blocking_delivery_lock(delivery_reference):
            identity_guard_complete.set()
            if not allow_claim_to_finish.wait(timeout=10):
                raise AssertionError("Delivery claim was never released.")
            return original_lock(delivery_reference)

        def claim():
            close_old_connections()
            try:
                actor = get_user_model().objects.get(pk=self.integration.pk)
                presented = Token.objects.get(pk=credential_key)
                return services.claim_delivery(
                    delivery.id,
                    actor=actor,
                    credential=presented,
                    now=self.now,
                )
            finally:
                close_old_connections()

        def revoke_token():
            close_old_connections()
            try:
                connection.ensure_connection()
                revocation_pid.append(connection.connection.info.backend_pid)
                revocation_started.set()
                Token.objects.filter(pk=credential_key).delete()
            finally:
                close_old_connections()

        with (
            patch(
                "apps.correspondence.services."
                "_lock_correspondence_and_delivery",
                side_effect=blocking_delivery_lock,
            ),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            claim_future = executor.submit(claim)
            self.assertTrue(identity_guard_complete.wait(timeout=10))
            revocation_future = executor.submit(revoke_token)
            self.assertTrue(revocation_started.wait(timeout=10))
            try:
                self.assertTrue(
                    self._wait_for_backend_lock(revocation_pid[0]),
                    "Token revocation bypassed the presented-token row lock.",
                )
                self.assertFalse(revocation_future.done())
            finally:
                allow_claim_to_finish.set()
            completed_claim = claim_future.result(timeout=20)
            revocation_future.result(timeout=20)

        delivery.refresh_from_db()
        self.assertEqual(
            completed_claim.delivery.status,
            CorrespondenceDelivery.Status.CLAIMED,
        )
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.CLAIMED)
        self.assertFalse(Token.objects.filter(pk=credential_key).exists())

    def test_recipient_update_waits_for_approval_snapshot_and_then_fails_closed(self):
        draft = services.create_authored_draft(
            self.prr,
            kind=Correspondence.Kind.GENERAL,
            subject="Concurrent approval",
            body="The approved recipient snapshot must remain exact.",
            actor=self.officer,
        )
        recipient = draft.recipients.get()
        snapshot_captured = threading.Event()
        allow_approval_to_finish = threading.Event()
        mutation_started = threading.Event()
        mutation_pid = []
        original_snapshot = services._recipient_snapshot_evidence

        def blocking_snapshot(correspondence):
            snapshot = original_snapshot(correspondence)
            snapshot_captured.set()
            if not allow_approval_to_finish.wait(timeout=10):
                raise AssertionError("Approval snapshot was never released.")
            return snapshot

        def approve():
            close_old_connections()
            try:
                actor = get_user_model().objects.get(pk=self.officer.pk)
                correspondence = Correspondence.objects.get(pk=draft.pk)
                return services.approve_draft(
                    correspondence,
                    actor=actor,
                    expected_version=correspondence.version,
                )
            finally:
                close_old_connections()

        def mutate_recipient():
            close_old_connections()
            try:
                connection.ensure_connection()
                mutation_pid.append(connection.connection.info.backend_pid)
                mutation_started.set()
                try:
                    CorrespondenceRecipient.objects.filter(
                        pk=recipient.pk
                    ).update(display_name="Late concurrent rewrite")
                except ValidationError:
                    return "rejected"
                return "changed"
            finally:
                close_old_connections()

        with (
            patch(
                "apps.correspondence.services._recipient_snapshot_evidence",
                side_effect=blocking_snapshot,
            ),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            approval_future = executor.submit(approve)
            self.assertTrue(snapshot_captured.wait(timeout=10))
            mutation_future = executor.submit(mutate_recipient)
            self.assertTrue(mutation_started.wait(timeout=10))
            waited_for_parent = self._wait_for_backend_lock(mutation_pid[0])
            allow_approval_to_finish.set()
            approval_future.result(timeout=20)
            mutation_result = mutation_future.result(timeout=20)

        self.assertTrue(waited_for_parent)
        self.assertEqual(mutation_result, "rejected")
        draft.refresh_from_db()
        recipient.refresh_from_db()
        self.assertEqual(draft.status, Correspondence.Status.APPROVED)
        self.assertNotEqual(recipient.display_name, "Late concurrent rewrite")

    def _make_attachment_approval_draft(self, *, with_attachment=False):
        draft = services.create_authored_draft(
            self.prr,
            kind=Correspondence.Kind.GENERAL,
            subject="Concurrent attachment approval",
            body="The approved attachment snapshot must remain exact.",
            actor=self.officer,
        )
        if not with_attachment:
            return draft, None
        attachment = CorrespondenceAttachment.objects.create(
            correspondence=draft,
            original_filename="approval-evidence.pdf",
            content_type="application/pdf",
            size=len(b"approved attachment bytes"),
            sha256=hashlib.sha256(b"approved attachment bytes").hexdigest(),
            uploaded_by=self.officer,
        )
        attachment = CorrespondenceAttachment.objects.filter(
            pk=attachment.pk
        ).finalize_locked_scan(
            expected_storage_key=attachment.storage_key,
            scan_status=CorrespondenceAttachment.ScanStatus.CLEAN,
            scanned_at=timezone.now(),
            promoted_storage_key=(
                f"correspondence/attachments/{draft.pk}/{attachment.pk}"
            ),
        )
        return draft, attachment

    def _race_attachment_mutation_against_approval(self, draft, mutation):
        validation_complete = threading.Event()
        allow_approval_to_finish = threading.Event()
        mutation_started = threading.Event()
        mutation_pid = []
        original_validation = services._validate_for_approval

        def blocking_validation(correspondence):
            result = original_validation(correspondence)
            validation_complete.set()
            if not allow_approval_to_finish.wait(timeout=10):
                raise AssertionError("Approval validation was never released.")
            return result

        def approve():
            close_old_connections()
            try:
                actor = get_user_model().objects.get(pk=self.officer.pk)
                correspondence = Correspondence.objects.get(pk=draft.pk)
                return services.approve_draft(
                    correspondence,
                    actor=actor,
                    expected_version=correspondence.version,
                )
            finally:
                close_old_connections()

        def mutate_attachment():
            close_old_connections()
            try:
                connection.ensure_connection()
                mutation_pid.append(connection.connection.info.backend_pid)
                mutation_started.set()
                try:
                    mutation()
                except ValidationError:
                    return "rejected"
                return "changed"
            finally:
                close_old_connections()

        with (
            patch(
                "apps.correspondence.services._validate_for_approval",
                side_effect=blocking_validation,
            ),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            approval_future = executor.submit(approve)
            self.assertTrue(validation_complete.wait(timeout=10))
            mutation_future = executor.submit(mutate_attachment)
            self.assertTrue(mutation_started.wait(timeout=10))
            waited_for_parent = self._wait_for_backend_lock(mutation_pid[0])
            allow_approval_to_finish.set()
            approval_future.result(timeout=20)
            mutation_result = mutation_future.result(timeout=20)

        self.assertTrue(waited_for_parent)
        self.assertEqual(mutation_result, "rejected")
        draft.refresh_from_db()
        self.assertEqual(draft.status, Correspondence.Status.APPROVED)

    def test_attachment_create_waits_for_approval_and_then_fails_closed(self):
        draft, _ = self._make_attachment_approval_draft()

        self._race_attachment_mutation_against_approval(
            draft,
            lambda: CorrespondenceAttachment.objects.create(
                correspondence_id=draft.pk,
                original_filename="late-pending.pdf",
                content_type="application/pdf",
                size=123,
                sha256="a" * 64,
                uploaded_by_id=self.officer.pk,
            ),
        )

        self.assertFalse(draft.attachments.exists())

    def test_attachment_bulk_create_waits_for_approval_and_then_fails_closed(self):
        draft, _ = self._make_attachment_approval_draft()

        self._race_attachment_mutation_against_approval(
            draft,
            lambda: CorrespondenceAttachment.objects.bulk_create(
                [
                    CorrespondenceAttachment(
                        correspondence_id=draft.pk,
                        original_filename="late-bulk-pending.pdf",
                        content_type="application/pdf",
                        size=123,
                        sha256="b" * 64,
                        uploaded_by_id=self.officer.pk,
                    )
                ]
            ),
        )

        self.assertFalse(draft.attachments.exists())

    def test_attachment_delete_waits_for_approval_and_then_fails_closed(self):
        draft, attachment = self._make_attachment_approval_draft(
            with_attachment=True
        )

        self._race_attachment_mutation_against_approval(
            draft,
            lambda: CorrespondenceAttachment.objects.filter(
                pk=attachment.pk
            ).delete(),
        )

        self.assertTrue(draft.attachments.filter(pk=attachment.pk).exists())

    def test_attachment_update_waits_for_approval_and_then_fails_closed(self):
        draft, attachment = self._make_attachment_approval_draft(
            with_attachment=True
        )

        self._race_attachment_mutation_against_approval(
            draft,
            lambda: CorrespondenceAttachment.objects.filter(
                pk=attachment.pk
            ).update(original_filename="late-rewrite.pdf"),
        )

        attachment.refresh_from_db()
        self.assertEqual(attachment.original_filename, "approval-evidence.pdf")

    def test_denial_basis_delete_waits_for_approval_and_then_fails_closed(self):
        exemption = ExemptionCitation.objects.create(
            citation="RCW 42.56.250",
            short_name="Examination questions",
        )
        draft = services.create_authored_draft(
            self.prr,
            kind=Correspondence.Kind.DENIAL,
            subject="Concurrent denial",
            body=(
                "The requested records are fully exempt under RCW 42.56.250."
            ),
            actor=self.officer,
            denial_exemption_citation_ids=[exemption.pk],
        )
        basis = draft.denial_citations.get()
        snapshot_captured = threading.Event()
        allow_approval_to_finish = threading.Event()
        deletion_started = threading.Event()
        deletion_pid = []
        original_evidence = services.denial_basis_evidence

        def blocking_evidence(correspondence, **kwargs):
            evidence = original_evidence(correspondence, **kwargs)
            snapshot_captured.set()
            if not allow_approval_to_finish.wait(timeout=10):
                raise AssertionError("Denial approval was never released.")
            return evidence

        def approve():
            close_old_connections()
            try:
                actor = get_user_model().objects.get(pk=self.officer.pk)
                correspondence = Correspondence.objects.get(pk=draft.pk)
                return services.approve_draft(
                    correspondence,
                    actor=actor,
                    expected_version=correspondence.version,
                )
            finally:
                close_old_connections()

        def delete_basis():
            close_old_connections()
            try:
                connection.ensure_connection()
                deletion_pid.append(connection.connection.info.backend_pid)
                deletion_started.set()
                try:
                    CorrespondenceDenialCitation.objects.filter(
                        pk=basis.pk
                    ).delete()
                except ValidationError:
                    return "rejected"
                return "deleted"
            finally:
                close_old_connections()

        with (
            patch(
                "apps.correspondence.services.denial_basis_evidence",
                side_effect=blocking_evidence,
            ),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            approval_future = executor.submit(approve)
            self.assertTrue(snapshot_captured.wait(timeout=10))
            deletion_future = executor.submit(delete_basis)
            self.assertTrue(deletion_started.wait(timeout=10))
            waited_for_parent = self._wait_for_backend_lock(deletion_pid[0])
            allow_approval_to_finish.set()
            approval_future.result(timeout=20)
            deletion_result = deletion_future.result(timeout=20)

        self.assertTrue(waited_for_parent)
        self.assertEqual(deletion_result, "rejected")
        draft.refresh_from_db()
        self.assertEqual(draft.status, Correspondence.Status.APPROVED)
        self.assertTrue(draft.denial_citations.filter(pk=basis.pk).exists())

    def test_simultaneous_lifecycle_queues_serialize_on_request(self):
        acknowledgment = services.create_acknowledgment_draft(
            self.prr,
            actor=self.officer,
        )
        clarification = services.create_clarification_draft(
            self.prr,
            clarification_questions="Which schools and years are in scope?",
            clarification_due=dt.date(2026, 8, 10),
            actor=self.officer,
        )
        approved = [
            services.approve_draft(
                correspondence,
                actor=self.officer,
                expected_version=correspondence.version,
            )
            for correspondence in (acknowledgment, clarification)
        ]

        def queue_callback(correspondence_id):
            def callback():
                actor = get_user_model().objects.get(pk=self.officer.pk)
                correspondence = Correspondence.objects.get(
                    pk=correspondence_id
                )
                try:
                    delivery = services.queue_approved(
                        correspondence,
                        actor=actor,
                        expected_version=correspondence.version,
                    )
                except ValidationError as exc:
                    return ("rejected", " ".join(exc.messages))
                return ("queued", str(delivery.id))

            return callback

        with patch("apps.correspondence.tasks.enqueue_queued_delivery"):
            results = self._run_together(
                *(queue_callback(correspondence.id) for correspondence in approved)
            )

        self.assertCountEqual(
            [result[0] for result in results],
            ["queued", "rejected"],
        )
        rejection = next(result for result in results if result[0] == "rejected")
        self.assertIn(
            "Only one requester-lifecycle message may be outstanding",
            rejection[1],
        )
        self.assertEqual(
            Correspondence.objects.filter(
                request=self.prr,
                status=Correspondence.Status.QUEUED,
            ).count(),
            1,
        )
        self.assertEqual(
            Correspondence.objects.filter(
                request=self.prr,
                status=Correspondence.Status.APPROVED,
            ).count(),
            1,
        )
        self.assertEqual(
            CorrespondenceDelivery.objects.filter(
                correspondence__request=self.prr
            ).count(),
            1,
        )

    def test_simultaneous_identical_results_apply_success_once(self):
        correspondence, delivery, _ = self._queue_delivery(
            kind=Correspondence.Kind.ACKNOWLEDGMENT
        )
        claim = services.claim_delivery(
            delivery,
            actor=self.integration,
            credential=Token.objects.get(user=self.integration),
            now=self.now,
        )
        provider_timestamp = self.now + dt.timedelta(seconds=10)
        callback = self._result_callback(
            delivery.id,
            claim.attempt.id,
            provider_message_id=f"gmail-{delivery.id}",
            provider_thread_id="concurrent-identical-thread",
            provider_timestamp=provider_timestamp,
        )

        results = self._run_together(callback, callback)

        self.assertCountEqual(
            [(result[0], result[1]) for result in results],
            [("recorded", False), ("recorded", True)],
        )
        delivery.refresh_from_db()
        correspondence.refresh_from_db()
        self.prr.refresh_from_db()
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.SENT)
        self.assertEqual(correspondence.status, Correspondence.Status.SENT)
        self.assertEqual(self.prr.status, RequestStatus.ACKNOWLEDGED)
        self.assertEqual(
            StatusEvent.objects.filter(
                request=self.prr,
                to_status=RequestStatus.ACKNOWLEDGED,
            ).count(),
            1,
        )
        self.assertEqual(
            CorrespondenceDeliveryAttempt.objects.filter(
                delivery=delivery
            ).count(),
            1,
        )

    def test_simultaneous_conflicting_results_record_one_and_reject_one(self):
        _, delivery, _ = self._queue_delivery()
        claim = services.claim_delivery(
            delivery,
            actor=self.integration,
            credential=Token.objects.get(user=self.integration),
            now=self.now,
        )
        provider_timestamp = self.now + dt.timedelta(seconds=10)

        results = self._run_together(
            self._result_callback(
                delivery.id,
                claim.attempt.id,
                provider_message_id="gmail-concurrent-a",
                provider_thread_id="gmail-thread-a",
                provider_timestamp=provider_timestamp,
            ),
            self._result_callback(
                delivery.id,
                claim.attempt.id,
                provider_message_id="gmail-concurrent-b",
                provider_thread_id="gmail-thread-b",
                provider_timestamp=provider_timestamp,
            ),
        )

        self.assertCountEqual(
            [result[0] for result in results],
            ["recorded", "conflict"],
        )
        conflict = next(result for result in results if result[0] == "conflict")
        self.assertEqual(conflict[1], "delivery_result_conflict")
        recorded = next(result for result in results if result[0] == "recorded")
        self.assertFalse(recorded[1])
        delivery.refresh_from_db()
        attempt = CorrespondenceDeliveryAttempt.objects.get(pk=claim.attempt.pk)
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.SENT)
        self.assertEqual(delivery.provider_message_id, recorded[2])
        self.assertEqual(attempt.provider_message_id, recorded[2])
        self.assertEqual(
            CorrespondenceDeliveryAttempt.objects.filter(
                delivery=delivery
            ).count(),
            1,
        )

    @override_settings(
        PRR_GMAIL_ENABLED=True,
        PRR_DEPLOYMENT_MODE="production",
        PRR_ENVIRONMENT="production",
    )
    def test_simultaneous_watchdogs_dispatch_exactly_one_gmail_send(self):
        _, delivery, _ = self._queue_delivery()
        delivery.recovery_not_before = self.now - dt.timedelta(seconds=1)
        delivery.save(update_fields=["recovery_not_before"])
        observed_at = self.now + dt.timedelta(hours=2)

        def watch():
            return services.watch_stuck_deliveries(
                now=observed_at,
                limit=10,
            )

        def execute_task(delivery_id, *, attempt_count):
            tasks.send_queued_delivery.run(
                delivery_id,
                attempt_count=attempt_count,
            )
            return True

        with (
            patch(
                "apps.correspondence.tasks.gmail_client.send_raw",
                return_value=SentMessage(
                    message_id="single-watchdog-send",
                    thread_id="single-watchdog-thread",
                ),
            ) as send_raw,
            patch(
                "apps.correspondence.tasks.enqueue_queued_delivery",
                side_effect=execute_task,
            ) as enqueue,
        ):
            results = self._run_together(watch, watch)

        delivery.refresh_from_db()
        self.assertEqual(
            sum(result.nudged_deliveries for result in results),
            1,
        )
        self.assertEqual(
            sum(result.expired_claims for result in results),
            0,
        )
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.SENT)
        self.assertEqual(delivery.attempts.count(), 1)
        enqueue.assert_called_once_with(str(delivery.id), attempt_count=0)
        send_raw.assert_called_once()

    @override_settings(
        PRR_GMAIL_ENABLED=True,
        PRR_DEPLOYMENT_MODE="production",
        PRR_ENVIRONMENT="production",
    )
    def test_lease_expiry_racing_recovery_reconciles_without_second_send(self):
        correspondence, delivery, _ = self._queue_delivery()
        original_message_id = correspondence.rfc_message_id
        sent = SentMessage(
            message_id="sent-before-expiry-race",
            thread_id="sent-before-expiry-race-thread",
        )
        with (
            patch(
                "apps.correspondence.tasks.gmail_client.send_raw",
                return_value=sent,
            ) as initial_send,
            patch(
                "apps.correspondence.tasks.services.report_delivery_result",
                side_effect=RuntimeError("worker stopped before result"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "worker stopped"):
                tasks.send_queued_delivery.run(
                    str(delivery.id),
                    attempt_count=0,
                )

        delivery.refresh_from_db()
        first_attempt = delivery.attempts.get(attempt_number=1)
        observed_at = timezone.now()
        CorrespondenceDelivery.objects.filter(pk=delivery.pk).update(
            claim_expires_at=observed_at - dt.timedelta(seconds=1),
        )

        def watch():
            return services.watch_stuck_deliveries(
                now=observed_at,
                limit=10,
            )

        def recover():
            return tasks.send_queued_delivery.run(
                str(delivery.id),
                attempt_count=1,
            )

        with (
            patch("apps.correspondence.tasks.gmail_client.send_raw") as resend,
            patch(
                "apps.correspondence.tasks.gmail_client.find_by_rfc822_message_id",
                return_value=sent,
            ) as find_message,
            patch("apps.correspondence.tasks.enqueue_queued_delivery"),
        ):
            self._run_together(watch, recover)

        delivery.refresh_from_db()
        correspondence.refresh_from_db()
        first_attempt.refresh_from_db()
        attempts = list(delivery.attempts.order_by("attempt_number"))
        self.assertEqual(initial_send.call_count, 1)
        resend.assert_not_called()
        find_message.assert_called_once_with(original_message_id)
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.SENT)
        self.assertEqual(delivery.attempt_count, 2)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(
            first_attempt.result_category,
            CorrespondenceDeliveryAttempt.ResultCategory.LEASE_EXPIRED,
        )
        self.assertEqual(
            attempts[1].action,
            CorrespondenceDeliveryAttempt.Action.RECONCILE,
        )
        self.assertEqual(
            attempts[1].result_category,
            CorrespondenceDeliveryAttempt.ResultCategory.RECONCILED,
        )
        self.assertEqual(correspondence.status, Correspondence.Status.SENT)
        self.assertEqual(correspondence.rfc_message_id, original_message_id)

    def test_watchdog_waits_for_success_callback_and_cannot_overwrite_it(self):
        correspondence, delivery, _ = self._queue_delivery()
        claim = services.claim_delivery(
            delivery,
            actor=self.integration,
            credential=Token.objects.get(user=self.integration),
            now=self.now,
        )
        provider_timestamp = self.now + dt.timedelta(seconds=10)
        callback = self._result_callback(
            delivery.id,
            claim.attempt.id,
            provider_message_id=f"gmail-{delivery.id}",
            provider_thread_id="watchdog-race-thread",
            provider_timestamp=provider_timestamp,
        )
        success_started = threading.Event()
        allow_success = threading.Event()
        watchdog_started = threading.Event()
        original_record_success = services._record_successful_result

        def blocking_record_success(**kwargs):
            success_started.set()
            if not allow_success.wait(timeout=10):
                raise AssertionError("Success callback was never released.")
            return original_record_success(**kwargs)

        def watch():
            close_old_connections()
            try:
                watchdog_started.set()
                return services.watch_stuck_deliveries(
                    now=(
                        claim.lease_expires_at
                        + dt.timedelta(microseconds=1)
                    ),
                    limit=10,
                )
            finally:
                close_old_connections()

        with (
            patch(
                "apps.correspondence.services._record_successful_result",
                side_effect=blocking_record_success,
            ),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            callback_future = executor.submit(callback)
            self.assertTrue(success_started.wait(timeout=10))
            watchdog_future = executor.submit(watch)
            self.assertTrue(watchdog_started.wait(timeout=10))
            allow_success.set()
            callback_result = callback_future.result(timeout=20)
            watchdog_result = watchdog_future.result(timeout=20)

        delivery.refresh_from_db()
        correspondence.refresh_from_db()
        claim.attempt.refresh_from_db()
        self.assertEqual(callback_result[0], "recorded")
        self.assertFalse(callback_result[1])
        self.assertEqual(watchdog_result.expired_claims, 0)
        self.assertEqual(watchdog_result.nudged_deliveries, 0)
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.SENT)
        self.assertEqual(correspondence.status, Correspondence.Status.SENT)
        self.assertEqual(
            claim.attempt.result_category,
            CorrespondenceDeliveryAttempt.ResultCategory.SENT,
        )

    def test_attachment_open_serializes_simultaneous_quarantine_revocation(self):
        _, delivery, attachment = self._queue_delivery(with_attachment=True)
        claim = services.claim_delivery(
            delivery,
            actor=self.integration,
            credential=Token.objects.get(user=self.integration),
            now=self.now,
        )
        open_started = threading.Event()
        allow_open_to_finish = threading.Event()
        revocation_started = threading.Event()
        revocation_finished = threading.Event()

        def blocking_open(_attachment):
            open_started.set()
            if not allow_open_to_finish.wait(timeout=10):
                raise AssertionError("Attachment open was never released.")
            return BytesIO(b"approved attachment bytes")

        def open_attachment():
            close_old_connections()
            try:
                actor = get_user_model().objects.get(pk=self.integration.pk)
                opened = services.open_claim_attachment(
                    delivery.id,
                    attempt_id=claim.attempt.id,
                    attachment_id=attachment.id,
                    actor=actor,
                    credential=Token.objects.get(user=actor),
                    now=self.now,
                )
                return opened.body.read()
            finally:
                close_old_connections()

        def revoke_attachment():
            close_old_connections()
            try:
                revocation_started.set()
                CorrespondenceAttachment.objects.filter(
                    pk=attachment.pk
                ).requarantine_locked(
                    quarantine_reason=(
                        "Clean release was revoked after security review."
                    ),
                    quarantined_at=timezone.now(),
                )
            finally:
                revocation_finished.set()
                close_old_connections()

        with patch(
            "apps.correspondence.services.open_clean_attachment",
            side_effect=blocking_open,
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                open_future = executor.submit(open_attachment)
                self.assertTrue(
                    open_started.wait(timeout=10),
                    "Attachment open never reached object storage.",
                )
                revoke_future = executor.submit(revoke_attachment)
                self.assertTrue(
                    revocation_started.wait(timeout=10),
                    "Attachment revocation never started.",
                )
                try:
                    self.assertFalse(
                        revocation_finished.wait(timeout=1),
                        "Revocation bypassed the attachment row lock.",
                    )
                finally:
                    allow_open_to_finish.set()

                self.assertEqual(
                    open_future.result(timeout=10),
                    b"approved attachment bytes",
                )
                revoke_future.result(timeout=10)

        attachment.refresh_from_db()
        self.assertEqual(
            attachment.scan_status,
            CorrespondenceAttachment.ScanStatus.ERROR,
        )
        self.assertTrue(attachment.quarantined)
        with self.assertRaises(services.DeliveryAttachmentNotAvailable):
            services.open_claim_attachment(
                delivery.id,
                attempt_id=claim.attempt.id,
                attachment_id=attachment.id,
                actor=self.integration,
                credential=Token.objects.get(user=self.integration),
                now=self.now,
            )
