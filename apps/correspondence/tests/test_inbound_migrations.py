from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class InboundReceiptContractMigrationTests(TransactionTestCase):
    migrate_from = ("correspondence", "0003_delivery_attempt_contract")
    migrate_to = ("correspondence", "0004_inbound_receipt_contract")

    def test_forward_migration_refuses_to_invent_legacy_receipt_evidence(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps
        Receipt = old_apps.get_model(
            "correspondence",
            "InboundCorrespondenceReceipt",
        )
        receipt = Receipt.objects.create(
            provider="gmail",
            provider_account="records-inbox",
            provider_message_id="legacy-inbound-receipt",
            received_at=timezone.now(),
            raw_message_size=128,
            raw_message_sha256="a" * 64,
        )

        try:
            executor = MigrationExecutor(connection)
            with self.assertRaisesMessage(RuntimeError, str(receipt.pk)):
                executor.migrate([self.migrate_to])
        finally:
            executor = MigrationExecutor(connection)
            old_apps = executor.loader.project_state([self.migrate_from]).apps
            old_apps.get_model(
                "correspondence",
                "InboundCorrespondenceReceipt",
            ).objects.all().delete()
            executor.migrate([self.migrate_to])

    def test_forward_migration_refuses_unknown_inbound_attachment_provenance(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps
        Requester = old_apps.get_model("records", "Requester")
        PublicRecordsRequest = old_apps.get_model(
            "records",
            "PublicRecordsRequest",
        )
        Correspondence = old_apps.get_model(
            "correspondence",
            "Correspondence",
        )
        Attachment = old_apps.get_model(
            "correspondence",
            "CorrespondenceAttachment",
        )
        requester = Requester.objects.create(email="legacy-inbound@example.org")
        prr = PublicRecordsRequest.objects.create(
            requester=requester,
            description="Legacy inbound attachment migration guard.",
        )
        correspondence = Correspondence.objects.create(
            request=prr,
            direction="inbound",
            kind="general",
            status="received",
            received_subject="Legacy inbound message",
            received_body="Legacy inbound body",
            received_at=timezone.now(),
        )
        attachment = Attachment.objects.create(
            correspondence=correspondence,
            original_filename="legacy.pdf",
            content_type="application/pdf",
            size=128,
            sha256="b" * 64,
            storage_key="quarantine/correspondence/attachments/legacy",
        )

        try:
            executor = MigrationExecutor(connection)
            with self.assertRaisesMessage(RuntimeError, str(attachment.pk)):
                executor.migrate([self.migrate_to])
        finally:
            executor = MigrationExecutor(connection)
            old_apps = executor.loader.project_state([self.migrate_from]).apps
            old_apps.get_model(
                "correspondence",
                "CorrespondenceAttachment",
            ).objects.all().delete()
            old_apps.get_model(
                "correspondence",
                "Correspondence",
            ).objects.filter(pk=correspondence.pk).delete()
            executor.migrate([self.migrate_to])

    def test_forward_migration_preserves_outbound_attachment_without_provenance(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps
        Requester = old_apps.get_model("records", "Requester")
        PublicRecordsRequest = old_apps.get_model(
            "records",
            "PublicRecordsRequest",
        )
        Correspondence = old_apps.get_model(
            "correspondence",
            "Correspondence",
        )
        Attachment = old_apps.get_model(
            "correspondence",
            "CorrespondenceAttachment",
        )
        requester = Requester.objects.create(email="outbound@example.org")
        prr = PublicRecordsRequest.objects.create(
            requester=requester,
            description="Preserved outbound attachment migration test.",
        )
        correspondence = Correspondence.objects.create(
            request=prr,
            direction="outbound",
            kind="general",
            status="draft",
            draft_subject="Outbound draft",
            draft_body="Outbound body",
        )
        attachment = Attachment.objects.create(
            correspondence=correspondence,
            original_filename="outbound.pdf",
            content_type="application/pdf",
            size=128,
            sha256="c" * 64,
            storage_key="quarantine/correspondence/attachments/outbound",
        )

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        current_apps = executor.loader.project_state([self.migrate_to]).apps
        migrated = current_apps.get_model(
            "correspondence",
            "CorrespondenceAttachment",
        ).objects.get(pk=attachment.pk)

        self.assertIsNone(migrated.inbound_receipt_id)
        self.assertEqual(
            migrated.storage_key,
            "quarantine/correspondence/attachments/outbound",
        )

    def test_forward_migration_refuses_invented_legacy_scan_evidence(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps
        Requester = old_apps.get_model("records", "Requester")
        PublicRecordsRequest = old_apps.get_model(
            "records",
            "PublicRecordsRequest",
        )
        Correspondence = old_apps.get_model(
            "correspondence",
            "Correspondence",
        )
        Attachment = old_apps.get_model(
            "correspondence",
            "CorrespondenceAttachment",
        )
        requester = Requester.objects.create(email="invalid-scan@example.org")
        prr = PublicRecordsRequest.objects.create(
            requester=requester,
            description="Legacy scan evidence migration guard.",
        )
        correspondence = Correspondence.objects.create(
            request=prr,
            direction="outbound",
            kind="general",
            status="draft",
            draft_subject="Outbound draft",
            draft_body="Outbound body",
        )
        attachment = Attachment.objects.create(
            correspondence=correspondence,
            original_filename="unverified-clean.pdf",
            content_type="application/pdf",
            size=128,
            sha256="f" * 64,
            storage_key="correspondence/attachments/unverified-clean",
            scan_status="clean",
            quarantined=False,
            quarantine_reason="",
            scanned_at=None,
        )

        try:
            executor = MigrationExecutor(connection)
            with self.assertRaisesMessage(RuntimeError, str(attachment.pk)):
                executor.migrate([self.migrate_to])
        finally:
            executor = MigrationExecutor(connection)
            old_apps = executor.loader.project_state([self.migrate_from]).apps
            old_apps.get_model(
                "correspondence",
                "CorrespondenceAttachment",
            ).objects.filter(pk=attachment.pk).delete()
            executor.migrate([self.migrate_to])

    def test_reverse_migration_refuses_to_discard_inbound_receipt_evidence(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        current_apps = executor.loader.project_state([self.migrate_to]).apps
        User = current_apps.get_model("auth", "User")
        Receipt = current_apps.get_model(
            "correspondence",
            "InboundCorrespondenceReceipt",
        )
        integration = User.objects.create(username="migration-inbound")
        receipt = Receipt.objects.create(
            provider="gmail",
            provider_account="records-inbox",
            provider_message_id="new-contract-receipt",
            received_at=timezone.now(),
            parsing_status="pending",
            matching_status="unmatched",
            quarantined=True,
            raw_message_size=128,
            raw_message_sha256="d" * 64,
            payload_sha256="e" * 64,
            idempotency_key="gmail:new-contract-receipt",
            submitted_by=integration,
        )

        try:
            executor = MigrationExecutor(connection)
            with self.assertRaisesMessage(RuntimeError, str(receipt.pk)):
                executor.migrate([self.migrate_from])
        finally:
            executor = MigrationExecutor(connection)
            current_apps = executor.loader.project_state([self.migrate_to]).apps
            current_apps.get_model(
                "correspondence",
                "InboundCorrespondenceReceipt",
            ).objects.all().delete()
            executor.migrate([self.migrate_from])
            executor = MigrationExecutor(connection)
            executor.migrate([self.migrate_to])


class AttachmentQuarantineCleanupMigrationTests(TransactionTestCase):
    migrate_from = ("correspondence", "0004_inbound_receipt_contract")
    migrate_to = ("correspondence", "0005_attachment_quarantine_cleanup")

    def test_reverse_refuses_to_discard_cleanup_work_or_evidence(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        current_apps = executor.loader.project_state([self.migrate_to]).apps
        User = current_apps.get_model("auth", "User")
        Requester = current_apps.get_model("records", "Requester")
        PublicRecordsRequest = current_apps.get_model(
            "records",
            "PublicRecordsRequest",
        )
        Correspondence = current_apps.get_model(
            "correspondence",
            "Correspondence",
        )
        Attachment = current_apps.get_model(
            "correspondence",
            "CorrespondenceAttachment",
        )
        Cleanup = current_apps.get_model(
            "correspondence",
            "CorrespondenceAttachmentQuarantineCleanup",
        )
        actor = User.objects.create(username="cleanup-migration")
        requester = Requester.objects.create(email="cleanup@example.org")
        prr = PublicRecordsRequest.objects.create(
            requester=requester,
            description="Cleanup migration evidence.",
        )
        correspondence = Correspondence.objects.create(
            request=prr,
            direction="outbound",
            kind="general",
            status="draft",
            draft_subject="Cleanup migration",
            draft_body="Body",
            created_by=actor,
        )
        attachment = Attachment.objects.create(
            correspondence=correspondence,
            original_filename="cleanup.pdf",
            content_type="application/pdf",
            size=8,
            sha256="f" * 64,
            storage_key="quarantine/correspondence/attachments/migration",
            uploaded_by=actor,
        )
        cleanup = Cleanup.objects.create(
            attachment=attachment,
            source_storage_key=attachment.storage_key,
        )

        try:
            executor = MigrationExecutor(connection)
            with self.assertRaisesMessage(RuntimeError, str(cleanup.pk)):
                executor.migrate([self.migrate_from])
        finally:
            executor = MigrationExecutor(connection)
            current_apps = executor.loader.project_state([self.migrate_to]).apps
            current_apps.get_model(
                "correspondence",
                "CorrespondenceAttachmentQuarantineCleanup",
            ).objects.all().delete()
            executor.migrate([self.migrate_from])
            executor = MigrationExecutor(connection)
            executor.migrate([self.migrate_to])


class InboundQuarantineWriteReservationMigrationTests(TransactionTestCase):
    migrate_from = ("correspondence", "0005_attachment_quarantine_cleanup")
    migrate_to = (
        "correspondence",
        "0006_inbound_quarantine_write_reservation",
    )

    def test_reverse_refuses_to_discard_reservation_or_cleanup_evidence(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        current_apps = executor.loader.project_state([self.migrate_to]).apps
        Reservation = current_apps.get_model(
            "correspondence",
            "InboundQuarantineWriteReservation",
        )
        reservation = Reservation.objects.create(
            logical_identity="a" * 64,
            kind="raw_message",
            size=128,
            sha256="b" * 64,
            storage_key="quarantine/correspondence/raw/migration-reservation",
            transport_idempotency_identity="c" * 64,
            transport_provider_identity="d" * 64,
        )

        try:
            executor = MigrationExecutor(connection)
            with self.assertRaisesMessage(RuntimeError, str(reservation.pk)):
                executor.migrate([self.migrate_from])
        finally:
            executor = MigrationExecutor(connection)
            current_apps = executor.loader.project_state([self.migrate_to]).apps
            current_apps.get_model(
                "correspondence",
                "InboundQuarantineWriteReservation",
            ).objects.all().delete()
            executor.migrate([self.migrate_from])
            executor = MigrationExecutor(connection)
            executor.migrate([self.migrate_to])

    def test_reverse_refuses_to_discard_alias_only_evidence(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        current_apps = executor.loader.project_state([self.migrate_to]).apps
        Alias = current_apps.get_model(
            "correspondence",
            "InboundQuarantineTransportAlias",
        )
        alias = Alias.objects.create(
            kind="provider",
            identity="e" * 64,
            logical_identity="f" * 64,
        )

        try:
            executor = MigrationExecutor(connection)
            with self.assertRaisesMessage(RuntimeError, str(alias.pk)):
                executor.migrate([self.migrate_from])
        finally:
            executor = MigrationExecutor(connection)
            current_apps = executor.loader.project_state([self.migrate_to]).apps
            current_apps.get_model(
                "correspondence",
                "InboundQuarantineTransportAlias",
            ).objects.all().delete()
            executor.migrate([self.migrate_from])
            executor = MigrationExecutor(connection)
            executor.migrate([self.migrate_to])
