"""Vault count proposals are durable, bounded, and collection-free."""

from unittest import mock
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from apps.audit.models import AuditLog
from apps.records import tasks as record_tasks
from apps.records.models import PublicRecordsRequest, Requester, VaultCountRun
from apps.records.vault_counts import (
    advance_vault_count,
    request_vault_count,
    start_vault_count,
)


@override_settings(
    PRR_VAULT_CREDENTIALS_FILE="",
    PRR_VAULT_ADMIN_USER="vault-admin@example.org",
    PRR_VAULT_IMPERSONATION_TARGET=(
        "prr-vault@psd401-prr-identity.iam.gserviceaccount.com"
    ),
    PRR_GOOGLE_HOSTED_DOMAIN="psd401.net",
)
class VaultCountWorkflowTests(TestCase):
    def setUp(self):
        self.officer = get_user_model().objects.create_user(
            "vault-count-officer",
            "officer@psd401.net",
            "pw",
            is_staff=True,
        )
        requester = Requester.objects.create(email="requester@example.org")
        self.request = PublicRecordsRequest.objects.create(
            requester=requester,
            description="Emails about a district construction project.",
        )

    @patch("apps.records.tasks.execute_vault_count.delay")
    def test_human_scope_is_normalized_persisted_and_queued(self, delay):
        run = request_vault_count(
            self.request.pk,
            actor=self.officer,
            terms='"field project" OR contractor',
            custodian_emails=[" Superintendent@PSD401.NET ", "cfo@psd401.net"],
            date_range_start="2026-01-01",
            date_range_end="2026-06-30",
        )

        self.assertEqual(run.state, VaultCountRun.State.PENDING)
        self.assertEqual(run.search_scope, VaultCountRun.SearchScope.ACCOUNTS)
        self.assertEqual(
            run.custodian_emails,
            ["superintendent@psd401.net", "cfo@psd401.net"],
        )
        self.assertEqual(len(run.input_sha256), 64)
        delay.assert_called_once_with(str(run.pk))
        self.request.refresh_from_db()
        self.assertEqual(self.request.status, "received")
        self.assertFalse(self.request.collection_tasks.exists())
        self.assertEqual(
            AuditLog.objects.get(action="vault.count.queued").actor,
            self.officer,
        )

    @patch("apps.records.tasks.execute_vault_count.delay")
    def test_entire_org_scope_is_explicit_persisted_and_audited(self, delay):
        run = request_vault_count(
            self.request.pk,
            actor=self.officer,
            terms='"Jordan Sample"',
            search_scope=VaultCountRun.SearchScope.ENTIRE_ORG,
            custodian_emails=[],
            date_range_start="2026-01-01",
            date_range_end="2026-08-01",
        )

        self.assertEqual(run.search_scope, VaultCountRun.SearchScope.ENTIRE_ORG)
        self.assertEqual(run.custodian_emails, [])
        delay.assert_called_once_with(str(run.pk))
        log = AuditLog.objects.get(action="vault.count.queued")
        self.assertEqual(log.detail["search_scope"], "entire_org")
        self.assertEqual(log.detail["custodian_count"], 0)

    @patch("apps.records.tasks.execute_vault_count.delay")
    def test_entire_org_scope_rejects_ambiguous_custodians(self, delay):
        with self.assertRaisesMessage(Exception, "Leave custodian accounts empty"):
            request_vault_count(
                self.request.pk,
                actor=self.officer,
                terms="project",
                search_scope=VaultCountRun.SearchScope.ENTIRE_ORG,
                custodian_emails=["someone@psd401.net"],
            )

        delay.assert_not_called()
        self.assertFalse(VaultCountRun.objects.exists())

    @patch("apps.records.tasks.execute_vault_count.delay")
    def test_scope_rejects_non_district_accounts(self, delay):
        with self.assertRaisesMessage(Exception, "district Google Workspace"):
            request_vault_count(
                self.request.pk,
                actor=self.officer,
                terms="project",
                custodian_emails=["someone@example.com"],
            )

        delay.assert_not_called()
        self.assertFalse(VaultCountRun.objects.exists())

    @patch("apps.records.vault_counts.vault.start_mail_count")
    @patch("apps.records.vault_counts.vault.ensure_matter")
    def test_start_creates_only_a_matter_and_count_operation(
        self,
        ensure_matter,
        start_mail_count,
    ):
        ensure_matter.return_value = "matter-1"
        start_mail_count.return_value = {"name": "operations/count-1"}
        run = VaultCountRun.objects.create(
            request=self.request,
            requested_by=self.officer,
            terms="project",
            custodian_emails=["superintendent@psd401.net"],
            date_range_start="2026-01-01",
            date_range_end="2026-06-30",
            input_sha256="a" * 64,
        )

        complete = start_vault_count(run.pk)

        self.assertFalse(complete)
        run.refresh_from_db()
        self.assertEqual(run.state, VaultCountRun.State.RUNNING)
        self.assertEqual(run.vault_matter_id, "matter-1")
        self.assertEqual(run.operation_name, "operations/count-1")
        start_mail_count.assert_called_once_with(
            "matter-1",
            ["superintendent@psd401.net"],
            "project",
            start_time="2026-01-01T08:00:00Z",
            end_time="2026-07-01T07:00:00Z",
        )
        self.assertFalse(self.request.collection_tasks.exists())

    @patch("apps.records.vault_counts.vault.start_mail_count")
    @patch("apps.records.vault_counts.vault.ensure_matter")
    def test_start_uses_entire_org_provider_scope(
        self,
        ensure_matter,
        start_mail_count,
    ):
        ensure_matter.return_value = "matter-1"
        start_mail_count.return_value = {"name": "operations/count-org"}
        run = VaultCountRun.objects.create(
            request=self.request,
            requested_by=self.officer,
            search_scope=VaultCountRun.SearchScope.ENTIRE_ORG,
            terms="project",
            custodian_emails=[],
            input_sha256="f" * 64,
        )

        self.assertFalse(start_vault_count(run.pk))
        start_mail_count.assert_called_once_with(
            "matter-1",
            [],
            "project",
            start_time=None,
            end_time=None,
            entire_org=True,
        )

    @patch("apps.records.vault_counts.vault.get_operation")
    def test_completed_count_retains_bounded_totals(self, get_operation):
        get_operation.return_value = {
            "name": "operations/count-1",
            "done": True,
            "response": {
                "totalCount": "1842",
                "mailCountResult": {
                    "queriedAccountsCount": "2",
                    "matchingAccountsCount": "2",
                    "nonQueryableAccounts": ["hidden@psd401.net"],
                    "accountCountErrors": [{"account": "hidden@psd401.net"}],
                    "accountCounts": [
                        {"account": "superintendent@psd401.net", "count": "1800"}
                    ],
                },
            },
        }
        run = VaultCountRun.objects.create(
            request=self.request,
            requested_by=self.officer,
            state=VaultCountRun.State.RUNNING,
            terms="project",
            custodian_emails=["superintendent@psd401.net", "cfo@psd401.net"],
            input_sha256="b" * 64,
            vault_matter_id="matter-1",
            operation_name="operations/count-1",
        )

        complete = advance_vault_count(run.pk)

        self.assertTrue(complete)
        run.refresh_from_db()
        self.assertEqual(run.state, VaultCountRun.State.COMPLETE)
        self.assertEqual(run.total_count, 1842)
        self.assertEqual(run.queried_accounts_count, 2)
        self.assertEqual(run.matching_accounts_count, 2)
        self.assertEqual(run.non_queryable_accounts_count, 1)
        self.assertEqual(run.accounts_with_errors_count, 1)
        self.assertIsNotNone(run.completed_at)
        # Provider account-level result details are not retained.
        self.assertFalse(hasattr(run, "provider_response"))
        log = AuditLog.objects.get(action="vault.count.completed")
        self.assertEqual(log.detail["total_count"], 1842)

    @patch("apps.records.vault_counts.vault.get_operation")
    def test_zero_result_count_accepts_omitted_provider_totals(
        self,
        get_operation,
    ):
        get_operation.return_value = {
            "name": "operations/count-empty",
            "done": True,
            "response": {
                "mailCountResult": {
                    "queriedAccountsCount": "0",
                },
            },
        }
        run = VaultCountRun.objects.create(
            request=self.request,
            requested_by=self.officer,
            state=VaultCountRun.State.RUNNING,
            search_scope=VaultCountRun.SearchScope.ENTIRE_ORG,
            terms="subject:__PRR_DEV_SMOKE__",
            custodian_emails=[],
            input_sha256="0" * 64,
            vault_matter_id="matter-empty",
            operation_name="operations/count-empty",
        )

        self.assertTrue(advance_vault_count(run.pk))

        run.refresh_from_db()
        self.assertEqual(run.state, VaultCountRun.State.COMPLETE)
        self.assertEqual(run.total_count, 0)
        self.assertEqual(run.queried_accounts_count, 0)
        self.assertEqual(run.matching_accounts_count, 0)
        self.assertEqual(run.failure_code, "")

    @patch("apps.records.vault_counts.vault.get_operation")
    def test_provider_failure_is_safe_and_terminal(self, get_operation):
        get_operation.return_value = {
            "name": "operations/count-1",
            "done": True,
            "error": {"code": 403, "message": "sensitive provider detail"},
        }
        run = VaultCountRun.objects.create(
            request=self.request,
            requested_by=self.officer,
            state=VaultCountRun.State.RUNNING,
            terms="project",
            custodian_emails=["superintendent@psd401.net"],
            input_sha256="c" * 64,
            vault_matter_id="matter-1",
            operation_name="operations/count-1",
        )

        self.assertTrue(advance_vault_count(run.pk))
        run.refresh_from_db()
        self.assertEqual(run.state, VaultCountRun.State.FAILED)
        self.assertEqual(run.failure_code, "provider_rejected_count")
        self.assertNotIn("sensitive", run.failure_detail)

    @patch("apps.records.tasks.poll_vault_count.apply_async")
    @patch("apps.records.vault_counts.start_vault_count", return_value=False)
    def test_worker_starts_then_schedules_bounded_polling(self, start, poll):
        run = VaultCountRun.objects.create(
            request=self.request,
            requested_by=self.officer,
            terms="project",
            custodian_emails=["superintendent@psd401.net"],
            input_sha256="d" * 64,
        )

        record_tasks.execute_vault_count.run(str(run.pk))

        start.assert_called_once_with(str(run.pk))
        poll.assert_called_once_with(args=[str(run.pk)], countdown=2)

    @patch(
        "apps.records.vault_counts.start_vault_count",
        side_effect=RuntimeError("sensitive provider failure"),
    )
    def test_worker_start_failure_retains_only_safe_detail(self, start):
        run = VaultCountRun.objects.create(
            request=self.request,
            requested_by=self.officer,
            terms="project",
            custodian_emails=["superintendent@psd401.net"],
            input_sha256="e" * 64,
        )

        with self.assertRaises(RuntimeError):
            record_tasks.execute_vault_count.run(str(run.pk))

        run.refresh_from_db()
        self.assertEqual(run.state, VaultCountRun.State.FAILED)
        self.assertEqual(run.failure_code, "provider_unavailable")
        self.assertNotIn("sensitive", run.failure_detail)


