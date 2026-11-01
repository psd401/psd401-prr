import datetime as dt
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import models, transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.correspondence import services
from apps.correspondence.models import (
    ClarificationCycle,
    Correspondence,
    CorrespondenceAttachment,
    CorrespondenceDelivery,
    CorrespondenceDeliveryAttempt,
    CorrespondenceDenialCitation,
    CorrespondenceRecipient,
)
from apps.records import letters
from apps.records import services as record_services
from apps.records.exemption_logs import current_exemption_log_manifest_sha256
from apps.records.models import (
    EstimateRevision,
    ExemptionCitation,
    Installment,
    PublicRecordsRequest,
    RecordItem,
    Requester,
    RequestStatus,
    StatusEvent,
)
from apps.records.production import (
    current_installment_manifest_sha256,
    installment_download_url,
)


@override_settings(PRR_LOCAL_IMPLICIT_STAFF_ROLES=())
class CorrespondenceServiceTestCase(TestCase):
    def setUp(self):
        User = get_user_model()
        self.officer = User.objects.create_user(
            "officer",
            "officer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.admin = User.objects.create_user(
            "admin",
            "admin@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.admin, {StaffRole.ADMIN})
        self.liaison = User.objects.create_user(
            "liaison",
            "liaison@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.liaison, {StaffRole.LIAISON})
        self.integration = User.objects.create_user(
            "outbound-integration",
            password=None,
            is_staff=False,
        )
        requester = Requester.objects.create(
            name="Jane Requester",
            email="jane@example.org",
        )
        self.prr = PublicRecordsRequest.objects.create(
            requester=requester,
            description="All transportation-plan records.",
            estimated_completion=dt.date(2026, 8, 3),
        )
        self.denial_exemption = ExemptionCitation.objects.create(
            citation="RCW 42.56.250",
            short_name="Examination questions",
        )

    def approve_and_queue(self, correspondence):
        correspondence = services.approve_draft(
            correspondence,
            actor=self.officer,
            expected_version=correspondence.version,
        )
        delivery = services.queue_approved(
            correspondence,
            actor=self.officer,
            expected_version=correspondence.version,
        )
        correspondence.refresh_from_db()
        return correspondence, delivery

    def mark_sent(self, correspondence, delivery, *, sent_at=None):
        now = sent_at or timezone.now()
        provider_message_id = f"gmail-{delivery.id}"
        provider_thread_id = "thread-1"
        CorrespondenceDeliveryAttempt.objects.create(
            delivery=delivery,
            attempt_number=1,
            action=CorrespondenceDeliveryAttempt.Action.SEND,
            claim_owner=str(self.integration.pk),
            claimed_at=now,
            result_at=now,
            result_category=CorrespondenceDeliveryAttempt.ResultCategory.SENT,
            provider_message_id=provider_message_id,
            provider_thread_id=provider_thread_id,
            provider_timestamp=now,
            result_fingerprint="a" * 64,
            actor=self.integration,
        )
        delivery.status = CorrespondenceDelivery.Status.SENT
        delivery.provider_message_id = provider_message_id
        delivery.provider_thread_id = provider_thread_id
        delivery.attempt_count = 1
        delivery.successful_attempt_number = 1
        delivery.completed_at = now
        delivery.save()
        correspondence.status = Correspondence.Status.SENT
        correspondence.sent_at = now
        correspondence.provider_message_id = delivery.provider_message_id
        correspondence.provider_thread_id = delivery.provider_thread_id
        correspondence.save()
        return now

    def mark_retryable_failure(self, correspondence, delivery):
        now = timezone.now()
        category = "temporary_provider_failure"
        description = services.RETRYABLE_DELIVERY_FAILURES[category]
        CorrespondenceDeliveryAttempt.objects.create(
            delivery=delivery,
            attempt_number=1,
            action=CorrespondenceDeliveryAttempt.Action.SEND,
            claim_owner=str(self.integration.pk),
            claimed_at=now,
            result_at=now,
            result_category=CorrespondenceDeliveryAttempt.ResultCategory.FAILED,
            error_category=category,
            result_fingerprint="b" * 64,
            actor=self.integration,
        )
        delivery.status = CorrespondenceDelivery.Status.RETRYABLE
        delivery.attempt_count = 1
        delivery.last_failure_category = category
        delivery.last_failure_description = description
        delivery.completed_at = now
        delivery.save()
        correspondence.status = Correspondence.Status.FAILED
        correspondence.failed_at = now
        correspondence.failure_category = category
        correspondence.failure_description = description
        correspondence.save()
        return now


class DraftLifecycleTests(CorrespondenceServiceTestCase):
    def test_staff_user_without_current_application_role_cannot_create_draft(self):
        apply_staff_roles(self.liaison, set())
        starting_count = Correspondence.objects.count()

        with self.assertRaisesMessage(
            PermissionDenied,
            "A current human staff identity is required.",
        ):
            services.create_authored_draft(
                self.prr,
                kind=Correspondence.Kind.GENERAL,
                subject="Unauthorized draft",
                body="A stale staff flag is not an application role.",
                actor=self.liaison,
            )

        self.assertEqual(Correspondence.objects.count(), starting_count)

    def test_stale_deactivated_officer_cannot_approve_draft(self):
        draft = services.create_authored_draft(
            self.prr,
            kind=Correspondence.Kind.GENERAL,
            subject="Draft awaiting approval",
            body="This draft must remain unchanged after deactivation.",
            actor=self.officer,
        )
        get_user_model().objects.filter(pk=self.officer.pk).update(
            is_active=False
        )

        with self.assertRaisesMessage(
            PermissionDenied,
            "Records Officer approval is required.",
        ):
            services.approve_draft(
                draft,
                actor=self.officer,
                expected_version=draft.version,
            )

        draft.refresh_from_db()
        self.assertEqual(draft.status, Correspondence.Status.DRAFT)
        self.assertIsNone(draft.approved_by_id)
        self.assertFalse(
            AuditLog.objects.filter(
                action="correspondence.approved",
                detail__correspondence_id=str(draft.pk),
            ).exists()
        )

    def test_delivery_status_kind_is_inbound_only(self):
        with self.assertRaisesMessage(
            ValidationError,
            "Delivery-status notifications are inbound correspondence",
        ):
            services.create_authored_draft(
                self.prr,
                kind=Correspondence.Kind.DELIVERY_STATUS_NOTIFICATION,
                subject="Forged delivery status",
                body="This kind must never be sent outbound.",
                actor=self.officer,
            )

    def test_denial_draft_requires_persisted_active_exemption_citation(self):
        with self.assertRaisesMessage(
            ValidationError,
            "at least one active exemption citation",
        ):
            services.create_authored_draft(
                self.prr,
                kind=Correspondence.Kind.DENIAL,
                subject="Denial determination",
                body="The requested records are fully exempt from disclosure.",
                actor=self.officer,
                denial_exemption_citation_ids=[],
            )
        self.assertFalse(
            Correspondence.objects.filter(
                kind=Correspondence.Kind.DENIAL,
            ).exists()
        )

        draft = services.create_authored_draft(
            self.prr,
            kind=Correspondence.Kind.DENIAL,
            subject="Denial determination",
            body=(
                "The requested records are fully exempt under RCW 42.56.250."
            ),
            actor=self.officer,
            denial_exemption_citation_ids=[self.denial_exemption.pk],
        )

        basis = draft.denial_citations.select_related("exemption").get()
        self.assertEqual(basis.exemption, self.denial_exemption)
        self.assertEqual(basis.selected_by, self.officer)

    def test_denial_approval_requires_each_complete_citation_in_body(self):
        misleading_bodies = (
            (
                "The asserted authority is RCW 42.56.2500, which is not "
                "the selected citation."
            ),
            (
                "The asserted authority is RCW 42.56.250(1), which is a "
                "different, extended citation."
            ),
            (
                "The asserted authority is ＲＣＷ 42.56.250, using Unicode "
                "compatibility lookalikes."
            ),
            (
                "The asserted authority is R\u200bCW 42.56.250, with an "
                "embedded format character."
            ),
            (
                "The asserted authority is RCW 42.56.250\u200b0, using an "
                "invisible separator to disguise a longer citation."
            ),
            (
                "The asserted authority is RCW 42.56.250\u2060(1), using an "
                "invisible word joiner before an added subsection."
            ),
            (
                "The asserted authority is RCW 42.56.250\u20101, using a "
                "Unicode hyphen to disguise a longer citation."
            ),
            (
                "The asserted authority is RCW 42.56.250\uff081\uff09, using "
                "fullwidth parentheses around an added subsection."
            ),
            (
                "The asserted authority is RCW 42.56.250\uff0f1, using a "
                "fullwidth slash to disguise a longer citation."
            ),
            (
                "The asserted authority is RCW 42.56.250\uff0e1, using a "
                "fullwidth period to disguise a longer citation."
            ),
            (
                "The asserted authority is RCW 42.56.250\u20121, using a "
                "non-enumerated figure-dash continuation."
            ),
            (
                "The asserted authority is RCW 42.56.250\u22121, using a "
                "Unicode math-symbol continuation."
            ),
            (
                "The asserted authority is RCW 42.56.250 (1), using "
                "whitespace to disguise an added subsection."
            ),
            (
                "The asserted authority is RCW 42.56.250\u00a0(1), using "
                "Unicode whitespace before an added subsection."
            ),
        )
        for body in misleading_bodies:
            with self.subTest(body=body):
                draft = services.create_authored_draft(
                    self.prr,
                    kind=Correspondence.Kind.DENIAL,
                    subject="Denial determination",
                    body=body,
                    actor=self.officer,
                    denial_exemption_citation_ids=[
                        self.denial_exemption.pk
                    ],
                )

                with self.assertRaisesMessage(
                    ValidationError,
                    "recipient-visible body must contain every selected",
                ):
                    services.approve_draft(
                        draft,
                        actor=self.officer,
                        expected_version=draft.version,
                    )

                draft.refresh_from_db()
                self.assertEqual(
                    draft.status,
                    Correspondence.Status.DRAFT,
                )

        compatibility_citation = ExemptionCitation.objects.create(
            citation="USC 123",
            short_name="Compatibility-glyph boundary test",
        )
        compatibility_draft = services.create_authored_draft(
            self.prr,
            kind=Correspondence.Kind.DENIAL,
            subject="Denial determination",
            body=(
                "The asserted authority is UſC 123, using a long-s "
                "compatibility glyph."
            ),
            actor=self.officer,
            denial_exemption_citation_ids=[compatibility_citation.pk],
        )
        with self.assertRaisesMessage(
            ValidationError,
            "recipient-visible body must contain every selected",
        ):
            services.approve_draft(
                compatibility_draft,
                actor=self.officer,
                expected_version=compatibility_draft.version,
            )

        second = ExemptionCitation.objects.create(
            citation="RCW 42.56.230(1)",
            short_name="Personal information",
        )
        incomplete = services.create_authored_draft(
            self.prr,
            kind=Correspondence.Kind.DENIAL,
            subject="Denial determination",
            body=(
                "The requested records are exempt under RCW 42.56.250, "
                "but this notice omits the other selected authority."
            ),
            actor=self.officer,
            denial_exemption_citation_ids=[
                self.denial_exemption.pk,
                second.pk,
            ],
        )

        with self.assertRaisesMessage(
            ValidationError,
            "recipient-visible body must contain every selected",
        ):
            services.approve_draft(
                incomplete,
                actor=self.officer,
                expected_version=incomplete.version,
            )

    def test_denial_body_citation_match_normalizes_case_and_whitespace(self):
        bodies = (
            (
                "The requested records are exempt under "
                "rcw\u00a042.56.250 (the selected exemption)."
            ),
            (
                "The selected exemption is RCW 42.56.250— followed by "
                "ordinary explanatory prose."
            ),
        )
        for body in bodies:
            with self.subTest(body=body):
                draft = services.create_authored_draft(
                    self.prr,
                    kind=Correspondence.Kind.DENIAL,
                    subject="Denial determination",
                    body=body,
                    actor=self.officer,
                    denial_exemption_citation_ids=[
                        self.denial_exemption.pk
                    ],
                )

                approved = services.approve_draft(
                    draft,
                    actor=self.officer,
                    expected_version=draft.version,
                )

                self.assertEqual(
                    approved.status,
                    Correspondence.Status.APPROVED,
                )

    def test_denial_queue_rejects_approved_body_drift_even_with_citation(self):
        draft = services.create_authored_draft(
            self.prr,
            kind=Correspondence.Kind.DENIAL,
            subject="Denial determination",
            body=(
                "The requested records are exempt under RCW 42.56.250."
            ),
            actor=self.officer,
            denial_exemption_citation_ids=[self.denial_exemption.pk],
        )
        approved = services.approve_draft(
            draft,
            actor=self.officer,
            expected_version=draft.version,
        )
        models.QuerySet(
            model=Correspondence,
            using="default",
        ).filter(pk=approved.pk).update(
            approved_body=(
                "Altered denial language still names RCW 42.56.250.\n"
            )
        )

        with self.assertRaisesMessage(
            ValidationError,
            "Approved denial-basis snapshot integrity check failed",
        ):
            services.queue_approved(
                approved,
                actor=self.officer,
                expected_version=approved.version,
            )

        approved.refresh_from_db()
        self.assertEqual(
            approved.status,
            Correspondence.Status.APPROVED,
        )
        self.assertFalse(CorrespondenceDelivery.objects.exists())

    def test_denial_basis_is_frozen_at_approval_and_revalidated_before_queue(self):
        draft = services.create_authored_draft(
            self.prr,
            kind=Correspondence.Kind.DENIAL,
            subject="Denial determination",
            body=(
                "The requested records are fully exempt under RCW 42.56.250."
            ),
            actor=self.officer,
            denial_exemption_citation_ids=[self.denial_exemption.pk],
        )
        approved = services.approve_draft(
            draft,
            actor=self.officer,
            expected_version=draft.version,
        )
        approval = AuditLog.objects.get(
            action="correspondence.approved",
            detail__correspondence_id=str(approved.pk),
        )
        self.assertEqual(approval.detail["denial_citation_count"], 1)
        self.assertEqual(
            approval.detail["denial_exemption_citations"],
            ["RCW 42.56.250"],
        )
        snapshot = approval.detail["denial_basis_snapshot"]
        self.assertEqual(len(snapshot), 1)
        self.assertEqual(snapshot[0]["id"], draft.denial_citations.get().pk)
        self.assertEqual(snapshot[0]["selected_by_id"], self.officer.pk)
        self.assertEqual(snapshot[0]["citation"], "RCW 42.56.250")
        self.assertEqual(snapshot[0]["short_name"], "Examination questions")
        self.assertFalse(snapshot[0]["requires_second_review"])

        basis = approved.denial_citations.get()
        with self.assertRaisesMessage(
            ValidationError,
            "Denial citation snapshots are immutable after approval",
        ):
            basis.delete()
        with self.assertRaisesMessage(
            ValidationError,
            "Denial citation snapshots are immutable after approval",
        ):
            CorrespondenceDenialCitation.objects.create(
                correspondence=approved,
                exemption=self.denial_exemption,
                selected_by=self.officer,
            )
        with self.assertRaises(ValidationError):
            CorrespondenceDenialCitation._base_manager.filter(
                pk=basis.pk
            ).delete()
        with self.assertRaises(ValidationError):
            CorrespondenceDenialCitation._base_manager.filter(
                pk=basis.pk
            ).update(exemption=self.denial_exemption)
        with self.assertRaises(ValidationError):
            CorrespondenceDenialCitation._base_manager.bulk_create(
                [
                    CorrespondenceDenialCitation(
                        correspondence=approved,
                        exemption=self.denial_exemption,
                        selected_by=self.officer,
                    )
                ]
            )

        self.denial_exemption.active = False
        self.denial_exemption.save(update_fields=["active"])
        with self.assertRaisesMessage(
            ValidationError,
            "at least one active exemption citation",
        ):
            services.queue_approved(
                approved,
                actor=self.officer,
                expected_version=approved.version,
            )
        self.assertFalse(CorrespondenceDelivery.objects.exists())

    def test_non_denial_cannot_claim_denial_exemption_citations(self):
        with self.assertRaisesMessage(
            ValidationError,
            "only valid for denial correspondence",
        ):
            services.create_authored_draft(
                self.prr,
                kind=Correspondence.Kind.GENERAL,
                subject="General message",
                body="This is not a denial.",
                actor=self.liaison,
                denial_exemption_citation_ids=[self.denial_exemption.pk],
            )

    def test_denial_approval_fails_closed_without_second_review_evidence(self):
        second_review_citation = ExemptionCitation.objects.create(
            citation="20 U.S.C. § 1232g",
            short_name="FERPA education records",
            requires_second_review=True,
        )
        draft = services.create_authored_draft(
            self.prr,
            kind=Correspondence.Kind.DENIAL,
            subject="Denial determination",
            body=(
                "The requested records are fully exempt under "
                "20 U.S.C. § 1232g."
            ),
            actor=self.officer,
            denial_exemption_citation_ids=[second_review_citation.pk],
        )

        with self.assertRaisesMessage(
            ValidationError,
            "requires persisted second-review evidence",
        ):
            services.approve_draft(
                draft,
                actor=self.officer,
                expected_version=draft.version,
            )
        draft.refresh_from_db()
        self.assertEqual(draft.status, Correspondence.Status.DRAFT)

    def test_denial_cannot_be_approved_for_counsel_without_requester_as_to(self):
        draft = services.create_authored_draft(
            self.prr,
            kind=Correspondence.Kind.DENIAL,
            subject="Denial determination",
            body=(
                "The requested records are fully exempt under RCW 42.56.250."
            ),
            actor=self.officer,
            recipients=[
                {
                    "recipient_type": (
                        CorrespondenceRecipient.RecipientType.TO
                    ),
                    "email": "district-counsel@psd401.net",
                    "display_name": "District Counsel",
                }
            ],
            denial_exemption_citation_ids=[self.denial_exemption.pk],
        )

        with self.assertRaisesMessage(
            ValidationError,
            "current requester must be a To recipient",
        ):
            services.approve_draft(
                draft,
                actor=self.officer,
                expected_version=draft.version,
            )

        draft.refresh_from_db()
        self.assertEqual(draft.status, Correspondence.Status.DRAFT)

    def test_denial_queue_rechecks_requester_as_to_recipient(self):
        draft = services.create_authored_draft(
            self.prr,
            kind=Correspondence.Kind.DENIAL,
            subject="Denial determination",
            body=(
                "The requested records are fully exempt under RCW 42.56.250."
            ),
            actor=self.officer,
            denial_exemption_citation_ids=[self.denial_exemption.pk],
        )
        approved = services.approve_draft(
            draft,
            actor=self.officer,
            expected_version=draft.version,
        )
        models.QuerySet(
            model=CorrespondenceRecipient,
            using="default",
        ).filter(correspondence=approved).update(
            email="district-counsel@psd401.net",
            display_name="District Counsel",
            requester_id=None,
        )

        with self.assertRaisesMessage(
            ValidationError,
            "current requester must be a To recipient",
        ):
            services.queue_approved(
                approved,
                actor=self.officer,
                expected_version=approved.version,
            )

        approved.refresh_from_db()
        self.assertEqual(approved.status, Correspondence.Status.APPROVED)
        self.assertFalse(CorrespondenceDelivery.objects.exists())

    def test_template_draft_records_version_recipient_metadata_and_audit(self):
        draft = services.create_acknowledgment_draft(
            self.prr,
            actor=self.liaison,
        )

        self.assertEqual(draft.status, Correspondence.Status.DRAFT)
        self.assertEqual(draft.template_identifier, "acknowledgment")
        self.assertEqual(draft.template_version, "2")
        self.assertEqual(
            draft.draft_metadata["estimated_completion"],
            "2026-08-03",
        )
        recipient = draft.recipients.get()
        self.assertEqual(recipient.recipient_type, CorrespondenceRecipient.RecipientType.TO)
        self.assertEqual(recipient.email, "jane@example.org")
        self.assertEqual(recipient.requester, self.prr.requester)
        self.assertIsNone(self.prr.acknowledgment_sent_at)
        self.assertEqual(self.prr.status, RequestStatus.RECEIVED)
        self.assertTrue(
            AuditLog.objects.filter(
                action="correspondence.draft_created",
                detail__correspondence_id=str(draft.id),
            ).exists()
        )

    @override_settings(PORTAL_BASE_URL="https://staff.example.invalid")
    def test_current_requester_lifecycle_snapshots_omit_staff_status_route(self):
        acknowledgment = services.create_acknowledgment_draft(
            self.prr,
            actor=self.liaison,
        )
        estimate_revision = services.create_estimate_revision_draft(
            self.prr,
            new_date=dt.date(2026, 8, 17),
            reason="An additional archive requires review.",
            actor=self.liaison,
        )
        staff_route = (
            f"https://staff.example.invalid/requests/{self.prr.number}"
        )

        for draft in (acknowledgment, estimate_revision):
            with self.subTest(kind=draft.kind):
                self.assertEqual(draft.template_version, "2")
                self.assertEqual(services.preview(draft).body, draft.draft_body)
                self.assertNotIn(staff_route, draft.draft_body)
                self.assertNotIn("status", draft.draft_body.lower())
                self.assertNotIn("track the status", draft.draft_body.lower())
                self.assertNotIn("track status", draft.draft_body.lower())

                approved = services.approve_draft(
                    draft,
                    actor=self.officer,
                    expected_version=draft.version,
                )
                self.assertEqual(
                    services.preview(approved).body,
                    approved.approved_body,
                )
                self.assertNotIn(staff_route, approved.approved_body)
                self.assertNotIn("status", approved.approved_body.lower())

    def test_edit_is_audited_version_checked_and_preview_is_exact(self):
        draft = services.create_authored_draft(
            self.prr,
            kind=Correspondence.Kind.GENERAL,
            subject="Original",
            body="Original body",
            actor=self.liaison,
        )
        original_version = draft.version

        draft = services.edit_draft(
            draft,
            subject="Edited",
            body="Edited body",
            actor=self.liaison,
            expected_version=original_version,
        )
        preview = services.preview(draft)

        self.assertEqual((preview.subject, preview.body), ("Edited", "Edited body\n"))
        self.assertEqual(draft.version, original_version + 1)
        audit_row = AuditLog.objects.get(action="correspondence.draft_edited")
        self.assertEqual(audit_row.detail["correspondence_id"], str(draft.id))
        self.assertNotIn("Edited body", str(audit_row.detail))
        self.assertIn("body_sha256", audit_row.detail)

        with self.assertRaises(services.VersionConflict):
            services.edit_draft(
                draft,
                subject="Stale edit",
                body="Stale body",
                actor=self.liaison,
                expected_version=original_version,
            )

    def test_approval_freezes_the_exact_preview_and_structured_metadata(self):
        draft = services.create_estimate_revision_draft(
            self.prr,
            new_date=dt.date(2026, 8, 17),
            reason="An additional archive requires review.",
            actor=self.liaison,
        )
        preview = services.preview(draft)

        approved = services.approve_draft(
            draft,
            actor=self.officer,
            expected_version=draft.version,
        )

        self.assertEqual(approved.status, Correspondence.Status.APPROVED)
        self.assertEqual(approved.approved_subject, preview.subject)
        self.assertEqual(approved.approved_body, preview.body)
        self.assertEqual(approved.approved_metadata, draft.draft_metadata)
        self.assertEqual(services.preview(approved), preview)

        with self.assertRaises(ValidationError):
            services.edit_draft(
                approved,
                subject="Changed after approval",
                body="Changed",
                actor=self.officer,
                expected_version=approved.version,
            )

    def test_approval_requires_records_officer_not_application_admin_or_integration(self):
        for actor in (self.admin, self.integration):
            with self.subTest(actor=actor.username):
                draft = services.create_authored_draft(
                    self.prr,
                    kind=Correspondence.Kind.GENERAL,
                    subject="Subject",
                    body="Body",
                    actor=self.liaison,
                )
                with self.assertRaises(PermissionDenied):
                    services.approve_draft(
                        draft,
                        actor=actor,
                        expected_version=draft.version,
                    )

    def test_generic_authored_path_cannot_bypass_structured_legal_templates(self):
        for kind in (
            Correspondence.Kind.ACKNOWLEDGMENT,
            Correspondence.Kind.CLARIFICATION_REQUEST,
            Correspondence.Kind.ESTIMATE_REVISION,
            Correspondence.Kind.INSTALLMENT_NOTICE,
        ):
            with self.subTest(kind=kind), self.assertRaises(ValidationError):
                services.create_authored_draft(
                    self.prr,
                    kind=kind,
                    subject="Generic subject",
                    body="Generic body",
                    actor=self.liaison,
                )

    def test_generic_edit_cannot_split_structured_content_from_effect_metadata(self):
        draft = services.create_estimate_revision_draft(
            self.prr,
            new_date=dt.date(2026, 8, 17),
            reason="An additional archive requires review.",
            actor=self.liaison,
        )
        original_subject = draft.draft_subject
        original_body = draft.draft_body

        with self.assertRaisesMessage(
            ValidationError,
            "Structured template drafts",
        ):
            services.edit_draft(
                draft,
                subject="Public Records Request — Different Revision",
                body=(
                    "The requester-visible date is September 30 even though "
                    "the effect metadata still says August 17."
                ),
                actor=self.liaison,
                expected_version=draft.version,
            )

        draft.refresh_from_db()
        self.assertEqual(draft.draft_subject, original_subject)
        self.assertEqual(draft.draft_body, original_body)

    def test_structured_edit_rerenders_content_and_metadata_atomically(self):
        draft = services.create_clarification_draft(
            self.prr,
            clarification_questions=(
                "Please identify the employee names and date range requested."
            ),
            clarification_due=dt.date(2026, 8, 10),
            actor=self.liaison,
        )

        edited = services.edit_structured_draft(
            draft,
            actor=self.liaison,
            expected_version=draft.version,
            structured_data={
                "clarification_questions": (
                    "Please identify the departments and precise date range "
                    "that should be searched."
                ),
                "clarification_due": dt.date(2026, 8, 14),
            },
        )

        self.assertEqual(
            edited.draft_metadata,
            {
                "clarification_questions": (
                    "Please identify the departments and precise date range "
                    "that should be searched."
                ),
                "clarification_due": "2026-08-14",
            },
        )
        self.assertIn("departments and precise date range", edited.draft_body)
        self.assertIn("August 14, 2026", edited.draft_body)
        self.assertEqual(edited.version, draft.version + 1)
        self.assertTrue(
            AuditLog.objects.filter(
                action="correspondence.draft_edited",
                detail__correspondence_id=str(draft.id),
            ).exists()
        )

    def test_approval_rejects_structured_content_metadata_divergence(self):
        draft = services.create_estimate_revision_draft(
            self.prr,
            new_date=dt.date(2026, 8, 17),
            reason="An additional archive requires review.",
            actor=self.liaison,
        )
        Correspondence.objects.filter(pk=draft.pk).update(
            draft_body=(
                "This imported draft tells the requester September 30 while "
                "its effect metadata still says August 17.\n"
            )
        )

        with self.assertRaisesMessage(
            ValidationError,
            "does not match its versioned template inputs",
        ):
            services.approve_draft(
                draft,
                actor=self.officer,
                expected_version=draft.version,
            )

    def test_lifecycle_approval_requires_current_requester_as_to_recipient(self):
        draft = services.create_clarification_draft(
            self.prr,
            clarification_questions=(
                "Please identify the employee names and date range requested."
            ),
            clarification_due=dt.date(2026, 8, 10),
            actor=self.liaison,
        )
        draft.recipients.all().delete()
        CorrespondenceRecipient.objects.create(
            correspondence=draft,
            recipient_type=CorrespondenceRecipient.RecipientType.TO,
            email="counsel@example.org",
            display_name="Outside counsel",
        )

        with self.assertRaisesMessage(
            ValidationError,
            "current requester must be a To recipient",
        ):
            services.approve_draft(
                draft,
                actor=self.officer,
                expected_version=draft.version,
            )

    def test_lifecycle_queue_revalidates_current_requester_address(self):
        draft = services.create_clarification_draft(
            self.prr,
            clarification_questions=(
                "Please identify the employee names and date range requested."
            ),
            clarification_due=dt.date(2026, 8, 10),
            actor=self.liaison,
        )
        approved = services.approve_draft(
            draft,
            actor=self.officer,
            expected_version=draft.version,
        )
        requester = self.prr.requester
        requester.email = "corrected@example.org"
        requester.save(update_fields=["email"])

        with self.assertRaisesMessage(
            ValidationError,
            "current requester must be a To recipient",
        ):
            services.queue_approved(
                approved,
                actor=self.officer,
                expected_version=approved.version,
            )

        approved.refresh_from_db()
        self.assertEqual(approved.status, Correspondence.Status.APPROVED)
        self.assertFalse(CorrespondenceDelivery.objects.exists())

    def test_acknowledgment_estimate_must_match_current_request_snapshot(self):
        with self.assertRaisesMessage(
            ValidationError,
            "acknowledgment estimate snapshot is stale",
        ):
            services.create_acknowledgment_draft(
                self.prr,
                actor=self.liaison,
                estimated_completion=dt.date(2026, 8, 17),
            )

        stale_before_approval = services.create_acknowledgment_draft(
            self.prr,
            actor=self.liaison,
        )
        self.prr.estimated_completion = dt.date(2026, 8, 10)
        self.prr.save(update_fields=["estimated_completion"])
        with self.assertRaisesMessage(
            ValidationError,
            "acknowledgment estimate snapshot is stale",
        ):
            services.approve_draft(
                stale_before_approval,
                actor=self.officer,
                expected_version=stale_before_approval.version,
            )

        self.prr.estimated_completion = dt.date(2026, 8, 3)
        self.prr.save(update_fields=["estimated_completion"])
        current = services.create_acknowledgment_draft(
            self.prr,
            actor=self.liaison,
        )
        current = services.approve_draft(
            current,
            actor=self.officer,
            expected_version=current.version,
        )
        self.prr.estimated_completion = dt.date(2026, 8, 24)
        self.prr.save(update_fields=["estimated_completion"])

        with self.assertRaisesMessage(
            ValidationError,
            "acknowledgment estimate snapshot is stale",
        ):
            services.queue_approved(
                current,
                actor=self.officer,
                expected_version=current.version,
            )
        self.assertFalse(CorrespondenceDelivery.objects.exists())

    def test_closure_and_denial_drafts_require_officer_to_create_and_edit(self):
        for kind in (Correspondence.Kind.CLOSURE, Correspondence.Kind.DENIAL):
            with self.subTest(kind=kind):
                denial_basis = (
                    {
                        "denial_exemption_citation_ids": [
                            self.denial_exemption.pk
                        ]
                    }
                    if kind == Correspondence.Kind.DENIAL
                    else {}
                )
                with self.assertRaises(PermissionDenied):
                    services.create_authored_draft(
                        self.prr,
                        kind=kind,
                        subject="Restricted legal draft",
                        body="Records Officer review required.",
                        actor=self.liaison,
                        **denial_basis,
                    )

                draft = services.create_authored_draft(
                    self.prr,
                    kind=kind,
                    subject="Restricted legal draft",
                    body="Records Officer review required.",
                    actor=self.officer,
                    **denial_basis,
                )
                with self.assertRaises(PermissionDenied):
                    services.edit_draft(
                        draft,
                        subject="Unauthorized rewrite",
                        body="This edit must be rejected.",
                        actor=self.liaison,
                        expected_version=draft.version,
                    )

    def test_recipient_display_names_reject_control_characters(self):
        with self.assertRaises(ValidationError):
            services.create_authored_draft(
                self.prr,
                kind=Correspondence.Kind.GENERAL,
                subject="Subject",
                body="Body",
                actor=self.liaison,
                recipients=[
                    {
                        "recipient_type": CorrespondenceRecipient.RecipientType.TO,
                        "email": "jane@example.org",
                        "display_name": "Jane\r\nBcc: attacker@example.org",
                    }
                ],
            )
        with self.assertRaises(ValidationError):
            services.create_authored_draft(
                self.prr,
                kind=Correspondence.Kind.GENERAL,
                subject="Subject\x00Injected",
                body="Body",
                actor=self.liaison,
            )

    def test_approval_revalidates_imported_recipient_and_attachment_headers(self):
        recipient_draft = services.create_authored_draft(
            self.prr,
            kind=Correspondence.Kind.GENERAL,
            subject="Subject",
            body="Body",
            actor=self.liaison,
        )
        recipient_draft.recipients.update(
            display_name="Jane\r\nBcc: attacker@example.org"
        )
        with self.assertRaises(ValidationError):
            services.approve_draft(
                recipient_draft,
                actor=self.officer,
                expected_version=recipient_draft.version,
            )

        attachment_draft = services.create_authored_draft(
            self.prr,
            kind=Correspondence.Kind.GENERAL,
            subject="Subject",
            body="Body",
            actor=self.liaison,
        )
        attachment = CorrespondenceAttachment.objects.create(
            correspondence=attachment_draft,
            original_filename="records.pdf",
            content_type="application/pdf",
            size=4,
            sha256="a" * 64,
            uploaded_by=self.liaison,
        )
        attachment = CorrespondenceAttachment.objects.filter(
            pk=attachment.pk
        ).finalize_locked_scan(
            expected_storage_key=attachment.storage_key,
            scan_status=CorrespondenceAttachment.ScanStatus.CLEAN,
            scanned_at=timezone.now(),
            promoted_storage_key=(
                f"correspondence/attachments/{attachment_draft.pk}/"
                f"{attachment.pk}"
            ),
        )
        models.QuerySet(
            model=CorrespondenceAttachment,
            using="default",
        ).filter(pk=attachment.pk).update(
            content_type="application/pdf\r\nX-Evil: true",
            sha256="NOT-A-LOWERCASE-SHA256",
        )
        with self.assertRaises(ValidationError):
            services.approve_draft(
                attachment_draft,
                actor=self.officer,
                expected_version=attachment_draft.version,
            )

    @patch("apps.records.attachments._object_store_client")
    def test_clean_attachment_helper_uses_correspondence_storage_key(self, client):
        draft = services.create_authored_draft(
            self.prr,
            kind=Correspondence.Kind.GENERAL,
            subject="Attachment storage key",
            body="Verify the shared clean-download helper.",
            actor=self.liaison,
        )
        attachment = CorrespondenceAttachment.objects.create(
            correspondence=draft,
            original_filename="records.pdf",
            content_type="application/pdf",
            size=4,
            sha256="a" * 64,
            uploaded_by=self.liaison,
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
        body = object()
        client.return_value.get_object.return_value = {"Body": body}

        from apps.records.attachments import open_clean_attachment

        self.assertIs(open_clean_attachment(attachment), body)
        self.assertEqual(
            client.return_value.get_object.call_args.kwargs["Key"],
            attachment.storage_key,
        )

    def test_installment_notice_requires_this_request(self):
        other_requester = Requester.objects.create(email="other@example.org")
        other_request = PublicRecordsRequest.objects.create(
            requester=other_requester,
            description="Other request.",
        )
        other_installment = Installment.objects.create(
            request=other_request,
            sequence=1,
        )
        with self.assertRaisesMessage(
            ValidationError,
            "must belong to this request",
        ):
            services.create_installment_notice_draft(
                self.prr,
                installment=other_installment,
                is_final=False,
                actor=self.officer,
            )

    def test_installment_notice_link_is_derived_not_supplied(self):
        """No caller can point a requester notice anywhere but the token."""
        installment = Installment.objects.create(
            request=self.prr,
            sequence=1,
            package_storage_key="productions/request/1/package.zip",
            package_sha256="b" * 64,
            package_size=123,
            package_manifest_sha256="c" * 64,
            package_prepared_at=timezone.now(),
            released_at=timezone.now(),
            download_expires_at=timezone.now() + dt.timedelta(days=7),
        )
        item = RecordItem.objects.create(
            request=self.prr,
            storage_key="records/one.pdf",
            sha256="a" * 64,
            content_length=4,
            content_type="application/pdf",
            review_state=RecordItem.ReviewState.RESPONSIVE,
            reviewed_by=self.officer,
            reviewed_at=timezone.now(),
        )
        installment.items.add(item)
        installment.package_manifest_sha256 = (
            current_installment_manifest_sha256(installment)
        )
        installment.save(update_fields=["package_manifest_sha256"])

        draft = services.create_installment_notice_draft(
            self.prr,
            installment=installment,
            is_final=False,
            actor=self.officer,
        )

        self.assertEqual(
            draft.draft_metadata["download_url"],
            installment_download_url(installment),
        )
        with self.assertRaises(TypeError):
            services.create_installment_notice_draft(
                self.prr,
                installment=installment,
                download_url="https://attacker.example/download",
                is_final=False,
                actor=self.officer,
            )

    def test_a_portal_address_changed_after_drafting_blocks_the_notice(self):
        """The portal address is a Settings-page value now, so it can change
        between drafting a notice and approving it. The drafted link then
        points at the old address, and the notice must not go out with it."""
        from apps.configuration import services as settings_services

        installment = Installment.objects.create(
            request=self.prr,
            sequence=1,
            package_storage_key="productions/request/1/package.zip",
            package_sha256="b" * 64,
            package_size=123,
            package_manifest_sha256="c" * 64,
            package_prepared_at=timezone.now(),
            released_at=timezone.now(),
            download_expires_at=timezone.now() + dt.timedelta(days=7),
        )
        item = RecordItem.objects.create(
            request=self.prr,
            storage_key="records/one.pdf",
            sha256="a" * 64,
            content_length=4,
            content_type="application/pdf",
            review_state=RecordItem.ReviewState.RESPONSIVE,
            reviewed_by=self.officer,
            reviewed_at=timezone.now(),
        )
        installment.items.add(item)
        installment.package_manifest_sha256 = (
            current_installment_manifest_sha256(installment)
        )
        installment.save(update_fields=["package_manifest_sha256"])
        draft = services.create_installment_notice_draft(
            self.prr,
            installment=installment,
            is_final=False,
            actor=self.officer,
        )

        settings_services.set_value(
            "PORTAL_BASE_URL",
            "https://records.example.org",
            actor=self.admin,
            reason="Portal moved to the district's new records host.",
        )

        with self.assertRaisesMessage(
            ValidationError, "configured application origin"
        ):
            services.approve_draft(
                draft, actor=self.officer, expected_version=draft.version
            )
        draft.refresh_from_db()
        self.assertEqual(draft.status, Correspondence.Status.DRAFT)

    def test_installment_notice_is_rejected_until_verified_download_exists(self):
        installment = Installment.objects.create(
            request=self.prr,
            sequence=1,
            released_at=timezone.now(),
            download_expires_at=timezone.now() + dt.timedelta(days=7),
        )
        with self.assertRaisesMessage(
            ValidationError,
            "verified, released, unexpired installment package is required",
        ):
            services.create_installment_notice_draft(
                self.prr,
                installment=installment,
                is_final=True,
                actor=self.officer,
            )

    def test_revoked_installment_cannot_be_announced(self):
        installment = Installment.objects.create(
            request=self.prr,
            sequence=1,
            package_storage_key="productions/request/1/package.zip",
            package_sha256="b" * 64,
            package_size=123,
            package_manifest_sha256="c" * 64,
            package_prepared_at=timezone.now(),
            released_at=timezone.now(),
            download_expires_at=timezone.now() + dt.timedelta(days=7),
            revoked_at=timezone.now(),
            revocation_reason="Wrong records were packaged.",
        )
        with self.assertRaisesMessage(
            ValidationError,
            "revoked installment download cannot be announced",
        ):
            services.create_installment_notice_draft(
                self.prr,
                installment=installment,
                is_final=False,
                actor=self.officer,
            )

    def test_installment_notice_derives_fully_withheld_exemption_fact(self):
        installment = Installment.objects.create(
            request=self.prr,
            sequence=1,
            package_storage_key="productions/request/1/package.zip",
            package_sha256="b" * 64,
            package_size=123,
            package_manifest_sha256="c" * 64,
            package_prepared_at=timezone.now(),
            exemption_log_storage_key="exemption-logs/request/1/log.pdf",
            exemption_log_sha256="d" * 64,
            exemption_log_size=456,
            exemption_log_manifest_sha256="e" * 64,
            exemption_log_prepared_at=timezone.now(),
            released_at=timezone.now(),
            download_expires_at=timezone.now() + dt.timedelta(days=7),
        )
        citation = ExemptionCitation.objects.create(
            citation="RCW 42.56.230(1)",
            short_name="Personal information",
        )
        item = RecordItem.objects.create(
            request=self.prr,
            storage_key="records/withheld.pdf",
            sha256="a" * 64,
            review_state=RecordItem.ReviewState.WITHHELD,
            withholding_exemption=citation,
            reviewed_by=self.officer,
            reviewed_at=timezone.now(),
        )
        item.withholding_exemptions.add(citation)
        installment.items.add(item)
        installment.exemption_log_manifest_sha256 = (
            current_exemption_log_manifest_sha256(installment)
        )
        installment.package_manifest_sha256 = current_installment_manifest_sha256(
            installment
        )
        installment.save(update_fields=[
            "exemption_log_manifest_sha256",
            "package_manifest_sha256",
        ])

        draft = services.create_installment_notice_draft(
            self.prr,
            installment=installment,
            is_final=False,
            actor=self.officer,
        )

        self.assertEqual(draft.draft_metadata["item_count"], 1)
        self.assertIs(draft.draft_metadata["has_exemptions"], True)
        self.assertEqual(draft.template_version, "3")
        self.assertIn("includes an exemption and withholding log", draft.draft_body)

    def test_replacement_must_correct_an_approved_message_on_the_same_request(self):
        draft = services.create_authored_draft(
            self.prr,
            kind=Correspondence.Kind.GENERAL,
            subject="Original",
            body="Original body",
            actor=self.liaison,
        )
        with self.assertRaises(ValidationError):
            services.create_authored_draft(
                self.prr,
                kind=Correspondence.Kind.GENERAL,
                subject="Correction",
                body="Correction body",
                actor=self.liaison,
                replaces=draft,
            )

        approved = services.approve_draft(
            draft,
            actor=self.officer,
            expected_version=draft.version,
        )
        replacement = services.create_authored_draft(
            self.prr,
            kind=Correspondence.Kind.GENERAL,
            subject="Correction",
            body="Correction body",
            actor=self.liaison,
            replaces=approved,
        )
        self.assertEqual(replacement.replaces, approved)

    def test_approval_requires_valid_to_recipient_and_clean_attachments(self):
        missing_recipient = services.create_authored_draft(
            self.prr,
            kind=Correspondence.Kind.GENERAL,
            subject="Subject",
            body="Body",
            actor=self.liaison,
            recipients=[],
        )
        with self.assertRaises(ValidationError):
            services.approve_draft(
                missing_recipient,
                actor=self.officer,
                expected_version=missing_recipient.version,
            )

        unsafe = services.create_authored_draft(
            self.prr,
            kind=Correspondence.Kind.GENERAL,
            subject="Subject",
            body="Body",
            actor=self.liaison,
        )
        CorrespondenceAttachment.objects.create(
            correspondence=unsafe,
            original_filename="unscanned.txt",
            content_type="text/plain",
            size=4,
            sha256="a" * 64,
            uploaded_by=self.liaison,
        )
        with self.assertRaises(ValidationError):
            services.approve_draft(
                unsafe,
                actor=self.officer,
                expected_version=unsafe.version,
            )

    def test_approval_enforces_frozen_attachment_count_and_size_limits(self):
        cases = (
            (
                "count",
                {
                    "PRR_CORRESPONDENCE_ATTACHMENT_MAX_COUNT": 1,
                    "PRR_CORRESPONDENCE_ATTACHMENT_MAX_BYTES": 10,
                    "PRR_CORRESPONDENCE_ATTACHMENT_TOTAL_MAX_BYTES": 20,
                },
                (4, 4),
                "too many attachments",
            ),
            (
                "per_file",
                {
                    "PRR_CORRESPONDENCE_ATTACHMENT_MAX_COUNT": 2,
                    "PRR_CORRESPONDENCE_ATTACHMENT_MAX_BYTES": 3,
                    "PRR_CORRESPONDENCE_ATTACHMENT_TOTAL_MAX_BYTES": 6,
                },
                (4,),
                "per-file size limit",
            ),
            (
                "combined",
                {
                    "PRR_CORRESPONDENCE_ATTACHMENT_MAX_COUNT": 2,
                    "PRR_CORRESPONDENCE_ATTACHMENT_MAX_BYTES": 4,
                    "PRR_CORRESPONDENCE_ATTACHMENT_TOTAL_MAX_BYTES": 7,
                },
                (4, 4),
                "combined size limit",
            ),
        )
        for label, limits, sizes, message in cases:
            with self.subTest(label=label), override_settings(**limits):
                draft = services.create_authored_draft(
                    self.prr,
                    kind=Correspondence.Kind.GENERAL,
                    subject=f"Attachment bounds {label}",
                    body="Body",
                    actor=self.liaison,
                )
                for index, size in enumerate(sizes):
                    attachment = CorrespondenceAttachment.objects.create(
                        correspondence=draft,
                        original_filename=f"bounded-{index}.txt",
                        content_type="text/plain",
                        size=size,
                        sha256="a" * 64,
                        uploaded_by=self.liaison,
                    )
                    CorrespondenceAttachment.objects.filter(
                        pk=attachment.pk
                    ).finalize_locked_scan(
                        expected_storage_key=attachment.storage_key,
                        scan_status=CorrespondenceAttachment.ScanStatus.CLEAN,
                        scanned_at=timezone.now(),
                        promoted_storage_key=(
                            f"correspondence/attachments/{draft.pk}/"
                            f"{attachment.pk}"
                        ),
                    )

                with self.assertRaisesMessage(ValidationError, message):
                    services.approve_draft(
                        draft,
                        actor=self.officer,
                        expected_version=draft.version,
                    )

    @patch("apps.correspondence.tasks.enqueue_queued_delivery")
    def test_queue_creates_one_delivery_and_enqueues_by_identifier(self, enqueue):
        draft = services.create_authored_draft(
            self.prr,
            kind=Correspondence.Kind.GENERAL,
            subject="Subject",
            body="Body",
            actor=self.liaison,
        )
        approved = services.approve_draft(
            draft,
            actor=self.officer,
            expected_version=draft.version,
        )

        with self.captureOnCommitCallbacks(execute=True):
            delivery = services.queue_approved(
                approved,
                actor=self.officer,
                expected_version=approved.version,
            )

        approved.refresh_from_db()
        self.prr.refresh_from_db()
        self.assertEqual(approved.status, Correspondence.Status.QUEUED)
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.PENDING)
        self.assertEqual(delivery.correspondence, approved)
        self.assertEqual(
            delivery.idempotency_key,
            f"correspondence-delivery:{approved.id}",
        )
        self.assertEqual(self.prr.status, RequestStatus.RECEIVED)
        enqueue.assert_called_once_with(str(delivery.id), attempt_count=0)
        serialized = str(enqueue.call_args)
        self.assertNotIn(self.prr.requester.email, serialized)
        self.assertNotIn(self.prr.requester.name, serialized)
        self.assertNotIn(approved.approved_body, serialized)

    @patch("apps.correspondence.tasks.enqueue_queued_delivery")
    def test_transaction_rollback_does_not_enqueue_delivery(self, enqueue):
        draft = services.create_authored_draft(
            self.prr,
            kind=Correspondence.Kind.GENERAL,
            subject="Subject",
            body="Body",
            actor=self.liaison,
        )
        approved = services.approve_draft(
            draft,
            actor=self.officer,
            expected_version=draft.version,
        )

        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                services.queue_approved(
                    approved,
                    actor=self.officer,
                    expected_version=approved.version,
                )
                raise RuntimeError("roll back")

        enqueue.assert_not_called()
        approved.refresh_from_db()
        self.assertEqual(approved.status, Correspondence.Status.APPROVED)
        self.assertFalse(CorrespondenceDelivery.objects.exists())

    def test_closed_request_rejects_newly_queued_correspondence(self):
        draft = services.create_authored_draft(
            self.prr,
            kind=Correspondence.Kind.GENERAL,
            subject="Stale update",
            body="This message must not be queued after closure.",
            actor=self.liaison,
        )
        approved = services.approve_draft(
            draft,
            actor=self.officer,
            expected_version=draft.version,
        )
        record_services.transition(
            self.prr,
            RequestStatus.FULFILLED,
            actor=self.officer,
            note="The Records Officer documented final fulfillment.",
        )

        with self.assertRaisesMessage(
            ValidationError,
            "cannot be queued after the request is closed",
        ):
            services.queue_approved(
                approved,
                actor=self.officer,
                expected_version=approved.version,
            )

        approved.refresh_from_db()
        self.assertEqual(approved.status, Correspondence.Status.APPROVED)
        self.assertFalse(CorrespondenceDelivery.objects.exists())

    def test_request_cannot_close_until_unsent_delivery_is_resolved(self):
        draft = services.create_authored_draft(
            self.prr,
            kind=Correspondence.Kind.GENERAL,
            subject="Pending update",
            body="This delivery must be resolved before closure.",
            actor=self.liaison,
        )
        correspondence, delivery = self.approve_and_queue(draft)

        with self.assertRaisesMessage(
            record_services.CorrespondenceDeliveryRequired,
            "Resolve or cancel every unsent correspondence delivery",
        ):
            record_services.transition(
                self.prr,
                RequestStatus.FULFILLED,
                actor=self.officer,
                note="The Records Officer documented final fulfillment.",
            )

        self.prr.refresh_from_db()
        self.assertEqual(self.prr.status, RequestStatus.RECEIVED)
        correspondence = services.cancel_unsent(
            correspondence,
            actor=self.officer,
            expected_version=correspondence.version,
            reason="Request closure supersedes this pending update.",
        )
        closed = record_services.transition(
            self.prr,
            RequestStatus.FULFILLED,
            actor=self.officer,
            note="The Records Officer documented final fulfillment.",
        )
        delivery.refresh_from_db()
        self.assertEqual(correspondence.status, Correspondence.Status.CANCELLED)
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.CANCELLED)
        self.assertEqual(closed.status, RequestStatus.FULFILLED)

    def test_cancel_and_retry_reuse_the_same_delivery_and_message_id(self):
        first = services.create_authored_draft(
            self.prr,
            kind=Correspondence.Kind.GENERAL,
            subject="Cancel me",
            body="Body",
            actor=self.liaison,
        )
        first, first_delivery = self.approve_and_queue(first)
        cancelled = services.cancel_unsent(
            first,
            actor=self.officer,
            expected_version=first.version,
            reason="Superseded before transport.",
        )
        first_delivery.refresh_from_db()
        self.assertEqual(cancelled.status, Correspondence.Status.CANCELLED)
        self.assertEqual(first_delivery.status, CorrespondenceDelivery.Status.CANCELLED)

        second = services.create_authored_draft(
            self.prr,
            kind=Correspondence.Kind.GENERAL,
            subject="Retry me",
            body="Body",
            actor=self.liaison,
        )
        second, delivery = self.approve_and_queue(second)
        original_message_id = second.rfc_message_id
        self.mark_retryable_failure(second, delivery)

        with (
            patch("apps.correspondence.tasks.enqueue_queued_delivery"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            retried = services.retry_delivery(
                delivery,
                actor=self.officer,
                expected_version=delivery.version,
            )

        second.refresh_from_db()
        self.assertEqual(retried.pk, delivery.pk)
        self.assertEqual(retried.status, CorrespondenceDelivery.Status.PENDING)
        self.assertEqual(second.status, Correspondence.Status.QUEUED)
        self.assertEqual(second.rfc_message_id, original_message_id)
        self.assertEqual(CorrespondenceDelivery.objects.count(), 2)

    def test_retry_rejects_fabricated_failure_state_without_attempt_evidence(self):
        correspondence = services.create_authored_draft(
            self.prr,
            kind=Correspondence.Kind.GENERAL,
            subject="Fabricated retry",
            body="Body",
            actor=self.liaison,
        )
        correspondence, delivery = self.approve_and_queue(correspondence)
        now = timezone.now()
        delivery.status = CorrespondenceDelivery.Status.RETRYABLE
        delivery.last_failure_category = "temporary_provider_failure"
        delivery.last_failure_description = "Provider unavailable."
        delivery.completed_at = now
        delivery.save()
        correspondence.status = Correspondence.Status.FAILED
        correspondence.failed_at = now
        correspondence.failure_category = "temporary_provider_failure"
        correspondence.failure_description = "Provider unavailable."
        correspondence.save()

        with self.assertRaisesMessage(
            ValidationError,
            "matching finalized failure evidence",
        ):
            services.retry_delivery(
                delivery,
                actor=self.officer,
                expected_version=delivery.version,
            )


@override_settings(PRR_CORRESPONDENCE_AUTOMATED_OUTBOUND_ENABLED=True)
class LegalEffectTests(CorrespondenceServiceTestCase):
    def test_legal_effects_reject_fabricated_sent_fields_without_attempt_evidence(self):
        draft = services.create_acknowledgment_draft(
            self.prr,
            actor=self.liaison,
        )
        correspondence, delivery = self.approve_and_queue(draft)
        sent_at = timezone.now()
        delivery.status = CorrespondenceDelivery.Status.SENT
        delivery.provider_message_id = f"gmail-{delivery.id}"
        delivery.provider_thread_id = "fabricated-thread"
        delivery.attempt_count = 1
        delivery.successful_attempt_number = 1
        delivery.completed_at = sent_at
        delivery.save()
        correspondence.status = Correspondence.Status.SENT
        correspondence.sent_at = sent_at
        correspondence.provider_message_id = delivery.provider_message_id
        correspondence.provider_thread_id = delivery.provider_thread_id
        correspondence.save()

        with self.assertRaisesMessage(
            ValidationError,
            "matching finalized provider evidence",
        ):
            services.apply_successful_delivery_effects(correspondence)

        self.prr.refresh_from_db()
        self.assertEqual(self.prr.status, RequestStatus.RECEIVED)
        self.assertIsNone(self.prr.acknowledgment_sent_at)

    def test_only_one_clarification_request_can_be_outstanding(self):
        first = services.create_clarification_draft(
            self.prr,
            clarification_questions="Which school years are in scope?",
            clarification_due=dt.date(2026, 7, 31),
            actor=self.liaison,
        )
        second = services.create_clarification_draft(
            self.prr,
            clarification_questions="Which departments are in scope?",
            clarification_due=dt.date(2026, 8, 3),
            actor=self.liaison,
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
        first_delivery = services.queue_approved(
            first,
            actor=self.officer,
            expected_version=first.version,
        )

        with self.assertRaises(ValidationError):
            services.queue_approved(
                second,
                actor=self.officer,
                expected_version=second.version,
            )
        first.refresh_from_db()
        self.mark_sent(first, first_delivery)
        services.apply_successful_delivery_effects(first)
        with self.assertRaises(ValidationError):
            services.queue_approved(
                second,
                actor=self.officer,
                expected_version=second.version,
            )

    def test_only_one_lifecycle_effect_delivery_can_be_outstanding(self):
        acknowledgment = services.create_acknowledgment_draft(
            self.prr,
            actor=self.liaison,
        )
        acknowledgment, delivery = self.approve_and_queue(acknowledgment)
        clarification = services.create_clarification_draft(
            self.prr,
            clarification_questions="Which schools and years are in scope?",
            clarification_due=dt.date(2026, 8, 10),
            actor=self.liaison,
        )
        clarification = services.approve_draft(
            clarification,
            actor=self.officer,
            expected_version=clarification.version,
        )
        estimate = services.create_estimate_revision_draft(
            self.prr,
            new_date=dt.date(2026, 8, 17),
            reason="An additional archive requires review.",
            actor=self.liaison,
        )
        estimate = services.approve_draft(
            estimate,
            actor=self.officer,
            expected_version=estimate.version,
        )

        for pending in (clarification, estimate):
            with self.subTest(kind=pending.kind):
                with self.assertRaisesMessage(
                    ValidationError,
                    "Only one requester-lifecycle message may be outstanding",
                ):
                    services.queue_approved(
                        pending,
                        actor=self.officer,
                        expected_version=pending.version,
                    )

        with self.assertRaisesMessage(
            record_services.CorrespondenceDeliveryRequired,
            "Resolve or cancel the outstanding requester-lifecycle delivery",
        ):
            record_services.transition(
                self.prr,
                RequestStatus.GATHERING,
                actor=self.officer,
            )

        self.mark_sent(acknowledgment, delivery)
        services.apply_successful_delivery_effects(acknowledgment)

    def test_manual_and_automated_acknowledgments_share_one_delivery_guard(self):
        first = services.create_acknowledgment_draft(
            self.prr,
            actor=self.liaison,
        )
        first, first_delivery = self.approve_and_queue(first)

        with self.captureOnCommitCallbacks(execute=False):
            automated = services.queue_automated_acknowledgment(self.prr)
        self.assertFalse(automated.created)
        self.assertEqual(automated.delivery.pk, first_delivery.pk)

        second = services.create_acknowledgment_draft(
            self.prr,
            actor=self.liaison,
        )
        second = services.approve_draft(
            second,
            actor=self.officer,
            expected_version=second.version,
        )
        with self.assertRaisesMessage(
            ValidationError,
            "Only one requester-lifecycle message may be outstanding",
        ):
            services.queue_approved(
                second,
                actor=self.officer,
                expected_version=second.version,
            )

        first.refresh_from_db()
        self.mark_sent(first, first_delivery)
        services.apply_successful_delivery_effects(first)
        second.refresh_from_db()
        with self.assertRaisesMessage(
            ValidationError,
            "already been acknowledged",
        ):
            services.queue_approved(
                second,
                actor=self.officer,
                expected_version=second.version,
            )

    def test_acknowledgment_effect_occurs_only_after_success_and_is_idempotent(self):
        draft = services.create_acknowledgment_draft(
            self.prr,
            actor=self.liaison,
        )
        correspondence, delivery = self.approve_and_queue(draft)

        with self.assertRaises(ValidationError):
            services.apply_successful_delivery_effects(correspondence)
        self.prr.refresh_from_db()
        self.assertEqual(self.prr.status, RequestStatus.RECEIVED)
        self.assertIsNone(self.prr.acknowledgment_sent_at)

        sent_at = self.mark_sent(correspondence, delivery)
        services.apply_successful_delivery_effects(correspondence)
        event_count = StatusEvent.objects.filter(request=self.prr).count()
        services.apply_successful_delivery_effects(correspondence)

        self.prr.refresh_from_db()
        correspondence.refresh_from_db()
        self.assertEqual(self.prr.status, RequestStatus.ACKNOWLEDGED)
        self.assertEqual(self.prr.acknowledgment_sent_at, sent_at)
        acknowledgment_events = StatusEvent.objects.filter(request=self.prr)
        self.assertTrue(acknowledgment_events.exists())
        self.assertTrue(
            all(
                event.occurred_at == sent_at
                for event in acknowledgment_events
            )
        )
        self.assertEqual(StatusEvent.objects.filter(request=self.prr).count(), event_count)
        self.assertIsNotNone(correspondence.resulting_status_event_id)

    def test_acknowledgment_links_current_uncommunicated_estimate_evidence(self):
        revision = EstimateRevision.objects.create(
            request=self.prr,
            previous_date=None,
            new_date=self.prr.estimated_completion,
            reason="Initial estimate prepared for the acknowledgment.",
            revised_by=self.officer,
        )
        draft = services.create_acknowledgment_draft(
            self.prr,
            actor=self.liaison,
        )
        correspondence, delivery = self.approve_and_queue(draft)
        sent_at = self.mark_sent(correspondence, delivery)

        services.apply_successful_delivery_effects(correspondence)

        revision.refresh_from_db()
        self.assertEqual(revision.communicated_at, sent_at)
        self.assertEqual(revision.correspondence, correspondence)
        self.assertEqual(EstimateRevision.objects.count(), 1)

    def test_acknowledgment_creates_missing_current_estimate_evidence(self):
        self.assertFalse(EstimateRevision.objects.exists())
        draft = services.create_acknowledgment_draft(
            self.prr,
            actor=self.liaison,
        )
        correspondence, delivery = self.approve_and_queue(draft)
        sent_at = self.mark_sent(correspondence, delivery)

        services.apply_successful_delivery_effects(correspondence)

        revision = EstimateRevision.objects.get()
        self.assertIsNone(revision.previous_date)
        self.assertEqual(revision.new_date, self.prr.estimated_completion)
        self.assertEqual(
            revision.reason,
            "Initial estimate communicated with acknowledgment.",
        )
        self.assertEqual(revision.revised_by, self.officer)
        self.assertFalse(revision.automated)
        self.assertEqual(revision.communicated_at, sent_at)
        self.assertEqual(revision.correspondence, correspondence)

    def test_acknowledgment_does_not_duplicate_communicated_estimate_evidence(self):
        communicated_at = timezone.now() - dt.timedelta(days=1)
        revision = EstimateRevision.objects.create(
            request=self.prr,
            previous_date=None,
            new_date=self.prr.estimated_completion,
            reason="Initial estimate already communicated to the requester.",
            revised_by=self.officer,
            communicated_at=communicated_at,
        )
        draft = services.create_acknowledgment_draft(
            self.prr,
            actor=self.liaison,
        )
        correspondence, delivery = self.approve_and_queue(draft)
        self.mark_sent(correspondence, delivery)

        services.apply_successful_delivery_effects(correspondence)

        revision.refresh_from_db()
        self.assertEqual(revision.communicated_at, communicated_at)
        self.assertIsNone(revision.correspondence_id)
        self.assertEqual(EstimateRevision.objects.count(), 1)

    def test_clarification_success_opens_cycle_but_reply_requires_human_resolution(self):
        draft = services.create_clarification_draft(
            self.prr,
            clarification_questions="Which school years are in scope?",
            clarification_due=dt.date(2026, 7, 31),
            actor=self.liaison,
        )
        correspondence, delivery = self.approve_and_queue(draft)
        sent_at = self.mark_sent(correspondence, delivery)

        services.apply_successful_delivery_effects(correspondence)
        services.apply_successful_delivery_effects(correspondence)

        self.prr.refresh_from_db()
        cycle = ClarificationCycle.objects.get(request=self.prr)
        self.assertEqual(self.prr.status, RequestStatus.CLARIFICATION)
        self.assertEqual(cycle.outbound_correspondence, correspondence)
        self.assertEqual(cycle.opened_at, sent_at)
        self.assertIsNone(cycle.resolved_at)

        inbound = Correspondence.objects.create(
            request=self.prr,
            direction=Correspondence.Direction.INBOUND,
            kind=Correspondence.Kind.CLARIFICATION_RESPONSE,
            status=Correspondence.Status.RECEIVED,
            received_subject="Re: clarification",
            received_body="School years 2024 through 2026.",
        )
        cycle.inbound_response_correspondence = inbound
        cycle.responded_at = timezone.now()
        cycle.save()
        self.assertIsNone(cycle.resolved_at)

        with self.assertRaises(ValidationError):
            services.resolve_clarification(
                cycle,
                actor=self.officer,
                resolution_notes="short",
            )
        with self.assertRaises(PermissionDenied):
            services.resolve_clarification(
                cycle,
                actor=self.admin,
                resolution_notes="Scope confirmed as school years 2024 through 2026.",
            )

        cycle = services.resolve_clarification(
            cycle,
            actor=self.officer,
            resolution_notes="Scope confirmed as school years 2024 through 2026.",
        )
        self.prr.refresh_from_db()
        self.assertIsNotNone(cycle.resolved_at)
        self.assertEqual(cycle.resolved_by, self.officer)
        self.assertEqual(self.prr.status, RequestStatus.GATHERING)

    @patch("apps.records.tasks._notify")
    def test_delivered_clarification_permanently_satisfies_initial_response(
        self,
        mocked_notify,
    ):
        from apps.records import tasks

        self.prr.statutory_response_due = (
            timezone.localdate() - dt.timedelta(days=1)
        )
        self.prr.estimated_completion = None
        self.prr.save(
            update_fields=[
                "statutory_response_due",
                "estimated_completion",
            ]
        )
        draft = services.create_clarification_draft(
            self.prr,
            clarification_questions="Which school years are in scope?",
            clarification_due=timezone.localdate() + dt.timedelta(days=7),
            actor=self.liaison,
        )
        correspondence, delivery = self.approve_and_queue(draft)
        self.mark_sent(correspondence, delivery)
        services.apply_successful_delivery_effects(correspondence)

        with self.captureOnCommitCallbacks(execute=False):
            tasks.deadline_watchdog()
            tasks.auto_acknowledge_safety_net()

        self.prr.refresh_from_db()
        self.assertEqual(self.prr.status, RequestStatus.CLARIFICATION)
        self.assertIsNone(self.prr.acknowledgment_sent_at)
        self.assertFalse(self.prr.acknowledgment_overdue)
        self.assertEqual(Correspondence.objects.count(), 1)
        self.assertEqual(CorrespondenceDelivery.objects.count(), 1)
        self.assertFalse(EstimateRevision.objects.exists())
        mocked_notify.assert_not_called()

        cycle = ClarificationCycle.objects.get(request=self.prr)
        inbound = Correspondence.objects.create(
            request=self.prr,
            direction=Correspondence.Direction.INBOUND,
            kind=Correspondence.Kind.CLARIFICATION_RESPONSE,
            status=Correspondence.Status.RECEIVED,
            received_subject="Re: clarification",
            received_body="School years 2024 through 2026.",
        )
        cycle.inbound_response_correspondence = inbound
        cycle.responded_at = timezone.now()
        cycle.save(
            update_fields=[
                "inbound_response_correspondence",
                "responded_at",
            ]
        )
        services.resolve_clarification(
            cycle,
            actor=self.officer,
            resolution_notes="Scope confirmed as school years 2024 through 2026.",
        )

        with self.captureOnCommitCallbacks(execute=False):
            tasks.deadline_watchdog()
            tasks.auto_acknowledge_safety_net()

        self.prr.refresh_from_db()
        self.assertEqual(self.prr.status, RequestStatus.GATHERING)
        self.assertFalse(self.prr.acknowledgment_overdue)
        self.assertEqual(
            Correspondence.objects.filter(
                direction=Correspondence.Direction.OUTBOUND
            ).count(),
            1,
        )
        self.assertEqual(CorrespondenceDelivery.objects.count(), 1)
        self.assertFalse(EstimateRevision.objects.exists())
        mocked_notify.assert_not_called()

    def test_clarification_resolution_uses_fresh_database_request_identity(self):
        draft = services.create_clarification_draft(
            self.prr,
            clarification_questions="Which school years are in scope?",
            clarification_due=dt.date(2026, 7, 31),
            actor=self.liaison,
        )
        correspondence, delivery = self.approve_and_queue(draft)
        self.mark_sent(correspondence, delivery)
        services.apply_successful_delivery_effects(correspondence)

        cycle = ClarificationCycle.objects.get(request=self.prr)
        inbound = Correspondence.objects.create(
            request=self.prr,
            direction=Correspondence.Direction.INBOUND,
            kind=Correspondence.Kind.CLARIFICATION_RESPONSE,
            status=Correspondence.Status.RECEIVED,
            received_subject="Re: clarification",
            received_body="School years 2024 through 2026.",
        )
        cycle.inbound_response_correspondence = inbound
        cycle.responded_at = timezone.now()
        cycle.save()

        unrelated_request = PublicRecordsRequest.objects.create(
            requester=self.prr.requester,
            description="All unrelated transportation records.",
            estimated_completion=dt.date(2026, 8, 4),
        )
        cycle.request_id = unrelated_request.pk

        resolved = services.resolve_clarification(
            cycle,
            actor=self.officer,
            resolution_notes="Scope confirmed as school years 2024 through 2026.",
        )

        self.prr.refresh_from_db()
        unrelated_request.refresh_from_db()
        resolved.refresh_from_db()
        self.assertEqual(self.prr.status, RequestStatus.GATHERING)
        self.assertEqual(unrelated_request.status, RequestStatus.RECEIVED)
        self.assertEqual(resolved.request_id, self.prr.pk)
        self.assertEqual(
            resolved.resolved_status_event.request_id,
            self.prr.pk,
        )

    def test_reconciled_clarification_uses_provider_time_for_tolling(self):
        received_at = dt.datetime(2026, 7, 20, 16, 0, tzinfo=dt.UTC)
        queued_at = dt.datetime(2026, 7, 20, 17, 0, tzinfo=dt.UTC)
        # Monday 6pm Pacific, represented as Tuesday UTC.
        sent_at = dt.datetime(2026, 7, 21, 1, 0, tzinfo=dt.UTC)
        callback_at = dt.datetime(2026, 7, 22, 19, 0, tzinfo=dt.UTC)
        resumed_at = dt.datetime(2026, 7, 23, 19, 0, tzinfo=dt.UTC)
        PublicRecordsRequest.objects.filter(pk=self.prr.pk).update(
            received_at=received_at,
        )
        self.prr.refresh_from_db()
        with patch("django.utils.timezone.now", return_value=queued_at):
            draft = services.create_clarification_draft(
                self.prr,
                clarification_questions="Which school years are in scope?",
                clarification_due=dt.date(2026, 7, 31),
                actor=self.liaison,
            )
            correspondence, delivery = self.approve_and_queue(draft)
        self.mark_sent(correspondence, delivery, sent_at=sent_at)

        with patch("django.utils.timezone.now", return_value=callback_at):
            services.apply_successful_delivery_effects(correspondence)

        event = StatusEvent.objects.get(
            request=self.prr,
            to_status=RequestStatus.CLARIFICATION,
        )
        self.assertEqual(event.occurred_at, sent_at)
        cycle = ClarificationCycle.objects.get(request=self.prr)
        inbound = Correspondence.objects.create(
            request=self.prr,
            direction=Correspondence.Direction.INBOUND,
            kind=Correspondence.Kind.CLARIFICATION_RESPONSE,
            status=Correspondence.Status.RECEIVED,
            received_subject="Re: clarification",
            received_body="School years 2024 through 2026.",
        )
        cycle.inbound_response_correspondence = inbound
        cycle.responded_at = callback_at
        cycle.save(
            update_fields=[
                "inbound_response_correspondence",
                "responded_at",
            ]
        )
        with patch("django.utils.timezone.now", return_value=resumed_at):
            services.resolve_clarification(
                cycle,
                actor=self.officer,
                resolution_notes=(
                    "Scope confirmed as school years 2024 through 2026."
                ),
            )
        self.prr.refresh_from_db()

        # Pacific dates: clarification Monday, resume Thursday => Tue/Wed/Thu.
        self.assertEqual(self.prr.tolled_business_days, 3)

    def test_estimate_revision_is_human_reasoned_and_communicated_only_on_success(self):
        with self.assertRaises(ValidationError):
            services.create_estimate_revision_draft(
                self.prr,
                new_date=dt.date(2026, 8, 17),
                reason="Too busy",
                actor=self.liaison,
            )
        with self.assertRaises(PermissionDenied):
            services.create_estimate_revision_draft(
                self.prr,
                new_date=dt.date(2026, 8, 17),
                reason="An additional archive requires review.",
                actor=self.integration,
            )

        draft = services.create_estimate_revision_draft(
            self.prr,
            new_date=dt.date(2026, 8, 17),
            reason="An additional archive requires review.",
            actor=self.liaison,
        )
        correspondence, delivery = self.approve_and_queue(draft)

        self.prr.refresh_from_db()
        self.assertEqual(self.prr.estimated_completion, dt.date(2026, 8, 3))
        self.assertFalse(EstimateRevision.objects.exists())

        sent_at = self.mark_sent(correspondence, delivery)
        services.apply_successful_delivery_effects(correspondence)
        services.apply_successful_delivery_effects(correspondence)

        self.prr.refresh_from_db()
        revision = EstimateRevision.objects.get()
        self.assertEqual(self.prr.estimated_completion, dt.date(2026, 8, 17))
        self.assertEqual(revision.previous_date, dt.date(2026, 8, 3))
        self.assertEqual(revision.new_date, dt.date(2026, 8, 17))
        self.assertEqual(revision.reason, "An additional archive requires review.")
        self.assertEqual(revision.revised_by, self.officer)
        self.assertEqual(revision.correspondence, correspondence)
        self.assertEqual(revision.communicated_at, sent_at)

    def test_estimate_correspondence_paths_reuse_shared_pure_validator(self):
        with patch(
            "apps.records.letters.validate_estimate_revision",
            wraps=letters.validate_estimate_revision,
        ) as shared_validator:
            draft = services.create_estimate_revision_draft(
                self.prr,
                new_date=dt.date(2026, 8, 17),
                reason="An additional archive requires initial review.",
                actor=self.liaison,
            )
            self.assertTrue(shared_validator.called)

            shared_validator.reset_mock()
            draft = services.edit_structured_draft(
                draft,
                actor=self.liaison,
                expected_version=draft.version,
                structured_data={
                    "new_date": dt.date(2026, 8, 24),
                    "reason": (
                        "A second archive export requires additional review."
                    ),
                },
            )
            self.assertTrue(shared_validator.called)
            self.prr.refresh_from_db()
            self.assertEqual(
                self.prr.estimated_completion,
                dt.date(2026, 8, 3),
            )
            self.assertFalse(EstimateRevision.objects.exists())

            correspondence, delivery = self.approve_and_queue(draft)
            self.mark_sent(correspondence, delivery)
            shared_validator.reset_mock()

            services.apply_successful_delivery_effects(correspondence)

            self.assertTrue(shared_validator.called)
            self.prr.refresh_from_db()
            self.assertEqual(
                self.prr.estimated_completion,
                dt.date(2026, 8, 24),
            )

    def test_estimate_revision_delivery_serializes_provider_order(self):
        first = services.create_estimate_revision_draft(
            self.prr,
            new_date=dt.date(2026, 8, 17),
            reason="The archive search identified another mailbox.",
            actor=self.liaison,
        )
        second = services.create_estimate_revision_draft(
            self.prr,
            new_date=dt.date(2026, 8, 24),
            reason="A second archive export requires legal review.",
            actor=self.liaison,
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
        first_delivery = services.queue_approved(
            first,
            actor=self.officer,
            expected_version=first.version,
        )

        with self.assertRaisesMessage(
            ValidationError,
            "Only one requester-lifecycle message may be outstanding",
        ):
            services.queue_approved(
                second,
                actor=self.officer,
                expected_version=second.version,
            )

        first.refresh_from_db()
        self.mark_sent(first, first_delivery)
        services.apply_successful_delivery_effects(first)
        second.refresh_from_db()
        with self.assertRaisesMessage(
            ValidationError,
            "estimate snapshot is stale",
        ):
            services.queue_approved(
                second,
                actor=self.officer,
                expected_version=second.version,
            )

    def test_estimate_wrapper_cannot_mutate_or_overtake_durable_delivery(self):
        draft = letters.revise_estimate(
            self.prr,
            dt.date(2026, 8, 17),
            "The initial archive count was corrected by staff.",
            actor=self.officer,
        )
        self.prr.refresh_from_db()
        self.assertEqual(
            self.prr.estimated_completion,
            dt.date(2026, 8, 3),
        )
        self.assertFalse(EstimateRevision.objects.exists())
        self.assertEqual(draft.status, Correspondence.Status.DRAFT)
        draft, _ = self.approve_and_queue(draft)

        later = letters.revise_estimate(
            self.prr,
            new_date=dt.date(2026, 8, 24),
            reason="A newly identified export requires legal review.",
            actor=self.officer,
        )
        later = services.approve_draft(
            later,
            actor=self.officer,
            expected_version=later.version,
        )
        with self.assertRaisesMessage(
            ValidationError,
            "Only one requester-lifecycle message may be outstanding",
        ):
            services.queue_approved(
                later,
                actor=self.officer,
                expected_version=later.version,
            )

    def test_estimate_success_applies_if_approver_was_deactivated_after_approval(self):
        draft = services.create_estimate_revision_draft(
            self.prr,
            new_date=dt.date(2026, 8, 17),
            reason="An additional archive requires review.",
            actor=self.liaison,
        )
        correspondence, delivery = self.approve_and_queue(draft)
        self.officer.is_active = False
        self.officer.is_staff = False
        self.officer.save(update_fields=["is_active", "is_staff"])
        sent_at = self.mark_sent(correspondence, delivery)

        services.apply_successful_delivery_effects(correspondence)

        self.prr.refresh_from_db()
        revision = EstimateRevision.objects.get()
        self.assertEqual(self.prr.estimated_completion, dt.date(2026, 8, 17))
        self.assertEqual(revision.revised_by, self.officer)
        self.assertEqual(revision.communicated_at, sent_at)

    def test_safety_net_acknowledgment_queue_deduplicates(self):
        with patch("apps.correspondence.tasks.enqueue_queued_delivery"):
            with self.captureOnCommitCallbacks(execute=True):
                first = services.queue_automated_acknowledgment(self.prr)
            with self.captureOnCommitCallbacks(execute=True):
                second = services.queue_automated_acknowledgment(self.prr)

        first.delivery.correspondence.refresh_from_db()
        self.prr.refresh_from_db()
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.delivery.pk, second.delivery.pk)
        self.assertTrue(first.delivery.correspondence.automated)
        self.assertEqual(
            first.delivery.correspondence.kind,
            Correspondence.Kind.ACKNOWLEDGMENT,
        )
        self.assertEqual(
            first.delivery.correspondence.status,
            Correspondence.Status.QUEUED,
        )
        self.assertEqual(Correspondence.objects.count(), 1)
        self.assertEqual(CorrespondenceDelivery.objects.count(), 1)
        self.assertEqual(self.prr.status, RequestStatus.RECEIVED)
        self.assertIsNone(self.prr.acknowledgment_sent_at)

    def test_automated_acknowledgment_audit_is_recorded_only_after_success(
        self,
    ):
        with self.captureOnCommitCallbacks(execute=False):
            queued = services.queue_automated_acknowledgment(self.prr)
        delivery = queued.delivery
        correspondence = delivery.correspondence
        sent_at = self.mark_sent(correspondence, delivery)

        services.apply_successful_delivery_effects(correspondence)
        services.apply_successful_delivery_effects(correspondence)

        self.prr.refresh_from_db()
        correspondence.refresh_from_db()
        self.assertEqual(self.prr.acknowledgment_sent_at, sent_at)
        self.assertEqual(
            AuditLog.objects.filter(
                action="request.auto_acknowledged",
                detail__correspondence_id=str(correspondence.id),
            ).count(),
            1,
        )

    def test_cancelled_safety_net_delivery_requires_human_review_not_replacement(self):
        with self.captureOnCommitCallbacks(execute=False):
            queued = services.queue_automated_acknowledgment(self.prr)
        services.cancel_unsent(
            queued.delivery.correspondence,
            actor=self.officer,
            expected_version=queued.delivery.correspondence.version,
            reason="Officer cancelled this acknowledgment for legal review.",
        )

        with self.assertRaisesMessage(
            ValidationError,
            "requires human review",
        ):
            services.queue_automated_acknowledgment(self.prr)

        self.assertEqual(CorrespondenceDelivery.objects.count(), 1)
        self.assertEqual(Correspondence.objects.count(), 1)
