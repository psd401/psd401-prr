import hashlib
import json
import re

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class RecipientSnapshotCutoverMigrationTests(TransactionTestCase):
    audit_target = ("audit", "0002_auditlog_append_only_trigger")
    migrate_from = (
        "correspondence",
        "0010_correspondence_recipient_base_manager",
    )
    migrate_to = (
        "correspondence",
        "0011_recipient_snapshot_cutover",
    )

    def _migrate(self, target):
        executor = MigrationExecutor(connection)
        executor.migrate([target, self.audit_target])
        return executor.loader.project_state(
            [target, self.audit_target]
        ).apps

    def _make_approved_correspondence(
        self,
        apps,
        *,
        suffix,
        modern,
        digest_override=None,
        malformed_detail=False,
    ):
        User = apps.get_model("auth", "User")
        Requester = apps.get_model("records", "Requester")
        Request = apps.get_model("records", "PublicRecordsRequest")
        Correspondence = apps.get_model("correspondence", "Correspondence")
        Recipient = apps.get_model(
            "correspondence",
            "CorrespondenceRecipient",
        )
        AuditLog = apps.get_model("audit", "AuditLog")

        officer = User.objects.create(
            username=f"cutover-officer-{suffix}",
            is_staff=True,
        )
        requester = Requester.objects.create(
            email=f"cutover-{suffix}@example.invalid",
        )
        request = Request.objects.create(
            requester=requester,
            description=f"Recipient snapshot cutover {suffix}.",
        )
        approved_at = timezone.now()
        correspondence = Correspondence.objects.create(
            request=request,
            direction="outbound",
            kind="general",
            status="approved",
            draft_subject="Approved subject",
            draft_body="Approved body",
            approved_subject="Approved subject",
            approved_body="Approved body",
            created_by=officer,
            approved_by=officer,
            approved_at=approved_at,
            rfc_message_id=f"<cutover-{suffix}@example.invalid>",
        )
        recipient = Recipient.objects.create(
            correspondence=correspondence,
            recipient_type="to",
            email=requester.email,
            requester=requester,
        )
        detail = {
            "correspondence_id": str(correspondence.pk),
            "recipient_count": 1,
        }
        if modern:
            snapshot = [
                {
                    "id": str(recipient.pk),
                    "recipient_type": recipient.recipient_type,
                    "email": recipient.email,
                    "display_name": recipient.display_name,
                    "requester_id": str(requester.pk),
                }
            ]
            detail["recipient_snapshot_sha256"] = hashlib.sha256(
                json.dumps(
                    snapshot,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        if digest_override is not None:
            detail["recipient_snapshot_sha256"] = digest_override
        AuditLog.objects.create(
            action="correspondence.approved",
            target=request.number,
            actor=officer,
            detail="malformed-approval-detail" if malformed_detail else detail,
        )
        return correspondence

    def test_forward_refuses_legacy_approval_without_snapshot_digest(self):
        apps = self._migrate(self.migrate_from)
        correspondence = self._make_approved_correspondence(
            apps,
            suffix="legacy",
            modern=False,
        )

        executor = MigrationExecutor(connection)
        with self.assertRaisesMessage(
            RuntimeError,
            str(correspondence.pk),
        ):
            executor.migrate([self.migrate_to, self.audit_target])

        # Leave the test database at the leaf migration for later tests.
        current_apps = executor.loader.project_state(
            [self.migrate_from, self.audit_target]
        ).apps
        current_apps.get_model("correspondence", "Correspondence").objects.filter(
            pk=correspondence.pk
        ).update(status="cancelled", cancelled_at=timezone.now())
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to, self.audit_target])

    def test_forward_accepts_approval_with_bounded_snapshot_evidence(self):
        apps = self._migrate(self.migrate_from)
        correspondence = self._make_approved_correspondence(
            apps,
            suffix="modern",
            modern=True,
        )

        apps = self._migrate(self.migrate_to)

        migrated = apps.get_model(
            "correspondence",
            "Correspondence",
        ).objects.get(pk=correspondence.pk)
        self.assertEqual(migrated.status, "approved")
        approval = apps.get_model("audit", "AuditLog").objects.get(
            action="correspondence.approved",
            detail__correspondence_id=str(correspondence.pk),
        )
        self.assertRegex(
            approval.detail["recipient_snapshot_sha256"],
            re.compile(r"\A[0-9a-f]{64}\Z"),
        )

    def test_forward_refuses_digest_that_does_not_match_recipients(self):
        apps = self._migrate(self.migrate_from)
        correspondence = self._make_approved_correspondence(
            apps,
            suffix="mismatch",
            modern=True,
            digest_override="a" * 64,
        )

        executor = MigrationExecutor(connection)
        with self.assertRaisesMessage(
            RuntimeError,
            str(correspondence.pk),
        ):
            executor.migrate([self.migrate_to, self.audit_target])

        current_apps = executor.loader.project_state(
            [self.migrate_from, self.audit_target]
        ).apps
        current_apps.get_model(
            "correspondence",
            "Correspondence",
        ).objects.filter(pk=correspondence.pk).update(
            status="cancelled",
            cancelled_at=timezone.now(),
        )
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to, self.audit_target])

    def test_forward_refuses_malformed_approval_detail(self):
        apps = self._migrate(self.migrate_from)
        correspondence = self._make_approved_correspondence(
            apps,
            suffix="malformed",
            modern=False,
            malformed_detail=True,
        )

        executor = MigrationExecutor(connection)
        with self.assertRaisesMessage(
            RuntimeError,
            str(correspondence.pk),
        ):
            executor.migrate([self.migrate_to, self.audit_target])

        current_apps = executor.loader.project_state(
            [self.migrate_from, self.audit_target]
        ).apps
        current_apps.get_model(
            "correspondence",
            "Correspondence",
        ).objects.filter(pk=correspondence.pk).update(
            status="cancelled",
            cancelled_at=timezone.now(),
        )
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to, self.audit_target])
