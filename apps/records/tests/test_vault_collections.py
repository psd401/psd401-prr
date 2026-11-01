"""Reviewed Vault counts can become durable, resumable collection work."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.records.models import (
    CollectionTask,
    PublicRecordsRequest,
    Requester,
    VaultCountRun,
)


@override_settings(
    PRR_GOOGLE_HOSTED_DOMAIN="psd401.net",
    PRR_VAULT_ADMIN_USER="vault-admin@example.org",
    PRR_VAULT_IMPERSONATION_TARGET=(
        "prr-vault@psd401-prr-identity.iam.gserviceaccount.com"
    ),
)
class VaultCollectionWorkflowTests(TestCase):
    def setUp(self):
        self.officer = get_user_model().objects.create_user(
            "vault-collection-officer",
            "officer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        requester = Requester.objects.create(email="requester@example.org")
        self.request = PublicRecordsRequest.objects.create(
            requester=requester,
            description="Emails about the field project.",
        )

    def completed_count(self, **overrides):
        data = {
            "request": self.request,
            "requested_by": self.officer,
            "state": VaultCountRun.State.COMPLETE,
            "search_scope": VaultCountRun.SearchScope.ACCOUNTS,
            "terms": '"field project" AND (contractor OR invoice)',
            "custodian_emails": ["superintendent@psd401.net"],
            "date_range_start": "2026-01-01",
            "date_range_end": "2026-06-30",
            "input_sha256": "a" * 64,
            "vault_matter_id": "matter-1",
            "total_count": 1842,
            "completed_at": "2026-08-11T16:00:00Z",
        }
        data.update(overrides)
        return VaultCountRun.objects.create(**data)

    @patch("apps.records.tasks.execute_vault_collection.delay")
    @patch("apps.integrations.vault.client.ensure_mail_export")
    @patch("apps.integrations.vault.client.ensure_mail_hold")
    def test_queue_creates_work_without_calling_vault(
        self,
        ensure_mail_hold,
        ensure_mail_export,
        delay,
    ):
        from apps.records.vault_collections import queue_vault_collection

        run = self.completed_count()

        result = queue_vault_collection(
            run.pk,
            actor=self.officer,
            expected_input_sha256=run.input_sha256,
        )

        self.assertTrue(result.created)
        task = result.task
        self.assertEqual(task.kind, CollectionTask.Kind.VAULT_MAIL)
        self.assertEqual(task.state, CollectionTask.State.PENDING)
        self.assertEqual(task.source_vault_count, run)
        self.assertEqual(task.search_terms, run.terms)
        self.assertEqual(task.custodian_emails, run.custodian_emails)
        self.assertFalse(task.vault_export_id)
        ensure_mail_hold.assert_not_called()
        ensure_mail_export.assert_not_called()
        delay.assert_called_once_with(task.pk)
        self.assertTrue(
            AuditLog.objects.filter(
                action="vault.collection.queued",
                target=self.request.number,
            ).exists()
        )

    @patch("apps.records.tasks.execute_vault_collection.delay")
    def test_queue_is_idempotent_for_one_count(self, delay):
        from apps.records.vault_collections import queue_vault_collection

        run = self.completed_count()

        first = queue_vault_collection(
            run.pk,
            actor=self.officer,
            expected_input_sha256=run.input_sha256,
        )
        second = queue_vault_collection(
            run.pk,
            actor=self.officer,
            expected_input_sha256=run.input_sha256,
        )

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(second.task.pk, first.task.pk)
        self.assertEqual(CollectionTask.objects.count(), 1)
        delay.assert_called_once_with(first.task.pk)

    @patch("apps.records.tasks.execute_vault_collection.delay")
    def test_queue_can_retry_a_failed_broker_handoff_without_duplicate_task(self, delay):
        from apps.records.vault_collections import queue_vault_collection

        run = self.completed_count()
        delay.side_effect = RuntimeError("broker unavailable")
        with self.assertRaisesMessage(ValidationError, "could not be queued"):
            queue_vault_collection(
                run.pk,
                actor=self.officer,
                expected_input_sha256=run.input_sha256,
            )
        task = CollectionTask.objects.get(source_vault_count=run)
        self.assertEqual(task.state, CollectionTask.State.FAILED)

        delay.side_effect = None
        retried = queue_vault_collection(
            run.pk,
            actor=self.officer,
            expected_input_sha256=run.input_sha256,
        )

        self.assertFalse(retried.created)
        self.assertEqual(retried.task.pk, task.pk)
        retried.task.refresh_from_db()
        self.assertEqual(retried.task.state, CollectionTask.State.PENDING)
        self.assertEqual(CollectionTask.objects.count(), 1)
        self.assertEqual(delay.call_count, 2)

    @patch("apps.records.tasks.execute_vault_collection.delay")
    def test_queue_rejects_stale_unfinished_or_empty_count(self, delay):
        from apps.records.vault_collections import queue_vault_collection

        complete = self.completed_count()
        with self.assertRaisesMessage(ValidationError, "changed since review"):
            queue_vault_collection(
                complete.pk,
                actor=self.officer,
                expected_input_sha256="b" * 64,
            )

        complete.total_count = 0
        complete.save(update_fields=["total_count"])
        with self.assertRaisesMessage(ValidationError, "zero possible messages"):
            queue_vault_collection(
                complete.pk,
                actor=self.officer,
                expected_input_sha256=complete.input_sha256,
            )

        complete.state = VaultCountRun.State.FAILED
        complete.save(update_fields=["state"])
        with self.assertRaisesMessage(ValidationError, "completed Vault count"):
            queue_vault_collection(
                complete.pk,
                actor=self.officer,
                expected_input_sha256=complete.input_sha256,
            )
        delay.assert_not_called()

    @patch(
        "apps.records.vault_collections.vault.ensure_mail_export",
        return_value="export-1",
    )
    @patch(
        "apps.records.vault_collections.vault.ensure_mail_hold",
        return_value="hold-1",
    )
    def test_worker_starts_hold_then_account_export(
        self,
        ensure_mail_hold,
        ensure_mail_export,
    ):
        from apps.records.vault_collections import start_vault_collection

        run = self.completed_count()
        task = CollectionTask.objects.create(
            request=self.request,
            kind=CollectionTask.Kind.VAULT_MAIL,
            state=CollectionTask.State.PENDING,
            source_vault_count=run,
            search_terms=run.terms,
            custodian_emails=run.custodian_emails,
            date_range_start=run.date_range_start,
            date_range_end=run.date_range_end,
        )

        started = start_vault_collection(task.pk)

        self.assertTrue(started)
        task.refresh_from_db()
        self.assertEqual(task.state, CollectionTask.State.RUNNING)
        self.assertEqual(task.vault_matter_id, "matter-1")
        self.assertEqual(task.vault_hold_id, "hold-1")
        self.assertEqual(task.vault_export_id, "export-1")
        ensure_mail_hold.assert_called_once_with(
            "matter-1",
            ["superintendent@psd401.net"],
            name=f"{self.request.number} — Vault count {run.pk}",
        )
        ensure_mail_export.assert_called_once_with(
            "matter-1",
            ["superintendent@psd401.net"],
            run.terms,
            start_time="2026-01-01T08:00:00Z",
            end_time="2026-07-01T07:00:00Z",
            name=f"{self.request.number} — Vault count {run.pk}",
            entire_org=False,
        )

    @patch(
        "apps.records.vault_collections.vault.ensure_mail_export",
        return_value="export-1",
    )
    @patch(
        "apps.records.vault_collections.vault.ensure_mail_hold",
        return_value="hold-1",
    )
    def test_deposit_applied_after_queueing_holds_the_export_not_the_hold(
        self,
        ensure_mail_hold,
        ensure_mail_export,
    ):
        """A collection deposit waits on money; preservation never does."""
        from datetime import date
        from decimal import Decimal

        from apps.records.fees import (
            activate_fee_schedule,
            apply_deposit_gate,
            lift_deposit_gate,
        )
        from apps.records.models import FeeDepositGate, FeeSchedule
        from apps.records.vault_collections import (
            queue_vault_collection,
            start_vault_collection,
        )

        run = self.completed_count()
        with patch("apps.records.tasks.execute_vault_collection.delay"):
            task = queue_vault_collection(
                run.pk,
                actor=self.officer,
                expected_input_sha256=run.input_sha256,
            ).task
        activate_fee_schedule(
            actor=self.officer,
            name="PSD adopted PRA copy schedule",
            basis=FeeSchedule.Basis.STATUTORY_DEFAULT,
            effective_on=date(2026, 8, 1),
            counsel_approval_reference="Legal memo PRA-fees-2026-07-28",
            adoption_reference="Board policy 4040 resolution 2026-08-01",
        )
        gate = apply_deposit_gate(
            self.request.number,
            actor=self.officer,
            scope=FeeDepositGate.Scope.COLLECTION,
            amount=Decimal("36.72"),
            estimated_total=Decimal("367.25"),
            reason="Ten percent deposit on the estimated review time.",
        )

        self.assertFalse(start_vault_collection(task.pk))

        task.refresh_from_db()
        self.assertEqual(task.state, CollectionTask.State.FAILED)
        self.assertEqual(task.vault_hold_id, "hold-1")
        self.assertEqual(task.vault_export_id, "")
        ensure_mail_export.assert_not_called()
        self.assertTrue(
            AuditLog.objects.filter(
                action="vault.collection.failed",
                target=self.request.number,
                detail__failure_code="deposit_gate_open",
            ).exists()
        )

        lift_deposit_gate(
            gate.pk,
            actor=self.officer,
            reason="Requester narrowed the request below the deposit need.",
        )
        with patch("apps.records.tasks.execute_vault_collection.delay"):
            queue_vault_collection(
                run.pk,
                actor=self.officer,
                expected_input_sha256=run.input_sha256,
            )
        self.assertTrue(start_vault_collection(task.pk))
        ensure_mail_hold.assert_called_once()
        ensure_mail_export.assert_called_once()

    @patch(
        "apps.records.vault_collections.vault.ensure_mail_export",
        return_value="export-1",
    )
    @patch(
        "apps.records.vault_collections.vault.ensure_mail_hold",
        return_value="hold-1",
    )
    def test_worker_lock_query_does_not_join_nullable_source_count(
        self,
        ensure_mail_hold,
        ensure_mail_export,
    ):
        """PostgreSQL rejects FOR UPDATE on the nullable side of a join."""
        from apps.records.vault_collections import start_vault_collection

        run = self.completed_count()
        task = CollectionTask.objects.create(
            request=self.request,
            kind=CollectionTask.Kind.VAULT_MAIL,
            state=CollectionTask.State.PENDING,
            source_vault_count=run,
            search_terms=run.terms,
        )

        with CaptureQueriesContext(connection) as queries:
            start_vault_collection(task.pk)

        lock_query = next(
            query["sql"]
            for query in queries.captured_queries
            if 'FROM "records_collectiontask"' in query["sql"]
        )
        self.assertNotIn(
            'LEFT OUTER JOIN "records_vaultcountrun"',
            lock_query,
        )

    @patch(
        "apps.records.vault_collections.vault.ensure_mail_export",
        return_value="export-org",
    )
    @patch("apps.records.vault_collections.vault.ensure_mail_hold")
    def test_entire_org_export_is_explicit_and_does_not_claim_a_targeted_hold(
        self,
        ensure_mail_hold,
        ensure_mail_export,
    ):
        from apps.records.vault_collections import start_vault_collection

        run = self.completed_count(
            search_scope=VaultCountRun.SearchScope.ENTIRE_ORG,
            custodian_emails=[],
        )
        task = CollectionTask.objects.create(
            request=self.request,
            kind=CollectionTask.Kind.VAULT_MAIL,
            state=CollectionTask.State.PENDING,
            source_vault_count=run,
            search_terms=run.terms,
        )

        start_vault_collection(task.pk)

        task.refresh_from_db()
        self.assertEqual(task.vault_hold_id, "")
        ensure_mail_hold.assert_not_called()
        self.assertTrue(ensure_mail_export.call_args.kwargs["entire_org"])

    def test_a_deposit_opened_mid_export_holds_ingest_until_resolved(self):
        """A completed export waits in RUNNING while a collection deposit is
        open, then ingests on the next poll after it is recorded or lifted."""
        from datetime import date
        from decimal import Decimal

        from apps.records import tasks as record_tasks
        from apps.records.fees import (
            activate_fee_schedule,
            apply_deposit_gate,
            lift_deposit_gate,
        )
        from apps.records.models import FeeDepositGate, FeeSchedule

        task = CollectionTask.objects.create(
            request=self.request,
            kind=CollectionTask.Kind.VAULT_MAIL,
            state=CollectionTask.State.RUNNING,
            vault_matter_id="matter-1",
            vault_export_id="export-1",
        )
        activate_fee_schedule(
            actor=self.officer,
            name="PSD adopted PRA copy schedule",
            basis=FeeSchedule.Basis.STATUTORY_DEFAULT,
            effective_on=date(2026, 8, 1),
            counsel_approval_reference="Legal memo PRA-fees-2026-07-28",
            adoption_reference="Board policy 4040 resolution 2026-08-01",
        )
        gate = apply_deposit_gate(
            self.request.number,
            actor=self.officer,
            scope=FeeDepositGate.Scope.COLLECTION,
            amount=Decimal("36.72"),
            estimated_total=Decimal("367.25"),
            reason="Ten percent deposit on the estimated review time.",
        )

        with patch(
            "apps.integrations.vault.client.get_export",
            return_value={"status": "COMPLETED"},
        ), patch.object(record_tasks.ingest_vault_export, "delay") as enqueue:
            record_tasks.poll_vault_exports()
            enqueue.assert_not_called()
            task.refresh_from_db()
            self.assertEqual(task.state, CollectionTask.State.RUNNING)

            lift_deposit_gate(
                gate.pk,
                actor=self.officer,
                reason="Requester narrowed the request below the deposit need.",
            )
            record_tasks.poll_vault_exports()
            enqueue.assert_called_once_with(task.pk)
