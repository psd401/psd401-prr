from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models.deletion import ProtectedError
from django.test import TransactionTestCase


class DenialBasisMigrationTests(TransactionTestCase):
    migrate_from = (
        "correspondence",
        "0008_correspondence_delivery_notification",
    )
    migrate_to = (
        "correspondence",
        "0009_correspondencedenialcitation",
    )
    migrate_latest = (
        "correspondence",
        "0011_recipient_snapshot_cutover",
    )

    def _migrate(self, target):
        executor = MigrationExecutor(connection)
        executor.migrate([target])
        return executor.loader.project_state([target]).apps

    def tearDown(self):
        self._migrate(self.migrate_latest)
        super().tearDown()

    def _make_basis(self, apps):
        User = apps.get_model("auth", "User")
        Requester = apps.get_model("records", "Requester")
        PublicRecordsRequest = apps.get_model(
            "records",
            "PublicRecordsRequest",
        )
        ExemptionCitation = apps.get_model(
            "records",
            "ExemptionCitation",
        )
        Correspondence = apps.get_model(
            "correspondence",
            "Correspondence",
        )
        DenialCitation = apps.get_model(
            "correspondence",
            "CorrespondenceDenialCitation",
        )
        officer = User.objects.create(username="denial-migration-officer")
        requester = Requester.objects.create(
            email="denial-migration@example.org",
        )
        prr = PublicRecordsRequest.objects.create(
            requester=requester,
            description="Denial migration evidence.",
        )
        correspondence = Correspondence.objects.create(
            request=prr,
            direction="outbound",
            kind="denial",
            status="draft",
            draft_subject="Denial determination",
            draft_body="The records are fully exempt.",
            created_by=officer,
        )
        exemption = ExemptionCitation.objects.create(
            citation="RCW 42.56.250",
            short_name="Examination questions",
        )
        basis = DenialCitation.objects.create(
            correspondence=correspondence,
            exemption=exemption,
            selected_by=officer,
            citation_snapshot=exemption.citation,
            short_name_snapshot=exemption.short_name,
            description_snapshot=exemption.description,
            requires_second_review_snapshot=(
                exemption.requires_second_review
            ),
        )
        return basis, correspondence, exemption, officer

    def test_forward_schema_requires_unique_protected_exemption_fk(self):
        apps = self._migrate(self.migrate_to)
        DenialCitation = apps.get_model(
            "correspondence",
            "CorrespondenceDenialCitation",
        )
        basis, correspondence, exemption, officer = self._make_basis(apps)
        self.assertIsNotNone(basis.created_at)

        with self.assertRaises(IntegrityError), transaction.atomic():
            DenialCitation.objects.create(
                correspondence=correspondence,
                exemption=exemption,
                selected_by=officer,
                citation_snapshot=exemption.citation,
                short_name_snapshot=exemption.short_name,
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            DenialCitation.objects.create(
                correspondence=correspondence,
                exemption_id=None,
                selected_by=officer,
                citation_snapshot="RCW 42.56.250",
                short_name_snapshot="Examination questions",
            )
        with self.assertRaises(ProtectedError):
            exemption.delete()

    def test_reverse_migration_refuses_to_discard_legal_basis(self):
        apps = self._migrate(self.migrate_to)
        basis, _, _, _ = self._make_basis(apps)

        with self.assertRaisesRegex(
            RuntimeError,
            "Cannot reverse denial-citation migration while legal-basis "
            "evidence exists",
        ):
            self._migrate(self.migrate_from)

        basis.delete()
        self._migrate(self.migrate_from)
