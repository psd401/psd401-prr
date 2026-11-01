import datetime as dt
import json
import uuid
from contextlib import contextmanager
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, models
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.correspondence import services, transport
from apps.correspondence.models import (
    Correspondence,
    CorrespondenceAttachment,
    CorrespondenceDelivery,
    CorrespondenceDeliveryAttempt,
    CorrespondenceDenialCitation,
    CorrespondenceRecipient,
)
from apps.records import services as records_services
from apps.records.models import (
    ExemptionCitation,
    PublicRecordsRequest,
    Requester,
    RequestStatus,
    StatusEvent,
)


@contextmanager
def _simulate_out_of_band_audit_corruption():
    """Bypass the DB trigger only while testing downstream fail-closed checks.

    Production rejects these rewrites outright. These tests deliberately model
    damage performed outside that guard and verify delivery still rejects the
    altered evidence.
    """
    if connection.vendor == "sqlite":
        with connection.cursor() as cursor:
            cursor.execute(
                "DROP TRIGGER IF EXISTS audit_reject_audit_log_update;"
            )
        try:
            yield
        finally:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TRIGGER audit_reject_audit_log_update
                    BEFORE UPDATE ON audit_auditlog
                    FOR EACH ROW
                    BEGIN
                        SELECT RAISE(ABORT, 'Audit logs are append-only.');
                    END;
                    """
                )
        return
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE audit_auditlog DISABLE TRIGGER "
                "audit_reject_audit_log_mutation;"
            )
        try:
            yield
        finally:
            with connection.cursor() as cursor:
                cursor.execute(
                    "ALTER TABLE audit_auditlog ENABLE TRIGGER "
                    "audit_reject_audit_log_mutation;"
                )
        return
    raise RuntimeError(
        "Audit-corruption tests support only PostgreSQL and SQLite."
    )


@override_settings(
    PRR_LOCAL_IMPLICIT_STAFF_ROLES=(),
    PRR_CORRESPONDENCE_MESSAGE_ID_DOMAIN="mail.prr.test",
    PRR_CORRESPONDENCE_REPLY_DOMAIN="mail.prr.test",
)
class OutboundDeliveryServiceTestCase(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.officer = user_model.objects.create_user(
            "records-officer",
            "officer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.integration = transport.system_transport_principal()
        self.other_integration = user_model.objects.create_user(
            "other-outbound-automation",
            password=None,
            is_staff=False,
        )
        permission = Permission.objects.get(
            content_type__app_label="correspondence",
            codename="deliver_outbound_correspondence",
        )
        self.other_integration.user_permissions.add(permission)
        self.other_integration_token = Token.objects.create(
            user=self.other_integration,
        )
        requester = Requester.objects.create(
            name="Jane Requester",
            email="jane@example.org",
        )
        self.prr = PublicRecordsRequest.objects.create(
            requester=requester,
            description="All records concerning the transportation plan.",
            estimated_completion=dt.date(2026, 8, 3),
        )
        self.denial_exemption = ExemptionCitation.objects.create(
            citation="RCW 42.56.250",
            short_name="Examination questions",
        )
        self.now = timezone.now().replace(microsecond=0)

    def queue_general(
        self,
        *,
        subject="Approved subject",
        body="Approved body",
        with_attachment=False,
        provider="gmail",
        provider_account="default",
    ):
        draft = services.create_authored_draft(
            self.prr,
            kind=Correspondence.Kind.GENERAL,
            subject=subject,
            body=body,
            actor=self.officer,
            recipients=[
                {
                    "recipient_type": CorrespondenceRecipient.RecipientType.TO,
                    "email": "jane@example.org",
                    "display_name": "Jane Requester",
                    "requester": self.prr.requester,
                },
                {
                    "recipient_type": CorrespondenceRecipient.RecipientType.CC,
                    "email": "authorized-copy@example.org",
                    "display_name": "Authorized Copy",
                },
            ],
        )
        draft.provider = provider
        draft.provider_account = provider_account
        draft.save(update_fields=["provider", "provider_account"])
        attachment = None
        if with_attachment:
            attachment = CorrespondenceAttachment.objects.create(
                correspondence=draft,
                original_filename="approved-notice.pdf",
                content_type="application/pdf",
                size=321,
                sha256="a" * 64,
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
        delivery = services.queue_approved(
            approved,
            actor=self.officer,
            expected_version=approved.version,
        )
        approved.refresh_from_db()
        return approved, delivery, attachment

    def queue_acknowledgment(self):
        draft = services.create_acknowledgment_draft(
            self.prr,
            actor=self.officer,
        )
        approved = services.approve_draft(
            draft,
            actor=self.officer,
            expected_version=draft.version,
        )
        delivery = services.queue_approved(
            approved,
            actor=self.officer,
            expected_version=approved.version,
        )
        approved.refresh_from_db()
        return approved, delivery

    def queue_clarification(self):
        draft = services.create_clarification_draft(
            self.prr,
            clarification_questions=(
                "Which schools and years should the search include? "
                "private-message-sentinel"
            ),
            clarification_due=dt.date(2026, 8, 3),
            actor=self.officer,
        )
        approved = services.approve_draft(
            draft,
            actor=self.officer,
            expected_version=draft.version,
        )
        delivery = services.queue_approved(
            approved,
            actor=self.officer,
            expected_version=approved.version,
        )
        approved.refresh_from_db()
        return approved, delivery

    def queue_denial(self):
        draft = services.create_authored_draft(
            self.prr,
            kind=Correspondence.Kind.DENIAL,
            subject="Determination on your public records request",
            body=(
                "The Records Officer has completed legal review and approved "
                "this denial notice for delivery under RCW 42.56.250."
            ),
            actor=self.officer,
            denial_exemption_citation_ids=[self.denial_exemption.pk],
        )
        approved = services.approve_draft(
            draft,
            actor=self.officer,
            expected_version=draft.version,
        )
        delivery = services.queue_approved(
            approved,
            actor=self.officer,
            expected_version=approved.version,
        )
        approved.refresh_from_db()
        return approved, delivery

    def claim(self, delivery, *, actor=None, now=None):
        selected_actor = actor or self.integration
        credential = (
            None
            if selected_actor.pk == self.integration.pk
            else Token.objects.get(user=selected_actor)
        )
        return services.claim_delivery(
            delivery,
            actor=selected_actor,
            credential=credential,
            now=now or self.now,
        )

    def report(self, delivery, claim, *, actor=None, now=None, **overrides):
        selected_actor = actor or self.integration
        credential = (
            None
            if selected_actor.pk == self.integration.pk
            else Token.objects.get(user=selected_actor)
        )
        values = {
            "attempt_id": claim.attempt.id,
            "outcome": "sent",
            "provider_message_id": f"gmail-{delivery.id}",
            "provider_thread_id": "gmail-thread-1",
            "provider_timestamp": self.now + dt.timedelta(seconds=10),
            "provider_metadata": {"http_status": 200},
        }
        values.update(overrides)
        return services.report_delivery_result(
            delivery,
            actor=selected_actor,
            credential=credential,
            now=now or self.now + dt.timedelta(seconds=15),
            **values,
        )


class OutboundDeliveryClaimTests(OutboundDeliveryServiceTestCase):
    def test_stale_deactivated_transport_identity_cannot_claim_delivery(self):
        _, delivery, _ = self.queue_general()
        get_user_model().objects.filter(pk=self.integration.pk).update(
            is_active=False
        )

        with self.assertRaisesMessage(
            PermissionDenied,
            "exact outbound-correspondence transport identity",
        ):
            self.claim(delivery)

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.PENDING)
        self.assertEqual(delivery.attempt_count, 0)
        self.assertFalse(delivery.attempts.exists())

    def _approved_general(self):
        draft = services.create_authored_draft(
            self.prr,
            kind=Correspondence.Kind.GENERAL,
            subject="Approved case update",
            body="The district search remains in progress.",
            actor=self.officer,
        )
        return services.approve_draft(
            draft,
            actor=self.officer,
            expected_version=draft.version,
        )

    def test_unresolved_general_delivery_blocks_denial_before_send(self):
        _, general_delivery, _ = self.queue_general()
        draft = services.create_authored_draft(
            self.prr,
            kind=Correspondence.Kind.DENIAL,
            subject="Determination on your public records request",
            body=(
                "The Records Officer completed legal review and approved "
                "this denial notice under RCW 42.56.250."
            ),
            actor=self.officer,
            denial_exemption_citation_ids=[self.denial_exemption.pk],
        )
        denial = services.approve_draft(
            draft,
            actor=self.officer,
            expected_version=draft.version,
        )

        with self.assertRaisesMessage(
            ValidationError,
            "unresolved correspondence delivery",
        ):
            services.queue_approved(
                denial,
                actor=self.officer,
                expected_version=denial.version,
            )

        general_delivery.refresh_from_db()
        denial.refresh_from_db()
        self.assertEqual(
            general_delivery.status,
            CorrespondenceDelivery.Status.PENDING,
        )
        self.assertEqual(denial.status, Correspondence.Status.APPROVED)
        self.assertFalse(
            CorrespondenceDelivery.objects.filter(
                correspondence=denial
            ).exists()
        )

    def test_claimed_denial_blocks_new_delivery_and_success_remains_durable(self):
        denial, denial_delivery = self.queue_denial()
        claim = self.claim(denial_delivery)
        general = self._approved_general()

        with self.assertRaisesMessage(
            ValidationError,
            "requester-lifecycle delivery",
        ):
            services.queue_approved(
                general,
                actor=self.officer,
                expected_version=general.version,
            )

        self.report(
            denial_delivery,
            claim,
            provider_message_id=f"gmail-denial-{denial_delivery.id}",
            provider_thread_id="denial-thread",
            provider_timestamp=self.now + dt.timedelta(seconds=5),
        )

        denial.refresh_from_db()
        denial_delivery.refresh_from_db()
        general.refresh_from_db()
        self.prr.refresh_from_db()
        self.assertEqual(denial.status, Correspondence.Status.SENT)
        self.assertEqual(
            denial_delivery.status,
            CorrespondenceDelivery.Status.SENT,
        )
        self.assertEqual(general.status, Correspondence.Status.APPROVED)
        self.assertEqual(self.prr.status, RequestStatus.DENIED)
        self.assertIsNotNone(denial.resulting_status_event_id)

    def test_denial_claim_rejects_legacy_unresolved_general_delivery(self):
        _denial, denial_delivery = self.queue_denial()
        general = self._approved_general()
        queued_at = timezone.now()
        Correspondence.objects.filter(pk=general.pk).update(
            status=Correspondence.Status.QUEUED,
            queued_at=queued_at,
        )
        legacy_delivery = CorrespondenceDelivery.objects.create(
            correspondence=general,
            provider=general.provider,
            provider_account=general.provider_account,
            idempotency_key=f"legacy-general:{general.id}",
            queued_at=queued_at,
        )

        with self.assertRaises(services.DeliveryNotClaimable):
            self.claim(denial_delivery)

        denial_delivery.refresh_from_db()
        legacy_delivery.refresh_from_db()
        self.assertEqual(
            denial_delivery.status,
            CorrespondenceDelivery.Status.PENDING,
        )
        self.assertEqual(
            legacy_delivery.status,
            CorrespondenceDelivery.Status.PENDING,
        )
        self.assertFalse(denial_delivery.attempts.exists())

    def test_denial_claim_rechecks_requester_as_to_recipient(self):
        correspondence, delivery = self.queue_denial()
        models.QuerySet(
            model=CorrespondenceRecipient,
            using="default",
        ).filter(correspondence=correspondence).update(
            email="district-counsel@psd401.net",
            display_name="District Counsel",
            requester_id=None,
        )
        recipient_sha256, recipient_count = (
            services._recipient_snapshot_evidence(correspondence)
        )
        approval = AuditLog.objects.get(
            action="correspondence.approved",
            detail__correspondence_id=str(correspondence.pk),
        )
        altered_detail = dict(approval.detail)
        altered_detail["recipient_snapshot_sha256"] = recipient_sha256
        altered_detail["recipient_count"] = recipient_count
        with _simulate_out_of_band_audit_corruption():
            models.QuerySet(model=AuditLog, using="default").filter(
                pk=approval.pk,
            ).update(detail=altered_detail)

        with self.assertRaisesMessage(
            ValidationError,
            "current requester must be a To recipient",
        ):
            self.claim(delivery)

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.PENDING)
        self.assertEqual(delivery.attempt_count, 0)
        self.assertFalse(delivery.attempts.exists())

    def test_claim_fails_closed_when_denial_basis_differs_from_approval(self):
        _, delivery = self.queue_denial()
        replacement = ExemptionCitation.objects.create(
            citation="RCW 42.56.260",
            short_name="Financial and commercial information",
        )
        models.QuerySet(
            model=CorrespondenceDenialCitation,
            using="default",
        ).filter(
            correspondence=delivery.correspondence,
        ).update(exemption_id=replacement.pk)

        with self.assertRaisesMessage(
            ValidationError,
            "Approved denial-basis snapshot integrity check failed",
        ):
            self.claim(delivery)

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.PENDING)
        self.assertEqual(delivery.attempt_count, 0)

    def test_claim_rejects_approved_denial_body_drift_even_with_citation(self):
        correspondence, delivery = self.queue_denial()
        models.QuerySet(
            model=Correspondence,
            using="default",
        ).filter(pk=correspondence.pk).update(
            approved_body=(
                "Altered denial language still names RCW 42.56.250.\n"
            )
        )

        with self.assertRaisesMessage(
            ValidationError,
            "Approved denial-basis snapshot integrity check failed",
        ):
            self.claim(delivery)

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.PENDING)
        self.assertEqual(delivery.attempt_count, 0)
        self.assertFalse(delivery.attempts.exists())

    def test_send_claim_revalidates_live_denial_citation_activity(self):
        _, delivery = self.queue_denial()
        models.QuerySet(
            model=ExemptionCitation,
            using="default",
        ).filter(pk=self.denial_exemption.pk).update(
            active=False,
        )

        with self.assertRaisesMessage(
            ValidationError,
            "at least one active exemption citation",
        ):
            self.claim(delivery)

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.PENDING)
        self.assertEqual(delivery.attempt_count, 0)

    def test_claim_fails_closed_when_recipients_differ_from_approval_snapshot(self):
        correspondence, delivery, _ = self.queue_general()
        approved_recipients = list(
            correspondence.recipients.order_by("created_at", "id").values_list(
                "recipient_type",
                "email",
                "display_name",
                "requester_id",
            )
        )
        approval = AuditLog.objects.get(
            action="correspondence.approved",
            detail__correspondence_id=str(correspondence.id),
        )
        self.assertRegex(
            approval.detail["recipient_snapshot_sha256"],
            r"\A[0-9a-f]{64}\Z",
        )

        # Deliberately bypass every configured model manager to simulate
        # out-of-band database corruption and retain claim-time defense in depth.
        models.QuerySet(
            model=CorrespondenceRecipient,
            using="default",
        ).bulk_create(
            [
                CorrespondenceRecipient(
                    correspondence=correspondence,
                    recipient_type=CorrespondenceRecipient.RecipientType.BCC,
                    email="late-addition@example.org",
                    display_name="Late Addition",
                )
            ]
        )

        with self.assertRaisesMessage(
            ValidationError,
            "Approved recipient snapshot integrity check failed.",
        ):
            self.claim(delivery)

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.PENDING)
        self.assertEqual(delivery.attempt_count, 0)
        self.assertFalse(delivery.attempts.exists())
        self.assertSequenceEqual(
            approved_recipients,
            [
                (
                    CorrespondenceRecipient.RecipientType.TO,
                    "jane@example.org",
                    "Jane Requester",
                    self.prr.requester_id,
                ),
                (
                    CorrespondenceRecipient.RecipientType.CC,
                    "authorized-copy@example.org",
                    "Authorized Copy",
                    None,
                ),
            ],
        )

    def test_queue_preserves_the_approved_provider_scope(self):
        correspondence, delivery, _ = self.queue_general(
            provider="gmail",
            provider_account="secondary-mailbox",
        )

        claim = self.claim(delivery)

        self.assertEqual(delivery.provider, correspondence.provider)
        self.assertEqual(
            delivery.provider_account,
            correspondence.provider_account,
        )
        self.assertEqual(claim.payload["provider"], "gmail")
        self.assertEqual(
            claim.payload["provider_account"],
            "secondary-mailbox",
        )

    def test_claim_rejects_a_legacy_delivery_with_mismatched_provider_scope(self):
        draft = services.create_authored_draft(
            self.prr,
            kind=Correspondence.Kind.GENERAL,
            subject="Approved subject",
            body="Approved body",
            actor=self.officer,
        )
        approved = services.approve_draft(
            draft,
            actor=self.officer,
            expected_version=draft.version,
        )
        queued_at = timezone.now()
        Correspondence.objects.filter(pk=approved.pk).update(
            status=Correspondence.Status.QUEUED,
            queued_at=queued_at,
        )
        legacy_delivery = CorrespondenceDelivery.objects.create(
            correspondence=approved,
            provider=approved.provider,
            provider_account="legacy-mismatched-account",
            idempotency_key=f"legacy-mismatched-scope:{approved.id}",
            queued_at=queued_at,
        )

        with self.assertRaises(services.DeliveryNotClaimable):
            self.claim(legacy_delivery)

        legacy_delivery.refresh_from_db()
        self.assertEqual(
            legacy_delivery.status,
            CorrespondenceDelivery.Status.PENDING,
        )
        self.assertEqual(legacy_delivery.attempt_count, 0)
        self.assertFalse(legacy_delivery.attempts.exists())

    def test_claim_rejects_a_legacy_pending_delivery_after_request_closure(self):
        _, delivery, _ = self.queue_general()
        PublicRecordsRequest.objects.filter(pk=self.prr.pk).update(
            status=RequestStatus.FULFILLED,
            closed_at=self.now,
        )

        with self.assertRaises(services.DeliveryNotClaimable):
            self.claim(delivery)

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.PENDING)
        self.assertEqual(delivery.attempt_count, 0)
        self.assertFalse(delivery.attempts.exists())

    def test_claim_revalidates_current_requester_address_before_send(self):
        draft = services.create_clarification_draft(
            self.prr,
            clarification_questions="Which school years are in scope?",
            clarification_due=dt.date(2026, 8, 3),
            actor=self.officer,
        )
        approved = services.approve_draft(
            draft,
            actor=self.officer,
            expected_version=draft.version,
        )
        delivery = services.queue_approved(
            approved,
            actor=self.officer,
            expected_version=approved.version,
        )
        requester = self.prr.requester
        requester.email = "corrected@example.org"
        requester.save(update_fields=["email"])

        with self.assertRaisesMessage(
            ValidationError,
            "current requester must be a To recipient",
        ):
            self.claim(delivery)

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.PENDING)
        self.assertEqual(delivery.attempt_count, 0)
        self.assertFalse(delivery.attempts.exists())

    def test_claim_rejects_a_legacy_second_outstanding_clarification(self):
        first = services.create_clarification_draft(
            self.prr,
            clarification_questions="Which school years are in scope?",
            clarification_due=dt.date(2026, 8, 3),
            actor=self.officer,
        )
        second = services.create_clarification_draft(
            self.prr,
            clarification_questions="Which departments are in scope?",
            clarification_due=dt.date(2026, 8, 4),
            actor=self.officer,
        )
        first = services.approve_draft(
            first,
            actor=self.officer,
            expected_version=first.version,
        )
        second = services.approve_draft(
            second,
            actor=self.officer,
            expected_version=second.version,
        )
        services.queue_approved(
            first,
            actor=self.officer,
            expected_version=first.version,
        )
        queued_at = timezone.now()
        Correspondence.objects.filter(pk=second.pk).update(
            status=Correspondence.Status.QUEUED,
            queued_at=queued_at,
        )
        second.refresh_from_db()
        legacy_delivery = CorrespondenceDelivery.objects.create(
            correspondence=second,
            provider=second.provider,
            provider_account=second.provider_account,
            idempotency_key=f"legacy-clarification:{second.id}",
            queued_at=queued_at,
        )

        with self.assertRaises(services.DeliveryNotClaimable):
            self.claim(legacy_delivery)

        legacy_delivery.refresh_from_db()
        self.assertEqual(
            legacy_delivery.status,
            CorrespondenceDelivery.Status.PENDING,
        )
        self.assertFalse(legacy_delivery.attempts.exists())

    def test_pending_claim_creates_immutable_send_attempt_bound_to_actor(self):
        correspondence, delivery, _ = self.queue_general()

        claim = self.claim(delivery)

        delivery.refresh_from_db()
        attempt = CorrespondenceDeliveryAttempt.objects.get(delivery=delivery)
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.CLAIMED)
        self.assertEqual(delivery.claim_owner, str(self.integration.pk))
        self.assertEqual(delivery.attempt_count, 1)
        self.assertEqual(delivery.claim_expires_at, claim.lease_expires_at)
        self.assertGreater(claim.lease_expires_at, self.now)
        self.assertEqual(claim.attempt.pk, attempt.pk)
        self.assertEqual(
            claim.action,
            CorrespondenceDeliveryAttempt.Action.SEND,
        )
        self.assertEqual(
            attempt.action,
            CorrespondenceDeliveryAttempt.Action.SEND,
        )
        self.assertEqual(attempt.actor, self.integration)
        self.assertEqual(attempt.claim_owner, str(self.integration.pk))
        self.assertEqual(attempt.claimed_at, self.now)
        self.assertIsNone(attempt.result_at)

        attempt.action = CorrespondenceDeliveryAttempt.Action.RECONCILE
        with self.assertRaises(ValidationError):
            attempt.save(update_fields=["action"])

        payload = claim.payload
        self.assertSetEqual(
            set(payload),
            {
                "schema_version",
                "action",
                "delivery_id",
                "correspondence_id",
                "attempt_id",
                "attempt_number",
                "lease_expires_at",
                "provider",
                "provider_account",
                "message",
            },
        )
        self.assertEqual(payload["schema_version"], "1")
        self.assertEqual(payload["action"], "send")
        self.assertEqual(payload["delivery_id"], str(delivery.id))
        self.assertEqual(payload["correspondence_id"], str(correspondence.id))
        self.assertEqual(payload["attempt_id"], str(attempt.id))
        self.assertEqual(payload["attempt_number"], attempt.attempt_number)
        self.assertEqual(
            payload["lease_expires_at"],
            claim.lease_expires_at.isoformat(),
        )
        self.assertEqual(payload["provider"], "gmail")
        self.assertEqual(payload["provider_account"], "default")
        self.assertSetEqual(
            set(payload["message"]),
            {"subject", "body", "recipients", "headers", "attachments"},
        )
        self.assertEqual(payload["message"]["subject"], "Approved subject")
        self.assertEqual(payload["message"]["body"], "Approved body\n")
        self.assertEqual(
            payload["message"]["recipients"],
            {
                "to": [
                    {
                        "email": "jane@example.org",
                        "display_name": "Jane Requester",
                    }
                ],
                "cc": [
                    {
                        "email": "authorized-copy@example.org",
                        "display_name": "Authorized Copy",
                    }
                ],
                "bcc": [],
            },
        )
        self.assertSetEqual(
            set(payload["message"]["headers"]),
            {"Message-ID", "Reply-To"},
        )
        self.assertEqual(
            payload["message"]["headers"]["Message-ID"],
            correspondence.rfc_message_id,
        )
        reply_to = payload["message"]["headers"]["Reply-To"]
        self.assertIn(str(correspondence.reply_token), reply_to)
        self.assertTrue(reply_to.endswith("@mail.prr.test"))
        self.assertEqual(payload["message"]["attachments"], [])
        serialized = json.dumps(payload)
        self.assertNotIn("draft_subject", serialized)
        self.assertNotIn("draft_body", serialized)
        self.assertNotIn(str(self.prr.id), serialized)
        self.assertNotIn(self.prr.number, serialized)

    def test_active_lease_conflicts_deterministically_even_for_same_actor(self):
        _, delivery, _ = self.queue_general()
        first = self.claim(delivery)

        conflicts = []
        for actor in (self.integration, self.other_integration):
            with self.subTest(actor=actor.username):
                with self.assertRaises(services.DeliveryClaimConflict) as raised:
                    self.claim(
                        delivery,
                        actor=actor,
                        now=self.now + dt.timedelta(seconds=1),
                    )
                conflicts.append((raised.exception.code, str(raised.exception)))

        self.assertEqual(
            conflicts,
            [
                ("delivery_claim_in_progress", conflicts[0][1]),
                ("delivery_claim_in_progress", conflicts[0][1]),
            ],
        )
        self.assertNotIn(self.integration.username, conflicts[0][1])
        self.assertNotIn(self.other_integration.username, conflicts[0][1])
        delivery.refresh_from_db()
        self.assertEqual(delivery.attempt_count, 1)
        self.assertEqual(delivery.claim_expires_at, first.lease_expires_at)
        self.assertEqual(
            CorrespondenceDeliveryAttempt.objects.filter(delivery=delivery).count(),
            1,
        )

    def test_expired_send_claim_is_finalized_then_reclaimed_for_reconciliation(self):
        correspondence, delivery, _ = self.queue_general()
        first = self.claim(delivery)
        reclaim_time = first.lease_expires_at + dt.timedelta(microseconds=1)

        second = self.claim(
            delivery,
            actor=self.other_integration,
            now=reclaim_time,
        )

        first.attempt.refresh_from_db()
        delivery.refresh_from_db()
        self.assertEqual(
            first.attempt.result_category,
            CorrespondenceDeliveryAttempt.ResultCategory.LEASE_EXPIRED,
        )
        self.assertEqual(first.attempt.result_at, reclaim_time)
        self.assertEqual(
            second.action,
            CorrespondenceDeliveryAttempt.Action.RECONCILE,
        )
        self.assertEqual(
            second.attempt.action,
            CorrespondenceDeliveryAttempt.Action.RECONCILE,
        )
        self.assertEqual(second.attempt.attempt_number, 2)
        self.assertEqual(second.attempt.actor, self.other_integration)
        self.assertEqual(delivery.attempt_count, 2)
        self.assertEqual(delivery.claim_owner, str(self.other_integration.pk))
        self.assertNotIn("message", second.payload)
        self.assertEqual(
            second.payload["reconciliation"]["message_id"],
            correspondence.rfc_message_id,
        )
        serialized = json.dumps(second.payload)
        self.assertNotIn(correspondence.approved_body, serialized)
        self.assertNotIn(self.prr.requester.email, serialized)

    def test_awaiting_reconciliation_claim_only_supplies_search_instruction(self):
        correspondence, delivery, _ = self.queue_general()
        send_claim = self.claim(delivery)
        self.report(
            delivery,
            send_claim,
            outcome="unknown",
            provider_message_id="",
            provider_thread_id="",
            provider_timestamp=None,
        )

        reconcile_claim = self.claim(
            delivery,
            now=self.now + dt.timedelta(minutes=1),
        )

        self.assertEqual(
            reconcile_claim.action,
            CorrespondenceDeliveryAttempt.Action.RECONCILE,
        )
        self.assertSetEqual(
            set(reconcile_claim.payload),
            {
                "schema_version",
                "action",
                "delivery_id",
                "correspondence_id",
                "attempt_id",
                "attempt_number",
                "lease_expires_at",
                "provider",
                "provider_account",
                "reconciliation",
            },
        )
        self.assertEqual(
            reconcile_claim.payload["reconciliation"]["message_id"],
            correspondence.rfc_message_id,
        )
        self.assertEqual(
            reconcile_claim.payload["reconciliation"],
            {
                "strategy": "gmail_rfc822_message_id",
                "query": f"rfc822msgid:{correspondence.rfc_message_id}",
                "message_id": correspondence.rfc_message_id,
            },
        )
        serialized = json.dumps(reconcile_claim.payload)
        self.assertNotIn(correspondence.approved_subject, serialized)
        self.assertNotIn(correspondence.approved_body, serialized)
        self.assertNotIn(self.prr.requester.email, serialized)

    def test_sent_cancelled_failed_and_retryable_deliveries_are_not_claimable(self):
        for status in (
            CorrespondenceDelivery.Status.SENT,
            CorrespondenceDelivery.Status.CANCELLED,
            CorrespondenceDelivery.Status.FAILED,
            CorrespondenceDelivery.Status.RETRYABLE,
        ):
            with self.subTest(status=status):
                _, delivery, _ = self.queue_general(subject=f"Subject {status}")
                values = {"status": status}
                if status == CorrespondenceDelivery.Status.SENT:
                    values.update(
                        provider_message_id=f"gmail-{delivery.id}",
                        successful_attempt_number=1,
                        completed_at=self.now,
                    )
                CorrespondenceDelivery.objects.filter(pk=delivery.pk).update(**values)

                with self.assertRaises(services.DeliveryNotClaimable) as raised:
                    self.claim(delivery)

                self.assertEqual(raised.exception.code, "delivery_not_claimable")
                delivery.refresh_from_db()
                self.assertEqual(delivery.status, status)
                self.assertEqual(delivery.attempt_count, 0)
                self.assertFalse(
                    CorrespondenceDeliveryAttempt.objects.filter(
                        delivery=delivery
                    ).exists()
                )

    def test_claim_includes_only_clean_attachment_metadata_and_fails_closed(self):
        _, safe_delivery, safe_attachment = self.queue_general(
            subject="Safe attachment",
            with_attachment=True,
        )

        safe_claim = self.claim(safe_delivery)

        payload_attachment = safe_claim.payload["message"]["attachments"][0]
        self.assertEqual(
            payload_attachment["id"],
            str(safe_attachment.id),
        )
        self.assertEqual(
            payload_attachment["filename"],
            "approved-notice.pdf",
        )
        self.assertEqual(
            payload_attachment["content_type"],
            "application/pdf",
        )
        self.assertEqual(payload_attachment["size"], 321)
        self.assertEqual(payload_attachment["sha256"], "a" * 64)
        self.assertNotIn("content_path", payload_attachment)
        serialized = json.dumps(safe_claim.payload)
        self.assertNotIn(safe_attachment.storage_key, serialized)
        self.assertNotIn("storage_key", payload_attachment)
        self.assertNotIn("quarantine", serialized)

        _, unsafe_delivery, unsafe_attachment = self.queue_general(
            subject="Changed attachment",
            with_attachment=True,
        )
        unsafe_attachment = CorrespondenceAttachment.objects.filter(
            pk=unsafe_attachment.pk
        ).requarantine_locked(
            quarantine_reason="Scanner could not verify this file.",
            quarantined_at=timezone.now(),
        )

        with self.assertRaises(services.UnsafeDeliveryAttachment):
            self.claim(unsafe_delivery)

        unsafe_delivery.refresh_from_db()
        self.assertEqual(
            unsafe_delivery.status,
            CorrespondenceDelivery.Status.PENDING,
        )
        self.assertEqual(unsafe_delivery.attempt_count, 0)
        self.assertFalse(
            CorrespondenceDeliveryAttempt.objects.filter(
                delivery=unsafe_delivery
            ).exists()
        )


class OutboundDeliveryResultTests(OutboundDeliveryServiceTestCase):
    def test_result_requires_the_claimed_attempt_and_actor(self):
        _, delivery, _ = self.queue_general()
        claim = self.claim(delivery)

        with self.assertRaises(services.DeliveryResultConflict) as wrong_attempt:
            services.report_delivery_result(
                delivery,
                actor=self.integration,
                attempt_id=uuid.uuid4(),
                outcome="sent",
                provider_message_id="gmail-wrong-attempt",
                provider_thread_id="thread-1",
                provider_timestamp=self.now,
                provider_metadata={"http_status": 200},
                now=self.now + dt.timedelta(seconds=1),
            )
        self.assertEqual(
            wrong_attempt.exception.code,
            "delivery_attempt_mismatch",
        )

        with self.assertRaises(services.DeliveryResultConflict) as wrong_actor:
            self.report(
                delivery,
                claim,
                actor=self.other_integration,
            )
        self.assertEqual(
            wrong_actor.exception.code,
            "delivery_claim_owner_mismatch",
        )
        claim.attempt.refresh_from_db()
        delivery.refresh_from_db()
        self.assertIsNone(claim.attempt.result_at)
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.CLAIMED)

    def assert_sent_with_unapplied_effect(
        self, correspondence, delivery, claim, *, error
    ):
        from apps.work_queue.models import WorkItem

        delivery.refresh_from_db()
        correspondence.refresh_from_db()
        claim.attempt.refresh_from_db()
        # The provider accepted the message, so the delivery evidence stands.
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.SENT)
        self.assertEqual(correspondence.status, Correspondence.Status.SENT)
        self.assertIsNotNone(claim.attempt.result_at)
        self.assertIsNone(correspondence.resulting_status_event_id)
        work_item = WorkItem.objects.get(
            source_type=WorkItem.SourceType.CORRESPONDENCE,
            source_id=str(correspondence.pk),
            work_kind=WorkItem.WorkKind.DELIVERY_EXCEPTION,
        )
        self.assertEqual(work_item.request_id, self.prr.pk)
        failure = AuditLog.objects.get(
            action="correspondence.delivery_effects_failed",
            detail__correspondence_id=str(correspondence.pk),
        )
        self.assertIn(error, failure.detail["error"])
        self.assertEqual(failure.detail["work_item_id"], str(work_item.pk))

    def test_closed_request_records_stale_clarification_send_without_effect(
        self,
    ):
        correspondence, delivery = self.queue_clarification()
        claim = self.claim(delivery)
        PublicRecordsRequest.objects.filter(pk=self.prr.pk).update(
            status=RequestStatus.FULFILLED,
            closed_at=self.now,
        )

        result = self.report(
            delivery,
            claim,
            provider_message_id=f"gmail-clarification-{delivery.id}",
            provider_thread_id="stale-clarification-thread",
        )

        self.assertEqual(result.status, CorrespondenceDelivery.Status.SENT)
        self.assert_sent_with_unapplied_effect(
            correspondence,
            delivery,
            claim,
            error="lifecycle effects cannot be applied after the request is closed",
        )
        self.prr.refresh_from_db()
        self.assertEqual(self.prr.status, RequestStatus.FULFILLED)
        self.assertFalse(
            StatusEvent.objects.filter(
                request=self.prr,
                to_status=RequestStatus.CLARIFICATION,
            ).exists()
        )

    def test_sent_result_is_atomic_idempotent_and_conflicting_callback_rejected(self):
        correspondence, delivery, _ = self.queue_general()
        claim = self.claim(delivery)
        sent_at = self.now + dt.timedelta(seconds=10)
        callback = {
            "outcome": "sent",
            "provider_message_id": f"gmail-{delivery.id}",
            "provider_thread_id": "gmail-thread-1",
            "provider_timestamp": sent_at,
            "provider_metadata": {
                "http_status": 200,
                "raw_response": "Delivered to jane@example.org: secret body",
            },
        }

        first = self.report(delivery, claim, **callback)
        second = self.report(delivery, claim, **callback)

        delivery.refresh_from_db()
        correspondence.refresh_from_db()
        attempt = CorrespondenceDeliveryAttempt.objects.get(pk=claim.attempt.pk)
        self.assertEqual(first.attempt.pk, attempt.pk)
        self.assertEqual(second.attempt.pk, attempt.pk)
        self.assertFalse(first.idempotent)
        self.assertTrue(second.idempotent)
        self.assertEqual(
            attempt.result_category,
            CorrespondenceDeliveryAttempt.ResultCategory.SENT,
        )
        self.assertEqual(attempt.provider_timestamp, sent_at)
        self.assertTrue(attempt.result_fingerprint)
        self.assertEqual(attempt.provider_response_metadata, {"http_status": 200})
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.SENT)
        self.assertEqual(delivery.successful_attempt_number, 1)
        self.assertEqual(
            delivery.provider_message_id,
            callback["provider_message_id"],
        )
        self.assertEqual(delivery.provider_thread_id, "gmail-thread-1")
        self.assertIsNone(delivery.claim_expires_at)
        self.assertEqual(correspondence.status, Correspondence.Status.SENT)
        self.assertEqual(correspondence.sent_at, sent_at)
        self.assertEqual(
            correspondence.provider_message_id,
            callback["provider_message_id"],
        )
        self.assertEqual(
            CorrespondenceDeliveryAttempt.objects.filter(delivery=delivery).count(),
            1,
        )
        stored = (
            json.dumps(attempt.provider_response_metadata)
            + delivery.last_failure_description
            + correspondence.failure_description
        )
        self.assertNotIn("jane@example.org", stored)
        self.assertNotIn("secret body", stored)

        with self.assertRaises(services.DeliveryResultConflict) as conflicting:
            self.report(
                delivery,
                claim,
                **(callback | {"provider_thread_id": "different-thread"}),
            )
        self.assertEqual(
            conflicting.exception.code,
            "delivery_result_conflict",
        )

    def test_success_timestamp_is_bounded_by_the_claim_and_callback(self):
        correspondence, delivery, _ = self.queue_general()
        claim = self.claim(delivery)

        for invalid_timestamp in (
            claim.attempt.claimed_at - dt.timedelta(days=1),
            self.now + dt.timedelta(seconds=16),
            self.now + dt.timedelta(days=1),
        ):
            with self.subTest(provider_timestamp=invalid_timestamp):
                with self.assertRaises(ValidationError):
                    self.report(
                        delivery,
                        claim,
                        provider_timestamp=invalid_timestamp,
                    )

        delivery.refresh_from_db()
        correspondence.refresh_from_db()
        claim.attempt.refresh_from_db()
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.CLAIMED)
        self.assertEqual(correspondence.status, Correspondence.Status.QUEUED)
        self.assertIsNone(claim.attempt.result_at)

    def test_lifecycle_success_cannot_predate_request_receipt(self):
        correspondence, delivery = self.queue_acknowledgment()
        claim = self.claim(delivery)
        impossible_sent_at = self.prr.received_at - dt.timedelta(seconds=1)

        with self.assertRaisesMessage(
            ValidationError,
            "predates the request or a prior lifecycle communication",
        ):
            self.report(
                delivery,
                claim,
                provider_timestamp=impossible_sent_at,
            )

        self.prr.refresh_from_db()
        correspondence.refresh_from_db()
        delivery.refresh_from_db()
        claim.attempt.refresh_from_db()
        self.assertIsNone(self.prr.acknowledgment_sent_at)
        self.assertEqual(correspondence.status, Correspondence.Status.QUEUED)
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.CLAIMED)
        self.assertIsNone(claim.attempt.result_at)

    def test_lifecycle_success_cannot_predate_prior_lifecycle_delivery(self):
        _acknowledgment, acknowledgment_delivery = self.queue_acknowledgment()
        acknowledgment_claim = self.claim(acknowledgment_delivery)
        first_sent_at = self.now + dt.timedelta(seconds=10)
        self.report(
            acknowledgment_delivery,
            acknowledgment_claim,
            provider_timestamp=first_sent_at,
        )
        self.prr.refresh_from_db()

        revision = services.create_estimate_revision_draft(
            self.prr,
            new_date=dt.date(2026, 8, 17),
            reason="An additional archive requires legal review.",
            actor=self.officer,
        )
        revision = services.approve_draft(
            revision,
            actor=self.officer,
            expected_version=revision.version,
        )
        revision_delivery = services.queue_approved(
            revision,
            actor=self.officer,
            expected_version=revision.version,
        )
        revision_claim = self.claim(
            revision_delivery,
            now=self.now + dt.timedelta(seconds=30),
        )

        with self.assertRaisesMessage(
            ValidationError,
            "predates the request or a prior lifecycle communication",
        ):
            self.report(
                revision_delivery,
                revision_claim,
                now=self.now + dt.timedelta(seconds=45),
                provider_timestamp=first_sent_at - dt.timedelta(seconds=1),
            )

        revision.refresh_from_db()
        revision_delivery.refresh_from_db()
        revision_claim.attempt.refresh_from_db()
        self.prr.refresh_from_db()
        self.assertEqual(self.prr.estimated_completion, dt.date(2026, 8, 3))
        self.assertEqual(revision.status, Correspondence.Status.QUEUED)
        self.assertEqual(
            revision_delivery.status,
            CorrespondenceDelivery.Status.CLAIMED,
        )
        self.assertIsNone(revision_claim.attempt.result_at)

    def test_lifecycle_success_cannot_predate_legacy_acknowledgment(self):
        legacy_sent_at = self.now + dt.timedelta(seconds=10)
        self.prr.acknowledgment_sent_at = legacy_sent_at
        self.prr.save(update_fields=["acknowledgment_sent_at"])
        revision = services.create_estimate_revision_draft(
            self.prr,
            new_date=dt.date(2026, 8, 17),
            reason="An additional archive requires legal review.",
            actor=self.officer,
        )
        revision = services.approve_draft(
            revision,
            actor=self.officer,
            expected_version=revision.version,
        )
        delivery = services.queue_approved(
            revision,
            actor=self.officer,
            expected_version=revision.version,
        )
        claim = self.claim(
            delivery,
            now=self.now + dt.timedelta(seconds=30),
        )

        with self.assertRaisesMessage(
            ValidationError,
            "predates the request or a prior lifecycle communication",
        ):
            self.report(
                delivery,
                claim,
                now=self.now + dt.timedelta(seconds=45),
                provider_timestamp=legacy_sent_at - dt.timedelta(seconds=1),
            )

    def test_identical_completed_callback_remains_idempotent_after_later_send(self):
        acknowledgment, acknowledgment_delivery = self.queue_acknowledgment()
        acknowledgment_claim = self.claim(acknowledgment_delivery)
        acknowledgment_callback = {
            "provider_message_id": f"gmail-{acknowledgment_delivery.id}",
            "provider_thread_id": "acknowledgment-thread",
            "provider_timestamp": self.now + dt.timedelta(seconds=5),
        }
        self.report(
            acknowledgment_delivery,
            acknowledgment_claim,
            now=self.now + dt.timedelta(seconds=10),
            **acknowledgment_callback,
        )
        self.prr.refresh_from_db()

        revision = services.create_estimate_revision_draft(
            self.prr,
            new_date=dt.date(2026, 8, 17),
            reason="An additional archive requires legal review.",
            actor=self.officer,
        )
        revision = services.approve_draft(
            revision,
            actor=self.officer,
            expected_version=revision.version,
        )
        revision_delivery = services.queue_approved(
            revision,
            actor=self.officer,
            expected_version=revision.version,
        )
        revision_claim = self.claim(
            revision_delivery,
            now=self.now + dt.timedelta(seconds=15),
        )
        self.report(
            revision_delivery,
            revision_claim,
            now=self.now + dt.timedelta(seconds=25),
            provider_timestamp=self.now + dt.timedelta(seconds=20),
        )

        replay = self.report(
            acknowledgment_delivery,
            acknowledgment_claim,
            now=self.now + dt.timedelta(seconds=30),
            **acknowledgment_callback,
        )

        self.assertTrue(replay.idempotent)
        acknowledgment.refresh_from_db()
        self.assertEqual(
            acknowledgment.sent_at,
            acknowledgment_callback["provider_timestamp"],
        )

    def test_first_sent_acknowledgment_applies_legal_effect_exactly_once(
        self,
    ):
        correspondence, delivery = self.queue_acknowledgment()
        claim = self.claim(delivery)
        callback = {
            "provider_message_id": f"gmail-{delivery.id}",
            "provider_thread_id": "ack-thread",
            "provider_timestamp": self.now + dt.timedelta(seconds=5),
            "provider_metadata": {"http_status": 200},
        }

        first = self.report(delivery, claim, **callback)
        event_count = StatusEvent.objects.filter(request=self.prr).count()
        second = self.report(delivery, claim, **callback)

        self.prr.refresh_from_db()
        correspondence.refresh_from_db()
        self.assertFalse(first.idempotent)
        self.assertTrue(second.idempotent)
        self.assertEqual(self.prr.status, RequestStatus.ACKNOWLEDGED)
        self.assertEqual(
            self.prr.acknowledgment_sent_at,
            callback["provider_timestamp"],
        )
        self.assertIsNotNone(correspondence.resulting_status_event_id)
        self.assertEqual(
            StatusEvent.objects.filter(request=self.prr).count(),
            event_count,
        )

    def test_denial_state_requires_first_confirmed_delivery_and_is_idempotent(
        self,
    ):
        correspondence, delivery = self.queue_denial()
        claim = self.claim(delivery)
        callback = {
            "provider_message_id": f"gmail-denial-{delivery.id}",
            "provider_thread_id": "denial-thread",
            "provider_timestamp": self.now + dt.timedelta(seconds=5),
            "provider_metadata": {"http_status": 200},
        }

        self.prr.refresh_from_db()
        self.assertEqual(self.prr.status, RequestStatus.RECEIVED)
        self.assertIsNone(correspondence.resulting_status_event_id)

        first = self.report(delivery, claim, **callback)
        event_count = StatusEvent.objects.filter(request=self.prr).count()
        second = self.report(delivery, claim, **callback)

        self.prr.refresh_from_db()
        correspondence.refresh_from_db()
        event = correspondence.resulting_status_event
        self.assertFalse(first.idempotent)
        self.assertTrue(second.idempotent)
        self.assertEqual(self.prr.status, RequestStatus.DENIED)
        self.assertEqual(self.prr.closed_at, callback["provider_timestamp"])
        self.assertIsNotNone(event)
        self.assertEqual(event.to_status, RequestStatus.DENIED)
        self.assertEqual(event.actor, self.officer)
        self.assertEqual(event.occurred_at, callback["provider_timestamp"])
        self.assertTrue(event.visible_to_requester)
        self.assertEqual(
            StatusEvent.objects.filter(request=self.prr).count(),
            event_count,
        )

    def test_denial_delivery_uses_frozen_basis_after_claim_deactivation(self):
        correspondence, delivery = self.queue_denial()
        claim = self.claim(delivery)
        models.QuerySet(
            model=ExemptionCitation,
            using="default",
        ).filter(pk=self.denial_exemption.pk).update(
            active=False,
        )

        self.report(delivery, claim)

        self.prr.refresh_from_db()
        correspondence.refresh_from_db()
        delivery.refresh_from_db()
        self.assertEqual(self.prr.status, RequestStatus.DENIED)
        self.assertEqual(correspondence.status, Correspondence.Status.SENT)
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.SENT)
        self.assertIsNotNone(correspondence.resulting_status_event_id)

    def test_denial_send_with_corrupted_frozen_through_row_is_surfaced(self):
        correspondence, delivery = self.queue_denial()
        claim = self.claim(delivery)
        models.QuerySet(
            model=CorrespondenceDenialCitation,
            using="default",
        ).filter(correspondence=correspondence).update(
            citation_snapshot="RCW 42.56.999"
        )

        self.report(delivery, claim)

        self.assert_sent_with_unapplied_effect(
            correspondence,
            delivery,
            claim,
            error="Approved denial-basis snapshot integrity check failed",
        )
        self.prr.refresh_from_db()
        self.assertEqual(self.prr.status, RequestStatus.RECEIVED)

    def test_denial_send_with_corrupted_basis_snapshot_is_surfaced(self):
        correspondence, delivery = self.queue_denial()
        claim = self.claim(delivery)
        approval = AuditLog.objects.get(
            action="correspondence.approved",
            detail__correspondence_id=str(correspondence.pk),
        )
        corrupted_detail = dict(approval.detail)
        corrupted_detail["denial_citation_snapshot_sha256"] = "0" * 64
        with _simulate_out_of_band_audit_corruption():
            models.QuerySet(model=AuditLog, using="default").filter(
                pk=approval.pk,
            ).update(detail=corrupted_detail)

        self.report(delivery, claim)

        self.assert_sent_with_unapplied_effect(
            correspondence,
            delivery,
            claim,
            error="Approved denial-basis snapshot integrity check failed",
        )
        self.prr.refresh_from_db()
        self.assertEqual(self.prr.status, RequestStatus.RECEIVED)

    def test_canonical_denied_transition_rejects_corrupted_basis_after_sent_evidence(
        self,
    ):
        correspondence, delivery = self.queue_denial()
        claim = self.claim(delivery)
        with mock.patch(
            "apps.correspondence.services.apply_successful_delivery_effects"
        ):
            self.report(
                delivery,
                claim,
                provider_message_id=f"gmail-denial-{delivery.id}",
                provider_thread_id="denial-thread",
                provider_timestamp=self.now + dt.timedelta(seconds=5),
            )

        replacement = ExemptionCitation.objects.create(
            citation="RCW 42.56.260",
            short_name="Financial and commercial information",
        )
        models.QuerySet(
            model=CorrespondenceDenialCitation,
            using="default",
        ).filter(correspondence=correspondence).update(
            exemption_id=replacement.pk
        )
        correspondence.refresh_from_db()
        delivery.refresh_from_db()
        self.prr.refresh_from_db()
        self.assertEqual(correspondence.status, Correspondence.Status.SENT)
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.SENT)
        self.assertEqual(self.prr.status, RequestStatus.RECEIVED)

        with self.assertRaisesMessage(
            records_services.CorrespondenceDeliveryRequired,
            "matching approved exemption-basis snapshot",
        ):
            records_services.transition(
                self.prr,
                RequestStatus.DENIED,
                actor=self.officer,
                note="Denial correspondence delivered to the requester.",
                visible_to_requester=True,
                occurred_at=correspondence.sent_at,
                confirmed_correspondence_id=correspondence.pk,
            )

        self.prr.refresh_from_db()
        self.assertEqual(self.prr.status, RequestStatus.RECEIVED)
        self.assertIsNone(self.prr.closed_at)
        self.assertFalse(self.prr.events.exists())

    def test_canonical_denied_transition_atomically_binds_the_delivery_evidence(
        self,
    ):
        correspondence, delivery = self.queue_denial()
        claim = self.claim(delivery)
        with mock.patch(
            "apps.correspondence.services.apply_successful_delivery_effects"
        ):
            self.report(
                delivery,
                claim,
                provider_message_id=f"gmail-denial-{delivery.id}",
                provider_thread_id="denial-thread",
                provider_timestamp=self.now + dt.timedelta(seconds=5),
            )
        correspondence.refresh_from_db()

        denied = records_services.transition(
            self.prr,
            RequestStatus.DENIED,
            actor=self.officer,
            note="Denial correspondence delivered to the requester.",
            visible_to_requester=True,
            occurred_at=correspondence.sent_at,
            confirmed_correspondence_id=correspondence.pk,
        )

        correspondence.refresh_from_db()
        event = denied.events.get(to_status=RequestStatus.DENIED)
        self.assertEqual(correspondence.resulting_status_event, event)
        self.assertEqual(
            services.apply_successful_delivery_effects(correspondence).pk,
            correspondence.pk,
        )
        self.assertEqual(denied.events.count(), 1)

    def test_corrupted_result_event_binding_fails_closed(self):
        correspondence, delivery = self.queue_acknowledgment()
        claim = self.claim(delivery)
        with mock.patch(
            "apps.correspondence.services.apply_successful_delivery_effects"
        ):
            self.report(
                delivery,
                claim,
                provider_message_id=f"gmail-ack-{delivery.id}",
                provider_thread_id="ack-thread",
                provider_timestamp=self.now + dt.timedelta(seconds=5),
            )
        unrelated_request = PublicRecordsRequest.objects.create(
            requester=self.prr.requester,
            description="A distinct request with unrelated lifecycle evidence.",
        )
        unrelated_event = StatusEvent.objects.create(
            request=unrelated_request,
            to_status=RequestStatus.ACKNOWLEDGED,
            note="Unrelated acknowledgment evidence.",
            visible_to_requester=True,
            occurred_at=self.now + dt.timedelta(seconds=5),
        )
        models.QuerySet(model=Correspondence, using="default").filter(
            pk=correspondence.pk,
        ).update(resulting_status_event=unrelated_event)
        correspondence.refresh_from_db()

        with self.assertRaisesMessage(
            ValidationError,
            "does not match",
        ):
            services.apply_successful_delivery_effects(correspondence)

        self.prr.refresh_from_db()
        self.assertEqual(self.prr.status, RequestStatus.RECEIVED)
        self.assertIsNone(self.prr.acknowledgment_sent_at)
        self.assertFalse(self.prr.events.exists())

    def test_direct_acknowledgment_replay_cannot_duplicate_legal_evidence(self):
        correspondence, delivery = self.queue_acknowledgment()
        claim = self.claim(delivery)
        sent_at = self.now + dt.timedelta(seconds=5)
        with mock.patch(
            "apps.correspondence.services.apply_successful_delivery_effects"
        ):
            self.report(
                delivery,
                claim,
                provider_message_id=f"gmail-ack-{delivery.id}",
                provider_thread_id="ack-thread",
                provider_timestamp=sent_at,
            )

        records_services.record_acknowledgment(
            self.prr,
            actor=self.officer,
            note="Statutory acknowledgment delivered.",
            communicated_at=sent_at,
            confirmed_correspondence_id=correspondence.pk,
        )
        event_count = StatusEvent.objects.filter(request=self.prr).count()
        audit_count = AuditLog.objects.filter(
            action__in=[
                "request.acknowledged",
                "request.status_changed",
            ],
            target=self.prr.number,
        ).count()

        with self.assertRaises(
            records_services.CorrespondenceDeliveryRequired
        ):
            records_services.record_acknowledgment(
                self.prr,
                actor=self.officer,
                note="Statutory acknowledgment delivered.",
                communicated_at=sent_at,
                confirmed_correspondence_id=correspondence.pk,
            )

        self.prr.refresh_from_db()
        correspondence.refresh_from_db()
        self.assertEqual(self.prr.acknowledgment_sent_at, sent_at)
        self.assertEqual(self.prr.status, RequestStatus.ACKNOWLEDGED)
        self.assertIsNotNone(correspondence.resulting_status_event_id)
        self.assertEqual(
            StatusEvent.objects.filter(request=self.prr).count(),
            event_count,
        )
        self.assertEqual(
            AuditLog.objects.filter(
                action__in=[
                    "request.acknowledged",
                    "request.status_changed",
                ],
                target=self.prr.number,
            ).count(),
            audit_count,
        )

    def test_late_direct_acknowledgment_binds_blank_event_and_replay_is_safe(self):
        self.prr = records_services.transition(
            self.prr,
            RequestStatus.GATHERING,
            actor=self.officer,
        )
        correspondence, delivery = self.queue_acknowledgment()
        claim = self.claim(delivery)
        sent_at = self.now + dt.timedelta(seconds=5)
        with mock.patch(
            "apps.correspondence.services.apply_successful_delivery_effects"
        ):
            self.report(
                delivery,
                claim,
                provider_message_id=f"gmail-late-ack-{delivery.id}",
                provider_thread_id="late-ack-thread",
                provider_timestamp=sent_at,
            )

        records_services.record_acknowledgment(
            self.prr,
            actor=self.officer,
            note="Statutory acknowledgment delivered.",
            communicated_at=sent_at,
            confirmed_correspondence_id=correspondence.pk,
        )
        correspondence.refresh_from_db()
        event_count = self.prr.events.count()
        audit_count = AuditLog.objects.filter(
            action="request.acknowledged",
            target=self.prr.number,
        ).count()
        self.assertIsNotNone(correspondence.resulting_status_event_id)
        self.assertEqual(correspondence.resulting_status_event.to_status, "")

        with self.assertRaises(
            records_services.CorrespondenceDeliveryRequired
        ):
            records_services.record_acknowledgment(
                self.prr,
                actor=self.officer,
                note="Statutory acknowledgment delivered.",
                communicated_at=sent_at,
                confirmed_correspondence_id=correspondence.pk,
            )

        self.prr.refresh_from_db()
        self.assertEqual(self.prr.status, RequestStatus.GATHERING)
        self.assertEqual(self.prr.acknowledgment_sent_at, sent_at)
        self.assertEqual(self.prr.events.count(), event_count)
        self.assertEqual(
            AuditLog.objects.filter(
                action="request.acknowledged",
                target=self.prr.number,
            ).count(),
            audit_count,
        )

    def test_denial_retry_revalidates_the_frozen_approval_basis(self):
        correspondence, delivery = self.queue_denial()
        claim = self.claim(delivery)
        self.report(
            delivery,
            claim,
            outcome="failed",
            provider_message_id="",
            provider_thread_id="",
            provider_timestamp=None,
            failure_category="temporary_provider_failure",
        )
        approval = AuditLog.objects.get(
            action="correspondence.approved",
            detail__correspondence_id=str(correspondence.pk),
        )
        corrupted_detail = dict(approval.detail)
        corrupted_detail["denial_citation_snapshot_sha256"] = "0" * 64
        with _simulate_out_of_band_audit_corruption():
            models.QuerySet(model=AuditLog, using="default").filter(
                pk=approval.pk,
            ).update(detail=corrupted_detail)
        correspondence.refresh_from_db()
        delivery.refresh_from_db()

        with self.assertRaisesMessage(
            ValidationError,
            "Approved denial-basis snapshot integrity check failed",
        ):
            services.retry_delivery(
                delivery,
                actor=self.officer,
                expected_version=delivery.version,
            )

        correspondence.refresh_from_db()
        delivery.refresh_from_db()
        self.assertEqual(correspondence.status, Correspondence.Status.FAILED)
        self.assertEqual(
            delivery.status,
            CorrespondenceDelivery.Status.RETRYABLE,
        )

    def test_denial_retry_revalidates_the_live_exemption_taxonomy(self):
        correspondence, delivery = self.queue_denial()
        claim = self.claim(delivery)
        self.report(
            delivery,
            claim,
            outcome="failed",
            provider_message_id="",
            provider_thread_id="",
            provider_timestamp=None,
            failure_category="temporary_provider_failure",
        )
        self.denial_exemption.active = False
        self.denial_exemption.save(update_fields=["active"])
        correspondence.refresh_from_db()
        delivery.refresh_from_db()

        with self.assertRaisesMessage(
            ValidationError,
            "at least one active exemption citation",
        ):
            services.retry_delivery(
                delivery,
                actor=self.officer,
                expected_version=delivery.version,
            )

        correspondence.refresh_from_db()
        delivery.refresh_from_db()
        self.assertEqual(correspondence.status, Correspondence.Status.FAILED)
        self.assertEqual(
            delivery.status,
            CorrespondenceDelivery.Status.RETRYABLE,
        )

    def test_failed_denial_delivery_does_not_change_request_state(self):
        correspondence, delivery = self.queue_denial()
        claim = self.claim(delivery)

        self.report(
            delivery,
            claim,
            outcome="failed",
            provider_message_id="",
            provider_thread_id="",
            provider_timestamp=None,
            failure_category="recipient_rejected",
        )

        self.prr.refresh_from_db()
        correspondence.refresh_from_db()
        self.assertEqual(self.prr.status, RequestStatus.RECEIVED)
        self.assertIsNone(self.prr.closed_at)
        self.assertIsNone(correspondence.resulting_status_event_id)
        self.assertFalse(
            StatusEvent.objects.filter(
                request=self.prr,
                to_status=RequestStatus.DENIED,
            ).exists()
        )


class OutboundCutoverGateTests(OutboundDeliveryServiceTestCase):
    def _approved_general(self):
        draft = services.create_authored_draft(
            self.prr,
            kind=Correspondence.Kind.GENERAL,
            subject="Approved subject",
            body="Approved body",
            actor=self.officer,
        )
        return services.approve_draft(
            draft,
            actor=self.officer,
            expected_version=draft.version,
        )

    @override_settings(PRR_CORRESPONDENCE_OUTBOUND_ENABLED=False)
    def test_disabled_transport_rolls_back_queue_state_and_audit(self):
        approved = self._approved_general()

        with self.assertRaises(services.OutboundTransportDisabled):
            services.queue_approved(
                approved,
                actor=self.officer,
                expected_version=approved.version,
            )

        approved.refresh_from_db()
        self.assertEqual(approved.status, Correspondence.Status.APPROVED)
        self.assertFalse(CorrespondenceDelivery.objects.exists())
        self.assertFalse(
            AuditLog.objects.filter(action="correspondence.queued").exists()
        )

    @override_settings(
        PRR_CORRESPONDENCE_OUTBOUND_ENABLED=True,
        PRR_CORRESPONDENCE_AUTOMATED_OUTBOUND_ENABLED=False,
    )
    def test_manual_only_mode_allows_human_approved_delivery(self):
        approved = self._approved_general()
        with self.captureOnCommitCallbacks(execute=False):
            delivery = services.queue_approved(
                approved,
                actor=self.officer,
                expected_version=approved.version,
            )

        claim = self.claim(delivery)

        self.assertEqual(
            claim.action,
            CorrespondenceDeliveryAttempt.Action.SEND,
        )
        self.assertEqual(delivery.correspondence.automated, False)

    @override_settings(PRR_CORRESPONDENCE_OUTBOUND_ENABLED=False)
    def test_disabled_transport_rejects_claim_without_creating_an_attempt(self):
        with override_settings(PRR_CORRESPONDENCE_OUTBOUND_ENABLED=True):
            _, delivery, _ = self.queue_general()

        with self.assertRaises(services.OutboundTransportDisabled):
            self.claim(delivery)

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.PENDING)
        self.assertEqual(delivery.attempt_count, 0)
        self.assertFalse(delivery.attempts.exists())

    def test_result_callback_remains_accepted_after_transport_is_disabled(self):
        correspondence, delivery, _ = self.queue_general()
        claim = self.claim(delivery)

        with override_settings(PRR_CORRESPONDENCE_OUTBOUND_ENABLED=False):
            result = self.report(delivery, claim)

        delivery.refresh_from_db()
        correspondence.refresh_from_db()
        self.assertEqual(result.status, CorrespondenceDelivery.Status.SENT)
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.SENT)
        self.assertEqual(correspondence.status, Correspondence.Status.SENT)

    def test_disabled_transport_rejects_retry_without_mutating_failure_evidence(self):
        correspondence, delivery, _ = self.queue_general()
        claim = self.claim(delivery)
        self.report(
            delivery,
            claim,
            outcome="failed",
            provider_message_id="",
            provider_thread_id="",
            provider_timestamp=None,
            failure_category="temporary_provider_failure",
        )
        delivery.refresh_from_db()
        correspondence.refresh_from_db()
        failed_version = delivery.version

        with (
            override_settings(PRR_CORRESPONDENCE_OUTBOUND_ENABLED=False),
            self.assertRaises(services.OutboundTransportDisabled),
        ):
            services.retry_delivery(
                delivery,
                actor=self.officer,
                expected_version=delivery.version,
            )

        delivery.refresh_from_db()
        correspondence.refresh_from_db()
        self.assertEqual(delivery.version, failed_version)
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.RETRYABLE)
        self.assertEqual(correspondence.status, Correspondence.Status.FAILED)
        self.assertEqual(delivery.attempts.count(), 1)


@override_settings(PRR_CORRESPONDENCE_STUCK_SECONDS=300)
class OutboundDeliveryWatchdogTests(OutboundDeliveryServiceTestCase):
    @mock.patch("apps.correspondence.tasks.enqueue_queued_delivery")
    def test_expired_claim_is_finalized_and_nudged_for_same_delivery(self, enqueue):
        correspondence, delivery, _ = self.queue_general()
        original_delivery_id = delivery.id
        original_message_id = correspondence.rfc_message_id
        claim = self.claim(delivery)
        watched_at = claim.lease_expires_at + dt.timedelta(microseconds=1)

        result = services.watch_stuck_deliveries(now=watched_at, limit=10)

        delivery.refresh_from_db()
        claim.attempt.refresh_from_db()
        correspondence.refresh_from_db()
        self.assertEqual(result.expired_claims, 1)
        self.assertEqual(result.nudged_deliveries, 1)
        self.assertEqual(delivery.id, original_delivery_id)
        self.assertEqual(correspondence.rfc_message_id, original_message_id)
        self.assertEqual(
            delivery.status,
            CorrespondenceDelivery.Status.AWAITING_RECONCILIATION,
        )
        self.assertEqual(delivery.claim_owner, "")
        self.assertIsNone(delivery.claim_expires_at)
        self.assertEqual(delivery.attempt_count, 1)
        self.assertEqual(
            delivery.recovery_not_before,
            watched_at + dt.timedelta(seconds=300),
        )
        self.assertEqual(delivery.attempts.count(), 1)
        self.assertEqual(correspondence.status, Correspondence.Status.QUEUED)
        self.prr.refresh_from_db()
        self.assertEqual(self.prr.status, RequestStatus.RECEIVED)
        self.assertEqual(
            claim.attempt.result_category,
            CorrespondenceDeliveryAttempt.ResultCategory.LEASE_EXPIRED,
        )
        self.assertEqual(claim.attempt.result_at, watched_at)
        enqueue.assert_called_once_with(
            str(original_delivery_id),
            attempt_count=1,
        )
        self.assertTrue(
            AuditLog.objects.filter(
                action="correspondence.delivery_lease_expired",
                detail__delivery_id=str(delivery.id),
                detail__attempt_id=str(claim.attempt.id),
            ).exists()
        )

    @mock.patch("apps.correspondence.tasks.enqueue_queued_delivery")
    def test_watchdog_rate_limits_repeat_nudges_without_new_attempts(
        self,
        enqueue,
    ):
        _, delivery, _ = self.queue_general()
        claim = self.claim(delivery)
        first_watch = claim.lease_expires_at + dt.timedelta(microseconds=1)
        first = services.watch_stuck_deliveries(now=first_watch, limit=10)
        enqueue.reset_mock()

        second = services.watch_stuck_deliveries(
            now=first_watch + dt.timedelta(seconds=299),
            limit=10,
        )
        third = services.watch_stuck_deliveries(
            now=first_watch + dt.timedelta(seconds=301),
            limit=10,
        )

        delivery.refresh_from_db()
        self.assertEqual(first.expired_claims, 1)
        self.assertEqual(second.expired_claims, 0)
        self.assertEqual(second.nudged_deliveries, 0)
        self.assertEqual(third.expired_claims, 0)
        self.assertEqual(third.nudged_deliveries, 1)
        self.assertEqual(delivery.attempts.count(), 1)
        enqueue.assert_called_once_with(str(delivery.id), attempt_count=1)

    @mock.patch("apps.correspondence.tasks.enqueue_queued_delivery")
    def test_stale_unclaimed_reconciliation_is_nudged_once_without_resend(
        self,
        enqueue,
    ):
        correspondence, delivery, _ = self.queue_general()
        claim = self.claim(delivery)
        unknown_at = self.now + dt.timedelta(seconds=15)
        self.report(
            delivery,
            claim,
            now=unknown_at,
            outcome="unknown",
            provider_message_id="",
            provider_thread_id="",
            provider_timestamp=None,
        )
        watched_at = unknown_at + dt.timedelta(seconds=301)
        original_message_id = correspondence.rfc_message_id

        too_early = services.watch_stuck_deliveries(
            now=unknown_at + dt.timedelta(seconds=299),
            limit=10,
        )
        first = services.watch_stuck_deliveries(now=watched_at, limit=10)
        enqueue.reset_mock()
        second = services.watch_stuck_deliveries(
            now=watched_at + dt.timedelta(seconds=299),
            limit=10,
        )

        delivery.refresh_from_db()
        correspondence.refresh_from_db()
        self.assertEqual(too_early.nudged_deliveries, 0)
        self.assertEqual(first.expired_claims, 0)
        self.assertEqual(first.nudged_deliveries, 1)
        self.assertEqual(second.nudged_deliveries, 0)
        self.assertEqual(
            delivery.status,
            CorrespondenceDelivery.Status.AWAITING_RECONCILIATION,
        )
        self.assertEqual(delivery.attempt_count, 1)
        self.assertEqual(delivery.attempts.count(), 1)
        self.assertEqual(correspondence.rfc_message_id, original_message_id)
        enqueue.assert_not_called()

    @mock.patch("apps.correspondence.tasks.enqueue_queued_delivery")
    def test_expired_reconciliation_lease_is_finalized_without_resend(
        self,
        enqueue,
    ):
        correspondence, delivery, _ = self.queue_general()
        send_claim = self.claim(delivery)
        self.report(
            delivery,
            send_claim,
            now=self.now + dt.timedelta(seconds=15),
            outcome="unknown",
            provider_message_id="",
            provider_thread_id="",
            provider_timestamp=None,
        )
        reconcile_claim = self.claim(
            delivery,
            now=self.now + dt.timedelta(seconds=30),
        )
        watched_at = (
            reconcile_claim.lease_expires_at
            + dt.timedelta(microseconds=1)
        )

        result = services.watch_stuck_deliveries(
            now=watched_at,
            limit=10,
        )

        delivery.refresh_from_db()
        reconcile_claim.attempt.refresh_from_db()
        correspondence.refresh_from_db()
        self.assertEqual(result.expired_claims, 1)
        self.assertEqual(result.nudged_deliveries, 1)
        self.assertEqual(
            delivery.status,
            CorrespondenceDelivery.Status.AWAITING_RECONCILIATION,
        )
        self.assertEqual(delivery.attempt_count, 2)
        self.assertEqual(delivery.attempts.count(), 2)
        self.assertEqual(delivery.claim_owner, "")
        self.assertEqual(
            reconcile_claim.attempt.result_category,
            CorrespondenceDeliveryAttempt.ResultCategory.LEASE_EXPIRED,
        )
        self.assertEqual(correspondence.status, Correspondence.Status.QUEUED)
        enqueue.assert_called_once_with(str(delivery.id), attempt_count=2)

    @mock.patch("apps.correspondence.tasks.enqueue_queued_delivery")
    def test_stale_pending_delivery_is_nudged_once_without_replacement(
        self,
        enqueue,
    ):
        queued_at = self.now - dt.timedelta(seconds=301)
        with mock.patch(
            "apps.correspondence.services.timezone.now",
            return_value=queued_at,
        ):
            correspondence, delivery, _ = self.queue_general()
        original_message_id = correspondence.rfc_message_id

        result = services.watch_stuck_deliveries(now=self.now, limit=10)

        delivery.refresh_from_db()
        correspondence.refresh_from_db()
        self.assertEqual(result.expired_claims, 0)
        self.assertEqual(result.nudged_deliveries, 1)
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.PENDING)
        self.assertEqual(delivery.attempt_count, 0)
        self.assertEqual(delivery.attempts.count(), 0)
        self.assertEqual(correspondence.rfc_message_id, original_message_id)
        enqueue.assert_called_once_with(str(delivery.id), attempt_count=0)

    @mock.patch(
        "apps.correspondence.tasks.enqueue_queued_delivery",
        return_value=False,
    )
    def test_existing_delivery_enqueue_suppresses_duplicate_nudge(self, enqueue):
        queued_at = self.now - dt.timedelta(seconds=301)
        with mock.patch(
            "apps.correspondence.services.timezone.now",
            return_value=queued_at,
        ):
            _, delivery, _ = self.queue_general()
        result = services.watch_stuck_deliveries(now=self.now, limit=10)

        delivery.refresh_from_db()
        self.assertEqual(result.nudged_deliveries, 0)
        self.assertEqual(result.suppressed_nudges, 1)
        enqueue.assert_called_once_with(str(delivery.id), attempt_count=0)
        self.assertEqual(
            delivery.recovery_not_before,
            self.now + dt.timedelta(seconds=300),
        )

    @mock.patch("apps.correspondence.tasks.enqueue_queued_delivery")
    def test_retry_resets_recovery_clock_before_watchdog_can_nudge(
        self,
        enqueue,
    ):
        queued_at = self.now - dt.timedelta(days=1)
        with mock.patch(
            "apps.correspondence.services.timezone.now",
            return_value=queued_at,
        ):
            correspondence, delivery, _ = self.queue_general()
        claim = self.claim(
            delivery,
            now=self.now - dt.timedelta(seconds=60),
        )
        self.report(
            delivery,
            claim,
            now=self.now - dt.timedelta(seconds=50),
            outcome="failed",
            provider_message_id="",
            provider_thread_id="",
            provider_timestamp=None,
            failure_category="temporary_provider_failure",
        )
        delivery.refresh_from_db()
        with mock.patch(
            "apps.correspondence.services.timezone.now",
            return_value=self.now,
        ):
            services.retry_delivery(
                delivery,
                actor=self.officer,
                expected_version=delivery.version,
            )
        enqueue.reset_mock()

        too_early = services.watch_stuck_deliveries(
            now=self.now + dt.timedelta(seconds=299),
            limit=10,
        )
        due = services.watch_stuck_deliveries(
            now=self.now + dt.timedelta(seconds=301),
            limit=10,
        )

        delivery.refresh_from_db()
        correspondence.refresh_from_db()
        self.assertEqual(too_early.nudged_deliveries, 0)
        self.assertEqual(due.nudged_deliveries, 1)
        self.assertEqual(delivery.attempts.count(), 1)
        self.assertEqual(correspondence.status, Correspondence.Status.QUEUED)
        enqueue.assert_called_once_with(str(delivery.id), attempt_count=1)

    @mock.patch("apps.correspondence.tasks.enqueue_queued_delivery")
    def test_terminal_and_human_retry_states_are_ignored(self, enqueue):
        _sent_correspondence, sent_delivery, _ = self.queue_general()
        sent_claim = self.claim(sent_delivery)
        self.report(sent_delivery, sent_claim)

        _retry_correspondence, retry_delivery, _ = self.queue_general()
        retry_claim = self.claim(
            retry_delivery,
            now=self.now + dt.timedelta(seconds=30),
        )
        self.report(
            retry_delivery,
            retry_claim,
            now=self.now + dt.timedelta(seconds=45),
            outcome="failed",
            provider_message_id="",
            provider_thread_id="",
            provider_timestamp=None,
            failure_category="temporary_provider_failure",
        )

        _failed_correspondence, failed_delivery, _ = self.queue_general()
        failed_claim = self.claim(
            failed_delivery,
            now=self.now + dt.timedelta(seconds=60),
        )
        self.report(
            failed_delivery,
            failed_claim,
            now=self.now + dt.timedelta(seconds=75),
            outcome="failed",
            provider_message_id="",
            provider_thread_id="",
            provider_timestamp=None,
            failure_category="recipient_rejected",
        )

        cancelled_correspondence, cancelled_delivery, _ = self.queue_general()
        services.cancel_unsent(
            cancelled_correspondence,
            actor=self.officer,
            expected_version=cancelled_correspondence.version,
            reason="The officer withdrew this duplicate outgoing message.",
        )
        enqueue.reset_mock()

        result = services.watch_stuck_deliveries(
            now=self.now + dt.timedelta(days=2),
            limit=100,
        )

        self.assertEqual(result.expired_claims, 0)
        self.assertEqual(result.nudged_deliveries, 0)
        self.assertEqual(result.suppressed_nudges, 0)
        self.assertEqual(result.notification_failures, 0)
        enqueue.assert_not_called()
        for delivery, expected in (
            (sent_delivery, CorrespondenceDelivery.Status.SENT),
            (retry_delivery, CorrespondenceDelivery.Status.RETRYABLE),
            (failed_delivery, CorrespondenceDelivery.Status.FAILED),
            (cancelled_delivery, CorrespondenceDelivery.Status.CANCELLED),
        ):
            delivery.refresh_from_db()
            self.assertEqual(delivery.status, expected)

    @mock.patch("apps.correspondence.tasks.enqueue_queued_delivery")
    @override_settings(PRR_CORRESPONDENCE_OUTBOUND_ENABLED=False)
    def test_disabled_transport_still_finalizes_expiry_without_nudging(
        self,
        enqueue,
    ):
        with override_settings(PRR_CORRESPONDENCE_OUTBOUND_ENABLED=True):
            _, delivery, _ = self.queue_general()
            claim = self.claim(delivery)
        enqueue.reset_mock()

        result = services.watch_stuck_deliveries(
            now=claim.lease_expires_at + dt.timedelta(seconds=1),
            limit=10,
        )

        delivery.refresh_from_db()
        claim.attempt.refresh_from_db()
        self.assertEqual(result.expired_claims, 1)
        self.assertEqual(result.nudged_deliveries, 0)
        self.assertEqual(result.suppressed_nudges, 1)
        self.assertEqual(
            delivery.status,
            CorrespondenceDelivery.Status.AWAITING_RECONCILIATION,
        )
        self.assertIsNotNone(claim.attempt.result_at)
        enqueue.assert_not_called()

    @mock.patch("apps.correspondence.tasks.enqueue_queued_delivery")
    def test_disabled_full_batch_drain_does_not_delay_stale_work_after_reenable(
        self,
        enqueue,
    ):
        drained = []
        for days_old in (3, 2):
            with mock.patch(
                "apps.correspondence.services.timezone.now",
                return_value=self.now - dt.timedelta(days=days_old),
            ):
                correspondence, delivery, _ = self.queue_general()
            claim = self.claim(delivery)
            drained.append((correspondence, delivery, claim))
        with mock.patch(
            "apps.correspondence.services.timezone.now",
            return_value=self.now - dt.timedelta(days=1),
        ):
            stale_correspondence, stale_delivery, _ = self.queue_general()
        enqueue.reset_mock()
        watched_at = max(
            claim.lease_expires_at for _, _, claim in drained
        ) + dt.timedelta(seconds=1)
        delivery_count = CorrespondenceDelivery.objects.count()
        identities = {
            delivery.id: correspondence.rfc_message_id
            for correspondence, delivery, _ in drained
        }

        with override_settings(PRR_CORRESPONDENCE_OUTBOUND_ENABLED=False):
            disabled_result = services.watch_stuck_deliveries(
                now=watched_at,
                limit=2,
            )
        enabled_result = services.watch_stuck_deliveries(
            now=watched_at + dt.timedelta(seconds=1),
            limit=2,
        )

        self.assertEqual(disabled_result.expired_claims, 2)
        self.assertEqual(disabled_result.nudged_deliveries, 0)
        self.assertEqual(disabled_result.suppressed_nudges, 2)
        self.assertEqual(enabled_result.expired_claims, 0)
        self.assertEqual(enabled_result.nudged_deliveries, 1)
        self.assertEqual(CorrespondenceDelivery.objects.count(), delivery_count)
        enqueue.assert_called_once()
        self.assertEqual(
            enqueue.call_args.args[0],
            str(stale_delivery.id),
        )
        stale_delivery.refresh_from_db()
        stale_correspondence.refresh_from_db()
        self.assertEqual(stale_delivery.attempt_count, 0)
        self.assertFalse(stale_delivery.attempts.exists())
        for correspondence, delivery, claim in drained:
            correspondence.refresh_from_db()
            delivery.refresh_from_db()
            claim.attempt.refresh_from_db()
            self.assertEqual(
                delivery.status,
                CorrespondenceDelivery.Status.AWAITING_RECONCILIATION,
            )
            self.assertEqual(
                delivery.recovery_not_before,
                watched_at + dt.timedelta(seconds=300),
            )
            self.assertEqual(delivery.attempt_count, 1)
            self.assertEqual(delivery.attempts.count(), 1)
            self.assertEqual(
                claim.attempt.result_category,
                CorrespondenceDeliveryAttempt.ResultCategory.LEASE_EXPIRED,
            )
            self.assertEqual(
                correspondence.rfc_message_id,
                identities[delivery.id],
            )

    @mock.patch("apps.correspondence.tasks.enqueue_queued_delivery")
    def test_malformed_expired_evidence_is_backed_off_without_batch_starvation(
        self,
        enqueue,
    ):
        poisoned = []
        with mock.patch(
            "apps.correspondence.services.timezone.now",
            return_value=self.now - dt.timedelta(days=3),
        ):
            missing_correspondence, missing_delivery, _ = self.queue_general()
        missing_delivery.status = CorrespondenceDelivery.Status.CLAIMED
        missing_delivery.claim_owner = str(self.integration.pk)
        missing_delivery.claim_expires_at = self.now + dt.timedelta(seconds=30)
        missing_delivery.attempt_count = 1
        missing_delivery.save(
            update_fields=[
                "status",
                "claim_owner",
                "claim_expires_at",
                "attempt_count",
            ]
        )
        poisoned.append((missing_correspondence, missing_delivery, None))

        with mock.patch(
            "apps.correspondence.services.timezone.now",
            return_value=self.now - dt.timedelta(days=2),
        ):
            mismatched_correspondence, mismatched_delivery, _ = self.queue_general()
        mismatched_claim = self.claim(mismatched_delivery)
        mismatched_delivery.refresh_from_db()
        mismatched_delivery.attempt_count += 1
        mismatched_delivery.save(update_fields=["attempt_count"])
        poisoned.append(
            (mismatched_correspondence, mismatched_delivery, mismatched_claim)
        )
        with mock.patch(
            "apps.correspondence.services.timezone.now",
            return_value=self.now - dt.timedelta(days=1),
        ):
            valid_correspondence, valid_delivery, _ = self.queue_general()
        enqueue.reset_mock()
        watched_at = max(
            delivery.claim_expires_at for _, delivery, _ in poisoned
        ) + dt.timedelta(seconds=1)
        delivery_count = CorrespondenceDelivery.objects.count()
        poison_identity = {
            delivery.id: {
                "message_id": correspondence.rfc_message_id,
                "claim_owner": delivery.claim_owner,
                "claim_expires_at": delivery.claim_expires_at,
                "attempt_count": delivery.attempt_count,
                "attempt_id": claim.attempt.id if claim is not None else None,
            }
            for correspondence, delivery, claim in poisoned
        }

        poison_batch = services.watch_stuck_deliveries(
            now=watched_at,
            limit=2,
        )
        progress_batch = services.watch_stuck_deliveries(
            now=watched_at,
            limit=2,
        )

        self.assertEqual(poison_batch.expired_claims, 0)
        self.assertEqual(poison_batch.nudged_deliveries, 0)
        self.assertEqual(progress_batch.expired_claims, 0)
        self.assertEqual(progress_batch.nudged_deliveries, 1)
        self.assertEqual(CorrespondenceDelivery.objects.count(), delivery_count)
        enqueue.assert_called_once()
        self.assertEqual(
            enqueue.call_args.args[0],
            str(valid_delivery.id),
        )
        valid_delivery.refresh_from_db()
        valid_correspondence.refresh_from_db()
        self.assertEqual(valid_delivery.attempt_count, 0)
        self.assertFalse(valid_delivery.attempts.exists())
        for correspondence, delivery, claim in poisoned:
            correspondence.refresh_from_db()
            delivery.refresh_from_db()
            expected_identity = poison_identity[delivery.id]
            self.assertEqual(
                delivery.status,
                CorrespondenceDelivery.Status.CLAIMED,
            )
            self.assertEqual(delivery.claim_owner, expected_identity["claim_owner"])
            self.assertEqual(
                delivery.claim_expires_at,
                expected_identity["claim_expires_at"],
            )
            self.assertEqual(
                delivery.recovery_not_before,
                watched_at + dt.timedelta(seconds=300),
            )
            self.assertEqual(
                delivery.attempt_count,
                expected_identity["attempt_count"],
            )
            if claim is None:
                self.assertFalse(delivery.attempts.exists())
            else:
                claim.attempt.refresh_from_db()
                self.assertEqual(delivery.attempts.count(), 1)
                self.assertEqual(
                    claim.attempt.id,
                    expected_identity["attempt_id"],
                )
                self.assertIsNone(claim.attempt.result_at)
                self.assertEqual(claim.attempt.result_category, "")
            self.assertEqual(
                correspondence.rfc_message_id,
                expected_identity["message_id"],
            )
            self.assertFalse(
                AuditLog.objects.filter(
                    action="correspondence.delivery_lease_expired",
                    detail__delivery_id=str(delivery.id),
                ).exists()
            )

    @mock.patch(
        "apps.correspondence.tasks.enqueue_queued_delivery",
        side_effect=RuntimeError("broker unavailable"),
    )
    def test_enqueue_failure_does_not_rollback_expiry_evidence(self, enqueue):
        _, delivery, _ = self.queue_general()
        claim = self.claim(delivery)
        result = services.watch_stuck_deliveries(
            now=claim.lease_expires_at + dt.timedelta(seconds=1),
            limit=10,
        )

        delivery.refresh_from_db()
        claim.attempt.refresh_from_db()
        self.assertEqual(result.expired_claims, 1)
        self.assertEqual(result.nudged_deliveries, 0)
        self.assertEqual(result.notification_failures, 1)
        self.assertEqual(
            delivery.status,
            CorrespondenceDelivery.Status.AWAITING_RECONCILIATION,
        )
        self.assertIsNone(delivery.claim_expires_at)
        self.assertIsNotNone(claim.attempt.result_at)
        enqueue.assert_called_once_with(str(delivery.id), attempt_count=1)

    def test_watchdog_task_returns_only_bounded_aggregate_counts(self):
        from apps.correspondence import tasks

        watchdog_result = services.DeliveryWatchdogResult(
            expired_claims=2,
            nudged_deliveries=3,
            suppressed_nudges=4,
            notification_failures=1,
        )
        with mock.patch(
            "apps.correspondence.tasks.services.watch_stuck_deliveries",
            return_value=watchdog_result,
        ) as watch:
            result = tasks.watch_stuck_correspondence_deliveries.run(limit=25)

        self.assertEqual(
            result,
            {
                "expired_claims": 2,
                "nudged_deliveries": 3,
                "suppressed_nudges": 4,
                "notification_failures": 1,
            },
        )
        watch.assert_called_once_with(limit=25)


class OutboundDeliveryCompletionTests(OutboundDeliveryServiceTestCase):

    def test_first_sent_clarification_records_durable_lifecycle_evidence(
        self,
    ):
        correspondence, delivery = self.queue_clarification()
        claim = self.claim(delivery)

        result = self.report(
            delivery,
            claim,
            provider_message_id=f"gmail-clarification-{delivery.id}",
            provider_thread_id="clarification-thread",
        )

        self.prr.refresh_from_db()
        correspondence.refresh_from_db()
        self.assertFalse(result.idempotent)
        self.assertEqual(self.prr.status, RequestStatus.CLARIFICATION)
        self.assertIsNotNone(correspondence.resulting_status_event_id)
        self.assertTrue(
            self.prr.clarification_cycles.filter(
                outbound_correspondence=correspondence,
                opened_status_event_id=correspondence.resulting_status_event_id,
            ).exists()
        )

    def test_failure_categories_are_allowlisted_and_use_server_descriptions(self):
        cases = (
            (
                "temporary_provider_failure",
                CorrespondenceDelivery.Status.RETRYABLE,
            ),
            (
                "recipient_rejected",
                CorrespondenceDelivery.Status.FAILED,
            ),
        )
        for category, expected_status in cases:
            with self.subTest(category=category):
                correspondence, delivery, _ = self.queue_general(
                    subject=f"Failure {category}"
                )
                claim = self.claim(delivery)

                result = self.report(
                    delivery,
                    claim,
                    outcome="failed",
                    provider_message_id="",
                    provider_thread_id="",
                    provider_timestamp=None,
                    failure_category=category,
                    provider_metadata={
                        "http_status": 503,
                        "diagnostic": "jane@example.org mailbox detail",
                    },
                )

                delivery.refresh_from_db()
                correspondence.refresh_from_db()
                result.attempt.refresh_from_db()
                self.assertEqual(delivery.status, expected_status)
                self.assertEqual(
                    correspondence.status,
                    Correspondence.Status.FAILED,
                )
                self.assertEqual(delivery.last_failure_category, category)
                self.assertEqual(correspondence.failure_category, category)
                self.assertTrue(delivery.last_failure_description)
                self.assertEqual(
                    correspondence.failure_description,
                    delivery.last_failure_description,
                )
                self.assertNotIn(
                    "jane@example.org",
                    delivery.last_failure_description,
                )
                self.assertEqual(
                    result.attempt.result_category,
                    CorrespondenceDeliveryAttempt.ResultCategory.FAILED,
                )
                self.assertIsNone(result.attempt.provider_timestamp)
                self.assertEqual(result.attempt.error_category, category)
                self.assertEqual(
                    result.attempt.provider_response_metadata,
                    {"http_status": 503},
                )
                self.assertTrue(result.attempt.result_fingerprint)

    def test_result_cannot_predate_its_claim(self):
        correspondence, delivery, _ = self.queue_general()
        claim = self.claim(delivery)

        with self.assertRaises(ValidationError):
            self.report(
                delivery,
                claim,
                now=claim.attempt.claimed_at - dt.timedelta(seconds=1),
                outcome="failed",
                provider_message_id="",
                provider_thread_id="",
                provider_timestamp=None,
                failure_category="temporary_provider_failure",
            )

        delivery.refresh_from_db()
        correspondence.refresh_from_db()
        claim.attempt.refresh_from_db()
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.CLAIMED)
        self.assertEqual(correspondence.status, Correspondence.Status.QUEUED)
        self.assertIsNone(claim.attempt.result_at)

    def test_unallowlisted_failure_is_rejected_without_completing_claim(self):
        correspondence, delivery, _ = self.queue_general()
        claim = self.claim(delivery)

        with self.assertRaises(ValidationError):
            self.report(
                delivery,
                claim,
                outcome="failed",
                provider_message_id="",
                provider_thread_id="",
                provider_timestamp=None,
                failure_category="gmail said whatever the caller wants",
                provider_metadata={"error": "untrusted raw provider response"},
            )
        with self.assertRaises(ValidationError):
            self.report(
                delivery,
                claim,
                outcome="failed",
                provider_message_id="",
                provider_thread_id="",
                provider_timestamp=None,
                failure_category="timeout",
            )

        delivery.refresh_from_db()
        correspondence.refresh_from_db()
        claim.attempt.refresh_from_db()
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.CLAIMED)
        self.assertEqual(correspondence.status, Correspondence.Status.QUEUED)
        self.assertIsNone(claim.attempt.result_at)

    def test_result_after_lease_expiry_requires_reconciliation(self):
        correspondence, delivery, _ = self.queue_general()
        claim = self.claim(delivery)
        callback_time = claim.lease_expires_at + dt.timedelta(seconds=1)

        with self.assertRaises(services.DeliveryResultConflict) as raised:
            self.report(
                delivery,
                claim,
                now=callback_time,
                provider_timestamp=claim.lease_expires_at,
            )

        self.assertEqual(raised.exception.code, "delivery_claim_expired")
        delivery.refresh_from_db()
        correspondence.refresh_from_db()
        claim.attempt.refresh_from_db()
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.CLAIMED)
        self.assertEqual(correspondence.status, Correspondence.Status.QUEUED)
        self.assertIsNone(claim.attempt.result_at)

    def test_non_success_result_rejects_provider_success_identifiers(self):
        correspondence, delivery, _ = self.queue_general()
        claim = self.claim(delivery)

        with self.assertRaises(ValidationError):
            self.report(
                delivery,
                claim,
                outcome="unknown",
                provider_message_id="unexpected-provider-id",
                provider_thread_id="unexpected-thread-id",
                provider_timestamp=None,
            )

        delivery.refresh_from_db()
        correspondence.refresh_from_db()
        claim.attempt.refresh_from_db()
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.CLAIMED)
        self.assertEqual(correspondence.status, Correspondence.Status.QUEUED)
        self.assertIsNone(claim.attempt.result_at)

    def test_unknown_send_awaits_reconciliation_without_legal_effect(self):
        correspondence, delivery = self.queue_acknowledgment()
        claim = self.claim(delivery)

        result = self.report(
            delivery,
            claim,
            outcome="unknown",
            provider_message_id="",
            provider_thread_id="",
            provider_timestamp=None,
            provider_metadata={
                "http_status": 504,
                "diagnostic": "callback outcome unknown for jane@example.org",
            },
        )

        delivery.refresh_from_db()
        correspondence.refresh_from_db()
        self.prr.refresh_from_db()
        self.assertEqual(
            result.attempt.result_category,
            CorrespondenceDeliveryAttempt.ResultCategory.UNKNOWN,
        )
        self.assertEqual(
            delivery.status,
            CorrespondenceDelivery.Status.AWAITING_RECONCILIATION,
        )
        self.assertEqual(correspondence.status, Correspondence.Status.QUEUED)
        self.assertEqual(self.prr.status, RequestStatus.RECEIVED)
        self.assertIsNone(self.prr.acknowledgment_sent_at)
        self.assertIsNone(correspondence.resulting_status_event_id)
        self.assertEqual(
            result.attempt.provider_response_metadata,
            {"http_status": 504},
        )

    def test_reconciliation_not_found_requires_staff_retry_before_safe_resend(self):
        correspondence, delivery, _ = self.queue_general()
        original_delivery_id = delivery.id
        original_message_id = correspondence.rfc_message_id
        send_claim = self.claim(delivery)
        self.report(
            delivery,
            send_claim,
            outcome="unknown",
            provider_message_id="",
            provider_thread_id="",
            provider_timestamp=None,
        )
        reconcile_claim = self.claim(
            delivery,
            now=self.now + dt.timedelta(minutes=1),
        )

        result = self.report(
            delivery,
            reconcile_claim,
            now=self.now + dt.timedelta(minutes=1, seconds=5),
            outcome="not_found",
            provider_message_id="",
            provider_thread_id="",
            provider_timestamp=None,
            provider_metadata={"http_status": 200},
        )

        delivery.refresh_from_db()
        correspondence.refresh_from_db()
        self.assertEqual(
            result.attempt.result_category,
            CorrespondenceDeliveryAttempt.ResultCategory.NOT_FOUND,
        )
        self.assertEqual(
            delivery.status,
            CorrespondenceDelivery.Status.RETRYABLE,
        )
        self.assertEqual(correspondence.status, Correspondence.Status.FAILED)
        self.assertEqual(
            delivery.last_failure_category,
            "reconciliation_not_found",
        )
        self.assertTrue(delivery.last_failure_description)
        self.assertEqual(delivery.id, original_delivery_id)
        self.assertEqual(correspondence.rfc_message_id, original_message_id)

        retried = services.retry_delivery(
            delivery,
            actor=self.officer,
            expected_version=delivery.version,
        )
        resend_claim = self.claim(
            retried,
            now=self.now + dt.timedelta(minutes=2),
        )
        self.assertEqual(retried.id, original_delivery_id)
        self.assertEqual(
            resend_claim.action,
            CorrespondenceDeliveryAttempt.Action.SEND,
        )
        correspondence.refresh_from_db()
        self.assertEqual(correspondence.rfc_message_id, original_message_id)
        self.assertIsNone(correspondence.failed_at)

    def test_unknown_reconciliation_stays_awaiting_and_never_becomes_send(self):
        _, delivery, _ = self.queue_general()
        send_claim = self.claim(delivery)
        self.report(
            delivery,
            send_claim,
            outcome="unknown",
            provider_message_id="",
            provider_thread_id="",
            provider_timestamp=None,
        )
        first_reconcile = self.claim(
            delivery,
            now=self.now + dt.timedelta(minutes=1),
        )

        result = self.report(
            delivery,
            first_reconcile,
            now=self.now + dt.timedelta(minutes=1, seconds=5),
            outcome="unknown",
            provider_message_id="",
            provider_thread_id="",
            provider_timestamp=None,
        )
        second_reconcile = self.claim(
            delivery,
            now=self.now + dt.timedelta(minutes=2),
        )

        delivery.refresh_from_db()
        self.assertEqual(
            result.attempt.result_category,
            CorrespondenceDeliveryAttempt.ResultCategory.UNKNOWN,
        )
        self.assertEqual(
            delivery.status,
            CorrespondenceDelivery.Status.AWAITING_RECONCILIATION,
        )
        self.assertEqual(
            first_reconcile.action,
            CorrespondenceDeliveryAttempt.Action.RECONCILE,
        )
        self.assertEqual(
            second_reconcile.action,
            CorrespondenceDeliveryAttempt.Action.RECONCILE,
        )

    def test_denial_reconciliation_uses_frozen_basis_and_recipient_after_unknown_send(
        self,
    ):
        correspondence, delivery = self.queue_denial()
        send_claim = self.claim(delivery)
        self.report(
            delivery,
            send_claim,
            outcome="unknown",
            provider_message_id="",
            provider_thread_id="",
            provider_timestamp=None,
        )
        models.QuerySet(
            model=ExemptionCitation,
            using="default",
        ).filter(pk=self.denial_exemption.pk).update(
            active=False,
        )
        requester = self.prr.requester
        requester.email = "updated-requester@example.org"
        requester.save(update_fields=["email"])

        reconcile_claim = self.claim(
            delivery,
            now=self.now + dt.timedelta(minutes=1),
        )
        self.assertEqual(
            reconcile_claim.action,
            CorrespondenceDeliveryAttempt.Action.RECONCILE,
        )
        self.report(
            delivery,
            reconcile_claim,
            now=self.now + dt.timedelta(minutes=1, seconds=5),
            outcome="sent",
            provider_message_id=f"gmail-reconciled-{delivery.id}",
            provider_thread_id="reconciled-denial-thread",
            provider_timestamp=self.now + dt.timedelta(seconds=5),
        )

        self.prr.refresh_from_db()
        correspondence.refresh_from_db()
        delivery.refresh_from_db()
        self.assertEqual(self.prr.status, RequestStatus.DENIED)
        self.assertEqual(correspondence.status, Correspondence.Status.SENT)
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.SENT)
        self.assertIsNotNone(correspondence.resulting_status_event_id)

    def test_reconciled_success_is_idempotent_and_applies_legal_effect_once(self):
        correspondence, delivery = self.queue_acknowledgment()
        send_claim = self.claim(delivery)
        reconcile_claim = self.claim(
            delivery,
            actor=self.other_integration,
            now=send_claim.lease_expires_at + dt.timedelta(microseconds=1),
        )
        callback = {
            "outcome": "sent",
            "provider_message_id": f"gmail-reconciled-{delivery.id}",
            "provider_thread_id": "reconciled-thread",
            "provider_timestamp": self.now + dt.timedelta(minutes=5),
            "provider_metadata": {"http_status": 200},
        }

        first = self.report(
            delivery,
            reconcile_claim,
            actor=self.other_integration,
            now=self.now + dt.timedelta(minutes=6),
            **callback,
        )
        event_count = StatusEvent.objects.filter(request=self.prr).count()
        second = self.report(
            delivery,
            reconcile_claim,
            actor=self.other_integration,
            now=self.now + dt.timedelta(minutes=7),
            **(callback | {"outcome": "reconciled"}),
        )

        delivery.refresh_from_db()
        correspondence.refresh_from_db()
        self.prr.refresh_from_db()
        self.assertEqual(
            first.attempt.result_category,
            CorrespondenceDeliveryAttempt.ResultCategory.RECONCILED,
        )
        self.assertEqual(
            first.attempt.provider_timestamp,
            callback["provider_timestamp"],
        )
        self.assertFalse(first.idempotent)
        self.assertTrue(second.idempotent)
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.SENT)
        self.assertEqual(
            delivery.successful_attempt_number,
            reconcile_claim.attempt.attempt_number,
        )
        self.assertEqual(correspondence.status, Correspondence.Status.SENT)
        self.assertEqual(self.prr.status, RequestStatus.ACKNOWLEDGED)
        self.assertEqual(
            StatusEvent.objects.filter(request=self.prr).count(),
            event_count,
        )