class VaultClientCountTests(TestCase):
    @override_settings(
        DEBUG=False,
        PRR_VAULT_ADMIN_USER="vault-admin@example.org",
        PRR_VAULT_IMPERSONATION_TARGET=(
            "prr-vault@psd401-prr-identity.iam.gserviceaccount.com"
        ),
        PRR_VAULT_CREDENTIALS_FILE="",
    )
    @patch("apps.integrations.vault.client.impersonated_credentials.Credentials")
    @patch("apps.integrations.vault.client.google.auth.default")
    def test_production_credentials_are_keyless_and_subject_pinned(
        self,
        default_credentials,
        impersonated,
    ):
        from apps.integrations.vault import client

        source = mock.Mock()
        default_credentials.return_value = (source, "project")
        delegated = mock.Mock()
        impersonated.return_value = delegated

        self.assertIs(client.vault_credentials(), delegated)
        default_credentials.assert_called_once_with(scopes=[client.IAM_SCOPE])
        impersonated.assert_called_once_with(
            source_credentials=source,
            target_principal=(
                "prr-vault@psd401-prr-identity.iam.gserviceaccount.com"
            ),
            target_scopes=client.SCOPES,
            subject="vault-admin@example.org",
            lifetime=client.IMPERSONATION_LIFETIME_SECONDS,
        )

    @override_settings(
        DEBUG=False,
        PRR_VAULT_ADMIN_USER="vault-admin@example.org",
        PRR_VAULT_IMPERSONATION_TARGET="",
        PRR_VAULT_CREDENTIALS_FILE="/forbidden/key.json",
    )
    def test_production_refuses_a_service_account_key_file(self):
        from apps.integrations.vault import client

        with self.assertRaisesMessage(
            client.VaultUnavailable,
            "private key is forbidden",
        ):
            client.vault_credentials()

    @patch("apps.integrations.vault.client._vault_service")
    def test_client_uses_count_without_export_hold_or_saved_query(self, service):
        operation = MagicMock()
        operation.execute.return_value = {"name": "operations/count-1"}
        count = service.return_value.matters.return_value.count
        count.return_value = operation

        from apps.integrations.vault.client import start_mail_count

        result = start_mail_count(
            "matter-1",
            ["officer@psd401.net"],
            '"field project"',
            start_time="2026-01-01T08:00:00Z",
            end_time="2026-07-01T07:00:00Z",
        )

        self.assertEqual(result["name"], "operations/count-1")
        count.assert_called_once_with(
            matterId="matter-1",
            body={
                "query": {
                    "corpus": "MAIL",
                    "dataScope": "ALL_DATA",
                    "method": "ACCOUNT",
                    "accountInfo": {"emails": ["officer@psd401.net"]},
                    "terms": '"field project"',
                    "mailOptions": {"excludeDrafts": False},
                    "startTime": "2026-01-01T08:00:00Z",
                    "endTime": "2026-07-01T07:00:00Z",
                },
                "view": "TOTAL_COUNT",
            },
        )
        service.return_value.matters.return_value.exports.assert_not_called()
        service.return_value.matters.return_value.holds.assert_not_called()

    @patch("apps.integrations.vault.client._vault_service")
    def test_client_uses_entire_org_count_without_account_info(self, service):
        operation = MagicMock()
        operation.execute.return_value = {"name": "operations/count-org"}
        count = service.return_value.matters.return_value.count
        count.return_value = operation

        from apps.integrations.vault.client import start_mail_count

        result = start_mail_count(
            "matter-1",
            [],
            '"Jordan Sample"',
            entire_org=True,
        )

        self.assertEqual(result["name"], "operations/count-org")
        count.assert_called_once_with(
            matterId="matter-1",
            body={
                "query": {
                    "corpus": "MAIL",
                    "dataScope": "ALL_DATA",
                    "method": "ENTIRE_ORG",
                    "terms": '"Jordan Sample"',
                    "mailOptions": {"excludeDrafts": False},
                },
                "view": "TOTAL_COUNT",
            },
        )
        service.return_value.matters.return_value.exports.assert_not_called()
        service.return_value.matters.return_value.holds.assert_not_called()

    @patch("apps.integrations.vault.client.create_mail_export")
    @patch("apps.integrations.vault.client._vault_service")
    def test_client_reuses_named_export_after_uncertain_worker_result(
        self,
        service,
        create_mail_export,
    ):
        exports = service.return_value.matters.return_value.exports.return_value
        exports.list.return_value.execute.side_effect = [
            {
                "exports": [
                    {"id": "export-existing", "name": "PRR-2026-0001 export"},
                    {"id": "export-other", "name": "Another export"},
                ],
                "nextPageToken": "page-2",
            },
            {"exports": []},
        ]

        from apps.integrations.vault.client import ensure_mail_export

        export_id = ensure_mail_export(
            "matter-1",
            ["officer@psd401.net"],
            '"field project"',
            name="PRR-2026-0001 export",
        )

        self.assertEqual(export_id, "export-existing")
        create_mail_export.assert_not_called()
        self.assertEqual(exports.list.call_count, 2)
