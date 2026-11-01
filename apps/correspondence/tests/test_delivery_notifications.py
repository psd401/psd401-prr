import datetime as dt

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.deletion import PROTECT, ProtectedError
from django.test import TestCase
from django.utils import timezone

from apps.correspondence.models import (
    Correspondence,
    CorrespondenceDelivery,
    CorrespondenceDeliveryNotification,
    CorrespondenceRecipient,
    InboundCorrespondenceReceipt,
)
from apps.records.models import PublicRecordsRequest, Requester


class CorrespondenceDeliveryNotificationTests(TestCase):
    def setUp(self):
        self.integration = get_user_model().objects.create_user(
            "dsn-model-integration",
            password=None,
        )
        self.prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="requester@example.org"),
            description="Delivery-status notification evidence.",
        )
        self.correspondence = Correspondence.objects.create(
            request=self.prr,
            direction=Correspondence.Direction.OUTBOUND,
            kind=Correspondence.Kind.GENERAL,
            status=Correspondence.Status.DRAFT,
            draft_subject="Subject",
            draft_body="Body",
            created_by=self.integration,
        )
        self.recipient = CorrespondenceRecipient.objects.create(
            correspondence=self.correspondence,
            recipient_type=CorrespondenceRecipient.RecipientType.TO,
            email="requester@example.org",
        )
        self.delivery = CorrespondenceDelivery.objects.create(
            correspondence=self.correspondence,
            idempotency_key=f"dsn-model:{self.correspondence.pk}",
        )
        received_at = timezone.now()
        self.inbound = Correspondence.objects.create(
            request=self.prr,
            direction=Correspondence.Direction.INBOUND,
            kind=Correspondence.Kind.DELIVERY_STATUS_NOTIFICATION,
            status=Correspondence.Status.RECEIVED,
            received_subject="Delivery Status Notification",
            created_by=self.integration,
            received_at=received_at,
            matched_at=received_at,
            provider="gmail",
            provider_account="records-inbox",
            provider_message_id="dsn-model-inbound",
        )
        self.receipt = InboundCorrespondenceReceipt.objects.create(
            provider="gmail",
            provider_account="records-inbox",
            provider_message_id="dsn-model-receipt",
            received_at=received_at,
            parsing_status=InboundCorrespondenceReceipt.ParsingStatus.PARSED,
            matching_status=InboundCorrespondenceReceipt.MatchingStatus.MATCHED,
            match_method=InboundCorrespondenceReceipt.MatchMethod.DELIVERY_STATUS,
            delivery_status_original_message_id=(
                self.correspondence.rfc_message_id
            ),
            matched_request=self.prr,
            matched_delivery=self.delivery,
            matched_correspondence=self.inbound,
            matched_at=received_at,
            quarantined=False,
            raw_message_size=512,
            raw_message_sha256="a" * 64,
            payload_sha256="b" * 64,
            idempotency_key="dsn-model-receipt",
            submitted_by=self.integration,
            completed_at=received_at,
        )

    def make_notification(self, **overrides):
        defaults = {
            "inbound_receipt": self.receipt,
            "delivery": self.delivery,
            "recipient_index": 1,
            "recipient": self.recipient,
            "recipient_match_status": (
                CorrespondenceDeliveryNotification.RecipientMatchStatus.EXACT
            ),
            "action": CorrespondenceDeliveryNotification.Action.FAILED,
            "enhanced_status": "5.1.1",
        }
        defaults.update(overrides)
        return CorrespondenceDeliveryNotification.objects.create(**defaults)

    def test_required_dsn_kinds_and_match_method_are_explicit(self):
        self.assertEqual(
            Correspondence.Kind.DELIVERY_STATUS_NOTIFICATION,
            "delivery_status_notification",
        )
        self.assertEqual(
            InboundCorrespondenceReceipt.MatchMethod.DELIVERY_STATUS,
            "delivery_status",
        )

    def test_creates_append_only_notification_evidence(self):
        notification = self.make_notification()

        self.assertEqual(notification.inbound_receipt, self.receipt)
        self.assertEqual(notification.delivery, self.delivery)
        self.assertEqual(notification.recipient, self.recipient)
        self.assertEqual(notification.recipient_index, 1)
        self.assertIsNotNone(notification.created_at)

    def test_rejects_a_receipt_that_is_not_matched_delivery_status_evidence(self):
        invalid_receipt = InboundCorrespondenceReceipt.objects.create(
            provider="gmail",
            provider_account="records-inbox",
            provider_message_id="ordinary-pending-receipt",
            received_at=timezone.now(),
            raw_message_size=512,
            raw_message_sha256="c" * 64,
            payload_sha256="d" * 64,
            idempotency_key="ordinary-pending-receipt",
            submitted_by=self.integration,
        )

        with self.assertRaisesRegex(
            ValidationError,
            "matched delivery-status receipt",
        ):
            self.make_notification(
                inbound_receipt=invalid_receipt,
                recipient_index=2,
            )

    def test_rejects_a_receipt_and_delivery_from_different_requests(self):
        other_request = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="other@example.org"),
            description="Other delivery-status request.",
        )
        other_correspondence = Correspondence.objects.create(
            request=other_request,
            direction=Correspondence.Direction.OUTBOUND,
            kind=Correspondence.Kind.GENERAL,
            status=Correspondence.Status.DRAFT,
            draft_subject="Other",
            draft_body="Other",
            created_by=self.integration,
        )
        other_delivery = CorrespondenceDelivery.objects.create(
            correspondence=other_correspondence,
            idempotency_key=f"dsn-model:{other_correspondence.pk}",
        )

        with self.assertRaisesRegex(
            ValidationError,
            "same request",
        ):
            self.make_notification(
                delivery=other_delivery,
                recipient=None,
                recipient_match_status=(
                    CorrespondenceDeliveryNotification
                    .RecipientMatchStatus.UNMATCHED
                ),
                recipient_index=3,
            )

    def test_rejects_non_dsn_matched_correspondence(self):
        received_at = timezone.now()
        ordinary_inbound = Correspondence.objects.create(
            request=self.prr,
            direction=Correspondence.Direction.INBOUND,
            kind=Correspondence.Kind.GENERAL,
            status=Correspondence.Status.RECEIVED,
            received_subject="Ordinary requester reply",
            created_by=self.integration,
            received_at=received_at,
            matched_at=received_at,
            provider="gmail",
            provider_account="records-inbox",
            provider_message_id="ordinary-model-inbound",
        )
        ordinary_receipt = InboundCorrespondenceReceipt.objects.create(
            provider="gmail",
            provider_account="records-inbox",
            provider_message_id="ordinary-model-receipt",
            received_at=received_at,
            parsing_status=InboundCorrespondenceReceipt.ParsingStatus.PARSED,
            matching_status=InboundCorrespondenceReceipt.MatchingStatus.MATCHED,
            match_method=InboundCorrespondenceReceipt.MatchMethod.DELIVERY_STATUS,
            delivery_status_original_message_id=(
                self.correspondence.rfc_message_id
            ),
            matched_request=self.prr,
            matched_delivery=self.delivery,
            matched_correspondence=ordinary_inbound,
            matched_at=received_at,
            quarantined=False,
            raw_message_size=512,
            raw_message_sha256="e" * 64,
            payload_sha256="f" * 64,
            idempotency_key="ordinary-model-receipt",
            submitted_by=self.integration,
            completed_at=received_at,
        )

        with self.assertRaisesRegex(
            ValidationError,
            "delivery-status correspondence",
        ):
            self.make_notification(
                inbound_receipt=ordinary_receipt,
                recipient_index=4,
            )

    def test_receipt_recipient_index_is_unique_and_positive(self):
        self.make_notification()
        with self.assertRaises(ValidationError):
            self.make_notification()

        with self.assertRaises(ValidationError):
            self.make_notification(recipient_index=0)

    def test_declared_recipient_match_and_action_enums_are_exact(self):
        self.assertSetEqual(
            {
                value
                for value, _ in (
                    CorrespondenceDeliveryNotification.RecipientMatchStatus.choices
                )
            },
            {"exact", "unmatched", "ambiguous"},
        )
        self.assertSetEqual(
            {
                value
                for value, _ in CorrespondenceDeliveryNotification.Action.choices
            },
            {"failed", "delayed", "delivered", "relayed", "expanded"},
        )

        for field, value in (
            ("recipient_match_status", "provider_defined_later"),
            ("action", "provider_defined_later"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(ValidationError):
                    self.make_notification(
                        recipient_index=2,
                        **{field: value},
                    )

    def test_recipient_link_matches_exactness_and_delivery_correspondence(self):
        unmatched = self.make_notification(
            recipient_index=10,
            recipient=None,
            recipient_match_status=(
                CorrespondenceDeliveryNotification.RecipientMatchStatus.UNMATCHED
            ),
        )
        self.assertIsNone(unmatched.recipient_id)

        for overrides in (
            {
                "recipient_index": 2,
                "recipient": None,
                "recipient_match_status": (
                    CorrespondenceDeliveryNotification.RecipientMatchStatus.EXACT
                ),
            },
            {
                "recipient_index": 3,
                "recipient": self.recipient,
                "recipient_match_status": (
                    CorrespondenceDeliveryNotification.RecipientMatchStatus.UNMATCHED
                ),
            },
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValidationError):
                    self.make_notification(**overrides)

        other_correspondence = Correspondence.objects.create(
            request=self.prr,
            direction=Correspondence.Direction.OUTBOUND,
            kind=Correspondence.Kind.GENERAL,
            status=Correspondence.Status.DRAFT,
            draft_subject="Other",
            draft_body="Other body",
            created_by=self.integration,
        )
        other_recipient = CorrespondenceRecipient.objects.create(
            correspondence=other_correspondence,
            recipient_type=CorrespondenceRecipient.RecipientType.TO,
            email="other@example.org",
        )
        with self.assertRaises(ValidationError):
            self.make_notification(
                recipient_index=4,
                recipient=other_recipient,
            )

    def test_enhanced_status_is_bounded_ascii_rfc_shape(self):
        for index, invalid_status in enumerate(
            (
                "5.1",
                "5.1.1.1",
                "5.1234.1",
                "5.1.1234",
                "5.١.١",
                "3.1.1",
                "x.1.1",
            ),
            start=2,
        ):
            with self.subTest(enhanced_status=invalid_status):
                with self.assertRaises(ValidationError):
                    self.make_notification(
                        recipient_index=index,
                        enhanced_status=invalid_status,
                    )

    def test_action_must_match_enhanced_status_class(self):
        incompatible_pairs = (
            (CorrespondenceDeliveryNotification.Action.FAILED, "4.2.0"),
            (CorrespondenceDeliveryNotification.Action.DELAYED, "5.1.1"),
            (CorrespondenceDeliveryNotification.Action.DELIVERED, "5.1.1"),
            (CorrespondenceDeliveryNotification.Action.RELAYED, "4.2.0"),
            (CorrespondenceDeliveryNotification.Action.EXPANDED, "5.1.1"),
        )
        for index, (action, enhanced_status) in enumerate(
            incompatible_pairs,
            start=2,
        ):
            with self.subTest(action=action, enhanced_status=enhanced_status):
                with self.assertRaises(ValidationError):
                    self.make_notification(
                        recipient_index=index,
                        action=action,
                        enhanced_status=enhanced_status,
                    )

        positive_actions = (
            CorrespondenceDeliveryNotification.Action.DELIVERED,
            CorrespondenceDeliveryNotification.Action.RELAYED,
            CorrespondenceDeliveryNotification.Action.EXPANDED,
        )
        for index, action in enumerate(positive_actions, start=20):
            self.make_notification(
                recipient_index=index,
                action=action,
                enhanced_status="2.0.0",
            )

    def test_instance_and_queryset_mutations_are_forbidden(self):
        notification = self.make_notification()
        notification.enhanced_status = "5.2.0"

        with self.assertRaises(ValidationError):
            notification.save(update_fields=["enhanced_status"])
        with self.assertRaises(ValidationError):
            notification.delete()
        with self.assertRaises(ValidationError):
            CorrespondenceDeliveryNotification.objects.filter(
                pk=notification.pk
            ).update(enhanced_status="5.2.0")
        with self.assertRaises(ValidationError):
            CorrespondenceDeliveryNotification.objects.filter(
                pk=notification.pk
            ).delete()
        with self.assertRaises(ValidationError):
            CorrespondenceDeliveryNotification.objects.bulk_update(
                [notification],
                fields=["enhanced_status"],
            )

        notification.refresh_from_db()
        self.assertEqual(notification.enhanced_status, "5.1.1")

    def test_conflict_upsert_cannot_rewrite_notification(self):
        notification = self.make_notification()
        replacement = CorrespondenceDeliveryNotification(
            id=notification.id,
            inbound_receipt=self.receipt,
            delivery=self.delivery,
            recipient_index=1,
            recipient=self.recipient,
            recipient_match_status=(
                CorrespondenceDeliveryNotification.RecipientMatchStatus.EXACT
            ),
            action=CorrespondenceDeliveryNotification.Action.FAILED,
            enhanced_status="5.2.0",
        )

        with self.assertRaises(ValidationError):
            CorrespondenceDeliveryNotification.objects.bulk_create(
                [replacement],
                update_conflicts=True,
                update_fields=["enhanced_status"],
                unique_fields=["id"],
            )
        with self.assertRaises(ValidationError):
            CorrespondenceDeliveryNotification.objects.update_or_create(
                pk=notification.pk,
                defaults={"enhanced_status": "5.2.0"},
            )

    def test_bulk_create_runs_model_validation(self):
        invalid = CorrespondenceDeliveryNotification(
            inbound_receipt=self.receipt,
            delivery=self.delivery,
            recipient_index=2,
            recipient=self.recipient,
            recipient_match_status=(
                CorrespondenceDeliveryNotification.RecipientMatchStatus.EXACT
            ),
            action=CorrespondenceDeliveryNotification.Action.FAILED,
            enhanced_status="4.2.0",
        )

        with self.assertRaises(ValidationError):
            CorrespondenceDeliveryNotification.objects.bulk_create([invalid])
        self.assertFalse(
            CorrespondenceDeliveryNotification.objects.filter(
                recipient_index=2
            ).exists()
        )

    def test_evidence_foreign_keys_are_protected(self):
        notification = self.make_notification()

        for field_name, evidence in (
            ("inbound_receipt", self.receipt),
            ("delivery", self.delivery),
            ("recipient", self.recipient),
        ):
            with self.subTest(field=field_name):
                self.assertIs(
                    notification._meta.get_field(
                        field_name
                    ).remote_field.on_delete,
                    PROTECT,
                )
                with self.assertRaises(ProtectedError):
                    models.Model.delete(evidence)

    def test_created_at_is_immutable(self):
        notification = self.make_notification()
        notification.created_at -= dt.timedelta(days=1)

        with self.assertRaises(ValidationError):
            notification.save(update_fields=["created_at"])
