from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class DeliveryAttemptContractMigrationTests(TransactionTestCase):
    migrate_from = ("correspondence", "0002_correspondence_metadata")
    migrate_to = ("correspondence", "0003_delivery_attempt_contract")

    def test_migration_invalidates_legacy_installment_drafts(self):
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
        requester = Requester.objects.create(email="installment@example.org")
        prr = PublicRecordsRequest.objects.create(
            requester=requester,
            description="Legacy installment template migration test.",
        )
        draft = Correspondence.objects.create(
            request=prr,
            direction="outbound",
            kind="installment_notice",
            status="draft",
            draft_subject="Records are available",
            draft_body="The request is now closed.",
            draft_metadata={
                "installment_id": 1,
                "download_url": "https://prr.psd401.net/downloads/legacy",
                "item_count": 1,
                "has_exemptions": False,
                "is_final": True,
            },
            template_identifier="installment_notice",
            template_version="1",
            version=3,
        )

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        current_apps = executor.loader.project_state([self.migrate_to]).apps
        migrated = current_apps.get_model(
            "correspondence",
            "Correspondence",
        ).objects.get(pk=draft.pk)

        self.assertEqual(migrated.template_version, "2")
        self.assertEqual(migrated.version, 4)
        self.assertNotIn("available", migrated.draft_subject.casefold())
        self.assertNotIn("now closed", migrated.draft_body.casefold())
        self.assertIn("cannot be approved or sent", migrated.draft_body)

    def test_migration_rejects_precontract_pending_delivery_without_attempt(self):
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
        Delivery = old_apps.get_model(
            "correspondence",
            "CorrespondenceDelivery",
        )

        requester = Requester.objects.create(email="pending@example.org")
        prr = PublicRecordsRequest.objects.create(
            requester=requester,
            description="Pending pre-contract delivery migration test.",
        )
        correspondence = Correspondence.objects.create(
            request=prr,
            direction="outbound",
            kind="general",
            status="draft",
            draft_subject="Pending",
            draft_body="Pending body",
        )
        delivery = Delivery.objects.create(
            correspondence=correspondence,
            idempotency_key=f"pending:{correspondence.pk}",
        )

        try:
            executor = MigrationExecutor(connection)
            with self.assertRaisesMessage(
                RuntimeError,
                str(delivery.pk),
            ):
                executor.migrate([self.migrate_to])
        finally:
            executor = MigrationExecutor(connection)
            old_apps = executor.loader.project_state([self.migrate_from]).apps
            old_apps.get_model(
                "correspondence",
                "CorrespondenceDelivery",
            ).objects.all().delete()
            executor.migrate([self.migrate_to])

    def test_migration_fails_closed_instead_of_inventing_legacy_evidence(self):
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
        Delivery = old_apps.get_model(
            "correspondence",
            "CorrespondenceDelivery",
        )
        Attempt = old_apps.get_model(
            "correspondence",
            "CorrespondenceDeliveryAttempt",
        )

        requester = Requester.objects.create(email="legacy@example.org")
        prr = PublicRecordsRequest.objects.create(
            requester=requester,
            description="Legacy delivery attempt migration test.",
        )
        correspondence = Correspondence.objects.create(
            request=prr,
            direction="outbound",
            kind="general",
            status="draft",
            draft_subject="Legacy",
            draft_body="Legacy body",
        )
        delivery = Delivery.objects.create(
            correspondence=correspondence,
            idempotency_key=f"legacy:{correspondence.pk}",
        )
        attempt = Attempt.objects.create(
            delivery=delivery,
            attempt_number=1,
            claim_owner="legacy-integration",
            claimed_at=timezone.now(),
        )

        try:
            executor = MigrationExecutor(connection)
            with self.assertRaisesMessage(
                RuntimeError,
                str(attempt.pk),
            ):
                executor.migrate([self.migrate_to])
        finally:
            executor = MigrationExecutor(connection)
            old_apps = executor.loader.project_state([self.migrate_from]).apps
            old_apps.get_model(
                "correspondence",
                "CorrespondenceDeliveryAttempt",
            ).objects.all().delete()
            old_apps.get_model(
                "correspondence",
                "CorrespondenceDelivery",
            ).objects.all().delete()
            executor.migrate([self.migrate_to])

    def test_reverse_migration_refuses_to_discard_delivery_evidence(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        current_apps = executor.loader.project_state([self.migrate_to]).apps

        Requester = current_apps.get_model("records", "Requester")
        PublicRecordsRequest = current_apps.get_model(
            "records",
            "PublicRecordsRequest",
        )
        Correspondence = current_apps.get_model(
            "correspondence",
            "Correspondence",
        )
        Delivery = current_apps.get_model(
            "correspondence",
            "CorrespondenceDelivery",
        )
        Attempt = current_apps.get_model(
            "correspondence",
            "CorrespondenceDeliveryAttempt",
        )

        requester = Requester.objects.create(email="rollback@example.org")
        prr = PublicRecordsRequest.objects.create(
            requester=requester,
            description="Rollback evidence guard test.",
        )
        correspondence = Correspondence.objects.create(
            request=prr,
            direction="outbound",
            kind="general",
            status="draft",
            draft_subject="Rollback",
            draft_body="Rollback body",
        )
        delivery = Delivery.objects.create(
            correspondence=correspondence,
            idempotency_key=f"rollback:{correspondence.pk}",
        )
        attempt = Attempt.objects.create(
            delivery=delivery,
            attempt_number=1,
            claim_owner="rollback-integration",
            claimed_at=timezone.now(),
        )

        try:
            executor = MigrationExecutor(connection)
            with self.assertRaisesMessage(
                RuntimeError,
                str(attempt.pk),
            ):
                executor.migrate([self.migrate_from])
        finally:
            executor = MigrationExecutor(connection)
            current_apps = executor.loader.project_state([self.migrate_to]).apps
            current_apps.get_model(
                "correspondence",
                "CorrespondenceDeliveryAttempt",
            ).objects.all().delete()
            current_apps.get_model(
                "correspondence",
                "CorrespondenceDelivery",
            ).objects.all().delete()
            executor.migrate([self.migrate_to])

    def test_reverse_migration_refuses_v2_installment_drafts(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        current_apps = executor.loader.project_state([self.migrate_to]).apps

        Requester = current_apps.get_model("records", "Requester")
        PublicRecordsRequest = current_apps.get_model(
            "records",
            "PublicRecordsRequest",
        )
        Correspondence = current_apps.get_model(
            "correspondence",
            "Correspondence",
        )
        requester = Requester.objects.create(email="rollback-v2@example.org")
        prr = PublicRecordsRequest.objects.create(
            requester=requester,
            description="Rollback v2 installment guard test.",
        )
        draft = Correspondence.objects.create(
            request=prr,
            direction="outbound",
            kind="installment_notice",
            status="draft",
            draft_subject="Safe installment foundation",
            draft_body="This notice cannot be approved or sent.",
            template_identifier="installment_notice",
            template_version="2",
        )

        try:
            executor = MigrationExecutor(connection)
            with self.assertRaisesMessage(RuntimeError, str(draft.pk)):
                executor.migrate([self.migrate_from])
        finally:
            executor = MigrationExecutor(connection)
            current_apps = executor.loader.project_state([self.migrate_to]).apps
            current_apps.get_model(
                "correspondence",
                "Correspondence",
            ).objects.filter(pk=draft.pk).delete()
            executor.migrate([self.migrate_from])
            executor = MigrationExecutor(connection)
            executor.migrate([self.migrate_to])
