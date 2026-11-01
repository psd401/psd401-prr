import datetime as dt
import uuid

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from apps.correspondence.models import (
    Correspondence,
    CorrespondenceAttachment,
    InboundCorrespondenceReceipt,
)
from apps.records.models import PublicRecordsRequest, Requester


class InboundModelContractTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.integration = User.objects.create_user(
            "automation-correspondence-inbound",
            password=None,
        )
        self.other_integration = User.objects.create_user(
            "automation-correspondence-inbound-secondary",
            password=None,
        )
        self.officer = User.objects.create_user(
            "inbound-model-officer",
            "inbound-model-officer@psd401.net",
            "pw",
            is_staff=True,
        )
        self.prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="requester@example.org"),
            description="Inbound correspondence model contract.",
        )
        self._sequence = 0

    def make_inbound_correspondence(self, *, request=None, status=None):
        self._sequence += 1
        matched_at = (
            timezone.now()
            if status == Correspondence.Status.RECEIVED
            else None
        )
        return Correspondence.objects.create(
            request=request,
            direction=Correspondence.Direction.INBOUND,
            kind=Correspondence.Kind.GENERAL,
            status=status or Correspondence.Status.UNMATCHED,
            received_subject="Re: public records request",
            received_body="The requested clarification is attached.",
            received_at=timezone.now(),
            matched_at=matched_at,
            matched_by=self.officer if matched_at else None,
            provider="gmail",
            provider_account="records-inbox",
            provider_message_id=f"inbound-message-{self._sequence}",
        )

    def receipt_values(self, **overrides):
        self._sequence += 1
        defaults = {
            "provider": "gmail",
            "provider_account": "records-inbox",
            "provider_message_id": f"gmail-receipt-{self._sequence}",
            "received_at": timezone.now(),
            "envelope_sender": "requester@example.org",
            "envelope_recipients": ["records@psd401.net"],
            "parsing_status": InboundCorrespondenceReceipt.ParsingStatus.PARSED,
            "matching_status": (
                InboundCorrespondenceReceipt.MatchingStatus.UNMATCHED
            ),
            "quarantined": False,
            "raw_message_size": 512,
            "raw_message_sha256": f"{self._sequence:064x}",
            "submitted_by": self.integration,
            "idempotency_key": f"gmail:receipt:{self._sequence}",
            "payload_sha256": f"{self._sequence + 1000:064x}",
            "ip_address": "127.0.0.1",
            "completed_at": timezone.now(),
        }
        defaults.update(overrides)
        if (
            "matched_correspondence" not in overrides
            and defaults["parsing_status"]
            == InboundCorrespondenceReceipt.ParsingStatus.PARSED
        ):
            defaults["matched_correspondence"] = (
                self.make_inbound_correspondence()
            )
        return defaults

    def make_receipt(self, **overrides):
        return InboundCorrespondenceReceipt.objects.create(
            **self.receipt_values(**overrides)
        )

    def make_matched_receipt(self):
        correspondence = self.make_inbound_correspondence(
            request=self.prr,
            status=Correspondence.Status.RECEIVED,
        )
        receipt = self.make_receipt(
            matching_status=InboundCorrespondenceReceipt.MatchingStatus.MATCHED,
            match_method=InboundCorrespondenceReceipt.MatchMethod.MANUAL,
            matched_request=self.prr,
            matched_correspondence=correspondence,
            matched_at=correspondence.matched_at,
            matched_by=self.officer,
        )
        return receipt, correspondence

    def test_receipt_idempotency_is_scoped_to_the_immutable_inbound_identity(self):
        values = self.receipt_values(
            idempotency_key="gmail:stable-delivery-1",
            provider_message_id="gmail-provider-message-1",
        )
        first = InboundCorrespondenceReceipt.objects.create(**values)

        self.assertEqual(first.submitted_by, self.integration)
        self.assertEqual(first.idempotency_key, "gmail:stable-delivery-1")
        self.assertEqual(first.payload_sha256, values["payload_sha256"])
        self.assertEqual(first.version, 1)

        conflicting_values = self.receipt_values(
            idempotency_key=first.idempotency_key,
            provider_message_id="gmail-provider-message-2",
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            InboundCorrespondenceReceipt.objects.create(**conflicting_values)

        other_identity = self.make_receipt(
            submitted_by=self.other_integration,
            idempotency_key=first.idempotency_key,
            provider_message_id="gmail-provider-message-3",
        )
        self.assertEqual(other_identity.idempotency_key, first.idempotency_key)

    def test_receipt_version_must_be_positive(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_receipt(version=0)

    def test_raw_transport_identity_and_evidence_are_immutable_and_undeletable(self):
        receipt = self.make_receipt(raw_storage_key="caller/chosen/raw-message")
        generated_key = receipt.raw_storage_key
        self.assertTrue(
            generated_key.startswith("quarantine/correspondence/raw/")
        )
        self.assertNotEqual(generated_key, "caller/chosen/raw-message")

        receipt.provider_message_id = "rewritten-provider-message"
        receipt.raw_message_size = 999
        receipt.raw_message_sha256 = "f" * 64
        receipt.raw_storage_key = "rewritten/storage/key"
        receipt.payload_sha256 = "e" * 64
        with self.assertRaises(ValidationError):
            receipt.save()

        with self.assertRaises(ValidationError):
            InboundCorrespondenceReceipt.objects.filter(pk=receipt.pk).update(
                raw_message_sha256="d" * 64
            )
        with self.assertRaises(ValidationError):
            receipt.delete()
        with self.assertRaises(ValidationError):
            InboundCorrespondenceReceipt.objects.filter(pk=receipt.pk).delete()

    def test_parsed_correspondence_provider_identity_is_immutable(self):
        correspondence = self.make_inbound_correspondence()
        original_message_id = correspondence.provider_message_id
        original_thread_id = correspondence.provider_thread_id

        correspondence.provider_message_id = "rewritten-provider-message"
        correspondence.provider_thread_id = "rewritten-provider-thread"
        with self.assertRaises(ValidationError):
            correspondence.save(
                update_fields=[
                    "provider_message_id",
                    "provider_thread_id",
                ]
            )

        with self.assertRaises(ValidationError):
            Correspondence.objects.filter(pk=correspondence.pk).update(
                provider_message_id="bulk-rewritten-provider-message",
                provider_thread_id="bulk-rewritten-provider-thread",
            )

        correspondence.refresh_from_db()
        self.assertEqual(correspondence.provider_message_id, original_message_id)
        self.assertEqual(correspondence.provider_thread_id, original_thread_id)

    def test_inbound_received_and_creation_timestamps_are_immutable(self):
        correspondence = self.make_inbound_correspondence()
        original_received_at = correspondence.received_at
        original_created_at = correspondence.created_at

        correspondence.received_at += dt.timedelta(minutes=1)
        correspondence.created_at += dt.timedelta(minutes=1)
        with self.assertRaises(ValidationError):
            correspondence.save(
                update_fields=["received_at", "created_at"],
            )

        correspondence.refresh_from_db()
        with self.assertRaises(ValidationError):
            Correspondence.objects.filter(pk=correspondence.pk).update(
                received_at=correspondence.received_at + dt.timedelta(minutes=2),
                created_at=correspondence.created_at + dt.timedelta(minutes=2),
            )
        with self.assertRaises(ValidationError):
            Correspondence._base_manager.filter(pk=correspondence.pk).update(
                received_at=correspondence.received_at + dt.timedelta(minutes=2),
                created_at=correspondence.created_at + dt.timedelta(minutes=2),
            )

        correspondence.received_at += dt.timedelta(minutes=3)
        correspondence.created_at += dt.timedelta(minutes=3)
        with transaction.atomic():
            with self.assertRaises(ValidationError):
                Correspondence.objects.bulk_update(
                    [correspondence],
                    fields=["received_at", "created_at"],
                )

        correspondence.refresh_from_db()
        self.assertEqual(correspondence.received_at, original_received_at)
        self.assertEqual(correspondence.created_at, original_created_at)

    def test_inbound_correspondence_requires_a_received_timestamp_at_database(self):
        correspondence = Correspondence(
            request=None,
            direction=Correspondence.Direction.INBOUND,
            kind=Correspondence.Kind.GENERAL,
            status=Correspondence.Status.UNMATCHED,
            received_subject="Subject",
            received_body="Body",
            provider="gmail",
            provider_account="records-inbox",
            provider_message_id="missing-received-at",
            received_at=None,
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            Correspondence.objects.bulk_create([correspondence])

    def test_raw_storage_keys_are_generated_unique_and_bulk_immutable(self):
        first = self.make_receipt()
        second = self.make_receipt()

        self.assertNotEqual(first.raw_storage_key, second.raw_storage_key)
        self.assertTrue(
            InboundCorrespondenceReceipt._meta.get_field(
                "raw_storage_key"
            ).unique
        )
        with self.assertRaises(ValidationError):
            InboundCorrespondenceReceipt.objects.filter(pk=second.pk).update(
                raw_storage_key=first.raw_storage_key
            )

    def test_network_and_completion_evidence_are_immutable_once_recorded(self):
        receipt = self.make_receipt()
        receipt.completed_at += dt.timedelta(seconds=1)
        with self.assertRaises(ValidationError):
            receipt.save(update_fields=["completed_at"])

        receipt.refresh_from_db()
        receipt.ip_address = "127.0.0.2"
        with self.assertRaises(ValidationError):
            receipt.save(update_fields=["ip_address"])

        with self.assertRaises(ValidationError):
            InboundCorrespondenceReceipt.objects.filter(pk=receipt.pk).update(
                completed_at=timezone.now()
            )

    def test_pending_receipt_parsing_is_finalized_once_under_lock(self):
        correspondence = self.make_inbound_correspondence()
        receipt = self.make_receipt(
            parsing_status=InboundCorrespondenceReceipt.ParsingStatus.PENDING,
            quarantined=True,
            completed_at=None,
        )
        completed_at = timezone.now()

        finalized = InboundCorrespondenceReceipt.objects.filter(
            pk=receipt.pk
        ).finalize_locked_parsing(
            parsing_status=(
                InboundCorrespondenceReceipt.ParsingStatus.PARSED
            ),
            completed_at=completed_at,
            matched_correspondence_id=correspondence.pk,
            rfc_message_id="<requester-message@example.org>",
            in_reply_to="<district-message@prr.psd401.net>",
            references=["<district-message@prr.psd401.net>"],
        )

        self.assertEqual(
            finalized.parsing_status,
            InboundCorrespondenceReceipt.ParsingStatus.PARSED,
        )
        self.assertEqual(
            finalized.matching_status,
            InboundCorrespondenceReceipt.MatchingStatus.UNMATCHED,
        )
        self.assertEqual(finalized.matched_correspondence, correspondence)
        self.assertEqual(finalized.completed_at, completed_at)
        self.assertFalse(finalized.quarantined)
        self.assertEqual(finalized.version, receipt.version + 1)

        with self.assertRaises(ValidationError):
            InboundCorrespondenceReceipt.objects.filter(
                pk=receipt.pk
            ).finalize_locked_parsing(
                parsing_status=(
                    InboundCorrespondenceReceipt.ParsingStatus.PARSED
                ),
                completed_at=timezone.now(),
                matched_correspondence_id=correspondence.pk,
            )

    def test_failed_parsing_finalization_stays_quarantined_without_content(self):
        receipt = self.make_receipt(
            parsing_status=InboundCorrespondenceReceipt.ParsingStatus.PENDING,
            quarantined=True,
            completed_at=None,
        )

        finalized = InboundCorrespondenceReceipt.objects.filter(
            pk=receipt.pk
        ).finalize_locked_parsing(
            parsing_status=(
                InboundCorrespondenceReceipt.ParsingStatus.QUARANTINED
            ),
            completed_at=timezone.now(),
            quarantine_reason="The message failed the safe MIME contract.",
            parsing_error="Inbound MIME validation failed.",
        )

        self.assertEqual(
            finalized.matching_status,
            InboundCorrespondenceReceipt.MatchingStatus.QUARANTINED,
        )
        self.assertTrue(finalized.quarantined)
        self.assertIsNone(finalized.matched_correspondence_id)
        self.assertEqual(finalized.references, [])

    def test_parsed_inbound_header_snapshot_is_immutable(self):
        correspondence = self.make_inbound_correspondence()
        correspondence.received_sender = "Jane Requester <jane@example.org>"
        correspondence.received_recipients = ["records@psd401.net"]
        correspondence.received_headers = {
            "From": "Jane Requester <jane@example.org>",
            "To": "records@psd401.net",
        }
        with self.assertRaises(ValidationError):
            correspondence.save()

        updated = Correspondence.objects.filter(pk=correspondence.pk)
        with self.assertRaises(ValidationError):
            updated.update(received_headers={"From": "rewritten@example.org"})

    def test_matching_association_rejects_unlocked_bulk_rewrites(self):
        receipt = self.make_receipt()

        with self.assertRaises(ValidationError):
            InboundCorrespondenceReceipt.objects.filter(pk=receipt.pk).update(
                matching_status=(
                    InboundCorrespondenceReceipt.MatchingStatus.AMBIGUOUS
                )
            )

    def test_candidate_duplicate_and_matched_states_require_exact_evidence(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_receipt(
                matching_status=(
                    InboundCorrespondenceReceipt.MatchingStatus.CANDIDATE
                ),
                match_method=InboundCorrespondenceReceipt.MatchMethod.SUBJECT,
            )

        candidate = self.make_receipt(
            matching_status=(
                InboundCorrespondenceReceipt.MatchingStatus.CANDIDATE
            ),
            match_method=InboundCorrespondenceReceipt.MatchMethod.SUBJECT,
            candidate_request=self.prr,
        )
        self.assertEqual(candidate.candidate_request, self.prr)

        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_receipt(
                matching_status=(
                    InboundCorrespondenceReceipt.MatchingStatus.MATCHED
                ),
                matched_request=self.prr,
                matched_at=timezone.now(),
                match_method=InboundCorrespondenceReceipt.MatchMethod.MANUAL,
            )

        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_receipt(
                matching_status=(
                    InboundCorrespondenceReceipt.MatchingStatus.UNMATCHED
                ),
                matched_request=self.prr,
                matched_at=timezone.now(),
                match_method=InboundCorrespondenceReceipt.MatchMethod.MANUAL,
            )

        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_receipt(
                parsing_status=(
                    InboundCorrespondenceReceipt.ParsingStatus.DUPLICATE
                ),
                matching_status=(
                    InboundCorrespondenceReceipt.MatchingStatus.DUPLICATE
                ),
            )

    def test_receipt_state_constraint_rejects_invalid_parsing_cross_products(self):
        invalid_states = (
            {
                "parsing_status": (
                    InboundCorrespondenceReceipt.ParsingStatus.PENDING
                ),
                "matching_status": (
                    InboundCorrespondenceReceipt.MatchingStatus.UNMATCHED
                ),
                "quarantined": False,
                "completed_at": None,
            },
            {
                "matched_correspondence": None,
            },
            {
                "quarantined": True,
            },
            {
                "matching_status": (
                    InboundCorrespondenceReceipt.MatchingStatus.CANDIDATE
                ),
                "match_method": (
                    InboundCorrespondenceReceipt.MatchMethod.SUBJECT
                ),
                "candidate_request": self.prr,
                "matched_correspondence": None,
            },
            {
                "matching_status": (
                    InboundCorrespondenceReceipt.MatchingStatus.AMBIGUOUS
                ),
                "matched_correspondence": None,
            },
            {
                "parsing_status": (
                    InboundCorrespondenceReceipt.ParsingStatus.FAILED
                ),
                "matching_status": (
                    InboundCorrespondenceReceipt.MatchingStatus.UNMATCHED
                ),
                "matched_correspondence": None,
                "quarantined": False,
                "completed_at": timezone.now(),
            },
        )
        for index, overrides in enumerate(invalid_states):
            with self.subTest(index=index), self.assertRaises(IntegrityError):
                with transaction.atomic():
                    self.make_receipt(**overrides)

        duplicate_target = self.make_receipt()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_receipt(
                parsing_status=(
                    InboundCorrespondenceReceipt.ParsingStatus.DUPLICATE
                ),
                matching_status=(
                    InboundCorrespondenceReceipt.MatchingStatus.DUPLICATE
                ),
                matched_correspondence=None,
                duplicate_of=duplicate_target,
                quarantined=False,
            )

    def test_inbound_attachment_can_be_created_with_receipt_provenance(self):
        receipt, correspondence = self.make_matched_receipt()
        attachment = CorrespondenceAttachment.objects.create(
            correspondence=correspondence,
            inbound_receipt=receipt,
            original_filename="requester\x00evidence\n.pdf",
            content_type="application/pdf",
            size=128,
            sha256="a" * 64,
            uploaded_by=self.integration,
        )

        self.assertEqual(attachment.original_filename, "requesterevidence.pdf")
        self.assertEqual(attachment.inbound_receipt, receipt)
        self.assertEqual(attachment.uploaded_by, self.integration)
        self.assertTrue(
            attachment.storage_key.startswith(
                "quarantine/correspondence/attachments/"
            )
        )
        self.assertTrue(attachment.quarantined)
        self.assertEqual(
            attachment.scan_status,
            CorrespondenceAttachment.ScanStatus.PENDING,
        )

    def test_inbound_attachment_requires_the_receipt_for_its_correspondence(self):
        receipt, correspondence = self.make_matched_receipt()
        other_correspondence = self.make_inbound_correspondence(
            request=self.prr,
            status=Correspondence.Status.RECEIVED,
        )

        with self.assertRaises(ValidationError):
            CorrespondenceAttachment.objects.create(
                correspondence=correspondence,
                original_filename="missing-provenance.pdf",
                content_type="application/pdf",
                size=128,
                sha256="b" * 64,
                uploaded_by=self.integration,
            )

        with self.assertRaises(ValidationError):
            CorrespondenceAttachment.objects.create(
                correspondence=other_correspondence,
                inbound_receipt=receipt,
                original_filename="wrong-provenance.pdf",
                content_type="application/pdf",
                size=128,
                sha256="c" * 64,
                uploaded_by=self.integration,
            )

    def test_outbound_attachment_cannot_claim_inbound_receipt_provenance(self):
        receipt, _ = self.make_matched_receipt()
        outbound = Correspondence.objects.create(
            request=self.prr,
            direction=Correspondence.Direction.OUTBOUND,
            kind=Correspondence.Kind.GENERAL,
            status=Correspondence.Status.DRAFT,
            draft_subject="Outbound subject",
            draft_body="Outbound body",
            created_by=self.officer,
        )

        with self.assertRaises(ValidationError):
            CorrespondenceAttachment.objects.create(
                correspondence=outbound,
                inbound_receipt=receipt,
                original_filename="wrong-direction.pdf",
                content_type="application/pdf",
                size=128,
                sha256="d" * 64,
                uploaded_by=self.officer,
            )

    def test_storage_key_is_application_generated_for_inbound_attachments(self):
        receipt, correspondence = self.make_matched_receipt()
        caller_key = f"caller/{uuid.uuid4()}"
        attachment = CorrespondenceAttachment.objects.create(
            correspondence=correspondence,
            inbound_receipt=receipt,
            original_filename="evidence.pdf",
            content_type="application/pdf",
            size=128,
            sha256="e" * 64,
            storage_key=caller_key,
            uploaded_by=self.integration,
        )

        self.assertNotEqual(attachment.storage_key, caller_key)
        self.assertTrue(
            attachment.storage_key.startswith(
                "quarantine/correspondence/attachments/"
            )
        )
