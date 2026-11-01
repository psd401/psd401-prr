import datetime as dt
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from django.utils import timezone

from apps.configuration import runtime, values
from apps.configuration.registry import SETTINGS_BY_KEY
from apps.correspondence.models import (
    ClarificationCycle,
    Correspondence,
    CorrespondenceAttachment,
    CorrespondenceDelivery,
    CorrespondenceDeliveryAttempt,
    CorrespondenceDenialCitation,
    CorrespondenceRecipient,
    InboundCorrespondenceReceipt,
)
from apps.records.models import (
    ExemptionCitation,
    PublicRecordsRequest,
    Requester,
    RequestStatus,
    StatusEvent,
)


class CorrespondenceModelTestCase(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_user(
            "records-officer", "officer@psd401.net", "pw", is_staff=True
        )
        requester = Requester.objects.create(
            name="Jane Requester", email="jane@example.org"
        )
        self.prr = PublicRecordsRequest.objects.create(
            requester=requester,
            description="All records concerning the transportation plan.",
        )

    def make_correspondence(self, **overrides):
        defaults = {
            "request": self.prr,
            "direction": Correspondence.Direction.OUTBOUND,
            "kind": Correspondence.Kind.GENERAL,
            "status": Correspondence.Status.DRAFT,
            "draft_subject": "Subject",
            "draft_body": "Body",
            "created_by": self.actor,
        }
        defaults.update(overrides)
        return Correspondence.objects.create(**defaults)


class CorrespondenceInvariantTests(CorrespondenceModelTestCase):
    def test_denial_basis_is_a_required_protected_exemption_fk(self):
        denial = self.make_correspondence(kind=Correspondence.Kind.DENIAL)
        citation = ExemptionCitation.objects.create(
            citation="RCW 42.56.250",
            short_name="Examination questions",
        )
        basis = CorrespondenceDenialCitation.objects.create(
            correspondence=denial,
            exemption=citation,
            selected_by=self.actor,
        )
        self.assertEqual(basis.exemption, citation)

        with self.assertRaises(ProtectedError):
            citation.delete()
        with self.assertRaises(IntegrityError), transaction.atomic():
            CorrespondenceDenialCitation.objects.create(
                correspondence=denial,
                exemption_id=None,
                selected_by=self.actor,
            )

    def test_required_correspondence_kinds_are_explicit(self):
        self.assertSetEqual(
            {value for value, _ in Correspondence.Kind.choices},
            {
                "acknowledgment",
                "clarification_request",
                "clarification_response",
                "delivery_status_notification",
                "estimate_revision",
                "fee_estimate",
                "installment_notice",
                "no_records_closure",
                "completion_closure",
                "nonpayment_closure",
                "third_party_notice",
                "responsive_records_notice",
                "scope_clarification",
                "submission_receipt",
                "closure",
                "denial",
                "general",
            },
        )

    def test_outbound_correspondence_must_belong_to_a_request(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_correspondence(request=None)

    def test_only_genuinely_unmatched_inbound_mail_may_lack_a_request(self):
        unmatched = self.make_correspondence(
            request=None,
            direction=Correspondence.Direction.INBOUND,
            status=Correspondence.Status.UNMATCHED,
            created_by=None,
        )
        self.assertIsNone(unmatched.request_id)

        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_correspondence(
                request=None,
                direction=Correspondence.Direction.INBOUND,
                status=Correspondence.Status.RECEIVED,
                created_by=None,
            )

    def test_direction_rejects_the_other_directions_lifecycle_states(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_correspondence(
                direction=Correspondence.Direction.INBOUND,
                status=Correspondence.Status.QUEUED,
            )

    def test_approved_or_later_outbound_content_requires_a_snapshot(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_correspondence(status=Correspondence.Status.APPROVED)

    def test_only_approved_system_correspondence_kinds_may_be_automated(self):
        for kind in (
            Correspondence.Kind.ACKNOWLEDGMENT,
            Correspondence.Kind.SUBMISSION_RECEIPT,
        ):
            with self.subTest(kind=kind):
                automated = self.make_correspondence(
                    kind=kind,
                    status=Correspondence.Status.APPROVED,
                    approved_subject="Automated correspondence",
                    approved_body="Approved body",
                    approved_at=timezone.now(),
                    automated=True,
                )
                self.assertTrue(automated.automated)

        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_correspondence(
                kind=Correspondence.Kind.GENERAL,
                status=Correspondence.Status.APPROVED,
                approved_subject="General",
                approved_body="Approved body",
                approved_at=timezone.now(),
                automated=True,
            )

    def test_sent_failed_and_cancelled_states_require_their_timestamps(self):
        base = {
            "approved_subject": "Approved subject",
            "approved_body": "Approved body",
            "approved_by": self.actor,
            "approved_at": timezone.now(),
            "queued_at": timezone.now(),
        }
        for status in (
            Correspondence.Status.SENT,
            Correspondence.Status.FAILED,
            Correspondence.Status.CANCELLED,
        ):
            with self.subTest(status=status):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    self.make_correspondence(status=status, **base)

    def test_approved_content_and_template_identity_are_immutable(self):
        correspondence = self.make_correspondence(
            status=Correspondence.Status.APPROVED,
            approved_subject="Approved subject",
            approved_body="Approved body",
            draft_metadata={"new_date": "2026-08-17"},
            approved_metadata={"new_date": "2026-08-17"},
            template_identifier="general",
            template_version="1",
            approved_by=self.actor,
            approved_at=timezone.now(),
        )
        correspondence.approved_body = "Silently changed"
        with self.assertRaises(ValidationError):
            correspondence.save()

        with self.assertRaises(ValidationError):
            Correspondence.objects.filter(pk=correspondence.pk).update(
                approved_body="Bulk rewrite"
            )

        correspondence.refresh_from_db()
        correspondence.template_version = "2"
        with self.assertRaises(ValidationError):
            correspondence.save()

        correspondence.refresh_from_db()
        correspondence.approved_metadata = {"new_date": "2026-09-01"}
        with self.assertRaises(ValidationError):
            correspondence.save()

        with self.assertRaises(ValidationError):
            Correspondence.objects.filter(pk=correspondence.pk).update(
                draft_metadata={"new_date": "2026-09-01"}
            )

    def test_non_draft_correspondence_cannot_regress_to_editable_draft(self):
        instance = self.make_correspondence(
            status=Correspondence.Status.APPROVED,
            approved_subject="Approved subject",
            approved_body="Approved body",
            approved_by=self.actor,
            approved_at=timezone.now(),
        )
        instance.status = Correspondence.Status.DRAFT
        with self.assertRaisesMessage(
            ValidationError,
            "cannot return to draft",
        ):
            instance.save(update_fields=["status"])

        instance.refresh_from_db()
        self.assertEqual(instance.status, Correspondence.Status.APPROVED)
        with self.assertRaisesMessage(
            ValidationError,
            "cannot return to draft",
        ):
            Correspondence.objects.filter(pk=instance.pk).update(
                status=Correspondence.Status.DRAFT
            )

        instance.refresh_from_db()
        self.assertEqual(instance.status, Correspondence.Status.APPROVED)

    def test_only_locked_drafts_can_be_deleted(self):
        draft = self.make_correspondence()
        draft_id = draft.pk
        draft.delete()
        self.assertFalse(
            Correspondence.objects.filter(pk=draft_id).exists()
        )

        for mutation in ("instance", "queryset"):
            with self.subTest(mutation=mutation):
                approved = self.make_correspondence(
                    status=Correspondence.Status.APPROVED,
                    approved_subject="Approved subject",
                    approved_body="Approved body",
                    approved_by=self.actor,
                    approved_at=timezone.now(),
                )
                with self.assertRaisesMessage(
                    ValidationError,
                    "immutable legal record",
                ):
                    if mutation == "instance":
                        approved.delete()
                    else:
                        Correspondence.objects.filter(
                            pk=approved.pk
                        ).delete()
                self.assertTrue(
                    Correspondence.objects.filter(pk=approved.pk).exists()
                )

    def test_sent_and_cancelled_correspondence_are_terminal(self):
        now = timezone.now()
        terminal_rows = (
            self.make_correspondence(
                status=Correspondence.Status.SENT,
                approved_subject="Sent subject",
                approved_body="Sent body",
                approved_by=self.actor,
                approved_at=now,
                queued_at=now,
                sent_at=now,
            ),
            self.make_correspondence(
                status=Correspondence.Status.CANCELLED,
                approved_subject="Cancelled subject",
                approved_body="Cancelled body",
                approved_by=self.actor,
                approved_at=now,
                cancelled_at=now,
            ),
        )

        for instance in terminal_rows:
            original_status = instance.status
            with self.subTest(status=original_status, mutation="instance"):
                instance.status = Correspondence.Status.APPROVED
                with self.assertRaisesMessage(
                    ValidationError,
                    "Sent and cancelled correspondence are terminal",
                ):
                    instance.save(update_fields=["status"])
                instance.refresh_from_db()
                self.assertEqual(instance.status, original_status)

            with self.subTest(status=original_status, mutation="queryset"):
                with self.assertRaisesMessage(
                    ValidationError,
                    "Sent and cancelled correspondence are terminal",
                ):
                    Correspondence.objects.filter(pk=instance.pk).update(
                        status=Correspondence.Status.APPROVED
                    )
                instance.refresh_from_db()
                self.assertEqual(instance.status, original_status)

    def test_resulting_status_event_uses_locked_one_shot_binding(self):
        now = timezone.now()
        correspondence = self.make_correspondence(
            kind=Correspondence.Kind.ACKNOWLEDGMENT,
            status=Correspondence.Status.SENT,
            approved_subject="Acknowledgment",
            approved_body="Approved body",
            approved_by=self.actor,
            approved_at=now,
            queued_at=now,
            sent_at=now,
        )
        event = StatusEvent.objects.create(
            request=self.prr,
            actor=self.actor,
            from_status=RequestStatus.RECEIVED,
            to_status=RequestStatus.ACKNOWLEDGED,
            note="Statutory acknowledgment delivered.",
            visible_to_requester=True,
            occurred_at=now,
            correspondence_effect=(
                StatusEvent.CorrespondenceEffect.ACKNOWLEDGMENT_DELIVERY
            ),
        )

        with transaction.atomic():
            bound = Correspondence.objects.filter(
                pk=correspondence.pk
            )._bind_locked_resulting_status_event(status_event=event)

        self.assertEqual(bound.resulting_status_event_id, event.pk)
        correspondence.refresh_from_db()
        correspondence.resulting_status_event = None
        with self.assertRaisesMessage(
            ValidationError,
            "resulting status event",
        ):
            correspondence.save(update_fields=["resulting_status_event"])
        with self.assertRaisesMessage(
            ValidationError,
            "resulting status event",
        ):
            Correspondence.objects.filter(pk=correspondence.pk).update(
                resulting_status_event=None
            )

        replacement = StatusEvent.objects.create(
            request=self.prr,
            actor=self.actor,
            from_status=RequestStatus.RECEIVED,
            to_status=RequestStatus.ACKNOWLEDGED,
            note="A duplicate event must not replace the binding.",
            visible_to_requester=True,
            occurred_at=now,
        )
        with self.assertRaisesMessage(ValidationError, "already bound"):
            with transaction.atomic():
                Correspondence.objects.filter(
                    pk=correspondence.pk
                )._bind_locked_resulting_status_event(
                    status_event=replacement
                )

    def test_resulting_status_event_rejects_cross_request_and_wrong_effect(self):
        now = timezone.now()
        other_request = PublicRecordsRequest.objects.create(
            requester=self.prr.requester,
            description="A separate request for event-binding validation.",
        )
        cross_request_event = StatusEvent.objects.create(
            request=other_request,
            actor=self.actor,
            from_status=RequestStatus.RECEIVED,
            to_status=RequestStatus.ACKNOWLEDGED,
            note="An unrelated acknowledgment event.",
            visible_to_requester=True,
            occurred_at=now,
            correspondence_effect=(
                StatusEvent.CorrespondenceEffect.ACKNOWLEDGMENT_DELIVERY
            ),
        )
        wrong_effect_event = StatusEvent.objects.create(
            request=self.prr,
            actor=self.actor,
            from_status=RequestStatus.RECEIVED,
            to_status=RequestStatus.GATHERING,
            note="A status event for the wrong legal effect.",
            visible_to_requester=True,
            occurred_at=now,
        )

        for event in (cross_request_event, wrong_effect_event):
            with self.subTest(event=event.pk):
                correspondence = self.make_correspondence(
                    kind=Correspondence.Kind.DENIAL,
                    status=Correspondence.Status.SENT,
                    approved_subject="Denial",
                    approved_body="Approved denial body",
                    approved_by=self.actor,
                    approved_at=now,
                    queued_at=now,
                    sent_at=now,
                )
                with self.assertRaisesMessage(
                    ValidationError,
                    "does not match",
                ):
                    with transaction.atomic():
                        Correspondence.objects.filter(
                            pk=correspondence.pk
                        )._bind_locked_resulting_status_event(
                            status_event=event
                        )
                correspondence.refresh_from_db()
                self.assertIsNone(correspondence.resulting_status_event_id)

    def test_resulting_status_event_cannot_bypass_binding_on_insert_or_upsert(self):
        now = timezone.now()
        event = StatusEvent.objects.create(
            request=self.prr,
            actor=self.actor,
            from_status=RequestStatus.RECEIVED,
            to_status=RequestStatus.ACKNOWLEDGED,
            note="Statutory acknowledgment delivered.",
            visible_to_requester=True,
            occurred_at=now,
            correspondence_effect=(
                StatusEvent.CorrespondenceEffect.ACKNOWLEDGMENT_DELIVERY
            ),
        )
        fields = {
            "request": self.prr,
            "direction": Correspondence.Direction.OUTBOUND,
            "kind": Correspondence.Kind.ACKNOWLEDGMENT,
            "status": Correspondence.Status.SENT,
            "draft_subject": "Acknowledgment",
            "draft_body": "Approved body",
            "approved_subject": "Acknowledgment",
            "approved_body": "Approved body",
            "approved_by": self.actor,
            "approved_at": now,
            "queued_at": now,
            "sent_at": now,
            "resulting_status_event": event,
        }
        with self.assertRaisesMessage(
            ValidationError,
            "resulting status event",
        ):
            Correspondence.objects.create(**fields)

        correspondence = self.make_correspondence(
            **(fields | {"resulting_status_event": None})
        )
        bypass = Correspondence(
            id=correspondence.pk,
            **fields,
        )
        with self.assertRaisesMessage(
            ValidationError,
            "resulting status event",
        ):
            Correspondence.objects.bulk_create(
                [bypass],
                update_conflicts=True,
                update_fields=["resulting_status_event"],
                unique_fields=["id"],
            )

        correspondence.refresh_from_db()
        self.assertIsNone(correspondence.resulting_status_event_id)

    def test_blank_acknowledgment_event_cannot_bind_as_estimate_effect(self):
        now = timezone.now()
        estimate = self.make_correspondence(
            kind=Correspondence.Kind.ESTIMATE_REVISION,
            status=Correspondence.Status.SENT,
            approved_subject="Estimate revision",
            approved_body="The revised estimate is August 31.",
            approved_metadata={
                "new_date": "2026-08-31",
                "reason": "An additional archive requires legal review.",
            },
            approved_by=self.actor,
            approved_at=now,
            queued_at=now,
            sent_at=now,
        )
        acknowledgment_event = StatusEvent.objects.create(
            request=self.prr,
            actor=self.actor,
            note="Statutory acknowledgment delivered.",
            visible_to_requester=True,
            occurred_at=now,
            correspondence_effect=(
                StatusEvent.CorrespondenceEffect.ACKNOWLEDGMENT_DELIVERY
            ),
        )

        with self.assertRaisesMessage(ValidationError, "does not match"):
            with transaction.atomic():
                Correspondence.objects.filter(
                    pk=estimate.pk
                )._bind_locked_resulting_status_event(
                    status_event=acknowledgment_event
                )

        estimate.refresh_from_db()
        self.assertIsNone(estimate.resulting_status_event_id)

    def test_one_status_event_cannot_result_from_two_correspondence_records(self):
        now = timezone.now()
        event = StatusEvent.objects.create(
            request=self.prr,
            actor=self.actor,
            from_status=RequestStatus.RECEIVED,
            to_status=RequestStatus.ACKNOWLEDGED,
            note="Statutory acknowledgment delivered.",
            visible_to_requester=True,
            occurred_at=now,
            correspondence_effect=(
                StatusEvent.CorrespondenceEffect.ACKNOWLEDGMENT_DELIVERY
            ),
        )
        rows = [
            self.make_correspondence(
                kind=Correspondence.Kind.ACKNOWLEDGMENT,
                status=Correspondence.Status.SENT,
                approved_subject=f"Acknowledgment {index}",
                approved_body="Approved acknowledgment body",
                approved_by=self.actor,
                approved_at=now,
                queued_at=now,
                sent_at=now,
            )
            for index in range(2)
        ]
        with transaction.atomic():
            Correspondence.objects.filter(
                pk=rows[0].pk
            )._bind_locked_resulting_status_event(status_event=event)

        with self.assertRaisesMessage(
            ValidationError,
            "already represents",
        ):
            with transaction.atomic():
                Correspondence.objects.filter(
                    pk=rows[1].pk
                )._bind_locked_resulting_status_event(status_event=event)

        rows[1].refresh_from_db()
        self.assertIsNone(rows[1].resulting_status_event_id)

    def test_failed_correspondence_can_return_to_queued_for_retry(self):
        now = timezone.now()
        instance = self.make_correspondence(
            status=Correspondence.Status.FAILED,
            approved_subject="Retry subject",
            approved_body="Retry body",
            approved_by=self.actor,
            approved_at=now,
            queued_at=now,
            failed_at=now,
        )

        instance.status = Correspondence.Status.QUEUED
        instance.failed_at = None
        instance.save(update_fields=["status", "failed_at"])

        instance.refresh_from_db()
        self.assertEqual(instance.status, Correspondence.Status.QUEUED)
        self.assertIsNone(instance.failed_at)

    def test_outbound_message_id_is_deterministic_and_stable(self):
        correspondence = self.make_correspondence()
        original = correspondence.rfc_message_id
        self.assertEqual(
            original,
            f"<{correspondence.id}.correspondence@prr.psd401.net>",
        )
        correspondence.draft_body = "Edited while still a draft"
        correspondence.save()
        correspondence.refresh_from_db()
        self.assertEqual(correspondence.rfc_message_id, original)

        supplied = self.make_correspondence(rfc_message_id="<caller-supplied>")
        self.assertEqual(
            supplied.rfc_message_id,
            f"<{supplied.id}.correspondence@prr.psd401.net>",
        )

    def test_message_id_domain_rejects_header_injection(self):
        """Two layers, because one of them is new.

        The Message-ID domain is an application-managed setting now, and its
        registry type is a single exact DNS domain -- so a value carrying CRLF
        cannot be saved, and a stored value that somehow is not a domain any
        more resolves to the safe default rather than being used. The model's
        own refusal is the second layer, and it is still the one that matters
        if a poisoned value ever reaches it, so it is asserted directly.
        """
        spec = SETTINGS_BY_KEY["PRR_CORRESPONDENCE_MESSAGE_ID_DOMAIN"]
        with self.assertRaises(ValueError):
            values.coerce(spec, "bad\r\nBcc: attacker")

        with mock.patch.object(
            runtime, "get", side_effect=lambda key: "bad\r\nBcc: attacker"
        ):
            with self.assertRaises(ValidationError):
                self.make_correspondence()

    def test_received_inbound_content_is_immutable(self):
        inbound = self.make_correspondence(
            direction=Correspondence.Direction.INBOUND,
            status=Correspondence.Status.RECEIVED,
            created_by=None,
            received_subject="Re: clarification",
            received_body="Here is the requested clarification.",
        )
        self.assertEqual(inbound.received_subject, "Re: clarification")
        inbound.received_body = "Rewritten inbound evidence"
        with self.assertRaises(ValidationError):
            inbound.save()

    def test_correction_is_a_new_explicit_replacement_record(self):
        original = self.make_correspondence(
            status=Correspondence.Status.APPROVED,
            approved_subject="Approved subject",
            approved_body="Approved body",
            approved_by=self.actor,
            approved_at=timezone.now(),
        )
        replacement = self.make_correspondence(replaces=original)
        self.assertEqual(replacement.replaces, original)
        self.assertSequenceEqual(list(original.replacements.all()), [replacement])

        replacement.replaces = replacement
        with self.assertRaises(IntegrityError), transaction.atomic():
            replacement.save()

    def test_version_must_be_positive(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_correspondence(version=0)


class RecipientSnapshotTests(CorrespondenceModelTestCase):
    def test_recipient_snapshot_is_unique_by_type_and_address(self):
        correspondence = self.make_correspondence()
        CorrespondenceRecipient.objects.create(
            correspondence=correspondence,
            recipient_type=CorrespondenceRecipient.RecipientType.TO,
            email="jane@example.org",
            display_name="Jane Requester",
            requester=self.prr.requester,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            CorrespondenceRecipient.objects.create(
                correspondence=correspondence,
                recipient_type=CorrespondenceRecipient.RecipientType.TO,
                email="jane@example.org",
            )

    def test_recipient_snapshot_cannot_change_or_be_deleted_after_approval(self):
        correspondence = self.make_correspondence()
        recipient = CorrespondenceRecipient.objects.create(
            correspondence=correspondence,
            recipient_type=CorrespondenceRecipient.RecipientType.TO,
            email="jane@example.org",
        )
        correspondence.status = Correspondence.Status.APPROVED
        correspondence.approved_subject = "Approved subject"
        correspondence.approved_body = "Approved body"
        correspondence.approved_by = self.actor
        correspondence.approved_at = timezone.now()
        correspondence.save()

        recipient.display_name = "Changed"
        with self.assertRaises(ValidationError):
            recipient.save()
        with self.assertRaises(ValidationError):
            recipient.delete()
        with self.assertRaises(ValidationError):
            CorrespondenceRecipient.objects.filter(pk=recipient.pk).update(
                display_name="Bulk rewrite"
            )
        with self.assertRaises(ValidationError):
            CorrespondenceRecipient.objects.filter(pk=recipient.pk).delete()
        with self.assertRaises(ValidationError):
            CorrespondenceRecipient.objects.create(
                correspondence=correspondence,
                recipient_type=CorrespondenceRecipient.RecipientType.CC,
                email="new-recipient@example.org",
            )
        recipient.display_name = "Bulk rewrite"
        with self.assertRaises(ValidationError):
            CorrespondenceRecipient.objects.bulk_update(
                [recipient],
                ["display_name"],
            )
        with self.assertRaises(ValidationError):
            CorrespondenceRecipient._base_manager.filter(
                pk=recipient.pk
            ).update(display_name="Base-manager rewrite")

    def test_recipient_snapshot_cannot_be_reassigned_away_from_approved_parent(self):
        approved = self.make_correspondence()
        recipient = CorrespondenceRecipient.objects.create(
            correspondence=approved,
            recipient_type=CorrespondenceRecipient.RecipientType.TO,
            email="jane@example.org",
        )
        approved.status = Correspondence.Status.APPROVED
        approved.approved_subject = "Approved subject"
        approved.approved_body = "Approved body"
        approved.approved_by = self.actor
        approved.approved_at = timezone.now()
        approved.save()
        other_draft = self.make_correspondence()

        recipient.correspondence = other_draft
        with self.assertRaisesMessage(
            ValidationError,
            "Recipient snapshots cannot be reassigned.",
        ):
            recipient.save()

        recipient.refresh_from_db()
        self.assertEqual(recipient.correspondence_id, approved.id)

    def test_recipient_snapshot_parent_is_immutable_even_while_draft(self):
        original_draft = self.make_correspondence()
        other_draft = self.make_correspondence()
        recipient = CorrespondenceRecipient.objects.create(
            correspondence=original_draft,
            recipient_type=CorrespondenceRecipient.RecipientType.TO,
            email="jane@example.org",
        )

        with self.assertRaisesMessage(
            ValidationError,
            "Recipient snapshots cannot be reassigned.",
        ):
            CorrespondenceRecipient.objects.filter(pk=recipient.pk).update(
                correspondence=other_draft
            )

        recipient.correspondence = other_draft
        with self.assertRaisesMessage(
            ValidationError,
            "Recipient snapshots cannot be reassigned.",
        ):
            CorrespondenceRecipient.objects.bulk_update(
                [recipient],
                ["correspondence"],
            )

        recipient.refresh_from_db()
        self.assertEqual(recipient.correspondence_id, original_draft.id)

    def test_bulk_create_cannot_extend_approved_or_queued_recipient_snapshot(self):
        for status in (
            Correspondence.Status.APPROVED,
            Correspondence.Status.QUEUED,
        ):
            with self.subTest(status=status):
                correspondence = self.make_correspondence()
                original = CorrespondenceRecipient.objects.create(
                    correspondence=correspondence,
                    recipient_type=CorrespondenceRecipient.RecipientType.TO,
                    email=f"{status}@example.org",
                    display_name="Approved Recipient",
                )
                correspondence.status = status
                correspondence.approved_subject = "Approved subject"
                correspondence.approved_body = "Approved body"
                correspondence.approved_by = self.actor
                correspondence.approved_at = timezone.now()
                if status == Correspondence.Status.QUEUED:
                    correspondence.queued_at = timezone.now()
                correspondence.save()

                with self.assertRaisesMessage(
                    ValidationError,
                    "Recipient snapshots are immutable after approval.",
                ):
                    CorrespondenceRecipient.objects.bulk_create(
                        [
                            CorrespondenceRecipient(
                                correspondence=correspondence,
                                recipient_type=(
                                    CorrespondenceRecipient.RecipientType.CC
                                ),
                                email=f"late-{status}@example.org",
                                display_name="Late Recipient",
                            )
                        ]
                    )

                self.assertSequenceEqual(
                    list(
                        correspondence.recipients.values_list(
                            "id",
                            "recipient_type",
                            "email",
                            "display_name",
                        )
                    ),
                    [
                        (
                            original.id,
                            CorrespondenceRecipient.RecipientType.TO,
                            f"{status}@example.org",
                            "Approved Recipient",
                        )
                    ],
                )


class AttachmentSnapshotTests(CorrespondenceModelTestCase):
    def make_attachment(self, correspondence):
        attachment = CorrespondenceAttachment.objects.create(
            correspondence=correspondence,
            original_filename="approved-record.pdf",
            content_type="application/pdf",
            size=123,
            sha256="a" * 64,
            uploaded_by=self.actor,
        )
        return CorrespondenceAttachment.objects.filter(
            pk=attachment.pk
        ).finalize_locked_scan(
            expected_storage_key=attachment.storage_key,
            scan_status=CorrespondenceAttachment.ScanStatus.CLEAN,
            scanned_at=timezone.now(),
            promoted_storage_key=(
                f"correspondence/attachments/{correspondence.pk}/"
                f"{attachment.pk}"
            ),
        )

    def approve(self, correspondence):
        correspondence.status = Correspondence.Status.APPROVED
        correspondence.approved_subject = "Approved subject"
        correspondence.approved_body = "Approved body"
        correspondence.approved_by = self.actor
        correspondence.approved_at = timezone.now()
        correspondence.save()

    def test_attachment_identity_is_frozen_at_approval(self):
        correspondence = self.make_correspondence()
        attachment = self.make_attachment(correspondence)
        self.approve(correspondence)

        attachment.original_filename = "rewritten.pdf"
        with self.assertRaises(ValidationError):
            attachment.save()
        with self.assertRaises(ValidationError):
            CorrespondenceAttachment.objects.filter(pk=attachment.pk).update(
                size=999
            )
        with self.assertRaises(ValidationError):
            attachment.delete()
        with self.assertRaises(ValidationError):
            CorrespondenceAttachment.objects.filter(pk=attachment.pk).delete()
        with self.assertRaises(ValidationError):
            self.make_attachment(correspondence)
        with self.assertRaises(ValidationError):
            CorrespondenceAttachment.objects.bulk_create(
                [
                    CorrespondenceAttachment(
                        correspondence=correspondence,
                        original_filename="late-bulk-attachment.pdf",
                        content_type="application/pdf",
                        size=123,
                        sha256="b" * 64,
                        uploaded_by=self.actor,
                    )
                ]
            )

    def test_finalized_attachment_blob_identity_is_frozen_while_draft(self):
        correspondence = self.make_correspondence()
        attachment = self.make_attachment(correspondence)
        original_identity = (
            attachment.storage_key,
            attachment.sha256,
            attachment.size,
        )

        instance_mutations = (
            ("storage_key", "correspondence/attachments/replaced"),
            ("sha256", "f" * 64),
            ("size", 999),
        )
        for field, value in instance_mutations:
            with self.subTest(path="instance", field=field):
                attachment.refresh_from_db()
                setattr(attachment, field, value)
                with self.assertRaisesMessage(
                    ValidationError,
                    "Finalized correspondence attachment blob identity is immutable.",
                ):
                    attachment.save(update_fields=[field])

        for field, value in instance_mutations:
            with self.subTest(path="queryset", field=field):
                with self.assertRaisesMessage(
                    ValidationError,
                    "Finalized correspondence attachment blob identity is immutable.",
                ):
                    CorrespondenceAttachment.objects.filter(
                        pk=attachment.pk
                    ).update(**{field: value})

        attachment.refresh_from_db()
        attachment.size = 999
        with self.assertRaisesMessage(
            ValidationError,
            "Finalized correspondence attachment blob identity is immutable.",
        ):
            CorrespondenceAttachment.objects.bulk_update(
                [attachment],
                fields=["size"],
            )

        attachment.refresh_from_db()
        self.assertEqual(
            (
                attachment.storage_key,
                attachment.sha256,
                attachment.size,
            ),
            original_identity,
        )

    def test_security_state_can_requarantine_an_approved_attachment(self):
        correspondence = self.make_correspondence()
        attachment = self.make_attachment(correspondence)
        self.approve(correspondence)

        attachment = CorrespondenceAttachment.objects.filter(
            pk=attachment.pk
        ).requarantine_locked(
            quarantine_reason="Clean release was revoked after security review.",
            quarantined_at=timezone.now(),
        )

        self.assertEqual(
            attachment.scan_status,
            CorrespondenceAttachment.ScanStatus.ERROR,
        )
        self.assertTrue(attachment.quarantined)
        self.assertFalse(attachment.is_downloadable)
        self.assertEqual(
            attachment.quarantine_reason,
            "Clean release was revoked after security review.",
        )
        with self.assertRaisesMessage(
            ValidationError,
            "explicitly requarantined attachment cannot be released",
        ):
            CorrespondenceAttachment.objects.filter(
                pk=attachment.pk
            ).finalize_locked_scan(
                expected_storage_key=attachment.storage_key,
                scan_status=CorrespondenceAttachment.ScanStatus.CLEAN,
                scanned_at=timezone.now(),
                promoted_storage_key=attachment.storage_key,
            )


class DeliveryInvariantTests(CorrespondenceModelTestCase):
    def make_delivery(self, **overrides):
        correspondence = overrides.pop("correspondence", None) or self.make_correspondence()
        defaults = {
            "correspondence": correspondence,
            "idempotency_key": f"delivery:{correspondence.id}",
        }
        defaults.update(overrides)
        return CorrespondenceDelivery.objects.create(**defaults)

    def test_only_one_delivery_job_exists_for_correspondence(self):
        correspondence = self.make_correspondence()
        self.make_delivery(correspondence=correspondence)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_delivery(
                correspondence=correspondence,
                idempotency_key="replacement-is-not-a-retry",
            )

    def test_normal_create_path_rejects_delivery_for_inbound_mail(self):
        inbound = self.make_correspondence(
            direction=Correspondence.Direction.INBOUND,
            status=Correspondence.Status.RECEIVED,
            created_by=None,
        )
        with self.assertRaises(ValidationError):
            self.make_delivery(correspondence=inbound)

    def test_sent_delivery_requires_completion_fields(self):
        correspondence = self.make_correspondence()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_delivery(
                correspondence=correspondence,
                status=CorrespondenceDelivery.Status.SENT,
            )

        sent = self.make_delivery(
            correspondence=correspondence,
            status=CorrespondenceDelivery.Status.SENT,
            provider_message_id="gmail-123",
            completed_at=timezone.now(),
            successful_attempt_number=1,
        )
        self.assertEqual(sent.status, CorrespondenceDelivery.Status.SENT)

    def test_provider_message_id_is_unique_within_provider(self):
        self.make_delivery(provider="gmail", provider_message_id="gmail-123")
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_delivery(
                provider="gmail",
                provider_message_id="gmail-123",
            )
        other_scope = self.make_delivery(
            provider="gmail",
            provider_account="secondary-mailbox",
            provider_message_id="gmail-123",
        )
        self.assertEqual(other_scope.provider_account, "secondary-mailbox")

    def test_claim_fields_are_complete_or_empty(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_delivery(
                status=CorrespondenceDelivery.Status.CLAIMED,
                claim_owner="automation-outbound",
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_delivery(claim_owner="automation-outbound")
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_delivery(claim_expires_at=timezone.now())

    def test_claim_fields_are_empty_outside_claimable_lease_states(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_delivery(
                status=CorrespondenceDelivery.Status.PENDING,
                claim_owner="automation-outbound",
                claim_expires_at=timezone.now() + dt.timedelta(minutes=5),
            )

        waiting = self.make_delivery(
            status=CorrespondenceDelivery.Status.AWAITING_RECONCILIATION,
        )
        leased = self.make_delivery(
            status=CorrespondenceDelivery.Status.AWAITING_RECONCILIATION,
            claim_owner="automation-outbound",
            claim_expires_at=timezone.now() + dt.timedelta(minutes=5),
        )
        self.assertEqual(waiting.claim_owner, "")
        self.assertEqual(leased.claim_owner, "automation-outbound")

    def test_delivery_status_must_be_a_declared_enum_value(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_delivery(status="transport_defined_later")

    def test_delivery_transport_identity_is_immutable(self):
        delivery = self.make_delivery()
        delivery.provider_account = "rewritten-account"
        with self.assertRaises(ValidationError):
            delivery.save(update_fields=["provider_account"])
        with self.assertRaises(ValidationError):
            CorrespondenceDelivery.objects.filter(pk=delivery.pk).update(
                provider="rewritten-provider"
            )
        with self.assertRaises(ValidationError):
            CorrespondenceDelivery._base_manager.filter(pk=delivery.pk).update(
                queued_at=timezone.now() + dt.timedelta(days=1)
            )
        replacement = CorrespondenceDelivery(
            id=delivery.id,
            correspondence=delivery.correspondence,
            provider="rewritten-provider",
            provider_account=delivery.provider_account,
            idempotency_key=delivery.idempotency_key,
            queued_at=delivery.queued_at,
        )
        with self.assertRaises(ValidationError):
            CorrespondenceDelivery.objects.bulk_create(
                [replacement],
                update_conflicts=True,
                update_fields=["provider"],
                unique_fields=["id"],
            )
        with self.assertRaises(ValidationError):
            delivery.delete()
        with self.assertRaises(ValidationError):
            CorrespondenceDelivery._base_manager.filter(pk=delivery.pk).delete()

    def test_attempt_numbers_are_unique_and_positive(self):
        delivery = self.make_delivery()
        CorrespondenceDeliveryAttempt.objects.create(
            delivery=delivery,
            attempt_number=1,
            claim_owner="automation-outbound",
            claimed_at=timezone.now(),
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            CorrespondenceDeliveryAttempt.objects.create(
                delivery=delivery,
                attempt_number=1,
                claim_owner="automation-outbound-2",
                claimed_at=timezone.now(),
            )

    def test_attempt_requires_owner_and_chronological_result(self):
        delivery = self.make_delivery()
        claimed_at = timezone.now()
        with self.assertRaises(IntegrityError), transaction.atomic():
            CorrespondenceDeliveryAttempt.objects.create(
                delivery=delivery,
                attempt_number=1,
                claim_owner="",
                claimed_at=claimed_at,
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            CorrespondenceDeliveryAttempt.objects.create(
                delivery=delivery,
                attempt_number=1,
                claim_owner="automation-outbound",
                claimed_at=claimed_at,
                result_at=claimed_at - dt.timedelta(seconds=1),
                result_category=(
                    CorrespondenceDeliveryAttempt.ResultCategory.UNKNOWN
                ),
                error_category="outcome_unknown",
                result_fingerprint="f" * 64,
            )

    def test_completed_attempt_is_immutable_and_cannot_be_deleted(self):
        delivery = self.make_delivery()
        attempt = CorrespondenceDeliveryAttempt.objects.create(
            delivery=delivery,
            attempt_number=1,
            claim_owner="automation-outbound",
            claimed_at=timezone.now(),
            result_at=timezone.now(),
            result_category=CorrespondenceDeliveryAttempt.ResultCategory.FAILED,
            error_category="provider_timeout",
            result_fingerprint="f" * 64,
        )
        attempt.error_category = "rewritten"
        with self.assertRaises(ValidationError):
            attempt.save()
        with self.assertRaises(ValidationError):
            attempt.delete()

    def test_attempt_created_at_is_immutable(self):
        delivery = self.make_delivery()
        attempt = CorrespondenceDeliveryAttempt.objects.create(
            delivery=delivery,
            attempt_number=1,
            claim_owner="automation-outbound",
            claimed_at=timezone.now(),
        )
        attempt.created_at -= dt.timedelta(days=1)
        with self.assertRaises(ValidationError):
            attempt.save(update_fields=["created_at"])

    def test_attempt_actor_is_protected_as_transport_evidence(self):
        delivery = self.make_delivery()
        CorrespondenceDeliveryAttempt.objects.create(
            delivery=delivery,
            attempt_number=1,
            claim_owner="automation-outbound",
            claimed_at=timezone.now(),
            actor=self.actor,
        )
        with self.assertRaises(ProtectedError):
            self.actor.delete()

    def test_conflict_upsert_cannot_rewrite_an_attempt(self):
        delivery = self.make_delivery()
        attempt = CorrespondenceDeliveryAttempt.objects.create(
            delivery=delivery,
            attempt_number=1,
            claim_owner="automation-outbound",
            claimed_at=timezone.now(),
        )
        replacement = CorrespondenceDeliveryAttempt(
            id=attempt.id,
            delivery=delivery,
            attempt_number=1,
            claim_owner="rewritten-owner",
            claimed_at=attempt.claimed_at,
        )
        with self.assertRaises(ValidationError):
            CorrespondenceDeliveryAttempt.objects.bulk_create(
                [replacement],
                update_conflicts=True,
                update_fields=["claim_owner"],
                unique_fields=["id"],
            )

    def test_base_manager_cannot_rewrite_or_delete_an_attempt(self):
        delivery = self.make_delivery()
        attempt = CorrespondenceDeliveryAttempt.objects.create(
            delivery=delivery,
            attempt_number=1,
            claim_owner="automation-outbound",
            claimed_at=timezone.now(),
        )
        with self.assertRaises(ValidationError):
            CorrespondenceDeliveryAttempt._base_manager.filter(
                pk=attempt.pk
            ).update(claim_owner="rewritten-owner")
        with self.assertRaises(ValidationError):
            CorrespondenceDeliveryAttempt._base_manager.filter(
                pk=attempt.pk
            ).delete()

    def test_attempt_action_must_be_a_declared_enum_value(self):
        delivery = self.make_delivery()
        with self.assertRaises(IntegrityError), transaction.atomic():
            CorrespondenceDeliveryAttempt.objects.create(
                delivery=delivery,
                attempt_number=1,
                action="provider_defined_later",
                claim_owner="automation-outbound",
                claimed_at=timezone.now(),
            )

    def test_completed_result_category_must_be_a_declared_enum_value(self):
        delivery = self.make_delivery()
        with self.assertRaises(IntegrityError), transaction.atomic():
            CorrespondenceDeliveryAttempt.objects.create(
                delivery=delivery,
                attempt_number=1,
                claim_owner="automation-outbound",
                claimed_at=timezone.now(),
                result_at=timezone.now(),
                result_category="provider_defined_later",
                result_fingerprint="f" * 64,
            )

    def test_completed_result_fingerprint_is_exact_lowercase_sha256_hex(self):
        for invalid_fingerprint in ("f" * 63, "F" * 64, "x" * 64):
            with self.subTest(invalid_fingerprint=invalid_fingerprint):
                delivery = self.make_delivery()
                with self.assertRaises(IntegrityError), transaction.atomic():
                    CorrespondenceDeliveryAttempt.objects.create(
                        delivery=delivery,
                        attempt_number=1,
                        claim_owner="automation-outbound",
                        claimed_at=timezone.now(),
                        result_at=timezone.now(),
                        result_category=(
                            CorrespondenceDeliveryAttempt.ResultCategory.FAILED
                        ),
                        error_category="message_rejected",
                        result_fingerprint=invalid_fingerprint,
                    )

    def test_successful_attempt_requires_the_provider_timestamp(self):
        delivery = self.make_delivery()
        with self.assertRaises(IntegrityError), transaction.atomic():
            CorrespondenceDeliveryAttempt.objects.create(
                delivery=delivery,
                attempt_number=1,
                claim_owner="automation-outbound",
                claimed_at=timezone.now(),
                result_at=timezone.now(),
                result_category=CorrespondenceDeliveryAttempt.ResultCategory.SENT,
                provider_message_id="gmail-message",
                provider_thread_id="gmail-thread",
                result_fingerprint="f" * 64,
            )

    def test_successful_attempt_requires_the_provider_thread(self):
        delivery = self.make_delivery()
        now = timezone.now()
        with self.assertRaises(IntegrityError), transaction.atomic():
            CorrespondenceDeliveryAttempt.objects.create(
                delivery=delivery,
                attempt_number=1,
                claim_owner="automation-outbound",
                claimed_at=now,
                result_at=now,
                result_category=CorrespondenceDeliveryAttempt.ResultCategory.SENT,
                provider_message_id="gmail-message",
                provider_timestamp=now,
                result_fingerprint="f" * 64,
            )

    def test_non_successful_attempt_rejects_provider_success_evidence(self):
        delivery = self.make_delivery()
        now = timezone.now()
        with self.assertRaises(IntegrityError), transaction.atomic():
            CorrespondenceDeliveryAttempt.objects.create(
                delivery=delivery,
                attempt_number=1,
                claim_owner="automation-outbound",
                claimed_at=now,
                result_at=now,
                result_category=CorrespondenceDeliveryAttempt.ResultCategory.FAILED,
                provider_message_id="gmail-message",
                provider_thread_id="gmail-thread",
                provider_timestamp=now,
                error_category="message_rejected",
                result_fingerprint="f" * 64,
            )

    def test_only_one_successful_attempt_may_exist_per_delivery(self):
        delivery = self.make_delivery()
        now = timezone.now()
        CorrespondenceDeliveryAttempt.objects.create(
            delivery=delivery,
            attempt_number=1,
            action=CorrespondenceDeliveryAttempt.Action.SEND,
            claim_owner="automation-outbound",
            claimed_at=now,
            result_at=now,
            result_category=CorrespondenceDeliveryAttempt.ResultCategory.SENT,
            provider_message_id="gmail-message-1",
            provider_thread_id="gmail-thread-1",
            provider_timestamp=now,
            result_fingerprint="a" * 64,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            CorrespondenceDeliveryAttempt.objects.create(
                delivery=delivery,
                attempt_number=2,
                action=CorrespondenceDeliveryAttempt.Action.RECONCILE,
                claim_owner="automation-outbound",
                claimed_at=now,
                result_at=now,
                result_category=(
                    CorrespondenceDeliveryAttempt.ResultCategory.RECONCILED
                ),
                provider_message_id="gmail-message-2",
                provider_thread_id="gmail-thread-2",
                provider_timestamp=now,
                result_fingerprint="b" * 64,
            )

    def test_only_one_unfinished_attempt_may_exist_per_delivery(self):
        delivery = self.make_delivery()
        CorrespondenceDeliveryAttempt.objects.create(
            delivery=delivery,
            attempt_number=1,
            claim_owner="automation-outbound",
            claimed_at=timezone.now(),
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            CorrespondenceDeliveryAttempt.objects.create(
                delivery=delivery,
                attempt_number=2,
                claim_owner="automation-outbound",
                claimed_at=timezone.now(),
            )

    def test_finalized_attempt_requires_a_result_fingerprint(self):
        delivery = self.make_delivery()
        with self.assertRaises(IntegrityError), transaction.atomic():
            CorrespondenceDeliveryAttempt.objects.create(
                delivery=delivery,
                attempt_number=1,
                claim_owner="automation-outbound",
                claimed_at=timezone.now(),
                result_at=timezone.now(),
                result_category=CorrespondenceDeliveryAttempt.ResultCategory.FAILED,
                error_category="provider_unavailable",
            )

    def test_reconciliation_results_require_a_reconciliation_action(self):
        delivery = self.make_delivery()
        with self.assertRaises(IntegrityError), transaction.atomic():
            CorrespondenceDeliveryAttempt.objects.create(
                delivery=delivery,
                attempt_number=1,
                action=CorrespondenceDeliveryAttempt.Action.SEND,
                claim_owner="automation-outbound",
                claimed_at=timezone.now(),
                result_at=timezone.now(),
                result_category=CorrespondenceDeliveryAttempt.ResultCategory.NOT_FOUND,
                error_category="reconciliation_not_found",
                result_fingerprint="f" * 64,
            )


class InboundReceiptInvariantTests(CorrespondenceModelTestCase):
    def make_receipt(self, **overrides):
        provider_account = overrides.get("provider_account", "default")
        defaults = {
            "provider": "gmail",
            "provider_account": provider_account,
            "provider_message_id": "gmail-inbound-1",
            "received_at": timezone.now(),
            "envelope_sender": "jane@example.org",
            "raw_message_size": 123,
            "raw_message_sha256": "a" * 64,
            "payload_sha256": "b" * 64,
            "idempotency_key": f"gmail:{provider_account}:inbound-1",
            "submitted_by": self.actor,
        }
        defaults.update(overrides)
        return InboundCorrespondenceReceipt.objects.create(**defaults)

    def test_provider_receipt_is_idempotent_within_integration_scope(self):
        self.make_receipt()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_receipt()
        other_scope = self.make_receipt(
            provider_account="secondary-mailbox",
        )
        self.assertEqual(other_scope.provider_account, "secondary-mailbox")

    def test_matched_receipt_requires_request_correspondence_and_method(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_receipt(
                matching_status=InboundCorrespondenceReceipt.MatchingStatus.MATCHED,
                matched_request=self.prr,
            )

    def test_raw_message_uses_an_application_generated_quarantine_key(self):
        receipt = self.make_receipt(raw_storage_key="caller/provided/path")
        self.assertTrue(
            receipt.raw_storage_key.startswith("quarantine/correspondence/raw/")
        )
        self.assertNotEqual(receipt.raw_storage_key, "caller/provided/path")


class AttachmentInvariantTests(CorrespondenceModelTestCase):
    def make_attachment(self):
        return CorrespondenceAttachment.objects.create(
            correspondence=self.make_correspondence(),
            original_filename="records.pdf",
            content_type="application/pdf",
            size=100,
            sha256="b" * 64,
            uploaded_by=self.actor,
        )

    def test_filename_is_sanitized_and_storage_key_is_generated(self):
        correspondence = self.make_correspondence()
        attachment = CorrespondenceAttachment.objects.create(
            correspondence=correspondence,
            original_filename="bad\x00name\n.pdf",
            content_type="application/pdf",
            size=100,
            sha256="b" * 64,
            uploaded_by=self.actor,
        )
        self.assertEqual(attachment.original_filename, "badname.pdf")
        self.assertTrue(
            attachment.storage_key.startswith("quarantine/correspondence/attachments/")
        )

        windows_path = CorrespondenceAttachment.objects.create(
            correspondence=correspondence,
            original_filename=r"C:\Users\requester\evidence.pdf",
            content_type="application/pdf",
            size=100,
            sha256="c" * 64,
        )
        self.assertEqual(windows_path.original_filename, "evidence.pdf")

        for index, unicode_path in enumerate(
            (
                "..／folder／normalized.pdf",
                "..＼folder＼normalized.pdf",
            ),
            start=1,
        ):
            with self.subTest(unicode_path=unicode_path):
                normalized_path = CorrespondenceAttachment.objects.create(
                    correspondence=correspondence,
                    original_filename=unicode_path,
                    content_type="application/pdf",
                    size=100,
                    sha256=f"{index}" * 64,
                )
                self.assertEqual(
                    normalized_path.original_filename,
                    "normalized.pdf",
                )
                self.assertNotIn("/", normalized_path.original_filename)
                self.assertNotIn("\\", normalized_path.original_filename)

        dot_path = CorrespondenceAttachment.objects.create(
            correspondence=correspondence,
            original_filename="..",
            content_type="application/pdf",
            size=100,
            sha256="d" * 64,
        )
        self.assertEqual(dot_path.original_filename, "attachment")

    def test_only_clean_nonquarantined_attachment_is_downloadable(self):
        attachment = self.make_attachment()
        self.assertFalse(attachment.is_downloadable)

        attachment = CorrespondenceAttachment.objects.filter(
            pk=attachment.pk
        ).finalize_locked_scan(
            expected_storage_key=attachment.storage_key,
            scan_status=CorrespondenceAttachment.ScanStatus.CLEAN,
            scanned_at=timezone.now(),
            promoted_storage_key=(
                f"correspondence/attachments/"
                f"{attachment.correspondence_id}/{attachment.pk}"
            ),
        )

        self.assertTrue(attachment.is_downloadable)

    def test_scan_evidence_cannot_be_forged_through_ordinary_model_paths(self):
        correspondence = self.make_correspondence()
        clean_values = {
            "scan_status": CorrespondenceAttachment.ScanStatus.CLEAN,
            "quarantined": False,
            "quarantine_reason": "",
            "scanned_at": timezone.now(),
        }
        create_values = {
            "correspondence": correspondence,
            "original_filename": "forged.pdf",
            "content_type": "application/pdf",
            "size": 100,
            "sha256": "c" * 64,
            "uploaded_by": self.actor,
            **clean_values,
        }

        with self.assertRaises(ValidationError):
            CorrespondenceAttachment.objects.create(**create_values)
        with self.assertRaises(ValidationError):
            CorrespondenceAttachment.objects.bulk_create(
                [CorrespondenceAttachment(**create_values)]
            )

        attachment = self.make_attachment()
        with self.assertRaises(ValidationError):
            CorrespondenceAttachment.objects.filter(pk=attachment.pk).update(
                **clean_values
            )
        base_manager_clean_values = {
            **clean_values,
            "scanned_at": timezone.now(),
        }
        with self.assertRaises(ValidationError):
            CorrespondenceAttachment._base_manager.filter(
                pk=attachment.pk
            ).update(**base_manager_clean_values)

        for field, value in clean_values.items():
            setattr(attachment, field, value)
        with self.assertRaises(ValidationError):
            attachment.save(
                update_fields=[
                    "scan_status",
                    "quarantined",
                    "quarantine_reason",
                    "scanned_at",
                ]
            )

        attachment.refresh_from_db()
        for field, value in clean_values.items():
            setattr(attachment, field, value)
        with self.assertRaises(ValidationError):
            CorrespondenceAttachment.objects.bulk_update(
                [attachment],
                fields=[
                    "scan_status",
                    "quarantined",
                    "quarantine_reason",
                    "scanned_at",
                ],
            )

        attachment.refresh_from_db()
        self.assertEqual(
            attachment.scan_status,
            CorrespondenceAttachment.ScanStatus.PENDING,
        )
        self.assertTrue(attachment.quarantined)
        self.assertEqual(attachment.quarantine_reason, "")
        self.assertIsNone(attachment.scanned_at)

    def test_database_rejects_inexact_scan_state_and_timestamp_combinations(self):
        invalid_states = (
            {
                "scan_status": CorrespondenceAttachment.ScanStatus.CLEAN,
                "quarantined": False,
                "quarantine_reason": "",
                "scanned_at": None,
            },
            {
                "scan_status": CorrespondenceAttachment.ScanStatus.ERROR,
                "quarantined": True,
                "quarantine_reason": "",
                "scanned_at": timezone.now(),
            },
            {
                "scan_status": CorrespondenceAttachment.ScanStatus.PENDING,
                "quarantined": True,
                "quarantine_reason": "",
                "scanned_at": timezone.now(),
            },
            {
                "scan_status": CorrespondenceAttachment.ScanStatus.INFECTED,
                "quarantined": False,
                "quarantine_reason": "Malware detected.",
                "scanned_at": timezone.now(),
            },
        )

        for invalid_state in invalid_states:
            attachment = self.make_attachment()
            queryset = CorrespondenceAttachment.objects.filter(pk=attachment.pk)
            with self.subTest(invalid_state=invalid_state):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    models.QuerySet.update(queryset, **invalid_state)

    def test_database_rejects_scan_timestamp_before_attachment_creation(self):
        attachment = self.make_attachment()
        queryset = CorrespondenceAttachment.objects.filter(pk=attachment.pk)

        with self.assertRaises(IntegrityError), transaction.atomic():
            models.QuerySet.update(
                queryset,
                scan_status=CorrespondenceAttachment.ScanStatus.ERROR,
                quarantined=True,
                quarantine_reason="Malware scanning is temporarily unavailable.",
                scanned_at=attachment.created_at - dt.timedelta(seconds=1),
            )

    def test_unscanned_attachment_cannot_be_removed_from_quarantine(self):
        correspondence = self.make_correspondence()
        with self.assertRaises(ValidationError):
            CorrespondenceAttachment.objects.create(
                correspondence=correspondence,
                original_filename="records.pdf",
                content_type="application/pdf",
                size=100,
                sha256="b" * 64,
                quarantined=False,
            )


class ClarificationCycleInvariantTests(CorrespondenceModelTestCase):
    def make_outbound_clarification(self):
        return self.make_correspondence(
            kind=Correspondence.Kind.CLARIFICATION_REQUEST,
        )

    def test_only_one_open_clarification_cycle_exists_per_request(self):
        outbound = self.make_outbound_clarification()
        ClarificationCycle.objects.create(
            request=self.prr,
            outbound_correspondence=outbound,
            opened_at=timezone.now(),
        )
        other_outbound = self.make_outbound_clarification()
        with self.assertRaises(IntegrityError), transaction.atomic():
            ClarificationCycle.objects.create(
                request=self.prr,
                outbound_correspondence=other_outbound,
                opened_at=timezone.now(),
            )

    def test_resolution_requires_inbound_response_human_and_notes(self):
        outbound = self.make_outbound_clarification()
        cycle = ClarificationCycle(
            request=self.prr,
            outbound_correspondence=outbound,
            opened_at=timezone.now(),
            resolved_at=timezone.now(),
        )
        with self.assertRaises(ValidationError):
            cycle.full_clean()

    def test_normal_create_path_rejects_non_clarification_correspondence(self):
        general = self.make_correspondence(kind=Correspondence.Kind.GENERAL)
        with self.assertRaises(ValidationError):
            ClarificationCycle.objects.create(
                request=self.prr,
                outbound_correspondence=general,
                opened_at=timezone.now(),
            )

    def test_inbound_response_does_not_resolve_cycle(self):
        outbound = self.make_outbound_clarification()
        inbound = self.make_correspondence(
            direction=Correspondence.Direction.INBOUND,
            kind=Correspondence.Kind.CLARIFICATION_RESPONSE,
            status=Correspondence.Status.RECEIVED,
            created_by=None,
            received_at=timezone.now(),
        )
        cycle = ClarificationCycle.objects.create(
            request=self.prr,
            outbound_correspondence=outbound,
            inbound_response_correspondence=inbound,
            opened_at=timezone.now() - dt.timedelta(days=1),
            responded_at=timezone.now(),
        )
        self.assertIsNone(cycle.resolved_at)
        self.assertIsNone(cycle.resolved_by_id)

    def test_response_timestamp_cannot_precede_cycle_opening(self):
        outbound = self.make_outbound_clarification()
        opened_at = timezone.now()
        inbound = self.make_correspondence(
            direction=Correspondence.Direction.INBOUND,
            kind=Correspondence.Kind.CLARIFICATION_RESPONSE,
            status=Correspondence.Status.RECEIVED,
            created_by=None,
            received_at=opened_at,
        )
        cycle = ClarificationCycle.objects.create(
            request=self.prr,
            outbound_correspondence=outbound,
            inbound_response_correspondence=inbound,
            opened_at=opened_at,
            responded_at=opened_at,
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            ClarificationCycle.objects.filter(pk=cycle.pk).update(
                responded_at=opened_at - dt.timedelta(seconds=1)
            )

    def test_resolution_timestamp_cannot_precede_response(self):
        outbound = self.make_outbound_clarification()
        opened_at = timezone.now() - dt.timedelta(minutes=2)
        responded_at = opened_at + dt.timedelta(minutes=1)
        inbound = self.make_correspondence(
            direction=Correspondence.Direction.INBOUND,
            kind=Correspondence.Kind.CLARIFICATION_RESPONSE,
            status=Correspondence.Status.RECEIVED,
            created_by=None,
            received_at=responded_at,
        )
        cycle = ClarificationCycle.objects.create(
            request=self.prr,
            outbound_correspondence=outbound,
            inbound_response_correspondence=inbound,
            opened_at=opened_at,
            responded_at=responded_at,
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            ClarificationCycle.objects.filter(pk=cycle.pk).update(
                resolved_at=responded_at - dt.timedelta(seconds=1),
                resolved_by=self.actor,
                resolution_notes="Substantive resolution chronology test.",
            )

    def test_cancellation_timestamp_cannot_precede_cycle_opening(self):
        outbound = self.make_outbound_clarification()
        opened_at = timezone.now()
        cycle = ClarificationCycle.objects.create(
            request=self.prr,
            outbound_correspondence=outbound,
            opened_at=opened_at,
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            ClarificationCycle.objects.filter(pk=cycle.pk).update(
                cancelled_at=opened_at - dt.timedelta(seconds=1)
            )


class IntegrationPermissionTests(TestCase):
    def test_outbound_and_inbound_transports_have_separate_scopes(self):
        outbound = Permission.objects.get(
            content_type__app_label="correspondence",
            codename="deliver_outbound_correspondence",
        )
        inbound = Permission.objects.get(
            content_type__app_label="correspondence",
            codename="submit_inbound_correspondence",
        )
        self.assertNotEqual(outbound.pk, inbound.pk)
