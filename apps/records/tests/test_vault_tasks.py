"""Celery handoff contracts for resumable Vault export ingestion."""

from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from apps.audit.models import AuditLog
from apps.records import tasks as record_tasks
from apps.records.models import (
    CollectionTask,
    PublicRecordsRequest,
    Requester,
    StatusEvent,
)
from apps.records.vault_ingest import (
    VaultIngestError,
    VaultIngestPermanentError,
)


class VaultIngestTaskTests(TestCase):
    def setUp(self):
        requester = Requester.objects.create(email="vault-worker@example.org")
        self.request = PublicRecordsRequest.objects.create(
            requester=requester,
            description="Messages retained by Google Vault.",
        )
        self.task = CollectionTask.objects.create(
            request=self.request,
            kind=CollectionTask.Kind.VAULT_MAIL,
            state=CollectionTask.State.INGESTING,
            instructions="Retain and ingest the completed export.",
            vault_matter_id="matter-1",
            vault_export_id="export-1",
        )

    def test_worker_returns_completed_ingest_run_identity(self):
        run = SimpleNamespace(pk=42)

        with patch(
            "apps.records.vault_ingest.ingest_completed_export",
            return_value=run,
        ) as ingest:
            result = record_tasks.ingest_vault_export.run(self.task.pk)

        self.assertEqual(result, 42)
        ingest.assert_called_once_with(self.task.pk)

    def test_permanent_error_marks_task_failed_with_safe_evidence(self):
        failure = VaultIngestPermanentError(
            "archive_unsafe",
            "sensitive/source/object/name.zip",
        )

        with patch(
            "apps.records.vault_ingest.ingest_completed_export",
            side_effect=failure,
        ), self.assertRaises(VaultIngestPermanentError):
            record_tasks.ingest_vault_export.run(self.task.pk)

        self.task.refresh_from_db()
        self.assertEqual(self.task.state, CollectionTask.State.FAILED)
        event = StatusEvent.objects.get(request=self.request)
        self.assertIn("archive_unsafe", event.note)
        self.assertNotIn("sensitive", event.note)
        log = AuditLog.objects.get(action="vault.ingest.permanent_failure")
        self.assertEqual(log.target, self.request.number)
        self.assertEqual(log.detail["error_code"], "archive_unsafe")
        self.assertNotIn("sensitive", str(log.detail))

    def test_retryable_error_preserves_ingesting_state_and_schedules_retry(self):
        failure = VaultIngestError("ingest_lease_lost", "retry safely")
        scheduled = RuntimeError("retry scheduled")

        with patch(
            "apps.records.vault_ingest.ingest_completed_export",
            side_effect=failure,
        ), patch.object(
            record_tasks.ingest_vault_export,
            "retry",
            side_effect=scheduled,
        ) as retry, self.assertRaisesRegex(RuntimeError, "retry scheduled"):
            record_tasks.ingest_vault_export.run(self.task.pk)

        self.task.refresh_from_db()
        self.assertEqual(self.task.state, CollectionTask.State.INGESTING)
        self.assertFalse(
            AuditLog.objects.filter(
                action__in={
                    "vault.ingest.permanent_failure",
                    "vault.ingest.retries_exhausted",
                }
            ).exists()
        )
        retry.assert_called_once()
        self.assertEqual(retry.call_args.kwargs["countdown"], 60)
