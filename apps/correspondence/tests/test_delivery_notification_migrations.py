from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class DeliveryNotificationContractMigrationTests(TransactionTestCase):
    migrate_from = (
        "correspondence",
        "0007_correspondencedelivery_recovery_not_before",
    )
    migrate_to = (
        "correspondence",
        "0008_correspondence_delivery_notification",
    )

    def _migrate_to_contract(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        return executor.loader.project_state([self.migrate_to]).apps

    def _make_evidence_graph(self, apps, *, suffix):
        User = apps.get_model("auth", "User")
        Requester = apps.get_model("records", "Requester")
        PublicRecordsRequest = apps.get_model(
            "records",
            "PublicRecordsRequest",
        )
        Correspondence = apps.get_model(
            "correspondence",
            "Correspondence",
        )
        Recipient = apps.get_model(
            "correspondence",
            "CorrespondenceRecipient",
        )
        Delivery = apps.get_model(
            "correspondence",
            "CorrespondenceDelivery",
        )
        Receipt = apps.get_model(
            "correspondence",
            "InboundCorrespondenceReceipt",
        )

        integration = User.objects.create(username=f"dsn-migration-{suffix}")
        requester = Requester.objects.create(
            email=f"dsn-migration-{suffix}@example.org"
        )
        prr = PublicRecordsRequest.objects.create(
            requester=requester,
            description=f"DSN migration evidence {suffix}.",
        )
        correspondence = Correspondence.objects.create(
            request=prr,
            direction="outbound",
            kind="general",
            status="draft",
            draft_subject="Subject",
            draft_body="Body",
            rfc_message_id=f"<dsn-migration-{suffix}@example.invalid>",
        )
        recipient = Recipient.objects.create(
            correspondence=correspondence,
            recipient_type="to",
            email=requester.email,
        )
        delivery = Delivery.objects.create(
            correspondence=correspondence,
            idempotency_key=f"dsn-migration:{suffix}",
        )
        received_at = timezone.now()
        inbound = Correspondence.objects.create(
            request=prr,
            direction="inbound",
            kind="delivery_status_notification",
            status="received",
            received_subject="Delivery Status Notification",
            received_at=received_at,
            matched_at=received_at,
            provider="gmail",
            provider_account="records-inbox",
            provider_message_id=f"dsn-migration-inbound-{suffix}",
        )
        receipt = Receipt.objects.create(
            provider="gmail",
            provider_account="records-inbox",
            provider_message_id=f"dsn-migration-{suffix}",
            received_at=received_at,
            parsing_status="parsed",
            matching_status="matched",
            match_method="delivery_status",
            delivery_status_original_message_id=correspondence.rfc_message_id,
            matched_request=prr,
            matched_delivery=delivery,
            matched_correspondence=inbound,
            matched_at=received_at,
            quarantined=False,
            raw_message_size=512,
            raw_message_sha256="a" * 64,
            payload_sha256="b" * 64,
            idempotency_key=f"dsn-migration-{suffix}",
            submitted_by=integration,
            completed_at=received_at,
        )
        return receipt, delivery, recipient

    def test_forward_schema_enforces_delivery_notification_contract(self):
        apps = self._migrate_to_contract()
        Notification = apps.get_model(
            "correspondence",
            "CorrespondenceDeliveryNotification",
        )
        Correspondence = apps.get_model(
            "correspondence",
            "Correspondence",
        )
        Receipt = apps.get_model(
            "correspondence",
            "InboundCorrespondenceReceipt",
        )
        receipt, delivery, recipient = self._make_evidence_graph(
            apps,
            suffix="forward",
        )

        self.assertIn(
            "delivery_status_notification",
            dict(Correspondence._meta.get_field("kind").choices),
        )
        self.assertIn(
            "delivery_status",
            dict(Receipt._meta.get_field("match_method").choices),
        )

        notification = Notification.objects.create(
            inbound_receipt=receipt,
            delivery=delivery,
            recipient_index=1,
            recipient=recipient,
            recipient_match_status="exact",
            action="failed",
            enhanced_status="5.1.1",
        )
        self.assertIsNotNone(notification.created_at)

        invalid_rows = (
            {
                "recipient_index": 1,
                "recipient": recipient,
                "recipient_match_status": "exact",
                "action": "failed",
                "enhanced_status": "5.1.1",
            },
            {
                "recipient_index": 0,
                "recipient": recipient,
                "recipient_match_status": "exact",
                "action": "failed",
                "enhanced_status": "5.1.1",
            },
            {
                "recipient_index": 2,
                "recipient": recipient,
                "recipient_match_status": "unknown",
                "action": "failed",
                "enhanced_status": "5.1.1",
            },
            {
                "recipient_index": 3,
                "recipient": recipient,
                "recipient_match_status": "exact",
                "action": "unknown",
                "enhanced_status": "5.1.1",
            },
            {
                "recipient_index": 4,
                "recipient": recipient,
                "recipient_match_status": "exact",
                "action": "failed",
                "enhanced_status": "5.1234.1",
            },
            {
                "recipient_index": 5,
                "recipient": recipient,
                "recipient_match_status": "exact",
                "action": "failed",
                "enhanced_status": "4.2.0",
            },
            {
                "recipient_index": 6,
                "recipient": None,
                "recipient_match_status": "exact",
                "action": "failed",
                "enhanced_status": "5.1.1",
            },
        )
        for values in invalid_rows:
            with self.subTest(values=values):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    Notification.objects.create(
                        inbound_receipt=receipt,
                        delivery=delivery,
                        **values,
                    )

    def test_reverse_migration_refuses_to_discard_notification_evidence(self):
        apps = self._migrate_to_contract()
        Notification = apps.get_model(
            "correspondence",
            "CorrespondenceDeliveryNotification",
        )
        receipt, delivery, recipient = self._make_evidence_graph(
            apps,
            suffix="reverse",
        )
        notification = Notification.objects.create(
            inbound_receipt=receipt,
            delivery=delivery,
            recipient_index=1,
            recipient=recipient,
            recipient_match_status="exact",
            action="delayed",
            enhanced_status="4.2.0",
        )

        try:
            executor = MigrationExecutor(connection)
            with self.assertRaisesMessage(RuntimeError, str(notification.pk)):
                executor.migrate([self.migrate_from])
        finally:
            executor = MigrationExecutor(connection)
            current_apps = executor.loader.project_state([self.migrate_to]).apps
            current_apps.get_model(
                "correspondence",
                "CorrespondenceDeliveryNotification",
            ).objects.all().delete()
            current_apps.get_model(
                "correspondence",
                "InboundCorrespondenceReceipt",
            ).objects.filter(pk=receipt.pk).delete()
            current_apps.get_model(
                "correspondence",
                "Correspondence",
            ).objects.filter(
                kind="delivery_status_notification",
            ).delete()
            executor.migrate([self.migrate_from])
            executor = MigrationExecutor(connection)
            executor.migrate([self.migrate_to])

    def test_reverse_migration_refuses_unmatched_dsn_without_notifications(self):
        apps = self._migrate_to_contract()
        User = apps.get_model("auth", "User")
        Correspondence = apps.get_model("correspondence", "Correspondence")
        Receipt = apps.get_model(
            "correspondence",
            "InboundCorrespondenceReceipt",
        )
        integration = User.objects.create(username="dsn-unmatched-reverse")
        received_at = timezone.now()
        inbound = Correspondence.objects.create(
            request=None,
            direction="inbound",
            kind="delivery_status_notification",
            status="unmatched",
            received_subject="Unmatched delivery report",
            received_at=received_at,
            provider="gmail",
            provider_account="records-inbox",
            provider_message_id="dsn-unmatched-reverse-inbound",
        )
        receipt = Receipt.objects.create(
            provider="gmail",
            provider_account="records-inbox",
            provider_message_id="dsn-unmatched-reverse",
            received_at=received_at,
            parsing_status="parsed",
            matching_status="unmatched",
            match_method="",
            delivery_status_original_message_id=(
                "<unknown-outbound@example.invalid>"
            ),
            matched_correspondence=inbound,
            quarantined=False,
            raw_message_size=512,
            raw_message_sha256="c" * 64,
            payload_sha256="d" * 64,
            idempotency_key="dsn-unmatched-reverse",
            submitted_by=integration,
            completed_at=received_at,
        )

        try:
            executor = MigrationExecutor(connection)
            with self.assertRaisesMessage(RuntimeError, str(inbound.pk)):
                executor.migrate([self.migrate_from])
        finally:
            executor = MigrationExecutor(connection)
            current_apps = executor.loader.project_state([self.migrate_to]).apps
            current_apps.get_model(
                "correspondence",
                "InboundCorrespondenceReceipt",
            ).objects.filter(pk=receipt.pk).delete()
            current_apps.get_model(
                "correspondence",
                "Correspondence",
            ).objects.filter(pk=inbound.pk).delete()
            executor.migrate([self.migrate_from])
            executor = MigrationExecutor(connection)
            executor.migrate([self.migrate_to])
